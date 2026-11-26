"""HTTP API for Gemini AI Dealer Finder."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.dealer_finder.constants import (
    DEFAULT_CATEGORIES,
    STATUS_CANCELLED,
    STATUS_PAUSED,
    STATUS_PENDING,
)
from app.dealer_finder.geo_plan import get_or_create_location_plan, plan_as_dict
from app.dealer_finder.history import log_public
from app.dealer_finder.india_geo import districts_for_state, list_states
from app.dealer_finder.jobs import (
    DISPLAY_ELIGIBLE_PHONE_LIMIT,
    build_import_confirm_prompt,
    build_job_status_report,
    create_search_job,
    import_eligible_places,
    job_public,
    job_results_ready,
    list_eligible_phone_places,
    place_counts_for_job,
    place_row_public,
    set_job_status,
    wake_dealer_finder_worker,
)
from app.dealer_finder.normalize import canon_location_state
from app.dealer_finder.settings_svc import (
    get_or_create_settings,
    patch_settings,
    settings_public,
    enrich_usage_with_free_tier,
    usage_totals,
)
from app.dealer_finder.all_india import (
    assert_state_allowed,
    gates_public,
    is_excluded_state,
    preflight_estimate,
)
from app.dealer_finder.all_india_jobs import (
    cancel_run,
    create_full_run,
    create_pilot_run,
    dashboard_snapshot,
    pause_run,
    resume_run,
    run_public,
    skip_current_state,
)
from app.models import DealerAllIndiaRun, DealerSearchJob, DealerSearchLog, DealerSearchPlace, User

router = APIRouter(prefix="/api/dealer-finder", tags=["dealer-finder"])
Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


def _require_admin(user: User) -> None:
    if user.role.lower() not in {"admin", "super_admin", "super admin", "management"}:
        raise HTTPException(status_code=403, detail="Admin or Management role required")


class StartJobIn(BaseModel):
    state: str = Field(min_length=2, max_length=80)
    district: str = ""
    city: str = ""
    categories: list[str] = Field(default_factory=list)
    search_depth: str = Field(default="full", max_length=40)
    lead_owner_id: int | None = None
    force_refresh: bool = False
    confirm: bool = False


class PlanIn(BaseModel):
    state: str = Field(min_length=2, max_length=80)
    force_refresh: bool = False


class SettingsPatch(BaseModel):
    cooldown_days: int | None = Field(default=None, ge=1, le=365)
    max_requests_per_job: int | None = Field(default=None, ge=1, le=1_000_000)
    max_requests_per_day: int | None = Field(default=None, ge=1, le=1_000_000)
    # Monthly FREE quota (not a hard stop). Alias: max_requests_per_month.
    free_requests_per_month: int | None = Field(default=None, ge=0, le=5_000_000)
    max_requests_per_month: int | None = Field(default=None, ge=0, le=5_000_000)
    # Paid ceiling AFTER free tier (USD). Default 100 — not 7000.
    max_monthly_spend_usd: float | None = Field(default=None, ge=0, le=100_000)
    max_concurrent_jobs: int | None = Field(default=None, ge=1, le=10)
    expensive_field_policy: str | None = None
    persist_website_on_import: bool | None = None
    all_india_enabled: bool | None = None
    pilot_state: str | None = None
    pilot_district: str | None = None


class ImportIn(BaseModel):
    job_id: int | None = None
    public_id: str | None = None
    place_row_ids: list[int] = Field(default_factory=list)
    confirm: bool = False
    with_phone_only: bool = False
    exclude_districts: list[str] = Field(default_factory=list)
    exclude_cities: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)


class RefreshLogIn(BaseModel):
    confirm_cost: bool = False


@router.get("/meta")
async def meta(db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    settings = await get_or_create_settings(db)
    return {
        "states": list_states(),
        "eligible_states": [s for s in list_states() if not is_excluded_state(s)],
        "excluded_states": sorted(
            {s for s in list_states() if is_excluded_state(s)}
            | {"Tamil Nadu", "Kerala", "Puducherry"}
        ),
        "all_india_gates": gates_public(settings),
        "categories": list(DEFAULT_CATEGORIES),
        "search_depths": [
            {"id": "full", "label": "District + City (maximum)"},
            {"id": "district_only", "label": "District only"},
            {"id": "city_only", "label": "City only"},
        ],
        "google_attribution": "Powered by Google Places / Google Maps Platform",
        "expansion_path": (
            "Gemini lists ALL districts → cities/towns per district → "
            "Places search district-wise AND city-wise × categories"
        ),
    }


@router.get("/districts")
async def districts(
    db: Db,
    user: CurrentUser,
    state: str = Query(..., min_length=2),
) -> dict[str, Any]:
    _ = db, user
    label = canon_location_state(state)
    return {"state": label, "districts": districts_for_state(label)}


@router.get("/settings")
async def get_settings(db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    row = await get_or_create_settings(db)
    usage = enrich_usage_with_free_tier(await usage_totals(db), row)
    job_rows = (
        await db.execute(
            select(DealerSearchJob).order_by(DealerSearchJob.id.desc()).limit(10)
        )
    ).scalars().all()
    await db.commit()
    return {
        **settings_public(row),
        "usage": usage,
        "recent_jobs": [job_public(j) for j in job_rows],
    }


@router.patch("/settings")
async def update_settings(body: SettingsPatch, db: Db, user: CurrentUser) -> dict[str, Any]:
    _require_admin(user)
    try:
        row = await patch_settings(db, user, body.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    return settings_public(row)


@router.post("/plans")
async def create_or_get_plan(body: PlanIn, db: Db, user: CurrentUser) -> dict[str, Any]:
    """Generate/cache Gemini expansion plan: ALL districts → cities per district."""
    try:
        plan = await get_or_create_location_plan(
            db,
            state=body.state,
            user=user,
            force_refresh=body.force_refresh,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    return {"ok": True, "plan": plan_as_dict(plan)}


@router.get("/plans/{state}")
async def get_plan(state: str, db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    label = canon_location_state(state)
    plan = await get_or_create_location_plan(db, state=label, user=None, force_refresh=False)
    await db.commit()
    return {"ok": True, "plan": plan_as_dict(plan)}


@router.post("/jobs")
async def start_job(body: StartJobIn, db: Db, user: CurrentUser) -> dict[str, Any]:
    """Start district→city×category Places batch. Requires confirm=true (costly).

    Single-state / manual jobs (incl. Tamil Nadu, Kerala/Keralam, Puducherry) are allowed.
  All-India orchestration uses assert_state_allowed separately."""
    label = canon_location_state(body.state)
    if not label:
        raise HTTPException(400, "state is required")
    if not body.confirm:
        districts = districts_for_state(label)
        cats = body.categories or list(DEFAULT_CATEGORIES)
        return {
            "ok": False,
            "needs_confirm": True,
            "summary": (
                f"Find weighing scale dealers in {label}: Gemini expands "
                f"{len(districts) or 'all'} districts → cities/towns, then Google Places "
                f"district-wise AND city-wise × {len(cats)} categories. "
                f"Depth={body.search_depth}. Force refresh={body.force_refresh}."
            ),
            "warning": (
                "This may call Google Places many times (cost). "
                "Recent location×category searches within cooldown are skipped unless Refresh."
            ),
            "please_confirm": "Set confirm=true to start.",
        }
    try:
        job = await create_search_job(
            db,
            user=user,
            state=body.state,
            categories=body.categories or None,
            district=body.district,
            city=body.city,
            search_depth=body.search_depth,
            lead_owner_id=body.lead_owner_id,
            force_refresh=body.force_refresh,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    wake_dealer_finder_worker()
    return {"ok": True, "job": job_public(job)}


@router.get("/jobs")
async def list_jobs(
    db: Db,
    user: CurrentUser,
    state: str = "",
    limit: int = Query(default=30, ge=1, le=100),
) -> dict[str, Any]:
    _ = user
    q = select(DealerSearchJob).order_by(DealerSearchJob.id.desc()).limit(limit)
    if state.strip():
        label = canon_location_state(state)
        q = q.where(DealerSearchJob.state == label)
    rows = (await db.execute(q)).scalars().all()
    return {"items": [job_public(r) for r in rows]}


@router.get("/jobs/{public_id}")
async def get_job(public_id: str, db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    job = (
        await db.execute(
            select(DealerSearchJob).where(DealerSearchJob.public_id == public_id)
        )
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    return {"ok": True, "job": job_public(job)}


@router.post("/jobs/{public_id}/pause")
async def pause_job(public_id: str, db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    job = await _get_job(db, public_id)
    await set_job_status(db, job, STATUS_PAUSED)
    await db.commit()
    return {"ok": True, "job": job_public(job)}


@router.post("/jobs/{public_id}/resume")
async def resume_job(public_id: str, db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    job = await _get_job(db, public_id)
    # Clear budget/pause residue so the worker can reclaim cleanly.
    job.error = ""
    job.finished_at = None
    await set_job_status(db, job, STATUS_PENDING)
    await db.commit()
    wake_dealer_finder_worker()
    return {"ok": True, "job": job_public(job)}


@router.post("/jobs/{public_id}/cancel")
async def cancel_job(public_id: str, db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    job = await _get_job(db, public_id)
    await set_job_status(db, job, STATUS_CANCELLED)
    await db.commit()
    return {"ok": True, "job": job_public(job)}


@router.get("/jobs/{public_id}/results")
async def job_results(
    public_id: str,
    db: Db,
    user: CurrentUser,
    crm_status: str = Query(default="all"),
    q: str = "",
    limit: int = Query(default=10_000, ge=1, le=DISPLAY_ELIGIBLE_PHONE_LIMIT),
    offset: int = Query(default=0, ge=0),
    filter: str = Query(
        default="eligible_new_with_phone",
        description=(
            "eligible_new_with_phone (default for AI chat): NEW + phone only. "
            "Use all for unfiltered unique places."
        ),
    ),
    include_partial: bool = Query(
        default=False,
        description="If false (default), withhold place rows until job is finished/paused/failed.",
    ),
) -> dict[str, Any]:
    """List places for a job.

    Default filter is phone-eligible NEW (matches AI chat card). By default does NOT
    drip incomplete batches while Running/Pending — empty items until results_ready.
    """
    _ = user
    job = await _get_job(db, public_id)
    ready = job_results_ready(job)
    status_report = await build_job_status_report(db, job)
    counts = await place_counts_for_job(db, job.id)
    phone_n = int(counts.get("eligible_with_phone") or 0)
    if not ready and not include_partial:
        return {
            "ok": True,
            "job": job_public(job),
            "results_ready": False,
            "total": 0,
            "filtered_total": 0,
            "items": [],
            "display_filter": filter or "eligible_new_with_phone",
            "summary": {
                "shown": 0,
                "with_website": 0,
                "with_phone": phone_n,
                "eligible_import": counts["eligible_import"],
                "eligible_with_phone": phone_n,
                "already_dealer": counts["already_dealer"],
                "already_lead": counts["already_lead"],
                "unique_places": counts["unique_places"],
            },
            "status_report": status_report,
            "example_import_confirm_prompt": build_import_confirm_prompt(counts),
            "note": (
                f"Job {job.status}: full unique-place list available when the batch "
                "completes (or pauses/fails). Progress tiles only until then."
            ),
            "google_attribution": "Results from Google Places. Map data © Google.",
        }

    filt = (filter or "eligible_new_with_phone").strip().lower()
    if filt in {"eligible_new_with_phone", "phone_new", "new_with_phone"}:
        rows, filtered_total = await list_eligible_phone_places(
            db, job.id, limit=DISPLAY_ELIGIBLE_PHONE_LIMIT, offset=0
        )
        if q.strip():
            needle = q.strip().lower()
            rows = [
                r
                for r in rows
                if needle in (r.name or "").lower()
                or needle in (r.city or "").lower()
                or needle in (r.district or "").lower()
                or needle in (r.phone or "").lower()
            ]
            filtered_total = len(rows)
        off = max(0, int(offset or 0))
        lim = max(1, min(DISPLAY_ELIGIBLE_PHONE_LIMIT, int(limit or DISPLAY_ELIGIBLE_PHONE_LIMIT)))
        page = rows[off : off + lim]
        items = [place_row_public(r) for r in page]
        pub = job_public(job)
        return {
            "ok": True,
            "job": pub,
            "results_ready": ready,
            "scan_complete": bool(pub.get("scan_complete")),
            "incomplete": not bool(pub.get("scan_complete")),
            "report_kind": "dealer_finder_results",
            "total": filtered_total,
            "filtered_total": filtered_total,
            "total_unique": counts["unique_places"],
            "with_phone": phone_n,
            "eligible_with_phone": phone_n,
            "phone_dupes_removed": int(counts.get("phone_dupes_removed") or 0),
            "eligible_with_phone_raw": int(counts.get("eligible_with_phone_raw") or phone_n),
            "phone_list_format_version": int(counts.get("phone_list_format_version") or 0),
            "display_filter": "eligible_new_with_mobile",
            "mobile_only": True,
            "truncated": filtered_total > len(items),
            "count": len(items),
            "items": items,
            "columns": ["name", "whatsapp", "website", "city", "district"],
            "summary": {
                "shown": len(items),
                "with_website": counts["with_website"],
                # Chat/UI "with phone" badge = phone-eligible NEW (filtered set).
                "with_phone": phone_n,
                "eligible_import": counts["eligible_import"],
                "eligible_with_phone": phone_n,
                "already_dealer": counts["already_dealer"],
                "already_lead": counts["already_lead"],
                "unique_places": counts["unique_places"],
            },
            "status_report": status_report,
            "example_import_confirm_prompt": build_import_confirm_prompt(counts),
            "google_attribution": "Results from Google Places. Map data © Google.",
        }

    # Unfiltered / legacy crm_status path (Dealer Finder page).
    if crm_status == "all" and filt not in {"all", ""}:
        # Unknown filter → treat as all
        pass
    stmt = select(DealerSearchPlace).where(DealerSearchPlace.job_id == job.id)
    if crm_status and crm_status != "all":
        stmt = stmt.where(DealerSearchPlace.crm_status == crm_status)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                DealerSearchPlace.name.ilike(like),
                DealerSearchPlace.city.ilike(like),
                DealerSearchPlace.district.ilike(like),
                DealerSearchPlace.phone.ilike(like),
            )
        )
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(DealerSearchPlace.id.asc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    items = [_place_public(r) for r in rows]
    return {
        "ok": True,
        "job": job_public(job),
        "results_ready": ready,
        "total": int(total or 0),
        "filtered_total": int(total or 0),
        "total_unique": counts["unique_places"],
        "display_filter": "all",
        "items": items,
        "summary": {
            "shown": len(items),
            "with_website": counts["with_website"],
            "with_phone": counts["with_phone"],
            "eligible_import": counts["eligible_import"],
            "eligible_with_phone": phone_n,
            "already_dealer": counts["already_dealer"],
            "already_lead": counts["already_lead"],
            "unique_places": counts["unique_places"],
        },
        "status_report": status_report,
        "example_import_confirm_prompt": build_import_confirm_prompt(counts),
        "google_attribution": "Results from Google Places. Map data © Google.",
    }


@router.get("/history")
async def search_history(
    db: Db,
    user: CurrentUser,
    state: str = "",
    district: str = "",
    city: str = "",
    category: str = "",
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    _ = user
    q = select(DealerSearchLog).order_by(DealerSearchLog.searched_at.desc().nullslast()).limit(limit)
    if state.strip():
        q = q.where(DealerSearchLog.state == canon_location_state(state))
    if district.strip():
        q = q.where(DealerSearchLog.district.ilike(f"%{district.strip()}%"))
    if city.strip():
        q = q.where(DealerSearchLog.city.ilike(f"%{city.strip()}%"))
    if category.strip():
        q = q.where(DealerSearchLog.category.ilike(f"%{category.strip()}%"))
    rows = (await db.execute(q)).scalars().all()
    items = []
    for r in rows:
        pids = (
            await db.execute(
                select(DealerSearchPlace.place_id).where(
                    DealerSearchPlace.search_log_id == r.id
                )
            )
        ).scalars().all()
        items.append(log_public(r, place_ids=[p for p in pids if p]))
    return {"items": items, "cooldown_note": "Default cooldown 30 days (admin settings)."}


@router.post("/history/{log_id}/refresh")
async def refresh_history(
    log_id: int, body: RefreshLogIn, db: Db, user: CurrentUser
) -> dict[str, Any]:
    """Authorized refresh — costs Google Places calls. Requires confirm_cost=true."""
    if not body.confirm_cost:
        return {
            "ok": False,
            "needs_confirm": True,
            "warning": (
                "Refresh will call Google Places again for this location×category "
                "(Enterprise contact fields may apply). Confirm to proceed."
            ),
        }
    log = await db.get(DealerSearchLog, log_id)
    if not log:
        raise HTTPException(404, "Search log not found")
    job = await create_search_job(
        db,
        user=user,
        state=log.state,
        categories=[log.category] if log.category else None,
        district=log.district,
        city=log.city,
        search_depth="city_only" if log.city else "district_only",
        force_refresh=True,
    )
    await db.commit()
    wake_dealer_finder_worker()
    return {
        "ok": True,
        "job": job_public(job),
        "notice": "Refresh started — Places API charges may apply.",
    }


async def _resolve_import_job(
    db: AsyncSession, body: ImportIn
) -> DealerSearchJob | None:
    """Bind import to one job — prefer public_id (chat card) over numeric job_id."""
    pid = (body.public_id or "").strip()
    if pid:
        try:
            return await _get_job(db, pid)
        except HTTPException:
            return None
    if body.job_id:
        return await db.get(DealerSearchJob, body.job_id)
    return None


async def _import_scope_place_ids(
    db: AsyncSession, job_id: int, body: ImportIn
) -> list[int] | None:
    """Place row ids for this card scope (explicit ids or list exclusions)."""
    if body.place_row_ids:
        return body.place_row_ids
    from app.dealer_finder.row_filters import (
        exclusions_active,
        filter_rows,
        parse_exclusion_args,
    )

    exclusions = parse_exclusion_args(
        {
            "exclude_districts": body.exclude_districts,
            "exclude_cities": body.exclude_cities,
            "exclude_keywords": body.exclude_keywords,
        }
    )
    if not exclusions_active(exclusions):
        return None
    rows, _filtered_total = await list_eligible_phone_places(
        db, job_id, limit=DISPLAY_ELIGIBLE_PHONE_LIMIT, offset=0
    )
    kept, _removed = filter_rows(rows, exclusions)
    return [int(r.id) for r in kept]


@router.post("/import")
async def import_places(body: ImportIn, db: Db, user: CurrentUser) -> dict[str, Any]:
    """Import eligible places as Leads. confirm=false returns preview; true imports + status report."""
    job = await _resolve_import_job(db, body)
    preview_counts: dict[str, int] = {}
    status_report: dict[str, Any] | None = None
    scoped_n: int | None = None
    if job:
        preview_counts = await place_counts_for_job(db, job.id)
        status_report = await build_job_status_report(db, job)
        scoped_ids = await _import_scope_place_ids(db, job.id, body)
        if scoped_ids is not None:
            scoped_n = len(scoped_ids)
    if not body.confirm:
        eligible = int(preview_counts.get("eligible_import") or 0)
        eligible_phone = int(preview_counts.get("eligible_with_phone") or 0)
        target = scoped_n if scoped_n is not None else (
            eligible_phone if body.with_phone_only else eligible
        )
        phone_note = (
            f" ({eligible_phone} phone-eligible in job)."
            if scoped_n is None and not body.with_phone_only
            else ""
        )
        if scoped_n is not None:
            phone_note = " (filtered list exclusions applied)."
        elif body.with_phone_only:
            phone_note = f" ({eligible_phone} phone-eligible in job)."
        return {
            "ok": False,
            "needs_confirm": True,
            "summary": (
                f"Import {target} eligible NEW places as Leads "
                "(source=Google, status=New). Existing dealers/leads are skipped."
                + phone_note
            ),
            "eligible": target,
            "eligible_with_phone": eligible_phone,
            "with_phone_only": body.with_phone_only,
            "preview": preview_counts,
            "status_report": status_report,
            "public_id": job.public_id if job else None,
            "please_confirm": "Set confirm=true to import.",
        }
    if not job:
        raise HTTPException(404, "Job not found")
    place_ids = body.place_row_ids or None
    if not place_ids:
        place_ids = await _import_scope_place_ids(db, job.id, body)
    result = await import_eligible_places(
        db,
        user=user,
        job_id=job.id,
        place_ids=place_ids,
        with_phone_only=body.with_phone_only,
    )
    await db.commit()
    result["public_id"] = job.public_id
    return result


async def _get_job(db: AsyncSession, public_id: str) -> DealerSearchJob:
    job = (
        await db.execute(
            select(DealerSearchJob).where(DealerSearchJob.public_id == public_id)
        )
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    return job


class AllIndiaPilotIn(BaseModel):
    state: str = Field(default="Rajasthan", min_length=2, max_length=80)
    district: str = Field(default="Jaipur", min_length=2, max_length=80)
    categories: list[str] = Field(default_factory=list)
    confirm: bool = False
    acknowledge_cost: bool = False


class AllIndiaFullIn(BaseModel):
    categories: list[str] = Field(default_factory=list)
    confirm: bool = False
    acknowledge_cost: bool = False


@router.get("/all-india/dashboard")
async def all_india_dashboard(db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    settings = await get_or_create_settings(db)
    snap = await dashboard_snapshot(db)
    usage = enrich_usage_with_free_tier(await usage_totals(db), settings)
    await db.commit()
    return {
        "ok": True,
        **snap,
        "gates": gates_public(settings),
        "usage": usage,
    }


@router.post("/all-india/preflight")
async def all_india_preflight(
    db: Db,
    user: CurrentUser,
    mode: str = Query(default="full"),
    state: str = "",
    district: str = "",
) -> dict[str, Any]:
    _ = user
    if mode == "pilot":
        st = assert_state_allowed(state or "Rajasthan")
        dist = (district or "Jaipur").strip()
        est = await preflight_estimate(
            db, states=[st], district_filter=dist, search_depth="full"
        )
    else:
        est = await preflight_estimate(db)
    await db.commit()
    return {"ok": True, "mode": mode, "estimate": est}


@router.post("/all-india/pilot")
async def start_all_india_pilot(body: AllIndiaPilotIn, db: Db, user: CurrentUser) -> dict[str, Any]:
    """Pilot: exactly one district (outside excluded states). Never auto-starts on deploy."""
    try:
        st = assert_state_allowed(body.state)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    dist = (body.district or "").strip()
    if not dist:
        raise HTTPException(400, "district is required")
    if not body.confirm:
        est = await preflight_estimate(
            db, states=[st], district_filter=dist, search_depth="full"
        )
        await db.commit()
        return {
            "ok": False,
            "needs_confirm": True,
            "pilot": {"state": st, "district": dist},
            "estimate": est,
            "please_confirm": "Set confirm=true and acknowledge_cost=true to start pilot.",
        }
    if not body.acknowledge_cost:
        raise HTTPException(400, "acknowledge_cost=true required")
    try:
        run = await create_pilot_run(
            db,
            user=user,
            state=st,
            district=dist,
            categories=body.categories or None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    wake_dealer_finder_worker()
    return {"ok": True, "run": run_public(run)}


@router.post("/all-india/full")
async def start_all_india_full(body: AllIndiaFullIn, db: Db, user: CurrentUser) -> dict[str, Any]:
    _require_admin(user)
    if not body.confirm:
        est = await preflight_estimate(db)
        settings = await get_or_create_settings(db)
        await db.commit()
        return {
            "ok": False,
            "needs_confirm": True,
            "gates": gates_public(settings),
            "estimate": est,
            "please_confirm": (
                "Requires pilot complete + all_india_enabled in settings. "
                "Set confirm=true and acknowledge_cost=true."
            ),
        }
    if not body.acknowledge_cost:
        raise HTTPException(400, "acknowledge_cost=true required")
    try:
        run = await create_full_run(
            db, user=user, categories=body.categories or None
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    wake_dealer_finder_worker()
    return {"ok": True, "run": run_public(run)}


async def _get_all_india_run(db: AsyncSession, public_id: str) -> DealerAllIndiaRun:
    run = (
        await db.execute(
            select(DealerAllIndiaRun).where(DealerAllIndiaRun.public_id == public_id)
        )
    ).scalar_one_or_none()
    if not run:
        raise HTTPException(404, "All-India run not found")
    return run


@router.get("/all-india/runs/{public_id}")
async def get_all_india_run(public_id: str, db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    run = await _get_all_india_run(db, public_id)
    return {"ok": True, "run": run_public(run)}


@router.post("/all-india/runs/{public_id}/pause")
async def pause_all_india(public_id: str, db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    run = await pause_run(db, await _get_all_india_run(db, public_id))
    await db.commit()
    return {"ok": True, "run": run_public(run)}


@router.post("/all-india/runs/{public_id}/resume")
async def resume_all_india(public_id: str, db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    run = await resume_run(db, await _get_all_india_run(db, public_id))
    await db.commit()
    wake_dealer_finder_worker()
    return {"ok": True, "run": run_public(run)}


@router.post("/all-india/runs/{public_id}/cancel")
async def cancel_all_india(public_id: str, db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    run = await cancel_run(db, await _get_all_india_run(db, public_id))
    await db.commit()
    return {"ok": True, "run": run_public(run)}


@router.post("/all-india/runs/{public_id}/skip-state")
async def skip_state_all_india(public_id: str, db: Db, user: CurrentUser) -> dict[str, Any]:
    _require_admin(user)
    run = await skip_current_state(db, await _get_all_india_run(db, public_id))
    await db.commit()
    wake_dealer_finder_worker()
    return {"ok": True, "run": run_public(run)}


def _place_public(r: DealerSearchPlace) -> dict[str, Any]:
    website = (r.website or "").strip()
    return {
        "id": r.id,
        "place_id": r.place_id,
        "name": r.name,
        "whatsapp": r.phone,
        "website": website,
        "website_label": website if website else "Not available",
        "city": r.city,
        "district": r.district,
        "state": r.state,
        "category": r.category,
        "address": r.address,
        "maps_url": r.maps_url,
        "lead_source": r.lead_source or "Google",
        "crm_status": r.crm_status,
        "crm_match": r.crm_match,
        "imported_lead_id": r.imported_lead_id,
        "eligible_import": r.crm_status == "new" and not r.imported_lead_id,
    }
