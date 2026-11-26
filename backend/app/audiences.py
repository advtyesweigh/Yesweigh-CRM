"""Custom audiences — dynamic filters over Dealers / Leads (CRM left-menu sources)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.dealer_exclusions import dealer_not_excluded_clause
from app.models import CustomAudience, Dealer, Lead, User

router = APIRouter(prefix="/api/audiences", tags=["audiences"])
Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

AudienceSource = Literal["dealers", "leads"]

# Public 2-digit audience UID range (like media 1000–9999, but shorter).
AUDIENCE_UID_MIN = 11
AUDIENCE_UID_MAX = 99

_STATE_MATCH_ALIASES: dict[str, list[str]] = {
    "kerala": ["Keralam", "Kerala"],
    "keralam": ["Keralam", "Kerala"],
    "tamilnadu": ["Tamil Nadu", "Tamilnadu"],
    "tamilnaduindia": ["Tamil Nadu", "Tamilnadu"],
    "karnataka": ["Karnataka"],
    "andhrapradesh": ["Andhra Pradesh"],
    "telangana": ["Telangana"],
    "odisha": ["Odisha", "Orissa"],
    "orissa": ["Odisha", "Orissa"],
    "maharashtra": ["Maharashtra"],
    "goa": ["Goa"],
    "gujarat": ["Gujarat"],
    "rajasthan": ["Rajasthan"],
    "delhi": ["Delhi", "NCT of Delhi"],
    "pondicherry": ["Puducherry", "Pondicherry"],
    "puducherry": ["Puducherry", "Pondicherry"],
}


def _norm_key(raw: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (raw or "").lower())


def resolve_state_match(raw: str) -> tuple[str, list[str]]:
    """Return (canonical_label, match_values) for Indian state filters."""
    from app.state_normalize import resolve_state_match as shared_resolve

    cleaned = (raw or "").strip()
    if not cleaned:
        return "", []
    # Prefer shared canon (Kerala/Keralam → Keralam) then local alias table.
    shared_label, shared_vals = shared_resolve(cleaned)
    if shared_vals and _norm_key(shared_label) in {"keralam", "kerala"}:
        return shared_label, shared_vals
    key = _norm_key(cleaned)
    if key in _STATE_MATCH_ALIASES:
        vals = _STATE_MATCH_ALIASES[key]
        return vals[0], vals
    # Partial alias hit (e.g. "tamil")
    for alias_key, vals in _STATE_MATCH_ALIASES.items():
        if alias_key.startswith(key) or key.startswith(alias_key):
            return vals[0], vals
    if shared_vals:
        return shared_label or cleaned, shared_vals
    return cleaned, [cleaned]


def normalize_filters(raw: dict[str, Any] | str | None) -> dict[str, str]:
    """Normalize filter payload to {state, city, district} strings (extensible)."""
    data: dict[str, Any] = {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "{}")
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError:
            data = {}
    elif isinstance(raw, dict):
        data = raw
    # Also accept nested rules: [{"field":"state","value":"Kerala"}]
    if "rules" in data and isinstance(data["rules"], list) and "state" not in data:
        for rule in data["rules"]:
            if not isinstance(rule, dict):
                continue
            field = str(rule.get("field") or "").strip().lower()
            value = str(rule.get("value") or rule.get("eq") or "").strip()
            if field in {"state", "city", "district"} and value:
                data[field] = value
    out: dict[str, str] = {}
    for key in ("state", "city", "district"):
        val = str(data.get(key) or "").strip()
        if val:
            out[key] = val
    return out


def filters_to_json(filters: dict[str, str]) -> str:
    return json.dumps(filters, ensure_ascii=False, sort_keys=True)


def filter_summary(filters: dict[str, str]) -> str:
    if not filters:
        return "All (no filters)"
    parts: list[str] = []
    for key in ("state", "city", "district"):
        if filters.get(key):
            parts.append(f"{key}={filters[key]}")
    return ", ".join(parts) if parts else "All (no filters)"


def normalize_source(raw: str | None) -> AudienceSource:
    s = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if s in {"dealer", "dealers", "dealer_menu", "left_dealer"}:
        return "dealers"
    if s in {"lead", "leads", "crm_leads", "left_leads"}:
        return "leads"
    raise ValueError("source must be 'dealers' or 'leads'")


class AudienceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source: AudienceSource
    description: str = ""
    filters: dict[str, Any] = Field(default_factory=dict)


class AudienceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    source: AudienceSource | None = None
    description: str | None = None
    filters: dict[str, Any] | None = None


class AudienceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    uid: int | None = None
    name: str
    source: str
    description: str = ""
    filters: dict[str, str] = Field(default_factory=dict)
    filter_summary: str = ""
    count: int = 0
    last_count: int | None = None
    created_by: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AudienceMemberOut(BaseModel):
    id: int
    name: str
    phone: str = ""
    company: str = ""
    city: str = ""
    district: str = ""
    state: str = ""


class AudiencePreviewOut(BaseModel):
    audience_id: int
    uid: int | None = None
    name: str
    source: str
    filters: dict[str, str]
    filter_summary: str
    total: int
    with_phone: int
    items: list[AudienceMemberOut]
    truncated: bool = False


def fmt_audience_uid(uid: int | None) -> str:
    if uid is None:
        return ""
    return f"{int(uid):02d}"


def _row_to_out(row: CustomAudience, *, count: int) -> AudienceOut:
    filters = normalize_filters(row.filters_json)
    return AudienceOut(
        id=row.id,
        uid=row.uid,
        name=row.name,
        source=row.source,
        description=row.description or "",
        filters=filters,
        filter_summary=filter_summary(filters),
        count=count,
        last_count=row.last_count,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def allocate_audience_uid(db: AsyncSession) -> int:
    """Next free 2-digit public UID in 11–99."""
    used = {
        int(v)
        for v in (
            await db.execute(select(CustomAudience.uid).where(CustomAudience.uid.is_not(None)))
        )
        .scalars()
        .all()
        if v is not None
    }
    for candidate in range(AUDIENCE_UID_MIN, AUDIENCE_UID_MAX + 1):
        if candidate not in used:
            return candidate
    raise ValueError("No free audience UIDs (11–99) remaining")


_KNOWN_STATE_LABELS = frozenset(
    {
        "Keralam",
        "Kerala",
        "Tamil Nadu",
        "Karnataka",
        "Andhra Pradesh",
        "Telangana",
        "Odisha",
        "Maharashtra",
        "Goa",
        "Gujarat",
        "Rajasthan",
        "Delhi",
        "Puducherry",
    }
)


def _split_state_tokens(raw: str) -> list[str]:
    """Split multi-state filter values on `|` or `,` (e.g. Tamil Nadu|Puducherry)."""
    s = (raw or "").strip()
    if not s:
        return []
    return [p.strip() for p in re.split(r"\s*[|,]\s*", s) if p.strip()]


def _state_token_clauses(col_state: Any, token: str) -> list[Any]:
    label, vals = resolve_state_match(token)
    if (
        vals
        and label
        and _norm_key(label) == _norm_key(vals[0])
        and label in _KNOWN_STATE_LABELS
    ):
        return [col_state.ilike(v) for v in vals]
    if vals:
        return [col_state.ilike(f"%{v}%") for v in vals]
    return [col_state.ilike(f"%{token}%")]


def _apply_geo_filters(q: Any, *, col_state: Any, col_city: Any, col_district: Any | None, filters: dict[str, str]):
    state = filters.get("state") or ""
    city = filters.get("city") or ""
    district = filters.get("district") or ""
    if state:
        state_clauses: list[Any] = []
        for token in _split_state_tokens(state):
            state_clauses.extend(_state_token_clauses(col_state, token))
        if state_clauses:
            q = q.where(or_(*state_clauses))
    if city:
        q = q.where(col_city.ilike(f"%{city}%"))
    if district and col_district is not None:
        q = q.where(col_district.ilike(f"%{district}%"))
    elif district and col_district is None:
        # Dealers store district-ish area on city
        q = q.where(col_city.ilike(f"%{district}%"))
    return q


async def count_audience_members(db: AsyncSession, row: CustomAudience) -> int:
    filters = normalize_filters(row.filters_json)
    source = (row.source or "dealers").strip().lower()
    if source == "leads":
        q = select(func.count()).select_from(Lead)
        q = _apply_geo_filters(q, col_state=Lead.state, col_city=Lead.city, col_district=Lead.district, filters=filters)
        return int((await db.execute(q)).scalar() or 0)

    visible = dealer_not_excluded_clause(Dealer.name, Dealer.company, Dealer.zoho_contact_id)
    q = select(func.count()).select_from(Dealer).where(visible)
    q = _apply_geo_filters(q, col_state=Dealer.state, col_city=Dealer.city, col_district=None, filters=filters)
    return int((await db.execute(q)).scalar() or 0)


async def resolve_audience_members(
    db: AsyncSession,
    row: CustomAudience,
    *,
    limit: int = 200,
    offset: int = 0,
    with_phone_only: bool = False,
) -> tuple[list[AudienceMemberOut], int, int]:
    """Return (items, total, with_phone). Live query — not a snapshot."""
    filters = normalize_filters(row.filters_json)
    source = (row.source or "dealers").strip().lower()
    limit = max(1, min(1500, int(limit)))
    offset = max(0, int(offset))

    if source == "leads":
        base = select(Lead)
        base = _apply_geo_filters(
            base, col_state=Lead.state, col_city=Lead.city, col_district=Lead.district, filters=filters
        )
        if with_phone_only:
            base = base.where(Lead.phone != "", Lead.phone.is_not(None))
        total = int(
            (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
        )
        phone_q = base.where(Lead.phone != "", Lead.phone.is_not(None))
        with_phone = int(
            (await db.execute(select(func.count()).select_from(phone_q.subquery()))).scalar() or 0
        )
        rows = list(
            (
                await db.execute(
                    base.order_by(Lead.name.asc(), Lead.company.asc()).offset(offset).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        items = [
            AudienceMemberOut(
                id=r.id,
                name=(r.name or r.company or "").strip() or f"Lead #{r.id}",
                phone=(r.phone or "").strip(),
                company=(r.company or "").strip(),
                city=(r.city or "").strip(),
                district=(r.district or "").strip(),
                state=(r.state or "").strip(),
            )
            for r in rows
        ]
        return items, total, with_phone

    visible = dealer_not_excluded_clause(Dealer.name, Dealer.company, Dealer.zoho_contact_id)
    base = select(Dealer).where(visible)
    base = _apply_geo_filters(
        base, col_state=Dealer.state, col_city=Dealer.city, col_district=None, filters=filters
    )
    if with_phone_only:
        base = base.where(Dealer.phone != "", Dealer.phone.is_not(None))
    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0)
    phone_q = base.where(Dealer.phone != "", Dealer.phone.is_not(None))
    with_phone = int(
        (await db.execute(select(func.count()).select_from(phone_q.subquery()))).scalar() or 0
    )
    rows = list(
        (
            await db.execute(
                base.order_by(Dealer.name.asc(), Dealer.company.asc()).offset(offset).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    items = [
        AudienceMemberOut(
            id=r.id,
            name=(r.company or r.name or "").strip() or f"Dealer #{r.id}",
            phone=(r.phone or "").strip(),
            company=(r.company or "").strip(),
            city=(r.city or "").strip(),
            district="",
            state=(r.state or "").strip(),
        )
        for r in rows
    ]
    return items, total, with_phone


async def refresh_last_count(db: AsyncSession, row: CustomAudience) -> int:
    count = await count_audience_members(db, row)
    row.last_count = count
    row.last_count_at = datetime.now(timezone.utc)
    return count


async def get_audience_or_404(db: AsyncSession, audience_id: int) -> CustomAudience:
    row = await db.get(CustomAudience, audience_id)
    if not row:
        raise HTTPException(404, "Audience not found")
    return row


async def find_audience_by_name(db: AsyncSession, name: str) -> CustomAudience | None:
    needle = (name or "").strip()
    if not needle:
        return None
    return (
        await db.execute(
            select(CustomAudience).where(func.lower(CustomAudience.name) == needle.lower())
        )
    ).scalar_one_or_none()


async def find_audience_by_uid(db: AsyncSession, uid: int) -> CustomAudience | None:
    try:
        n = int(uid)
    except (TypeError, ValueError):
        return None
    if n < AUDIENCE_UID_MIN or n > AUDIENCE_UID_MAX:
        return None
    return (
        await db.execute(select(CustomAudience).where(CustomAudience.uid == n))
    ).scalar_one_or_none()


def parse_audience_uid_token(raw: Any) -> int | None:
    """Parse a 2-digit audience UID from int/string ('11', '12'). Returns None if not in range."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if not re.fullmatch(r"\d{1,2}", s):
        return None
    try:
        n = int(s)
    except (TypeError, ValueError):
        return None
    if AUDIENCE_UID_MIN <= n <= AUDIENCE_UID_MAX:
        return n
    return None


async def create_audience_row(
    db: AsyncSession,
    *,
    name: str,
    source: str,
    description: str = "",
    filters: dict[str, Any] | None = None,
    created_by: int | None = None,
) -> CustomAudience:
    src = normalize_source(source)
    filt = normalize_filters(filters)
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("name is required")
    existing = await find_audience_by_name(db, clean_name)
    if existing:
        raise ValueError(
            f"Audience '{clean_name}' already exists "
            f"(uid={fmt_audience_uid(existing.uid) or existing.id})"
        )
    uid = await allocate_audience_uid(db)
    row = CustomAudience(
        name=clean_name[:200],
        source=src,
        description=(description or "").strip(),
        filters_json=filters_to_json(filt),
        created_by=created_by,
        uid=uid,
    )
    db.add(row)
    await db.flush()
    await refresh_last_count(db, row)
    await db.flush()
    return row


async def update_audience_row(
    db: AsyncSession,
    row: CustomAudience,
    *,
    name: str | None = None,
    source: str | None = None,
    description: str | None = None,
    filters: dict[str, Any] | None = None,
) -> CustomAudience:
    if name is not None:
        clean = name.strip()
        if not clean:
            raise ValueError("name cannot be empty")
        clash = await find_audience_by_name(db, clean)
        if clash and clash.id != row.id:
            raise ValueError(f"Audience '{clean}' already exists (id={clash.id})")
        row.name = clean[:200]
    if source is not None:
        row.source = normalize_source(source)
    if description is not None:
        row.description = description.strip()
    if filters is not None:
        row.filters_json = filters_to_json(normalize_filters(filters))
    await refresh_last_count(db, row)
    await db.flush()
    return row


async def list_audiences_with_counts(db: AsyncSession) -> list[AudienceOut]:
    rows = list(
        (
            await db.execute(
                select(CustomAudience).order_by(
                    CustomAudience.uid.asc().nulls_last(), CustomAudience.name.asc()
                )
            )
        )
        .scalars()
        .all()
    )
    out: list[AudienceOut] = []
    for row in rows:
        count = await refresh_last_count(db, row)
        out.append(_row_to_out(row, count=count))
    await db.flush()
    return out


async def seed_default_audiences(db: AsyncSession) -> list[str]:
    """Create Dealer Kerala / Leads Kerala if missing — filter defs only, no fake members."""
    created: list[str] = []
    defaults = [
        ("Dealer Kerala", "dealers", "CRM left-menu Dealers where state is Keralam", {"state": "Keralam"}),
        ("Leads Kerala", "leads", "CRM left-menu Leads where state is Keralam", {"state": "Keralam"}),
    ]
    for name, source, desc, filt in defaults:
        if await find_audience_by_name(db, name):
            continue
        row = await create_audience_row(
            db, name=name, source=source, description=desc, filters=filt, created_by=None
        )
        created.append(f"{row.name} (uid={fmt_audience_uid(row.uid)})")
    return created


# ─── HTTP API ───────────────────────────────────────────────────────────────


@router.get("", response_model=list[AudienceOut])
async def api_list_audiences(db: Db, user: CurrentUser):
    items = await list_audiences_with_counts(db)
    await db.commit()
    return items


@router.post("", response_model=AudienceOut, status_code=201)
async def api_create_audience(body: AudienceIn, db: Db, user: CurrentUser):
    try:
        row = await create_audience_row(
            db,
            name=body.name,
            source=body.source,
            description=body.description,
            filters=body.filters,
            created_by=user.id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    count = int(row.last_count or 0)
    await db.commit()
    await db.refresh(row)
    return _row_to_out(row, count=count)


@router.get("/{audience_id}", response_model=AudienceOut)
async def api_get_audience(audience_id: int, db: Db, user: CurrentUser):
    row = await get_audience_or_404(db, audience_id)
    count = await refresh_last_count(db, row)
    await db.commit()
    return _row_to_out(row, count=count)


@router.patch("/{audience_id}", response_model=AudienceOut)
async def api_update_audience(audience_id: int, body: AudienceUpdate, db: Db, user: CurrentUser):
    row = await get_audience_or_404(db, audience_id)
    try:
        await update_audience_row(
            db,
            row,
            name=body.name,
            source=body.source,
            description=body.description,
            filters=body.filters,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    count = int(row.last_count or 0)
    await db.commit()
    await db.refresh(row)
    return _row_to_out(row, count=count)


@router.delete("/{audience_id}")
async def api_delete_audience(audience_id: int, db: Db, user: CurrentUser):
    row = await get_audience_or_404(db, audience_id)
    name = row.name
    await db.delete(row)
    await db.commit()
    return {"ok": True, "deleted_id": audience_id, "name": name}


@router.get("/{audience_id}/members", response_model=AudiencePreviewOut)
async def api_preview_members(
    audience_id: int,
    db: Db,
    user: CurrentUser,
    limit: int = Query(100, ge=1, le=1500),
    offset: int = Query(0, ge=0),
    with_phone_only: bool = Query(False),
):
    row = await get_audience_or_404(db, audience_id)
    items, total, with_phone = await resolve_audience_members(
        db, row, limit=limit, offset=offset, with_phone_only=with_phone_only
    )
    row.last_count = total
    row.last_count_at = datetime.now(timezone.utc)
    await db.commit()
    filters = normalize_filters(row.filters_json)
    return AudiencePreviewOut(
        audience_id=row.id,
        uid=row.uid,
        name=row.name,
        source=row.source,
        filters=filters,
        filter_summary=filter_summary(filters),
        total=total,
        with_phone=with_phone,
        items=items,
        truncated=offset + len(items) < total,
    )
