"""Track per-staff WhatsApp reply latency (awaiting_since → staff outbound)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.whatsapp_chat_types import is_group_jid
from app.whatsapp_office_hours import IST, split_office_off_seconds
from app.whatsapp_service_meta import format_hold_clock

from app.models import WhatsAppConversation, WhatsAppInstance, WhatsAppMessage, WhatsAppReplyEvent

ORG = "yesweigh"

STAFF_SHORT = {
    "biju": "Biju",
    "namrata": "Namrata",
    "shibin": "Shibin",
    "visakh": "Visakh",
    "care": "Care",
}


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def short_staff_name(raw: str | None) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    low = s.lower()
    for key, label in STAFF_SHORT.items():
        if key in low:
            return label
    return s.split()[0] if s else s


def format_reply_duration(seconds: int | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    _, label = format_hold_clock(_utc(datetime.now(timezone.utc)) - timedelta(seconds=seconds))
    return label


async def resolve_staff_for_reply(
    db: AsyncSession,
    conv: WhatsAppConversation,
    *,
    inst: WhatsAppInstance | None = None,
    msg: WhatsAppMessage | None = None,
) -> tuple[str, int | None, int | None, int]:
    """Return staff_name, staff_user_id, staff_employee_id, whatsapp_instance_id."""
    iid = int(conv.whatsapp_instance_id)
    inst = inst or await db.get(WhatsAppInstance, iid)
    name = ""
    if inst:
        name = short_staff_name(inst.display_name or inst.employee_name or inst.evolution_instance_name)
    staff_user_id: int | None = None
    staff_employee_id: int | None = None
    if msg:
        staff_user_id = msg.sender_user_id
        staff_employee_id = msg.sender_employee_id
    if staff_employee_id is None:
        staff_employee_id = conv.assigned_employee_id
    if staff_user_id is None:
        staff_user_id = conv.assigned_user_id
    if not name and staff_employee_id:
        from app.models import Employee

        emp = await db.get(Employee, staff_employee_id)
        if emp:
            name = short_staff_name(emp.name)
    return name or (inst.display_name if inst else ""), staff_user_id, staff_employee_id, iid


def sync_awaiting_since(
    conv: WhatsAppConversation,
    *,
    needs_reply: bool,
    at: datetime | None = None,
) -> None:
    if is_group_jid(conv.customer_jid or ""):
        conv.awaiting_staff_reply = False
        conv.awaiting_since = None
        return
    if not needs_reply:
        conv.awaiting_since = None
        return
    if conv.awaiting_since is None:
        conv.awaiting_since = _utc(at or datetime.now(timezone.utc))


async def record_staff_reply_latency(
    db: AsyncSession,
    conv: WhatsAppConversation,
    *,
    awaiting_at: datetime,
    replied_at: datetime,
    source: str,
    inst: WhatsAppInstance | None = None,
    msg: WhatsAppMessage | None = None,
    skip_if_message_id_exists: bool = False,
) -> WhatsAppReplyEvent | None:
    if is_group_jid(conv.customer_jid or ""):
        return None
    if skip_if_message_id_exists and msg and msg.id:
        existing = (
            await db.execute(
                select(WhatsAppReplyEvent.id).where(WhatsAppReplyEvent.message_id == msg.id).limit(1)
            )
        ).scalar_one_or_none()
        if existing:
            return None
    awaiting_at = _utc(awaiting_at)
    replied_at = _utc(replied_at)
    total, office, off = split_office_off_seconds(awaiting_at, replied_at)
    staff_name, staff_user_id, staff_employee_id, iid = await resolve_staff_for_reply(
        db, conv, inst=inst, msg=msg
    )
    evt = WhatsAppReplyEvent(
        organization_id=ORG,
        conversation_id=conv.id,
        whatsapp_instance_id=iid,
        staff_user_id=staff_user_id,
        staff_employee_id=staff_employee_id,
        staff_name=staff_name,
        customer_jid=(conv.customer_jid or "")[:120],
        awaiting_at=awaiting_at,
        replied_at=replied_at,
        reply_seconds=total,
        office_seconds=office,
        off_seconds=off,
        source=(source or "crm")[:40],
        message_id=msg.id if msg else None,
    )
    db.add(evt)
    return evt


async def complete_staff_reply(
    db: AsyncSession,
    conv: WhatsAppConversation,
    *,
    replied_at: datetime | None = None,
    source: str,
    inst: WhatsAppInstance | None = None,
    msg: WhatsAppMessage | None = None,
) -> None:
    from app.whatsapp_reply_needed import mark_staff_replied

    awaiting_at = conv.awaiting_since if conv.awaiting_staff_reply and conv.awaiting_since else None
    mark_staff_replied(conv)
    if awaiting_at is None:
        return
    at = _utc(replied_at or (msg.created_at if msg and msg.created_at else datetime.now(timezone.utc)))
    await record_staff_reply_latency(
        db,
        conv,
        awaiting_at=awaiting_at,
        replied_at=at,
        source=source,
        inst=inst,
        msg=msg,
        skip_if_message_id_exists=True,
    )


def period_start(period: str, *, now: datetime | None = None) -> datetime | None:
    now = _utc(now or datetime.now(timezone.utc))
    p = (period or "7d").strip().lower()
    if p in {"all", ""}:
        return None
    if p == "today":
        now_ist = now.astimezone(IST)
        midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.astimezone(timezone.utc)
    if p == "7d":
        return now - timedelta(days=7)
    if p == "30d":
        return now - timedelta(days=30)
    return now - timedelta(days=7)


async def aggregate_reply_stats(
    db: AsyncSession,
    *,
    period: str = "7d",
    allowed_instance_ids: set[int] | None = None,
) -> dict:
    since = period_start(period)
    q = select(
        WhatsAppReplyEvent.whatsapp_instance_id,
        WhatsAppReplyEvent.staff_name,
        func.count().label("n"),
        func.avg(WhatsAppReplyEvent.reply_seconds).label("avg_secs"),
        func.avg(WhatsAppReplyEvent.office_seconds).label("avg_office"),
        func.avg(WhatsAppReplyEvent.off_seconds).label("avg_off"),
        func.sum(WhatsAppReplyEvent.office_seconds).label("sum_office"),
        func.sum(WhatsAppReplyEvent.off_seconds).label("sum_off"),
    ).where(WhatsAppReplyEvent.organization_id == ORG)
    if since is not None:
        q = q.where(WhatsAppReplyEvent.replied_at >= since)
    if allowed_instance_ids is not None:
        if not allowed_instance_ids:
            return {"period": period, "items": [], "generated_at": datetime.now(timezone.utc).isoformat()}
        q = q.where(WhatsAppReplyEvent.whatsapp_instance_id.in_(allowed_instance_ids))
    q = q.group_by(WhatsAppReplyEvent.whatsapp_instance_id, WhatsAppReplyEvent.staff_name)
    rows = (await db.execute(q)).all()

    inst_names: dict[int, str] = {}
    if rows:
        ids = {int(r.whatsapp_instance_id) for r in rows}
        inst_rows = (
            await db.execute(select(WhatsAppInstance).where(WhatsAppInstance.id.in_(ids)))
        ).scalars().all()
        for inst in inst_rows:
            inst_names[inst.id] = short_staff_name(inst.display_name or inst.employee_name)

    items = []
    for r in rows:
        iid = int(r.whatsapp_instance_id)
        sn = (r.staff_name or inst_names.get(iid) or "").strip() or inst_names.get(iid, "")
        n = int(r.n or 0)
        avg = float(r.avg_secs or 0)
        avg_office = float(r.avg_office or 0)
        avg_off = float(r.avg_off or 0)
        items.append(
            {
                "whatsapp_instance_id": iid,
                "staff_name": sn,
                "count": n,
                "avg_seconds": int(round(avg)),
                "avg_label": format_reply_duration(int(round(avg))),
                "office_avg_seconds": int(round(avg_office)),
                "office_avg_label": format_reply_duration(int(round(avg_office))),
                "off_avg_seconds": int(round(avg_off)),
                "off_avg_label": format_reply_duration(int(round(avg_off))),
                "office_seconds_total": int(r.sum_office or 0),
                "off_seconds_total": int(r.sum_off or 0),
            }
        )
    items.sort(key=lambda x: (-(x["count"] or 0), x["staff_name"] or ""))
    return {
        "period": period,
        "timezone": "Asia/Kolkata",
        "office_rules": "Mon–Fri 09:30–17:30; Sat 09:30–17:00 IST",
        "items": items,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def backfill_office_seconds_on_events(db: AsyncSession, *, batch: int = 2000) -> int:
    """Recompute office/off split on all stored events (idempotent)."""
    updated = 0
    last_id = 0
    while True:
        rows = (
            (
                await db.execute(
                    select(WhatsAppReplyEvent)
                    .where(WhatsAppReplyEvent.organization_id == ORG, WhatsAppReplyEvent.id > last_id)
                    .order_by(WhatsAppReplyEvent.id)
                    .limit(batch)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            break
        changed = 0
        for evt in rows:
            last_id = evt.id
            total, office, off = split_office_off_seconds(evt.awaiting_at, evt.replied_at)
            if evt.reply_seconds != total or evt.office_seconds != office or evt.off_seconds != off:
                evt.reply_seconds = total
                evt.office_seconds = office
                evt.off_seconds = off
                changed += 1
        await db.commit()
        updated += changed
        if len(rows) < batch:
            break
    return updated