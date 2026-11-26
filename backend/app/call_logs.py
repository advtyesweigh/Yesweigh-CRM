"""Call log sync API — Android YES CRM posts events; CRM Call List reads them."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.models import CallLog, User
from app.phone_util import normalize_in_mobile, store_crm_phone

router = APIRouter(prefix="/api/call-logs", tags=["call-logs"])

Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

CallType = Literal["missed", "received", "rejected", "outbound"]
CALL_TYPES = frozenset({"missed", "received", "rejected", "outbound"})

# Android heartbeats every ~45s; treat agent as connected within this window.
ONLINE_THRESHOLD = timedelta(minutes=2, seconds=30)


def _is_online(last_seen: datetime | None, *, now: datetime | None = None) -> bool:
    if last_seen is None:
        return False
    ts = last_seen
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    ref = now or datetime.now(timezone.utc)
    return (ref - ts) <= ONLINE_THRESHOLD


async def _touch_call_app_presence(db: AsyncSession, user: User) -> datetime:
    now = datetime.now(timezone.utc)
    user.call_app_last_seen_at = now
    await db.flush()
    return now


def _norm_phone(raw: str) -> str:
    stored = store_crm_phone(raw) or ""
    if stored:
        return stored
    digits = normalize_in_mobile(raw) or re.sub(r"\D+", "", raw or "")
    return digits[:40]


def _parse_started_at(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class CallLogEventIn(BaseModel):
    phone_number: str = Field(..., min_length=1, max_length=40)
    started_at: datetime | str
    call_type: CallType
    duration_seconds: int = Field(0, ge=0, le=86_400)
    contact_name: str = ""
    direction: str = ""
    android_call_id: str = ""
    android_type_code: int | None = None
    device_id: str = ""
    device_label: str = ""
    client_event_id: str = ""
    meta: dict[str, Any] | None = None


class CallLogBatchIn(BaseModel):
    events: list[CallLogEventIn] = Field(default_factory=list, max_length=200)


class CallLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_user_id: int
    agent_name: str = ""
    agent_email: str = ""
    agent_online: bool = False
    phone_number: str = ""
    phone_normalized: str = ""
    contact_name: str = ""
    call_type: str = ""
    direction: str = ""
    started_at: datetime
    duration_seconds: int = 0
    android_call_id: str = ""
    device_id: str = ""
    device_label: str = ""
    synced_at: datetime | None = None
    created_at: datetime | None = None


class CallLogPageOut(BaseModel):
    items: list[CallLogOut]
    total: int
    page: int
    page_size: int
    counts: dict[str, int]


class CallLogUpsertResult(BaseModel):
    id: int
    created: bool
    call_type: str
    phone_number: str
    started_at: datetime


class CallLogBatchOut(BaseModel):
    accepted: int
    results: list[CallLogUpsertResult]


class CallHeartbeatIn(BaseModel):
    device_id: str = ""
    device_label: str = ""


class CallHeartbeatOut(BaseModel):
    ok: bool = True
    last_seen_at: datetime
    online_threshold_seconds: int


class CallInsightAgent(BaseModel):
    id: int
    name: str
    email: str = ""
    call_count: int
    online: bool = False
    last_seen_at: datetime | None = None


class CallFollowUpItem(BaseModel):
    id: int
    contact_name: str = ""
    phone_number: str = ""
    call_type: str = ""
    started_at: datetime
    agent_name: str = ""


class CallLogInsightsOut(BaseModel):
    total_calls: int
    total_talk_seconds: int
    avg_duration_seconds: float
    top_agents: list[CallInsightAgent]
    follow_ups: list[CallFollowUpItem]
    follow_up_total: int
    devices: list[str]
    online_threshold_seconds: int = int(ONLINE_THRESHOLD.total_seconds())


def _direction_for(call_type: str, direction: str) -> str:
    d = (direction or "").strip().lower()
    if d in ("inbound", "outbound"):
        return d
    if call_type == "outbound":
        return "outbound"
    return "inbound"


async def _upsert_event(db: AsyncSession, user: User, event: CallLogEventIn) -> CallLogUpsertResult:
    call_type = str(event.call_type).strip().lower()
    if call_type not in CALL_TYPES:
        raise HTTPException(400, f"Invalid call_type: {event.call_type}")

    phone_raw = (event.phone_number or "").strip()[:40]
    if not phone_raw:
        raise HTTPException(400, "phone_number required")

    phone_norm = _norm_phone(phone_raw) or phone_raw
    started = _parse_started_at(event.started_at)
    direction = _direction_for(call_type, event.direction)
    meta_json = ""
    if event.meta:
        try:
            meta_json = json.dumps(event.meta, ensure_ascii=False)[:4000]
        except (TypeError, ValueError):
            meta_json = ""

    existing_id = (
        await db.execute(
            select(CallLog.id).where(
                CallLog.agent_user_id == user.id,
                CallLog.phone_normalized == phone_norm,
                CallLog.started_at == started,
                CallLog.call_type == call_type,
            )
        )
    ).scalar_one_or_none()
    created = existing_id is None

    values = {
        "agent_user_id": user.id,
        "phone_number": phone_raw,
        "phone_normalized": phone_norm,
        "contact_name": (event.contact_name or "").strip()[:160],
        "call_type": call_type,
        "direction": direction,
        "started_at": started,
        "duration_seconds": int(event.duration_seconds or 0),
        "android_call_id": (event.android_call_id or "").strip()[:80],
        "android_type_code": event.android_type_code,
        "device_id": (event.device_id or "").strip()[:120],
        "device_label": (event.device_label or "").strip()[:160],
        "client_event_id": (event.client_event_id or "").strip()[:120],
        "meta_json": meta_json,
        "synced_at": datetime.now(timezone.utc),
    }

    stmt = pg_insert(CallLog).values(**values)
    update_cols = {
        "phone_number": stmt.excluded.phone_number,
        "contact_name": stmt.excluded.contact_name,
        "direction": stmt.excluded.direction,
        "duration_seconds": stmt.excluded.duration_seconds,
        "android_call_id": stmt.excluded.android_call_id,
        "android_type_code": stmt.excluded.android_type_code,
        "device_id": stmt.excluded.device_id,
        "device_label": stmt.excluded.device_label,
        "client_event_id": stmt.excluded.client_event_id,
        "meta_json": stmt.excluded.meta_json,
        "synced_at": stmt.excluded.synced_at,
        "updated_at": datetime.now(timezone.utc),
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["agent_user_id", "phone_normalized", "started_at", "call_type"],
        set_=update_cols,
    ).returning(CallLog.id)

    row_id = (await db.execute(stmt)).scalar_one()
    return CallLogUpsertResult(
        id=int(row_id),
        created=created,
        call_type=call_type,
        phone_number=phone_raw,
        started_at=started,
    )


@router.post("/heartbeat", response_model=CallHeartbeatOut)
async def post_call_heartbeat(
    db: Db,
    user: CurrentUser,
    body: CallHeartbeatIn = CallHeartbeatIn(),
):
    """Android YES CRM: periodic presence ping while sync is on / logged in."""
    _ = body  # optional device metadata reserved for future use
    last_seen = await _touch_call_app_presence(db, user)
    await db.commit()
    return CallHeartbeatOut(
        ok=True,
        last_seen_at=last_seen,
        online_threshold_seconds=int(ONLINE_THRESHOLD.total_seconds()),
    )


@router.post("/events", response_model=CallLogUpsertResult)
async def post_call_event(body: CallLogEventIn, db: Db, user: CurrentUser):
    """Android: push one call event immediately (idempotent upsert)."""
    result = await _upsert_event(db, user, body)
    await _touch_call_app_presence(db, user)
    await db.commit()
    return result


@router.post("/events/batch", response_model=CallLogBatchOut)
async def post_call_events_batch(body: CallLogBatchIn, db: Db, user: CurrentUser):
    """Android: retry backlog when offline (idempotent)."""
    if not body.events:
        await _touch_call_app_presence(db, user)
        await db.commit()
        return CallLogBatchOut(accepted=0, results=[])
    results: list[CallLogUpsertResult] = []
    for ev in body.events:
        results.append(await _upsert_event(db, user, ev))
    await _touch_call_app_presence(db, user)
    await db.commit()
    return CallLogBatchOut(accepted=len(results), results=results)


DurationBucket = Literal["all", "zero", "under_1m", "1_5m", "over_5m"]


def _shared_filters(
    *,
    agent_user_id: int | None,
    q: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    device: str | None,
    duration: str | None,
    include_call_type: str | None = None,
) -> list[Any]:
    filters: list[Any] = []
    if include_call_type:
        ct = include_call_type.strip().lower()
        if ct not in CALL_TYPES:
            raise HTTPException(400, f"Invalid call_type: {include_call_type}")
        filters.append(CallLog.call_type == ct)
    if agent_user_id:
        filters.append(CallLog.agent_user_id == int(agent_user_id))
    if date_from:
        df = date_from if date_from.tzinfo else date_from.replace(tzinfo=timezone.utc)
        filters.append(CallLog.started_at >= df.astimezone(timezone.utc))
    if date_to:
        dt = date_to if date_to.tzinfo else date_to.replace(tzinfo=timezone.utc)
        filters.append(CallLog.started_at <= dt.astimezone(timezone.utc))
    if q and q.strip():
        term = f"%{q.strip()}%"
        filters.append(
            or_(
                CallLog.phone_number.ilike(term),
                CallLog.phone_normalized.ilike(term),
                CallLog.contact_name.ilike(term),
            )
        )
    if device and device.strip() and device.strip().lower() != "all":
        filters.append(CallLog.device_label.ilike(device.strip()))
    dur = (duration or "all").strip().lower()
    if dur == "zero":
        filters.append(CallLog.duration_seconds <= 0)
    elif dur == "under_1m":
        filters.append(and_(CallLog.duration_seconds > 0, CallLog.duration_seconds < 60))
    elif dur == "1_5m":
        filters.append(and_(CallLog.duration_seconds >= 60, CallLog.duration_seconds <= 300))
    elif dur == "over_5m":
        filters.append(CallLog.duration_seconds > 300)
    elif dur not in ("", "all"):
        raise HTTPException(400, f"Invalid duration filter: {duration}")
    return filters


@router.get("", response_model=CallLogPageOut)
async def list_call_logs(
    db: Db,
    user: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    call_type: str | None = Query(None, description="missed|received|rejected|outbound"),
    agent_user_id: int | None = None,
    q: str | None = Query(None, description="Search phone or contact name"),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    device: str | None = Query(None, description="Exact-ish device_label match"),
    duration: str | None = Query(None, description="all|zero|under_1m|1_5m|over_5m"),
):
    """CRM Call List — all synced calls with filters."""
    filters = _shared_filters(
        agent_user_id=agent_user_id,
        q=q,
        date_from=date_from,
        date_to=date_to,
        device=device,
        duration=duration,
        include_call_type=call_type,
    )
    where = and_(*filters) if filters else True

    # Type tile counts ignore call_type filter so users can see all buckets.
    count_base = _shared_filters(
        agent_user_id=agent_user_id,
        q=q,
        date_from=date_from,
        date_to=date_to,
        device=device,
        duration=duration,
        include_call_type=None,
    )
    count_where = and_(*count_base) if count_base else True

    total = (
        await db.execute(select(func.count()).select_from(CallLog).where(where))
    ).scalar() or 0

    count_rows = (
        await db.execute(
            select(CallLog.call_type, func.count())
            .where(count_where)
            .group_by(CallLog.call_type)
        )
    ).all()
    counts = {t: 0 for t in ("missed", "received", "rejected", "outbound")}
    for ct, n in count_rows:
        key = str(ct or "")
        if key in counts:
            counts[key] = int(n)
    counts["all"] = sum(counts[t] for t in ("missed", "received", "rejected", "outbound"))

    offset = (page - 1) * page_size
    rows = (
        await db.execute(
            select(CallLog, User.name, User.email, User.call_app_last_seen_at)
            .join(User, User.id == CallLog.agent_user_id)
            .where(where)
            .order_by(CallLog.started_at.desc())
            .offset(offset)
            .limit(page_size)
        )
    ).all()

    now = datetime.now(timezone.utc)
    items: list[CallLogOut] = []
    for log, agent_name, agent_email, agent_last_seen in rows:
        out = CallLogOut.model_validate(log)
        out.agent_name = agent_name or ""
        out.agent_email = agent_email or ""
        out.agent_online = _is_online(agent_last_seen, now=now)
        items.append(out)

    return CallLogPageOut(
        items=items,
        total=int(total),
        page=page,
        page_size=page_size,
        counts=counts,
    )


@router.get("/insights", response_model=CallLogInsightsOut)
async def call_log_insights(
    db: Db,
    user: CurrentUser,
    agent_user_id: int | None = None,
    q: str | None = Query(None),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    device: str | None = None,
    duration: str | None = None,
):
    """Sidebar aggregates: talk time, top agents, missed/rejected follow-ups."""
    base = _shared_filters(
        agent_user_id=agent_user_id,
        q=q,
        date_from=date_from,
        date_to=date_to,
        device=device,
        duration=duration,
    )
    where = and_(*base) if base else True

    total_calls = (
        await db.execute(select(func.count()).select_from(CallLog).where(where))
    ).scalar() or 0

    talk_filters = list(base) + [CallLog.duration_seconds > 0]
    talk_where = and_(*talk_filters)
    talk_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(CallLog.duration_seconds), 0),
                func.coalesce(func.avg(CallLog.duration_seconds), 0),
            ).where(talk_where)
        )
    ).one()
    total_talk = int(talk_row[0] or 0)
    avg_dur = float(talk_row[1] or 0)

    agent_rows = (
        await db.execute(
            select(
                User.id,
                User.name,
                User.email,
                User.call_app_last_seen_at,
                func.count(CallLog.id),
            )
            .join(CallLog, CallLog.agent_user_id == User.id)
            .where(where)
            .group_by(User.id, User.name, User.email, User.call_app_last_seen_at)
            .order_by(func.count(CallLog.id).desc())
            .limit(8)
        )
    ).all()
    now = datetime.now(timezone.utc)
    top_agents = [
        CallInsightAgent(
            id=int(i),
            name=n or e or f"User {i}",
            email=e or "",
            call_count=int(c),
            online=_is_online(last_seen, now=now),
            last_seen_at=last_seen,
        )
        for i, n, e, last_seen, c in agent_rows
    ]

    follow_filters = list(base) + [CallLog.call_type.in_(("missed", "rejected"))]
    follow_where = and_(*follow_filters)
    follow_up_total = (
        await db.execute(select(func.count()).select_from(CallLog).where(follow_where))
    ).scalar() or 0
    follow_rows = (
        await db.execute(
            select(CallLog, User.name)
            .join(User, User.id == CallLog.agent_user_id)
            .where(follow_where)
            .order_by(CallLog.started_at.desc())
            .limit(5)
        )
    ).all()
    follow_ups = [
        CallFollowUpItem(
            id=int(log.id),
            contact_name=log.contact_name or "",
            phone_number=log.phone_number or log.phone_normalized or "",
            call_type=log.call_type or "",
            started_at=log.started_at,
            agent_name=agent_name or "",
        )
        for log, agent_name in follow_rows
    ]

    device_rows = (
        await db.execute(
            select(CallLog.device_label)
            .where(and_(where, CallLog.device_label != ""))
            .distinct()
            .order_by(CallLog.device_label.asc())
            .limit(40)
        )
    ).scalars().all()
    devices = [str(d) for d in device_rows if d]

    return CallLogInsightsOut(
        total_calls=int(total_calls),
        total_talk_seconds=total_talk,
        avg_duration_seconds=avg_dur,
        top_agents=top_agents,
        follow_ups=follow_ups,
        follow_up_total=int(follow_up_total),
        devices=devices,
        online_threshold_seconds=int(ONLINE_THRESHOLD.total_seconds()),
    )


@router.get("/agents")
async def list_call_agents(db: Db, user: CurrentUser):
    """Agents who have synced at least one call (for Call List filter)."""
    rows = (
        await db.execute(
            select(
                User.id,
                User.name,
                User.email,
                User.call_app_last_seen_at,
                func.count(CallLog.id),
            )
            .join(CallLog, CallLog.agent_user_id == User.id)
            .group_by(User.id, User.name, User.email, User.call_app_last_seen_at)
            .order_by(User.name.asc())
        )
    ).all()
    now = datetime.now(timezone.utc)
    return [
        {
            "id": int(i),
            "name": n or "",
            "email": e or "",
            "call_count": int(c),
            "online": _is_online(last_seen, now=now),
            "last_seen_at": last_seen.isoformat() if last_seen else None,
        }
        for i, n, e, last_seen, c in rows
    ]
