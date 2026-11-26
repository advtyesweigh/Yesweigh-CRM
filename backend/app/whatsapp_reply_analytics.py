"""WhatsApp staff reply & inbox performance analytics (metrics only — no chat logs)."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealers import _normalized_phone_sql
from app.models import Dealer, Lead, WaAiSuggestion, WhatsAppConversation, WhatsAppInstance, WhatsAppMessage, WhatsAppReplyEvent, ZohoDealer
from app.phone_util import normalize_in_mobile
from app.whatsapp_chat_meta import chat_kind_from_jid
from app.whatsapp_chat_types import dm_inbox_filter, newsletter_list_filter
from app.whatsapp_reply_latency import ORG, aggregate_reply_stats, format_reply_duration, period_start, short_staff_name

KB_INTEREST_RE = re.compile(
    r"\b("
    r"price|prices|rate|rates|cost|warranty|calibrat|software|manual|catalog|catalogue|"
    r"specification|specifications|capacity|how\s+to|km\s*\d|model|feature|installation|"
    r"repair|troubleshoot|working\s+hours|woking\s+hours|dealer\s+regist|gst|quotation|quote|"
    r"knowledge|faq|spec\s+sheet|datasheet|brochure"
    r")\b",
    re.I,
)
KB_INTENT_HINTS = frozenset(
    {
        "product_info",
        "catalogue",
        "warranty",
        "dealer_registration",
        "troubleshooting",
        "company_info",
        "working_hours",
        "software",
    }
)

KB_DETECTION_DOC = (
    "Lead KB interest: (1) wa_ai_suggestions with non-empty kb_article_ids or matched_knowledge_title "
    "on lead-tagged conversations; (2) inbound DMs in period matching KB_INTEREST_RE or AI intent in "
    f"{sorted(KB_INTENT_HINTS)} on lead conversations (deduped per conversation)."
)


def _cat_bucket(raw: str | None) -> str:
    c = (raw or "").strip().lower()
    if c in {"sales", "support"}:
        return "sales"
    if c == "service":
        return "service"
    if c == "cloud":
        return "cloud"
    return "other"


def _avg_block(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "avg_seconds": 0, "avg_label": None, "office_avg_seconds": 0, "office_avg_label": None, "off_avg_seconds": 0, "off_avg_label": None}
    n = len(values)
    avg = int(round(sum(values) / n))
    return {
        "count": n,
        "avg_seconds": avg,
        "avg_label": format_reply_duration(avg),
        "office_avg_seconds": 0,
        "office_avg_label": None,
        "off_avg_seconds": 0,
        "off_avg_label": None,
    }


def _avg_block_full(
    total: list[int],
    office: list[int],
    off: list[int],
) -> dict[str, Any]:
    if not total:
        return _avg_block([])
    n = len(total)
    avg = int(round(sum(total) / n))
    o_avg = int(round(sum(office) / n)) if office else 0
    f_avg = int(round(sum(off) / n)) if off else 0
    return {
        "count": n,
        "avg_seconds": avg,
        "avg_label": format_reply_duration(avg),
        "office_avg_seconds": o_avg,
        "office_avg_label": format_reply_duration(o_avg),
        "off_avg_seconds": f_avg,
        "off_avg_label": format_reply_duration(f_avg),
    }


async def _contact_kinds_map(
    db: AsyncSession,
    rows: list[WhatsAppConversation],
    *,
    dealer_map: dict[int, Dealer] | None = None,
) -> dict[int, str]:
    kinds: dict[int, str] = {}
    pending: dict[int, str] = {}
    dealer_map = dealer_map or {}

    for conv in rows:
        if conv.dealer_id and conv.dealer_id in dealer_map:
            kinds[conv.id] = "dealer"
            continue
        if conv.dealer_id:
            linked = await db.get(Dealer, conv.dealer_id)
            if linked:
                kinds[conv.id] = "dealer"
                continue
        if chat_kind_from_jid(conv.customer_jid or "") == "group":
            continue
        key = normalize_in_mobile(conv.customer_phone)
        if key:
            pending[conv.id] = key

    if not pending:
        return kinds

    keys = list({k for k in pending.values()})
    dealer_keys: set[str] = set()
    for (p10,) in (
        await db.execute(select(_normalized_phone_sql(Dealer.phone)).where(_normalized_phone_sql(Dealer.phone).in_(keys)))
    ).all():
        if p10:
            dealer_keys.add(str(p10))
    for phone_p10, mobile_p10 in (
        await db.execute(
            select(
                _normalized_phone_sql(ZohoDealer.phone),
                _normalized_phone_sql(ZohoDealer.mobile),
            ).where(
                ZohoDealer.dealer_id.is_not(None),
                or_(
                    _normalized_phone_sql(ZohoDealer.phone).in_(keys),
                    _normalized_phone_sql(ZohoDealer.mobile).in_(keys),
                ),
            )
        )
    ).all():
        for p10 in (phone_p10, mobile_p10):
            if p10 and str(p10) in keys:
                dealer_keys.add(str(p10))

    for cid, key in pending.items():
        if key in dealer_keys:
            kinds[cid] = "dealer"

    lead_lookup = {k for cid, k in pending.items() if cid not in kinds}
    if lead_lookup:
        lead_keys: set[str] = set()
        for (p10,) in (
            await db.execute(
                select(_normalized_phone_sql(Lead.phone)).where(_normalized_phone_sql(Lead.phone).in_(list(lead_lookup)))
            )
        ).all():
            if p10:
                lead_keys.add(str(p10))
        for cid, key in pending.items():
            if cid in kinds:
                continue
            if key in lead_keys:
                kinds[cid] = "lead"
    return kinds


async def aggregate_reply_analytics(
    db: AsyncSession,
    *,
    period: str = "7d",
    allowed_instance_ids: set[int] | None = None,
) -> dict[str, Any]:
    since = period_start(period)
    staff_block = await aggregate_reply_stats(db, period=period, allowed_instance_ids=allowed_instance_ids)

    if allowed_instance_ids is not None and not allowed_instance_ids:
        return _empty_payload(period, staff_block.get("timezone"), staff_block.get("office_rules"))

    inst_rows = (
        await db.execute(
            select(WhatsAppInstance).where(
                WhatsAppInstance.organization_id == ORG,
                WhatsAppInstance.deleted_at.is_(None),
                *(
                    [WhatsAppInstance.id.in_(allowed_instance_ids)]
                    if allowed_instance_ids is not None
                    else []
                ),
            )
        )
    ).scalars().all()
    inst_label = {r.id: short_staff_name(r.display_name or r.employee_name or r.evolution_instance_name) for r in inst_rows}

    pending_scope = [
        WhatsAppConversation.organization_id == ORG,
        newsletter_list_filter(),
        dm_inbox_filter(),
        WhatsAppConversation.awaiting_staff_reply.is_(True),
        WhatsAppConversation.status != "Resolved",
    ]
    if allowed_instance_ids is not None:
        pending_scope.append(WhatsAppConversation.whatsapp_instance_id.in_(allowed_instance_ids))

    pending_rows = (
        await db.execute(
            select(
                WhatsAppConversation.whatsapp_instance_id,
                func.lower(WhatsAppConversation.service_category),
                func.count(),
            )
            .where(*pending_scope)
            .group_by(WhatsAppConversation.whatsapp_instance_id, func.lower(WhatsAppConversation.service_category))
        )
    ).all()

    pending_total = {"sales": 0, "service": 0, "cloud": 0, "other": 0}
    pending_by_staff: dict[int, dict[str, int]] = defaultdict(lambda: {"sales": 0, "service": 0, "cloud": 0, "other": 0, "total": 0})
    for iid, cat, n in pending_rows:
        bucket = _cat_bucket(cat)
        cnt = int(n or 0)
        pending_total[bucket] = pending_total.get(bucket, 0) + cnt
        pending_by_staff[int(iid)][bucket] += cnt
        pending_by_staff[int(iid)]["total"] += cnt

    pending_staff_items = [
        {
            "whatsapp_instance_id": iid,
            "staff_name": inst_label.get(iid, f"#{iid}"),
            **counts,
        }
        for iid, counts in sorted(pending_by_staff.items(), key=lambda x: (-x[1]["total"], inst_label.get(x[0], "")))
    ]

    cat_scope = [
        WhatsAppConversation.organization_id == ORG,
        newsletter_list_filter(),
        dm_inbox_filter(),
    ]
    if allowed_instance_ids is not None:
        cat_scope.append(WhatsAppConversation.whatsapp_instance_id.in_(allowed_instance_ids))
    if since is not None:
        cat_scope.append(
            or_(
                WhatsAppConversation.last_message_at >= since,
                and_(
                    WhatsAppConversation.awaiting_staff_reply.is_(True),
                    WhatsAppConversation.status != "Resolved",
                ),
            )
        )

    cat_rows = (
        await db.execute(
            select(
                func.lower(WhatsAppConversation.service_category),
                func.count(),
            )
            .where(*cat_scope)
            .group_by(func.lower(WhatsAppConversation.service_category))
        )
    ).all()
    category_totals = {"sales": 0, "service": 0, "cloud": 0, "other": 0}
    for cat, n in cat_rows:
        category_totals[_cat_bucket(cat)] += int(n or 0)

    evt_scope = [WhatsAppReplyEvent.organization_id == ORG]
    if since is not None:
        evt_scope.append(WhatsAppReplyEvent.replied_at >= since)
    if allowed_instance_ids is not None:
        evt_scope.append(WhatsAppReplyEvent.whatsapp_instance_id.in_(allowed_instance_ids))

    evt_rows = (
        await db.execute(
            select(WhatsAppReplyEvent).where(*evt_scope).order_by(WhatsAppReplyEvent.id.desc()).limit(8000)
        )
    ).scalars().all()

    conv_ids = list({e.conversation_id for e in evt_rows})
    conv_map: dict[int, WhatsAppConversation] = {}
    if conv_ids:
        for conv in (
            await db.execute(
                select(WhatsAppConversation).where(
                    WhatsAppConversation.organization_id == ORG,
                    WhatsAppConversation.id.in_(conv_ids),
                )
            )
        ).scalars().all():
            conv_map[conv.id] = conv

    kinds = await _contact_kinds_map(db, list(conv_map.values()))

    by_kind: dict[str, dict[str, list[int]]] = {
        "dealer": {"total": [], "office": [], "off": []},
        "lead": {"total": [], "office": [], "off": []},
        "other": {"total": [], "office": [], "off": []},
    }
    for evt in evt_rows:
        kind = kinds.get(evt.conversation_id, "other")
        if kind not in by_kind:
            kind = "other"
        by_kind[kind]["total"].append(int(evt.reply_seconds or 0))
        by_kind[kind]["office"].append(int(evt.office_seconds or 0))
        by_kind[kind]["off"].append(int(evt.off_seconds or 0))

    contact_reply = {
        k: _avg_block_full(v["total"], v["office"], v["off"])
        for k, v in by_kind.items()
    }

    # —— Lead KB interest ——
    sugg_scope = [WaAiSuggestion.organization_id == ORG]
    if since is not None:
        sugg_scope.append(WaAiSuggestion.created_at >= since)
    if allowed_instance_ids is not None:
        sugg_scope.append(
            or_(
                WaAiSuggestion.whatsapp_instance_id.in_(allowed_instance_ids),
                WaAiSuggestion.whatsapp_instance_id.is_(None),
            )
        )

    sugg_rows = (
        await db.execute(
            select(WaAiSuggestion, WhatsAppConversation)
            .join(WhatsAppConversation, WaAiSuggestion.conversation_id == WhatsAppConversation.id)
            .where(*sugg_scope)
            .order_by(WaAiSuggestion.id.desc())
            .limit(5000)
        )
    ).all()

    kb_lead_convs: set[int] = set()
    kb_by_staff: dict[int, int] = defaultdict(int)
    intent_counts: dict[str, int] = defaultdict(int)
    kb_suggestion_hits = 0

    sugg_kinds = await _contact_kinds_map(db, [c for _, c in sugg_rows])

    for sugg, conv in sugg_rows:
        has_kb = bool((sugg.matched_knowledge_title or "").strip())
        if not has_kb:
            try:
                ids = json.loads(sugg.kb_article_ids or "[]")
                has_kb = bool(ids)
            except json.JSONDecodeError:
                has_kb = False
        intent = (sugg.intent or "").strip().lower()
        if intent:
            intent_counts[intent] += 1
        if not has_kb and intent in KB_INTENT_HINTS:
            has_kb = True
        if not has_kb:
            continue
        kb_suggestion_hits += 1
        if sugg_kinds.get(conv.id) != "lead":
            continue
        kb_lead_convs.add(conv.id)
        iid = int(conv.whatsapp_instance_id)
        kb_by_staff[iid] += 1

    msg_scope = [
        WhatsAppMessage.organization_id == ORG,
        WhatsAppMessage.direction == "in",
    ]
    if since is not None:
        msg_scope.append(WhatsAppMessage.created_at >= since)
    if allowed_instance_ids is not None:
        msg_scope.append(WhatsAppMessage.whatsapp_instance_id.in_(allowed_instance_ids))

    heuristic_convs: set[int] = set()
    msg_rows = (
        await db.execute(
            select(WhatsAppMessage, WhatsAppConversation)
            .join(WhatsAppConversation, WhatsAppMessage.conversation_id == WhatsAppConversation.id)
            .where(
                *msg_scope,
                newsletter_list_filter(),
                dm_inbox_filter(),
            )
            .order_by(WhatsAppMessage.id.desc())
            .limit(6000)
        )
    ).all()
    msg_convs = {c.id: c for _, c in msg_rows}
    msg_kinds = await _contact_kinds_map(db, list(msg_convs.values()))
    for msg, conv in msg_rows:
        if msg_kinds.get(conv.id) != "lead":
            continue
        text = f"{msg.body or ''} {msg.transcript or ''}"
        if not KB_INTEREST_RE.search(text):
            continue
        if conv.id not in kb_lead_convs:
            heuristic_convs.add(conv.id)
            iid = int(conv.whatsapp_instance_id)
            kb_by_staff[iid] += 1
        kb_lead_convs.add(conv.id)

    active_convs = (
        await db.execute(select(WhatsAppConversation).where(*cat_scope))
    ).scalars().all()
    active_kinds = await _contact_kinds_map(db, list(active_convs))
    lead_active = sum(1 for k in active_kinds.values() if k == "lead")

    kb_unique = len(kb_lead_convs)
    kb_rate = round(100.0 * kb_unique / lead_active, 1) if lead_active else 0.0

    kb_staff_items = [
        {
            "whatsapp_instance_id": iid,
            "staff_name": inst_label.get(iid, f"#{iid}"),
            "kb_signals": n,
        }
        for iid, n in sorted(kb_by_staff.items(), key=lambda x: (-x[1], inst_label.get(x[0], "")))
    ]

    top_intents = [
        {"intent": k, "count": v}
        for k, v in sorted(intent_counts.items(), key=lambda x: (-x[1], x[0]))[:12]
    ]

    return {
        "period": period,
        "timezone": staff_block.get("timezone") or "Asia/Kolkata",
        "office_rules": staff_block.get("office_rules") or "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "staff_reply": staff_block,
        "pending": {
            "total": pending_total,
            "grand_total": sum(pending_total.values()),
            "by_staff": pending_staff_items,
        },
        "contact_reply": contact_reply,
        "categories": {
            "active_conversations": category_totals,
            "active_total": sum(category_totals.values()),
        },
        "kb_leads": {
            "detection": KB_DETECTION_DOC,
            "suggestion_kb_hits": kb_suggestion_hits,
            "heuristic_conversations": len(heuristic_convs),
            "lead_kb_conversations": kb_unique,
            "rate_of_active_chats_pct": kb_rate,
            "by_staff": kb_staff_items,
            "top_intents": top_intents,
        },
    }


def _empty_payload(period: str, tz: Any, rules: Any) -> dict[str, Any]:
    z = defaultdict(int)
    return {
        "period": period,
        "timezone": tz or "Asia/Kolkata",
        "office_rules": rules or "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "staff_reply": {"period": period, "items": [], "timezone": tz, "office_rules": rules},
        "pending": {"total": dict(z), "grand_total": 0, "by_staff": []},
        "contact_reply": {
            "dealer": _avg_block([]),
            "lead": _avg_block([]),
            "other": _avg_block([]),
        },
        "categories": {"active_conversations": {"sales": 0, "service": 0, "cloud": 0, "other": 0}, "active_total": 0},
        "kb_leads": {
            "detection": KB_DETECTION_DOC,
            "suggestion_kb_hits": 0,
            "heuristic_conversations": 0,
            "lead_kb_conversations": 0,
            "rate_of_active_chats_pct": 0.0,
            "by_staff": [],
            "top_intents": [],
        },
    }