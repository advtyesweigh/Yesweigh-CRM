"""Batch Dealer Finder jobs — district/city × category with history + resume."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.dealer_finder.constants import (
    DEFAULT_CATEGORIES,
    ORG_DEFAULT,
    STATUS_ALREADY_SEARCHED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PAUSED,
    STATUS_PENDING,
    STATUS_PARTIAL,
    STATUS_RUNNING,
)
from app.dealer_finder.crm_match import annotate_crm_status, load_crm_contacts
from app.dealer_finder.geo_plan import expand_search_units, get_or_create_location_plan, plan_as_dict
from app.dealer_finder.history import get_or_claim_log
from app.dealer_finder.normalize import canon_location_state, validate_website_url
from app.dealer_finder.places import place_to_finder_row, record_usage, search_places_staged
from app.dealer_finder.places_pricing import estimate_billable_usd_from_total_requests
from app.dealer_finder.settings_svc import budget_allows, get_or_create_settings
from app.models import DealerSearchJob, DealerSearchLog, DealerSearchPlace, Lead, User
from app.dealer_finder.phone_filters import count_phone_kinds_for_places
from app.phone_util import classify_indian_phone, is_indian_mobile, normalize_in_mobile, store_crm_phone
from app.state_normalize import canon_state

logger = logging.getLogger("crm.dealer_finder.jobs")


# Job statuses where the full unique-places list may be shown (no drip while running).
RESULTS_READY_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_CANCELLED,
        STATUS_PAUSED,
        STATUS_PARTIAL,
    }
)

_stop = asyncio.Event()
_task: asyncio.Task | None = None
_wake = asyncio.Event()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def wake_dealer_finder_worker() -> None:
    _wake.set()


def job_results_ready(job: DealerSearchJob) -> bool:
    """True when the batch has stopped — safe to list ALL unique places."""
    return (job.status or "") in RESULTS_READY_STATUSES


def job_scan_complete(job: DealerSearchJob) -> bool:
    """True only when every planned unit finished (not budget-paused mid-scan)."""
    total = int(job.total_units or 0)
    done = int(job.completed_units or 0)
    return (job.status or "") == STATUS_COMPLETED and (total == 0 or done >= total)


async def create_search_job(
    db: AsyncSession,
    *,
    user: User,
    state: str,
    categories: list[str] | None = None,
    district: str = "",
    city: str = "",
    search_depth: str = "full",
    lead_owner_id: int | None = None,
    force_refresh: bool = False,
    organization_id: str = ORG_DEFAULT,
) -> DealerSearchJob:
    state_label = canon_location_state(state)
    if not state_label:
        raise ValueError("state is required")

    settings = await get_or_create_settings(db, organization_id=organization_id)
    running = (
        await db.execute(
            select(DealerSearchJob).where(
                DealerSearchJob.organization_id == organization_id,
                DealerSearchJob.status == STATUS_RUNNING,
                DealerSearchJob.state == state_label,
            )
        )
    ).scalar_one_or_none()
    if running and not force_refresh:
        return running

    cats = [c.strip() for c in (categories or DEFAULT_CATEGORIES) if (c or "").strip()]
    if not cats:
        cats = list(DEFAULT_CATEGORIES)

    # Statewide (no district/city filter) always uses full district+city expansion.
    depth = (search_depth or "full").strip() or "full"
    if not (district or "").strip() and not (city or "").strip():
        depth = "full"

    plan = await get_or_create_location_plan(
        db,
        state=state_label,
        user=user,
        organization_id=organization_id,
        force_refresh=False,
    )
    plan_body = plan_as_dict(plan)
    units = expand_search_units(
        {"districts": plan_body.get("districts") or []},
        categories=cats,
        state=state_label,
        search_depth=depth,
        district_filter=district,
        city_filter=city,
    )
    if not units:
        raise ValueError("No search units — check state/district/city filters")

    job = DealerSearchJob(
        public_id=str(uuid.uuid4()),
        organization_id=organization_id,
        status=STATUS_PENDING,
        state=state_label,
        district_filter=(district or "").strip(),
        city_filter=(city or "").strip(),
        categories_json=json.dumps(cats, ensure_ascii=False),
        search_depth=depth,
        lead_owner_id=lead_owner_id or user.id,
        location_plan_id=plan.id,
        force_refresh=bool(force_refresh),
        total_units=len(units),
        units_json=json.dumps(units, ensure_ascii=False),
        created_by=user.id,
    )
    db.add(job)
    await db.flush()
    _ = settings
    wake_dealer_finder_worker()
    return job


async def set_job_status(
    db: AsyncSession, job: DealerSearchJob, status: str, *, error: str = ""
) -> DealerSearchJob:
    job.status = status
    if error:
        job.error = error[:2000]
    if status in {STATUS_PENDING, STATUS_RUNNING}:
        # Resuming / reclaiming — do not keep a prior pause finished_at.
        job.finished_at = None
    if status == STATUS_RUNNING and not job.started_at:
        job.started_at = _now()
        job.claimed_at = _now()
    if status in {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_CANCELLED,
        STATUS_PAUSED,
        STATUS_PARTIAL,
    }:
        job.finished_at = _now()
    await db.flush()
    wake_dealer_finder_worker()
    return job


def job_public(job: DealerSearchJob) -> dict[str, Any]:
    try:
        cats = json.loads(job.categories_json or "[]")
    except json.JSONDecodeError:
        cats = []
    ready = job_results_ready(job)
    return {
        "id": job.id,
        "public_id": job.public_id,
        "status": job.status,
        "state": job.state,
        "district_filter": job.district_filter,
        "city_filter": job.city_filter,
        "categories": cats,
        "search_depth": job.search_depth,
        "lead_owner_id": job.lead_owner_id,
        "force_refresh": job.force_refresh,
        "total_units": job.total_units,
        "completed_units": job.completed_units,
        "skipped_units": job.skipped_units,
        "failed_units": job.failed_units,
        "places_found": job.places_found,
        "places_new": job.places_new,
        "api_requests": job.api_requests,
        "next_unit_index": job.next_unit_index,
        "error": job.error,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "progress_pct": int(
            round(100.0 * job.completed_units / job.total_units)
        )
        if job.total_units
        else 0,
        "results_ready": ready,
        "scan_complete": job_scan_complete(job),
        "incomplete": ready and not job_scan_complete(job),
        "estimated_cost_usd": float(
            estimate_billable_usd_from_total_requests(int(job.api_requests or 0))
        ),
    }


# Chat / import-preview display: full phone-eligible NEW list (not page-local 200).
DISPLAY_ELIGIBLE_PHONE_LIMIT = 10_000
# Bump when list shaping changes (UI uses this to re-fetch stored chat cards).
PHONE_LIST_FORMAT_VERSION = 3


def _place_phone_dedupe_score(place: DealerSearchPlace) -> int:
    """Higher = prefer keeping this row when multiple places share one phone."""
    score = 0
    if (place.website or "").strip():
        score += 8
    name = (place.name or "").strip()
    if name:
        score += min(len(name), 160)
    if (place.address or "").strip():
        score += 2
    if (place.maps_url or "").strip():
        score += 1
    return score


def _phone_dedupe_key(phone: str) -> str:
    """Stable dedupe key for Indian mobile (10-digit) or any other present phone."""
    raw = (phone or "").strip()
    if not raw:
        return ""
    if is_indian_mobile(raw):
        return normalize_in_mobile(raw) or raw
    import re

    digits = re.sub(r"\D+", "", raw)
    return digits or raw


def dedupe_places_by_phone(
    places: list[DealerSearchPlace],
    *,
    mobile_only: bool = True,
) -> tuple[list[DealerSearchPlace], int]:
    """Keep the best row per normalized phone; stable order by place id."""
    by_phone: dict[str, DealerSearchPlace] = {}
    for p in places:
        phone = (p.phone or "").strip()
        if not phone:
            continue
        if mobile_only and not is_indian_mobile(phone):
            continue
        key = _phone_dedupe_key(phone)
        if not key:
            continue
        prev = by_phone.get(key)
        if prev is None:
            by_phone[key] = p
            continue
        p_score = _place_phone_dedupe_score(p)
        prev_score = _place_phone_dedupe_score(prev)
        if p_score > prev_score or (p_score == prev_score and int(p.id or 0) < int(prev.id or 0)):
            by_phone[key] = p
    deduped = sorted(by_phone.values(), key=lambda x: int(x.id or 0))
    dupes_removed = max(0, len(places) - len(deduped))
    return deduped, dupes_removed


def place_is_eligible_new(place: DealerSearchPlace) -> bool:
    """True when place is CRM-new and not yet imported as a lead."""
    st = (place.crm_status or "").lower()
    return st == "new" and not place.imported_lead_id


def place_is_preexisting_crm(place: DealerSearchPlace) -> bool:
    """True when place matched CRM dealer/lead before this job's import."""
    st = (place.crm_status or "").lower()
    return st in ("dealer", "lead") and not place.imported_lead_id


def place_is_discovery_new(place: DealerSearchPlace) -> bool:
    """Google-discovered NEW for this job, including rows already imported here."""
    if place_is_preexisting_crm(place):
        return False
    st = (place.crm_status or "").lower()
    return st == "new" or bool(place.imported_lead_id)


def place_is_eligible_new_with_any_phone(place: DealerSearchPlace) -> bool:
    return place_is_eligible_new(place) and bool((place.phone or "").strip())


def place_is_discovery_new_with_any_phone(place: DealerSearchPlace) -> bool:
    return place_is_discovery_new(place) and bool((place.phone or "").strip())


def place_is_discovery_mobile_new(place: DealerSearchPlace) -> bool:
    return place_is_discovery_new(place) and is_indian_mobile(place.phone or "")


def place_is_eligible_with_phone(place: DealerSearchPlace) -> bool:
    """True when place is import-eligible NEW and has an Indian mobile number."""
    return place_is_eligible_new(place) and is_indian_mobile(place.phone or "")


def place_row_public(place: DealerSearchPlace) -> dict[str, Any]:
    """Serialise a place for AI chat / results API (no crm_status needed in UI table)."""
    phone = (place.phone or "").strip()
    website = (place.website or "").strip()
    return {
        "id": place.id,
        "place_id": place.place_id or "",
        "name": place.name or "",
        "whatsapp": phone,
        "phone": phone,
        "website": website,
        "city": place.city or "",
        "district": place.district or "",
        "state": place.state or "",
        "category": place.category or "",
        "address": place.address or "",
        "maps_url": place.maps_url or "",
        "lead_source": place.lead_source or "Google",
        "crm_status": place.crm_status or "",
        "crm_match": place.crm_match,
        "imported_lead_id": place.imported_lead_id,
        "eligible_import": place_is_eligible_new(place),
        "has_phone": bool(phone),
    }


async def place_counts_for_job(db: AsyncSession, job_id: int) -> dict[str, int]:
    """Unique place counts for a job (places are already job-deduped by place_id)."""
    places = (
        await db.execute(
            select(DealerSearchPlace).where(DealerSearchPlace.job_id == job_id)
        )
    ).scalars().all()
    unique = len(places)
    already_dealer = 0
    already_lead = 0
    eligible = 0
    eligible_phone_raw: list[DealerSearchPlace] = []
    eligible_any_phone: list[DealerSearchPlace] = []
    imported = 0
    with_website = 0
    with_phone = 0
    for p in places:
        st = (p.crm_status or "").lower()
        if p.imported_lead_id:
            imported += 1
        if st == "dealer":
            already_dealer += 1
        elif st == "lead":
            already_lead += 1
        elif place_is_eligible_new(p):
            eligible += 1
            if (p.phone or "").strip():
                eligible_any_phone.append(p)
            if place_is_eligible_with_phone(p):
                eligible_phone_raw.append(p)
        if (p.website or "").strip():
            with_website += 1
        if (p.phone or "").strip():
            with_phone += 1

    deduped_importable, phone_dupes_removed = dedupe_places_by_phone(eligible_phone_raw)
    eligible_with_phone = len(deduped_importable)
    phone_kinds = count_phone_kinds_for_places(eligible_any_phone)
    eligible_landlines = int(phone_kinds.get("landline") or 0)
    eligible_no_phone = eligible - len(eligible_any_phone)
    eligible_invalid_phone = int(phone_kinds.get("invalid") or 0)

    discovery_any_raw = [
        p for p in places if place_is_discovery_new_with_any_phone(p)
    ]
    discovery_mobile_raw = [p for p in places if place_is_discovery_mobile_new(p)]
    deduped_any_discovery, discovery_any_dupes = dedupe_places_by_phone(
        discovery_any_raw, mobile_only=False
    )
    deduped_mobile_discovery, discovery_mobile_dupes = dedupe_places_by_phone(
        discovery_mobile_raw
    )
    discovery_kinds = count_phone_kinds_for_places(discovery_any_raw)

    return {
        "unique_places": unique,
        "already_dealer": already_dealer,
        "already_lead": already_lead,
        "eligible_import": eligible,
        "eligible_with_phone": eligible_with_phone,
        "importable_with_phone": eligible_with_phone,
        "eligible_with_phone_raw": len(eligible_phone_raw),
        "phone_dupes_removed": phone_dupes_removed,
        "already_imported": imported,
        "with_website": with_website,
        "with_phone": with_phone,
        "phone_list_format_version": PHONE_LIST_FORMAT_VERSION,
        "eligible_mobile_new": eligible_with_phone,
        "eligible_landlines_excluded": eligible_landlines,
        "eligible_no_phone_excluded": eligible_no_phone,
        "eligible_invalid_phone_excluded": eligible_invalid_phone,
        "discovery_any_phone_new": len(deduped_any_discovery),
        "discovery_any_phone_dupes_removed": discovery_any_dupes,
        "discovery_mobile_new": len(deduped_mobile_discovery),
        "discovery_mobile_dupes_removed": discovery_mobile_dupes,
        "discovery_landlines": int(discovery_kinds.get("landline") or 0),
    }


async def list_eligible_phone_places(
    db: AsyncSession,
    job_id: int,
    *,
    limit: int = DISPLAY_ELIGIBLE_PHONE_LIMIT,
    offset: int = 0,
    mobile_only: bool = True,
    discovery: bool = False,
) -> tuple[list[DealerSearchPlace], int]:
    """Return (rows, total) for phone-NEW list.

    ``discovery=True`` (finished jobs / chat card): include job-imported rows so sync
    does not collapse the list after partial import. Default filters to Indian mobiles.
    Import still uses ``discovery=False`` (importable NEW only).
    """
    places = (
        await db.execute(
            select(DealerSearchPlace)
            .where(DealerSearchPlace.job_id == job_id)
            .order_by(DealerSearchPlace.id.asc())
        )
    ).scalars().all()
    if discovery:
        if mobile_only:
            filtered = [p for p in places if place_is_discovery_mobile_new(p)]
            deduped, _dupes = dedupe_places_by_phone(filtered)
        else:
            filtered = [p for p in places if place_is_discovery_new_with_any_phone(p)]
            deduped, _dupes = dedupe_places_by_phone(filtered, mobile_only=False)
    elif mobile_only:
        filtered = [p for p in places if place_is_eligible_with_phone(p)]
        deduped, _dupes = dedupe_places_by_phone(filtered)
    else:
        filtered = [p for p in places if place_is_eligible_new_with_any_phone(p)]
        deduped, _dupes = dedupe_places_by_phone(filtered, mobile_only=False)
    total = len(deduped)
    lim = max(1, min(DISPLAY_ELIGIBLE_PHONE_LIMIT, int(limit or DISPLAY_ELIGIBLE_PHONE_LIMIT)))
    off = max(0, int(offset or 0))
    return deduped[off : off + lim], total


def build_import_confirm_prompt(
    counts: dict[str, int],
    *,
    incomplete: bool = False,
    done: int = 0,
    total_units: int = 0,
    error: str = "",
    list_phone_count: int | None = None,
) -> str:
    """Confirm copy for chat — matches unique-phone NEW list count."""
    phone_n = (
        int(list_phone_count)
        if list_phone_count is not None
        else int(counts.get("eligible_with_phone") or 0)
    )
    dupes = int(counts.get("phone_dupes_removed") or 0)
    dup_note = f" ({dupes} duplicate phones dropped from list)" if dupes else ""
    err = (error or "").strip()
    remaining = max(0, int(total_units or 0) - int(done or 0))
    imported = int(counts.get("already_imported") or 0)
    if incomplete or (total_units and done < total_units):
        return (
            f"Scan in progress: {done}/{total_units} units ({remaining} remaining)"
            f"{': ' + err if err and incomplete else ''}. "
            f"So far {phone_n} unique phone NEW{dup_note}. "
            f"Import {phone_n} unique mobile NEW places to Leads "
            f"(source Google, status New)? Reply yes to import when scan completes, "
            f"or say resume to finish the state first."
        )
    if imported and phone_n < int(counts.get("discovery_mobile_new") or phone_n):
        return (
            f"{imported} already imported from this job. "
            f"Import {phone_n} remaining unique mobile NEW to Leads "
            f"(source Google, status New)? Reply yes to import."
        )
    return (
        f"Import {phone_n} unique mobile NEW places to Leads "
        f"(source Google, status New)? Reply yes to import."
    )


async def build_job_status_report(
    db: AsyncSession,
    job: DealerSearchJob,
    *,
    import_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full job status report for UI / AI after search (and optionally after import)."""
    counts = await place_counts_for_job(db, job.id)
    api_n = int(job.api_requests or 0)
    cost = float(estimate_billable_usd_from_total_requests(api_n))
    report: dict[str, Any] = {
        "job_id": job.id,
        "public_id": job.public_id,
        "state": job.state,
        "status": job.status,
        "results_ready": job_results_ready(job),
        "searched_units": int(job.completed_units or 0),
        "total_units": int(job.total_units or 0),
        "skipped_units_history": int(job.skipped_units or 0),
        "failed_units": int(job.failed_units or 0),
        "api_requests": api_n,
        "estimated_cost_usd": cost,
        "unique_places": counts["unique_places"],
        "existing_dealers": counts["already_dealer"],
        "existing_leads": counts["already_lead"],
        "eligible": counts["eligible_import"],
        "eligible_with_phone": counts["eligible_with_phone"],
        "with_website": counts["with_website"],
        "with_phone": counts["with_phone"],
        "error": job.error or "",
    }
    if import_result:
        report["imported"] = int(import_result.get("inserted") or 0)
        report["failed_or_skipped"] = int(import_result.get("skipped_duplicates") or 0)
        report["import_received"] = int(import_result.get("received") or 0)
    else:
        report["imported"] = counts["already_imported"]
        report["failed_or_skipped"] = 0
    return report


async def _persist_places(
    db: AsyncSession,
    *,
    log: DealerSearchLog,
    job: DealerSearchJob,
    places: list[dict[str, Any]],
    unit: dict[str, Any],
    contacts: list[Any],
) -> tuple[int, int]:
    """Upsert places for this log; return (total, new_count)."""
    new_count = 0
    for p in places:
        row = place_to_finder_row(
            p,
            state_fallback=str(unit.get("state") or job.state),
            district_fallback=str(unit.get("district") or ""),
            category=str(unit.get("category") or ""),
        )
        annotated = annotate_crm_status(row, contacts)
        place_id = str(annotated.get("place_id") or "").strip()
        if not place_id:
            continue
        existing = (
            await db.execute(
                select(DealerSearchPlace).where(
                    DealerSearchPlace.search_log_id == log.id,
                    DealerSearchPlace.place_id == place_id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            existing.phone = annotated.get("phone") or existing.phone
            existing.website = annotated.get("website") or existing.website
            existing.crm_status = annotated.get("crm_status") or existing.crm_status
            existing.crm_match = annotated.get("crm_match") or existing.crm_match
            continue
        # Also dedupe within job by place_id
        job_hit = (
            await db.execute(
                select(DealerSearchPlace).where(
                    DealerSearchPlace.job_id == job.id,
                    DealerSearchPlace.place_id == place_id,
                )
            )
        ).scalar_one_or_none()
        if job_hit:
            continue
        db.add(
            DealerSearchPlace(
                search_log_id=log.id,
                job_id=job.id,
                place_id=place_id,
                name=str(annotated.get("name") or "")[:300],
                address=str(annotated.get("address") or ""),
                city=str(annotated.get("city") or "")[:80],
                district=str(annotated.get("district") or unit.get("district") or "")[:80],
                state=str(annotated.get("state") or job.state)[:80],
                phone=str(annotated.get("phone") or "")[:40],
                website=validate_website_url(str(annotated.get("website") or "")),
                category=str(annotated.get("category") or "")[:160],
                maps_url=str(annotated.get("maps_url") or "")[:500],
                lead_source="Google",
                crm_status=str(annotated.get("crm_status") or "new")[:40],
                crm_match=str(annotated.get("crm_match") or "")[:120],
            )
        )
        if annotated.get("eligible_import"):
            new_count += 1
    await db.flush()
    return len(places), new_count


async def _process_unit(
    db: AsyncSession,
    job: DealerSearchJob,
    unit: dict[str, Any],
    contacts: list[Any],
) -> str:
    """Process one search unit. Returns outcome: completed|skipped|failed|paused."""
    settings = await get_or_create_settings(db, organization_id=job.organization_id)
    ok_budget, reason = await budget_allows(
        db,
        settings,
        organization_id=job.organization_id,
        extra_requests=1,
        job_requests_so_far=int(job.api_requests or 0),
    )
    if not ok_budget:
        job.error = reason
        job.status = STATUS_PAUSED
        await db.flush()
        return "paused"

    log, action = await get_or_claim_log(
        db,
        state=job.state,
        district=str(unit.get("district") or ""),
        city=str(unit.get("city") or ""),
        category=str(unit.get("category") or ""),
        text_query=str(unit.get("text_query") or ""),
        job_id=job.id,
        user_id=job.created_by,
        force_refresh=bool(job.force_refresh),
        cooldown_days=int(settings.cooldown_days or 30),
        organization_id=job.organization_id,
    )

    if action == "reuse":
        log.status = STATUS_ALREADY_SEARCHED
        # Recount new from existing places
        places = (
            await db.execute(
                select(DealerSearchPlace).where(DealerSearchPlace.search_log_id == log.id)
            )
        ).scalars().all()
        # Attach to this job if missing
        for p in places:
            if p.job_id is None:
                p.job_id = job.id
        job.skipped_units = int(job.skipped_units or 0) + 1
        job.completed_units = int(job.completed_units or 0) + 1
        job.places_found = int(job.places_found or 0) + len(places)
        job.places_new = int(job.places_new or 0) + sum(
            1 for p in places if (p.crm_status or "") == "new"
        )
        await db.flush()
        return "skipped"

    policy = (settings.expensive_field_policy or "when_needed").lower()
    fetch_contacts = policy != "never"
    raw = await search_places_staged(
        text_query=str(unit.get("text_query") or log.text_query),
        max_results=int(log.max_results or 60),
        page_size=int(log.page_size or 20),
        start_page_token=log.next_page_token if action == "resume" else "",
        pages_already_done=int(log.pages_done or 0) if action == "resume" else 0,
        fetch_contacts=fetch_contacts,
    )
    s1 = int(raw.get("stage1_requests") or 0)
    s2 = int(raw.get("stage2_requests") or 0)
    job.api_requests = int(job.api_requests or 0) + s1 + s2
    log.api_requests = int(log.api_requests or 0) + s1 + s2
    log.pages_fetched = int(raw.get("pages") or 0)
    log.pages_done = int(raw.get("pages") or 0)
    log.next_page_token = str(raw.get("next_page_token") or "")[:500]
    await record_usage(
        db, organization_id=job.organization_id, stage1=s1, stage2=s2
    )

    if not raw.get("ok") and not raw.get("places"):
        log.status = STATUS_FAILED
        job.failed_units = int(job.failed_units or 0) + 1
        job.completed_units = int(job.completed_units or 0) + 1
        job.error = str(raw.get("error") or "Places failed")[:2000]
        await db.flush()
        return "failed"

    total, new_n = await _persist_places(
        db,
        log=log,
        job=job,
        places=list(raw.get("places") or []),
        unit=unit,
        contacts=contacts,
    )
    places_rows = (
        await db.execute(
            select(DealerSearchPlace).where(DealerSearchPlace.search_log_id == log.id)
        )
    ).scalars().all()
    log.results_count = len(places_rows)
    log.with_phone_count = sum(1 for p in places_rows if (p.phone or "").strip())
    log.with_website_count = sum(1 for p in places_rows if (p.website or "").strip())
    log.status = STATUS_COMPLETED
    log.searched_at = _now()
    job.completed_units = int(job.completed_units or 0) + 1
    job.places_found = int(job.places_found or 0) + total
    job.places_new = int(job.places_new or 0) + new_n
    await db.flush()
    return "completed"


async def run_job_once(db: AsyncSession, job: DealerSearchJob) -> None:
    if job.status in {STATUS_CANCELLED, STATUS_COMPLETED}:
        return
    if job.status == STATUS_PAUSED:
        return

    try:
        units = json.loads(job.units_json or "[]")
    except json.JSONDecodeError:
        units = []
    if not isinstance(units, list) or not units:
        job.status = STATUS_FAILED
        job.error = "No units on job"
        job.finished_at = _now()
        await db.flush()
        return

    job.status = STATUS_RUNNING
    if not job.started_at:
        job.started_at = _now()
    job.claimed_at = _now()
    await db.flush()

    contacts, _, _ = await load_crm_contacts(db, state=job.state)
    idx = int(job.next_unit_index or 0)
    while idx < len(units):
        await db.refresh(job)
        if job.status == STATUS_CANCELLED:
            return
        if job.status == STATUS_PAUSED:
            return
        unit = units[idx] if isinstance(units[idx], dict) else {}
        outcome = await _process_unit(db, job, unit, contacts)
        idx += 1
        job.next_unit_index = idx
        await db.commit()
        if outcome == "paused":
            return
        # Gentle pacing to respect Places rate limits
        await asyncio.sleep(0.35)

    job.status = STATUS_COMPLETED
    job.finished_at = _now()
    await db.commit()


async def _claim_next_job(db: AsyncSession) -> DealerSearchJob | None:
    # Recover stale Running jobs after VPS restart (claimed > 30 min ago with no progress claim refresh)
    stale_before = _now()
    rows = (
        await db.execute(
            select(DealerSearchJob).where(DealerSearchJob.status == STATUS_RUNNING)
        )
    ).scalars().all()
    for j in rows:
        claimed = j.claimed_at
        if claimed and (stale_before - claimed).total_seconds() > 1800:
            # Leave as Running — will resume from next_unit_index
            j.claimed_at = _now()
            await db.commit()
            return j
        if claimed and (stale_before - claimed).total_seconds() < 120:
            # Actively claimed
            continue
        j.claimed_at = _now()
        await db.commit()
        return j

    pending = (
        await db.execute(
            select(DealerSearchJob)
            .where(DealerSearchJob.status == STATUS_PENDING)
            .order_by(DealerSearchJob.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if pending:
        pending.status = STATUS_RUNNING
        pending.claimed_at = _now()
        pending.started_at = pending.started_at or _now()
        await db.commit()
        return pending
    return None


async def _worker_loop() -> None:
    logger.info("Dealer Finder worker started")
    while not _stop.is_set():
        try:
            async with SessionLocal() as db:
                job = await _claim_next_job(db)
                if job:
                    try:
                        await run_job_once(db, job)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Dealer Finder job %s failed", job.public_id)
                        job.status = STATUS_FAILED
                        job.error = f"{type(exc).__name__}: {exc}"[:2000]
                        job.finished_at = _now()
                        await db.commit()
                from app.dealer_finder.all_india_jobs import tick_all_india_runs

                await tick_all_india_runs(db)
                _wake.clear()
                try:
                    await asyncio.wait_for(_wake.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
        except Exception:  # noqa: BLE001
            logger.exception("Dealer Finder worker loop error")
            await asyncio.sleep(2.0)
    logger.info("Dealer Finder worker stopped")


def start_dealer_finder_worker() -> None:
    global _task
    _stop.clear()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("Dealer Finder worker not started — no running event loop")
        return
    if _task and not _task.done():
        return
    _task = loop.create_task(_worker_loop())
    logger.warning("Dealer Finder worker task created")


async def stop_dealer_finder_worker() -> None:
    global _task
    _stop.set()
    _wake.set()
    if _task:
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            _task.cancel()
        _task = None


async def import_eligible_places(
    db: AsyncSession,
    *,
    user: User,
    job_id: int | None = None,
    place_ids: list[int] | None = None,
    persist_website: bool | None = None,
    with_phone_only: bool = False,
) -> dict[str, Any]:
    """Import eligible (crm_status=new) places as Leads.

    source=Google; owner optional (job.lead_owner_id or importing user).
    with_phone_only skips places with no phone (left as NEW for later).
    """
    settings = await get_or_create_settings(db)
    allow_website = (
        bool(persist_website)
        if persist_website is not None
        else bool(settings.persist_website_on_import)
    )
    q = select(DealerSearchPlace).where(
        DealerSearchPlace.crm_status == "new",
        DealerSearchPlace.imported_lead_id.is_(None),
    )
    if job_id:
        q = q.where(DealerSearchPlace.job_id == job_id)
    if place_ids:
        q = q.where(DealerSearchPlace.id.in_(place_ids))
    rows = (await db.execute(q)).scalars().all()

    # Batch phone dedupe against existing leads
    existing_phones = {
        normalize_in_mobile(p)
        for (p,) in (
            await db.execute(select(Lead.phone))
        ).all()
        if normalize_in_mobile(p)
    }
    inserted = 0
    skipped = 0
    skipped_no_phone = 0
    skipped_job_phone_dupes = 0
    seen_job_phones: set[str] = set()
    job_owner: int | None = None
    if job_id:
        job = await db.get(DealerSearchJob, job_id)
        if job and job.lead_owner_id:
            job_owner = int(job.lead_owner_id)
    for r in rows:
        phone10 = normalize_in_mobile(r.phone or "")
        stored_phone = store_crm_phone(r.phone or "") if phone10 else (r.phone or "")[:40]
        if with_phone_only and not (phone10 and is_indian_mobile(r.phone or "")):
            skipped_no_phone += 1
            continue
        if phone10 and phone10 in existing_phones:
            r.crm_status = "lead"
            r.crm_match = "phone:lead:import_batch"
            skipped += 1
            continue
        if phone10 and phone10 in seen_job_phones:
            skipped_job_phone_dupes += 1
            continue
        # Owner optional: prefer job owner, else importing user (may be None).
        owner_id = job_owner if job_owner is not None else (user.id if user else None)
        display_name = (r.name or "").strip() or "Google place"
        lead = Lead(
            name=display_name[:160],
            phone=stored_phone[:40],
            company=display_name[:160],
            customer_website=(r.website or "")[:500] if allow_website else "",
            city=(r.city or "")[:80],
            district=(r.district or "")[:80],
            state=canon_state(r.state or "")[:80],
            address=(r.address or ""),
            source="Google",
            status="new",
            product_category=(r.category or "")[:160],
            notes="",
            owner_id=owner_id,
        )
        db.add(lead)
        await db.flush()
        r.imported_lead_id = lead.id
        r.crm_status = "lead"
        r.crm_match = f"imported:{lead.id}"
        if phone10:
            existing_phones.add(phone10)
            seen_job_phones.add(phone10)
        inserted += 1
    await db.flush()
    result = {
        "ok": True,
        "inserted": inserted,
        "skipped_duplicates": skipped,
        "skipped_job_phone_dupes": skipped_job_phone_dupes,
        "skipped_no_phone": skipped_no_phone,
        "with_phone_only": bool(with_phone_only),
        "received": len(rows),
    }
    if job_id:
        job = await db.get(DealerSearchJob, job_id)
        if job:
            result["status_report"] = await build_job_status_report(
                db, job, import_result=result
            )
    return result
