"""WhatsApp intelligence helpers for AI Agent tools (search / threads / enquiries).

Read-only queries over whatsapp_messages.body + transcript and conversations.
ACL is enforced by the caller via authorized_instance_ids (same as other AI WA tools).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WhatsAppConversation, WhatsAppInstance, WhatsAppMessage
from app.phone_util import normalize_in_mobile

ORG = "yesweigh"
_DEFAULT_DAYS = 45
_MAX_DAYS = 180
_SEARCH_LIMIT = 50
_THREAD_LIMIT = 50
_MSG_LIMIT = 80
_SNIPPET = 220

_TOKEN_SPLIT = re.compile(r"[\s,;|/]+")


def parse_keywords(raw: str | list[str] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            parts.extend(_TOKEN_SPLIT.split(str(item or "")))
        tokens = parts
    else:
        tokens = _TOKEN_SPLIT.split(str(raw))
    out: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        s = t.strip()
        if len(s) < 2:
            continue
        key = s.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= 12:
            break
    return out


def resolve_date_window(args: dict[str, Any]) -> tuple[datetime, datetime | None, int]:
    """Return (since_utc, until_utc|None, days_used). Default ~45 days."""
    now = datetime.now(timezone.utc)
    until: datetime | None = None
    date_to = str(args.get("date_to") or "").strip()
    date_from = str(args.get("date_from") or "").strip()
    days_raw = args.get("days")

    if date_to:
        until = _parse_day_bound(date_to, end=True) or until
    if date_from:
        since = _parse_day_bound(date_from, end=False)
        if since:
            return since, until, max(1, (now - since).days)

    try:
        days = int(days_raw) if days_raw is not None and str(days_raw).strip() != "" else _DEFAULT_DAYS
    except (TypeError, ValueError):
        days = _DEFAULT_DAYS
    days = max(1, min(_MAX_DAYS, days))
    since = now - timedelta(days=days)
    return since, until, days


def _parse_day_bound(value: str, *, end: bool) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if end:
                return dt.replace(hour=23, minute=59, second=59, microsecond=999999)
            return dt.replace(hour=0, minute=0, second=0, microsecond=0)
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _snippet(text: str, keywords: list[str]) -> str:
    body = (text or "").strip().replace("\n", " ")
    if not body:
        return ""
    lower = body.casefold()
    idx = -1
    for kw in keywords:
        pos = lower.find(kw.casefold())
        if pos >= 0 and (idx < 0 or pos < idx):
            idx = pos
    if idx < 0:
        return body[:_SNIPPET] + ("…" if len(body) > _SNIPPET else "")
    start = max(0, idx - 40)
    end = min(len(body), idx + _SNIPPET - 40)
    out = body[start:end]
    if start > 0:
        out = "…" + out
    if end < len(body):
        out = out + "…"
    return out


def _message_text(msg: WhatsAppMessage) -> str:
    body = (msg.body or "").strip()
    transcript = (msg.transcript or "").strip()
    if body and transcript and transcript not in body:
        return f"{body}\n{transcript}"
    return body or transcript


async def resolve_instance_ids(
    db: AsyncSession,
    *,
    allowed: list[int] | None,
    instance_query: str = "",
) -> tuple[list[int] | None, str | None]:
    """Filter authorized instances by public_id / name / employee / phone.

    Returns (ids_or_None_for_all_allowed, error).
    When instance_query empty: (allowed, None) — None means admin-all.
    """
    q = (instance_query or "").strip()
    base = select(WhatsAppInstance).where(
        WhatsAppInstance.organization_id == ORG,
        WhatsAppInstance.deleted_at.is_(None),
    )
    if allowed is not None:
        if not allowed:
            return [], "No WhatsApp instances assigned"
        base = base.where(WhatsAppInstance.id.in_(allowed))

    if not q:
        return allowed, None

    rows = list((await db.execute(base)).scalars().all())
    phone = normalize_in_mobile(q)
    q_l = q.casefold()
    matches: list[WhatsAppInstance] = []
    for inst in rows:
        if inst.public_id == q:
            matches = [inst]
            break
        names = [
            (inst.display_name or ""),
            (inst.employee_name or ""),
            (inst.employee_code or ""),
        ]
        if any(q_l in n.casefold() for n in names if n):
            matches.append(inst)
            continue
        if phone and (
            normalize_in_mobile(inst.connected_phone_number) == phone
            or normalize_in_mobile(inst.expected_phone_number) == phone
        ):
            matches.append(inst)

    if not matches:
        return [], f"No authorized instance matches {q!r}"
    return [m.id for m in matches], None


async def instance_label_map(db: AsyncSession, instance_ids: set[int]) -> dict[int, dict[str, str]]:
    if not instance_ids:
        return {}
    rows = (
        await db.execute(select(WhatsAppInstance).where(WhatsAppInstance.id.in_(instance_ids)))
    ).scalars().all()
    return {
        i.id: {
            "instance_public_id": i.public_id,
            "agent_name": i.display_name or i.employee_name or "",
            "employee_name": i.employee_name or "",
            "employee_code": i.employee_code or "",
            "connected_phone": i.connected_phone_number or "",
        }
        for i in rows
    }


async def search_messages(
    db: AsyncSession,
    *,
    allowed: list[int] | None,
    keywords: list[str],
    match_any: bool = False,
    days: int | None = None,
    date_from: str = "",
    date_to: str = "",
    direction: str = "",
    contact: str = "",
    instance_query: str = "",
    unread_only: bool = False,
    open_only: bool = False,
    limit: int = 25,
    dedupe_by_phone: bool = False,
) -> dict[str, Any]:
    if not keywords:
        return {
            "ok": False,
            "error": "Provide query or keywords (English and/or Malayalam).",
            "items": [],
        }

    args = {"days": days, "date_from": date_from, "date_to": date_to}
    since, until, days_used = resolve_date_window(args)
    limit = max(1, min(_SEARCH_LIMIT, int(limit or 25)))

    inst_ids, err = await resolve_instance_ids(db, allowed=allowed, instance_query=instance_query)
    if err and inst_ids == []:
        return {"ok": True, "total": 0, "items": [], "note": err, "days": days_used, "keywords": keywords}
    if allowed is not None and allowed == []:
        return {
            "ok": True,
            "total": 0,
            "items": [],
            "note": "No WhatsApp instances assigned",
            "days": days_used,
            "keywords": keywords,
        }

    text_preds = []
    for kw in keywords:
        like = f"%{kw}%"
        text_preds.append(
            or_(
                WhatsAppMessage.body.ilike(like),
                WhatsAppMessage.transcript.ilike(like),
            )
        )
    text_clause = or_(*text_preds) if match_any else and_(*text_preds)

    stmt = (
        select(WhatsAppMessage, WhatsAppConversation)
        .join(WhatsAppConversation, WhatsAppConversation.id == WhatsAppMessage.conversation_id)
        .where(
            WhatsAppMessage.organization_id == ORG,
            WhatsAppConversation.organization_id == ORG,
            WhatsAppMessage.created_at >= since,
            text_clause,
        )
        .order_by(WhatsAppMessage.created_at.desc())
    )
    if until is not None:
        stmt = stmt.where(WhatsAppMessage.created_at <= until)
    if inst_ids is not None:
        stmt = stmt.where(WhatsAppMessage.whatsapp_instance_id.in_(inst_ids))

    dir_l = (direction or "").strip().lower()
    if dir_l in {"in", "inbound", "incoming"}:
        stmt = stmt.where(WhatsAppMessage.direction == "in")
    elif dir_l in {"out", "outbound", "outgoing"}:
        stmt = stmt.where(WhatsAppMessage.direction == "out")

    contact_q = (contact or "").strip()
    if contact_q:
        phone = normalize_in_mobile(contact_q)
        like = f"%{contact_q}%"
        contact_preds = [
            WhatsAppConversation.customer_name.ilike(like),
            WhatsAppConversation.customer_phone.ilike(like),
        ]
        if phone:
            contact_preds.append(WhatsAppConversation.customer_phone.ilike(f"%{phone}%"))
        stmt = stmt.where(or_(*contact_preds))

    if unread_only:
        stmt = stmt.where(WhatsAppConversation.unread_count > 0)
    if open_only:
        stmt = stmt.where(WhatsAppConversation.status != "Resolved")

    # Over-fetch when deduping by phone so page still fills.
    fetch_n = min(_SEARCH_LIMIT * 3, limit * 4) if dedupe_by_phone else limit
    rows = list((await db.execute(stmt.limit(fetch_n))).all())

    inst_map = await instance_label_map(db, {m.whatsapp_instance_id for m, _ in rows})
    items: list[dict[str, Any]] = []
    seen_phones: set[str] = set()
    for msg, conv in rows:
        phone_key = normalize_in_mobile(conv.customer_phone) or (conv.customer_phone or "")
        if dedupe_by_phone and phone_key:
            if phone_key in seen_phones:
                continue
            seen_phones.add(phone_key)
        text = _message_text(msg)
        label = inst_map.get(msg.whatsapp_instance_id) or {}
        items.append(
            {
                "contact_name": conv.customer_name or "",
                "contact_phone": conv.customer_phone or "",
                "agent_name": label.get("agent_name") or "",
                "instance_public_id": label.get("instance_public_id") or "",
                "direction": msg.direction,
                "message_at": msg.created_at.isoformat() if msg.created_at else None,
                "snippet": _snippet(text, keywords),
                "thread_id": conv.public_id,
                "message_id": msg.public_id,
                "status": conv.status or "",
                "unread_count": conv.unread_count or 0,
                "has_transcript": bool((msg.transcript or "").strip()),
            }
        )
        if len(items) >= limit:
            break

    return {
        "ok": True,
        "total": len(items),
        "items": items,
        "keywords": keywords,
        "match_mode": "any" if match_any else "all",
        "days": days_used,
        "date_from": since.date().isoformat(),
        "date_to": (until.date().isoformat() if until else None),
        "direction": dir_l or "any",
        "deduped_by_phone": dedupe_by_phone,
        "note": (
            "Searched message body + voice transcripts (EN/ML). "
            "Summarize in Markdown for the user; do not invent contacts."
        ),
    }


async def list_threads(
    db: AsyncSession,
    *,
    allowed: list[int] | None,
    query: str = "",
    instance_query: str = "",
    unread_only: bool = False,
    open_only: bool = False,
    status: str = "",
    limit: int = 25,
) -> dict[str, Any]:
    limit = max(1, min(_THREAD_LIMIT, int(limit or 25)))
    if allowed is not None and allowed == []:
        return {"ok": True, "total": 0, "items": [], "note": "No WhatsApp instances assigned"}

    inst_ids, err = await resolve_instance_ids(db, allowed=allowed, instance_query=instance_query)
    if err and inst_ids == []:
        return {"ok": True, "total": 0, "items": [], "note": err}

    stmt = select(WhatsAppConversation).where(WhatsAppConversation.organization_id == ORG)
    if inst_ids is not None:
        stmt = stmt.where(WhatsAppConversation.whatsapp_instance_id.in_(inst_ids))

    q = (query or "").strip()
    if q:
        like = f"%{q}%"
        phone = normalize_in_mobile(q)
        preds = [
            WhatsAppConversation.customer_name.ilike(like),
            WhatsAppConversation.customer_phone.ilike(like),
            WhatsAppConversation.last_message_preview.ilike(like),
            WhatsAppConversation.tag.ilike(like),
        ]
        if phone:
            preds.append(WhatsAppConversation.customer_phone.ilike(f"%{phone}%"))
        stmt = stmt.where(or_(*preds))

    if unread_only:
        stmt = stmt.where(WhatsAppConversation.unread_count > 0)
    if open_only:
        stmt = stmt.where(WhatsAppConversation.status != "Resolved")
    status_q = (status or "").strip()
    if status_q:
        stmt = stmt.where(WhatsAppConversation.status.ilike(status_q))

    stmt = stmt.order_by(WhatsAppConversation.last_message_at.desc().nullslast()).limit(limit)
    rows = list((await db.execute(stmt)).scalars().all())
    inst_map = await instance_label_map(db, {c.whatsapp_instance_id for c in rows})

    items = []
    for c in rows:
        label = inst_map.get(c.whatsapp_instance_id) or {}
        items.append(
            {
                "thread_id": c.public_id,
                "contact_name": c.customer_name or "",
                "contact_phone": c.customer_phone or "",
                "agent_name": label.get("agent_name") or "",
                "instance_public_id": label.get("instance_public_id") or "",
                "status": c.status or "",
                "unread_count": c.unread_count or 0,
                "last_message_preview": (c.last_message_preview or "")[:160],
                "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
                "tag": c.tag or "",
            }
        )
    return {
        "ok": True,
        "total": len(items),
        "items": items,
        "note": "Use get_whatsapp_thread with thread_id to read message history.",
    }


async def get_thread(
    db: AsyncSession,
    *,
    allowed: list[int] | None,
    thread_id: str,
    limit: int = 40,
) -> dict[str, Any]:
    tid = (thread_id or "").strip()
    if not tid:
        return {"ok": False, "error": "thread_id is required", "items": []}

    conv = (
        await db.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.public_id == tid,
                WhatsAppConversation.organization_id == ORG,
            )
        )
    ).scalar_one_or_none()
    if not conv:
        return {"ok": False, "error": "Conversation not found", "items": []}
    if allowed is not None and conv.whatsapp_instance_id not in allowed:
        return {"ok": False, "error": "Not authorized for this conversation", "items": []}

    limit = max(1, min(_MSG_LIMIT, int(limit or 40)))
    msgs = list(
        (
            await db.execute(
                select(WhatsAppMessage)
                .where(WhatsAppMessage.conversation_id == conv.id)
                .order_by(WhatsAppMessage.created_at.desc(), WhatsAppMessage.id.desc())
                .limit(limit)
            )
        ).scalars().all()
    )
    msgs = list(reversed(msgs))
    inst_map = await instance_label_map(db, {conv.whatsapp_instance_id})
    label = inst_map.get(conv.whatsapp_instance_id) or {}

    items = []
    for m in msgs:
        text = _message_text(m)
        items.append(
            {
                "message_id": m.public_id,
                "direction": m.direction,
                "body": (m.body or "")[:800],
                "transcript": (m.transcript or "")[:800],
                "text": text[:1000],
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "media_type": m.media_type or "",
                "status": m.status or "",
            }
        )

    return {
        "ok": True,
        "thread": {
            "thread_id": conv.public_id,
            "contact_name": conv.customer_name or "",
            "contact_phone": conv.customer_phone or "",
            "agent_name": label.get("agent_name") or "",
            "instance_public_id": label.get("instance_public_id") or "",
            "status": conv.status or "",
            "unread_count": conv.unread_count or 0,
            "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
        },
        "message_count": len(items),
        "items": items,
        "note": "Oldest→newest in this page. Summarize for the user; quote sparingly.",
    }


async def report_enquiries(
    db: AsyncSession,
    *,
    allowed: list[int] | None,
    topic: str = "",
    keywords: list[str] | None = None,
    days: int | None = None,
    date_from: str = "",
    date_to: str = "",
    instance_query: str = "",
    inbound_only: bool = True,
    limit: int = 25,
) -> dict[str, Any]:
    """Thin wrapper: search + prefer inbound + dedupe by phone for ‘who asked about X?’."""
    kws = list(keywords or [])
    if topic and not kws:
        kws = parse_keywords(topic)
    elif topic:
        # Merge topic tokens with explicit keywords
        for t in parse_keywords(topic):
            if t.casefold() not in {k.casefold() for k in kws}:
                kws.append(t)
    if not kws:
        return {
            "ok": False,
            "error": "Provide topic and/or keywords (e.g. container, stock, warranty, OV).",
            "items": [],
        }

    result = await search_messages(
        db,
        allowed=allowed,
        keywords=kws,
        match_any=True,
        days=days,
        date_from=date_from,
        date_to=date_to,
        direction="in" if inbound_only else "",
        instance_query=instance_query,
        open_only=False,
        limit=limit,
        dedupe_by_phone=True,
    )
    if not result.get("ok"):
        return result

    # If inbound-only is sparse, widen to any direction but still dedupe (staff often name the topic).
    if inbound_only and result.get("total", 0) < 3:
        widened = await search_messages(
            db,
            allowed=allowed,
            keywords=kws,
            match_any=True,
            days=days,
            date_from=date_from,
            date_to=date_to,
            direction="",
            instance_query=instance_query,
            limit=limit,
            dedupe_by_phone=True,
        )
        if widened.get("ok") and (widened.get("total") or 0) > (result.get("total") or 0):
            result = widened
            result["note"] = (
                "Few inbound keyword hits; included threads where staff also discussed the topic. "
                "Deduped by phone. Summarize as a Markdown table for the user."
            )
        else:
            result["note"] = (
                "Inbound enquiries matching topic keywords, deduped by phone. "
                "Present as a Markdown table (contact, phone, agent, date, snippet)."
            )
    else:
        result["note"] = (
            "Enquiries matching topic keywords, deduped by phone. "
            "Present as a Markdown table (contact, phone, agent, date, snippet)."
        )

    result["topic"] = topic or ", ".join(kws)
    result["report_type"] = "enquiries"
    return result
