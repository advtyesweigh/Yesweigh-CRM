"""Load staff directory and decide which DMs belong on the Enquiry tab.

Page loads use rules plus a cached verdict. Gemini runs only from the worker.
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.ai.whatsapp_send_guards import load_org_staff_phone_keys
from app.intel.order_cycle import so_pdf_exists_clause
from app.intel.enquiry import (
    EnquiryVerdict,
    apply_model_verdict,
    assess_enquiry,
    assess_enquiry_outline,
    cache_from_json,
    cheap_exclude,
    enquiry_fingerprint,
    patched_outline,
    should_rewrite_outline,
    staff_first_names,
)
from app.models import AiThreadInsight, Employee, WhatsAppConversation, WhatsAppMessage
from app.whatsapp_chat_types import dm_inbox_filter, newsletter_list_filter
from app.intel.row_sort import sort_public_ids
from app.whatsapp_service_meta import parse_cached_outline_summary, parse_outline_lite

_CANDIDATE_LIMIT = 500
_PER_THREAD = 36
# Backfill/worker may sync-score message windows. HTTP uses cache + outline fallback.
_MAX_SYNC_PENDING = 40
# Message-scored excludes we trust on HTTP. ``other`` / stale negatives fall back to outline rules.
_TRUSTED_EXCLUDE_LANES = frozenset(
    {"auto_reply", "service", "stamping", "internal", "cloud", "closed", "answered", "other", "order_cycle"}
)
_STAFF_CACHE_TTL_S = 45.0
_staff_cache: tuple[float, set[str], set[str]] | None = None


async def staff_directory(db: AsyncSession) -> tuple[set[str], set[str]]:
    global _staff_cache
    import time as _time

    now = _time.monotonic()
    if _staff_cache is not None and now - _staff_cache[0] < _STAFF_CACHE_TTL_S:
        return _staff_cache[1], _staff_cache[2]
    phones = await load_org_staff_phone_keys(db)
    names = (
        await db.execute(
            select(Employee.name).where(
                or_(Employee.status == "Active", Employee.status == "", Employee.status.is_(None))
            )
        )
    ).scalars().all()
    first = staff_first_names(list(names))
    _staff_cache = (now, phones, first)
    return phones, first


def _message_dicts(rows: list) -> list[dict[str, str]]:
    from app.whatsapp_outline import message_text_for_outline

    out: list[dict[str, str]] = []
    for row in rows:
        if isinstance(row, dict):
            out.append({"direction": row.get("direction") or "", "text": row.get("text") or ""})
            continue
        text = message_text_for_outline(
            row
            if hasattr(row, "body")
            else SimpleNamespace(
                body=getattr(row, "body", "") or "",
                transcript=getattr(row, "transcript", "") or "",
                media_type=getattr(row, "media_type", "") or "",
                is_voice=False,
            )
        )
        media_name = (getattr(row, "media_name", None) or "").strip()
        if media_name and media_name.lower() not in (text or "").lower():
            text = f"{text} {media_name}".strip()
        out.append({"direction": getattr(row, "direction", "") or "", "text": text or ""})
    return out


async def message_windows(db: AsyncSession, conv_ids: list[int]) -> dict[int, list[dict[str, str]]]:
    if not conv_ids:
        return {}
    rn = func.row_number().over(
        partition_by=WhatsAppMessage.conversation_id,
        order_by=WhatsAppMessage.id.desc(),
    ).label("rn")
    ranked = (
        select(
            WhatsAppMessage.conversation_id.label("conversation_id"),
            WhatsAppMessage.direction.label("direction"),
            WhatsAppMessage.body.label("body"),
            WhatsAppMessage.transcript.label("transcript"),
            WhatsAppMessage.media_type.label("media_type"),
            WhatsAppMessage.media_name.label("media_name"),
            WhatsAppMessage.id.label("id"),
            rn,
        )
        .where(WhatsAppMessage.conversation_id.in_(conv_ids))
        .subquery()
    )
    fetched = (
        await db.execute(
            select(
                ranked.c.conversation_id,
                ranked.c.direction,
                ranked.c.body,
                ranked.c.transcript,
                ranked.c.media_type,
                ranked.c.media_name,
                ranked.c.id,
            )
            .where(ranked.c.rn <= _PER_THREAD)
            .order_by(ranked.c.conversation_id.asc(), ranked.c.id.asc())
        )
    ).all()
    grouped: dict[int, list] = {}
    for cid, direction, body, transcript, media_type, media_name, mid in fetched:
        grouped.setdefault(int(cid), []).append(
            SimpleNamespace(
                direction=direction or "",
                body=body or "",
                transcript=transcript or "",
                media_type=media_type or "",
                media_name=media_name or "",
                id=mid,
            )
        )
    return {cid: _message_dicts(items) for cid, items in grouped.items()}


def _fp(conv: WhatsAppConversation) -> str:
    moment = conv.last_message_at
    if isinstance(moment, datetime):
        stamp = moment.isoformat()
    else:
        stamp = str(moment or "")
    return enquiry_fingerprint(
        outline_fp=(conv.ai_outline_fingerprint or "")[:80],
        last_message_at=stamp,
        awaiting=bool(conv.awaiting_staff_reply),
        service_category=conv.service_category or "",
    )


async def verdict_for(
    db: AsyncSession,
    conv: WhatsAppConversation,
    messages: list[dict[str, str]],
    *,
    phones: set[str] | None = None,
    first: set[str] | None = None,
    allow_gemini: bool = False,
) -> EnquiryVerdict:
    if phones is None or first is None:
        phones, first = await staff_directory(db)
    outline = parse_cached_outline_summary(conv.ai_outline or "")
    verdict = assess_enquiry(
        service_category=conv.service_category or "",
        outline=outline,
        messages=messages,
        customer_name=conv.customer_name or "",
        customer_phone=conv.customer_phone or "",
        staff_phones=phones,
        staff_first=first,
        awaiting_staff_reply=bool(conv.awaiting_staff_reply),
    )
    if allow_gemini and verdict.needs_gemini:
        from app.intel.enquiry_gemini import refine_unanswered

        refined = await refine_unanswered(db, messages, conversation_id=conv.id)
        verdict = apply_model_verdict(verdict, refined)
    return verdict


def store_cache(insight: AiThreadInsight, conv: WhatsAppConversation, verdict: EnquiryVerdict) -> None:
    insight.enquiry_cache = verdict.to_cache(_fp(conv))


async def _insight_for(db: AsyncSession, conversation_id: int) -> AiThreadInsight:
    row = (
        await db.execute(select(AiThreadInsight).where(AiThreadInsight.conversation_id == conversation_id))
    ).scalar_one_or_none()
    if row is None:
        row = AiThreadInsight(conversation_id=conversation_id)
        db.add(row)
    return row


def rewrite_outline_if_needed(conv: WhatsAppConversation, verdict: EnquiryVerdict) -> bool:
    """Correct a sales-enquiry label. Does not touch awaiting_staff_reply."""
    summary = parse_cached_outline_summary(conv.ai_outline or "")
    if not should_rewrite_outline(summary, verdict):
        return False
    patched = patched_outline(summary, verdict)
    conv.ai_outline = json.dumps(patched, ensure_ascii=False)
    from app.whatsapp_service_meta import apply_service_meta_to_conversation

    apply_service_meta_to_conversation(conv, patched, fallback_text="")
    return True


def _enquiry_sql_candidates():
    """Likely open sales DMs — not the 150 most recent chats overall."""
    sales_status = or_(
        WhatsAppConversation.ai_outline.ilike("%New Enquiry%"),
        WhatsAppConversation.ai_outline.ilike("%Interested%"),
        WhatsAppConversation.ai_outline.ilike("%Quotation Required%"),
        WhatsAppConversation.ai_outline.ilike("%Awaiting Reply%"),
    )
    sales_awaiting = (WhatsAppConversation.awaiting_staff_reply.is_(True)) & (
        func.lower(func.coalesce(WhatsAppConversation.service_category, "")).in_(["sales", "support", ""])
    )
    return or_(sales_status, sales_awaiting)


async def select_sales_enquiries(db: AsyncSession, *, allow_sync: bool = False) -> tuple[list[str], dict[str, str]]:
    """Public ids that are still unreplied sales/price enquiries.

  HTTP uses enquiry_cache when warm, otherwise a fast outline fallback (no message load).
  Backfill/worker passes ``allow_sync=True`` to write caches from message rules.
    """
    from app.intel.cache import get_enquiry_cache, set_enquiry_cache

    if not allow_sync:
        warm = get_enquiry_cache()
        if warm is not None:
            return warm

    phones, first = await staff_directory(db)
    rows = (
        await db.execute(
            select(WhatsAppConversation)
            .options(
                load_only(
                    WhatsAppConversation.id,
                    WhatsAppConversation.public_id,
                    WhatsAppConversation.service_category,
                    WhatsAppConversation.ai_outline,
                    WhatsAppConversation.ai_outline_fingerprint,
                    WhatsAppConversation.customer_name,
                    WhatsAppConversation.customer_phone,
                    WhatsAppConversation.awaiting_staff_reply,
                    WhatsAppConversation.awaiting_since,
                    WhatsAppConversation.priority,
                    WhatsAppConversation.last_message_at,
                )
            )
            .where(
                WhatsAppConversation.organization_id == "yesweigh",
                newsletter_list_filter(),
                dm_inbox_filter(),
                ~WhatsAppConversation.customer_jid.ilike("%@broadcast"),
                ~WhatsAppConversation.customer_jid.ilike("status@%"),
                WhatsAppConversation.status != "Resolved",
                ~so_pdf_exists_clause(),
                _enquiry_sql_candidates(),
            )
            .order_by(WhatsAppConversation.last_message_at.desc().nulls_last())
            .limit(_CANDIDATE_LIMIT)
        )
    ).scalars().all()
    caches: dict[int, str] = {}
    ids = [int(c.id) for c in rows]
    if ids:
        for cid, raw in (
            await db.execute(
                select(AiThreadInsight.conversation_id, AiThreadInsight.enquiry_cache).where(
                    AiThreadInsight.conversation_id.in_(ids)
                )
            )
        ).all():
            caches[int(cid)] = raw or ""

    included: list[str] = []
    summaries: dict[str, str] = {}
    pending: list[tuple[WhatsAppConversation, str]] = []
    for conv in rows:
        # Lite parse: full Pydantic normalize was ~2.5s for ~150 candidates on HTTP.
        outline = parse_outline_lite(conv.ai_outline or "")
        if cheap_exclude(
            service_category=conv.service_category or "",
            outline=outline,
            customer_name=conv.customer_name or "",
            customer_phone=conv.customer_phone or "",
            staff_phones=phones,
            staff_first=first,
        ):
            continue
        fp = _fp(conv)
        cached = cache_from_json(caches.get(int(conv.id)), fp)
        if cached is not None and cached.include:
            included.append(conv.public_id)
            summaries[conv.public_id] = cached.summary or "Sales enquiry"
            continue
        if cached is not None and cached.lane in _TRUSTED_EXCLUDE_LANES:
            continue
        verdict = assess_enquiry_outline(
            service_category=conv.service_category or "",
            outline=outline,
            customer_name=conv.customer_name or "",
            customer_phone=conv.customer_phone or "",
            staff_phones=phones,
            staff_first=first,
            awaiting_staff_reply=bool(conv.awaiting_staff_reply),
        )
        if verdict.include:
            included.append(conv.public_id)
            summaries[conv.public_id] = verdict.summary or "Sales enquiry"
        if cached is None or (cached is not None and not cached.include):
            pending.append((conv, fp))

    if not allow_sync or not pending:
        by_pub = {c.public_id: c for c in rows}
        ordered = sort_public_ids(list(dict.fromkeys(included)), by_pub)
        if not allow_sync:
            set_enquiry_cache(ordered, summaries)
        return ordered, summaries
    sync_batch = pending[:_MAX_SYNC_PENDING]
    windows = await message_windows(db, [int(c.id) for c, _ in sync_batch])
    for conv, _fingerprint in sync_batch:
        verdict = await verdict_for(
            db,
            conv,
            windows.get(int(conv.id), []),
            phones=phones,
            first=first,
            allow_gemini=False,
        )
        insight = await _insight_for(db, int(conv.id))
        store_cache(insight, conv, verdict)
        if verdict.include:
            included.append(conv.public_id)
            summaries[conv.public_id] = verdict.summary
    if sync_batch:
        await db.flush()
    by_pub = {c.public_id: c for c in rows}
    ordered = sort_public_ids(list(dict.fromkeys(included)), by_pub)
    return ordered, summaries
