"""SQL findings for the Observe board.

Intelligence stage lists (Enquiry / Sales order / Waiting / Paid / Stamping /
Service / Commitments) are WhatsApp AI-read chat findings. Zoho invoice rows
are not used for those tile lists or totals.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealers import (
    HIGH_VALUE_SALES,
    _inactive_sales_clause,
    _sale_cutoff_iso,
    _sales_by_customer_subq,
    _visible_dealers_clause,
)
from app.intel.enquiry_list import select_sales_enquiries
from app.intel.row_sort import sort_intel_dict_rows
from app.intel.extract import service_keyword_tags
from app.intel.order_cycle import CycleHit, load_order_cycles
from app.intel.wa_stages import (
    count_stamping_chats,
    load_stamping_public_ids,
)
from app.models import Dealer, Employee, WhatsAppConversation, WhatsAppInstance, WhatsAppMessage, ZohoSale
from app.whatsapp_service_meta import (
    format_hold_clock,
    parse_cached_outline_summary,
    parse_outline_lite,
    priority_from_summary,
    stamping_keyword_tags,
)
from app.sales_amount import is_countable_invoice, zoho_sale_countable_clause
from app.whatsapp_chat_types import dm_inbox_filter, newsletter_list_filter

ORG = "yesweigh"
IST = ZoneInfo("Asia/Kolkata")
OBSERVE_TYPES = ("unreplied_dm", "inactive_dealer", "invoice_open", "unresolved_service")
FUNNEL_STAGE_KEYS = (
    "enquiry",
    "quotation_language",
    "sales_order",
    "invoice_open",
    "invoice_settled",
)


def require_sources(sources: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Reject a finding that does not cite a record."""
    clean: list[dict[str, str]] = []
    for src in sources or []:
        typ = str(src.get("type") or "").strip()
        sid = str(src.get("id") or "").strip()
        if not typ or not sid:
            continue
        clean.append(
            {
                "type": typ,
                "id": sid,
                "label": str(src.get("label") or "")[:160],
                "href": str(src.get("href") or ""),
            }
        )
    if not clean:
        raise ValueError("finding requires a source")
    return clean


def age_label(moment: datetime | None, now: datetime | None = None) -> str:
    if moment is None:
        return ""
    current = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    secs = int((current - moment).total_seconds())
    if secs < 0:
        secs = 0
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _format_ist(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    label = moment.astimezone(IST).strftime("%d %b, %I:%M %p")
    return label.replace(" 0", " ").lstrip("0")


def _timestamp_fields(moment: datetime | None, *, now: datetime | None = None) -> dict[str, Any]:
    if moment is None:
        return {
            "since_at": None,
            "since_ist": None,
            "age_seconds": None,
            "age_label": "",
            "time_till_label": "",
        }
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    secs = max(0, int((current - moment).total_seconds()))
    _, hold = format_hold_clock(moment, now=current)
    return {
        "since_at": moment.isoformat(),
        "since_ist": _format_ist(moment),
        "age_seconds": secs,
        "age_label": age_label(moment, current),
        "time_till_label": hold or age_label(moment, current),
    }


def _outline_summary_text(conv: WhatsAppConversation) -> tuple[str, dict[str, Any]]:
    # Lite parse for list rows — full Pydantic normalize is too slow at page scale.
    summary = parse_outline_lite(conv.ai_outline or "")
    parts: list[str] = []
    for key in ("topic", "customer_intent", "current_issue", "recommended_action"):
        bit = str(summary.get(key) or "").strip()
        if bit:
            parts.append(bit)
    line = " · ".join(parts)[:240] if parts else ""
    if not line:
        prev = (conv.last_message_preview or "").strip()
        line = prev[:200] if prev else ""
    return line, summary


def _dealer_display(dealer: Dealer | None, conv: WhatsAppConversation, *, fallback: str = "") -> str:
    if dealer is not None:
        name = (dealer.company or dealer.name or "").strip()
        if name:
            return name[:200]
    who = (conv.customer_name or conv.customer_phone or fallback or "Contact").strip()
    return who[:200]


def _phone_display(dealer: Dealer | None, conv: WhatsAppConversation, *, fallback: str = "") -> str:
    return (conv.customer_phone or (dealer.phone if dealer else "") or fallback or "").strip()[:40]


def _agent_display(emp: Employee | None, inst: WhatsAppInstance | None, *, fallback: str = "") -> str:
    if emp is not None and (emp.name or "").strip():
        return emp.name.strip()[:160]
    if inst is not None:
        label = (inst.employee_name or inst.display_name or "").strip()
        if label:
            return label[:160]
    return (fallback or "").strip()[:160]


def _priority_display(conv: WhatsAppConversation, summary: dict[str, Any]) -> str:
    raw = (conv.priority or "").strip() or priority_from_summary(summary)
    if raw:
        if raw.lower() in {"med", "mid"}:
            return "Medium"
        return raw.title() if raw.islower() else raw
    if conv.awaiting_staff_reply:
        return "Medium"
    return ""


def _since_moment(conv: WhatsAppConversation) -> datetime | None:
    if conv.awaiting_staff_reply and conv.awaiting_since is not None:
        return conv.awaiting_since
    return conv.last_message_at


def _invoice_priority(balance: float) -> str:
    if balance >= 100_000:
        return "High"
    if balance >= 10_000:
        return "Medium"
    return "Low"


def _zoho_since_fields(date_str: str) -> dict[str, Any]:
    raw = (date_str or "").strip()[:10]
    if not raw:
        return _timestamp_fields(None)
    try:
        moment = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=IST)
    except ValueError:
        return {"since_at": None, "since_ist": raw, "age_seconds": None, "age_label": "", "time_till_label": ""}
    return _timestamp_fields(moment)


async def _dealers_by_ids(db: AsyncSession, ids: set[int]) -> dict[int, Dealer]:
    if not ids:
        return {}
    rows = (await db.execute(select(Dealer).where(Dealer.id.in_(ids)))).scalars().all()
    return {int(d.id): d for d in rows}


async def _employees_by_ids(db: AsyncSession, ids: set[int]) -> dict[int, Employee]:
    if not ids:
        return {}
    rows = (await db.execute(select(Employee).where(Employee.id.in_(ids)))).scalars().all()
    return {int(e.id): e for e in rows}


async def _instances_by_ids(db: AsyncSession, ids: set[int]) -> dict[int, WhatsAppInstance]:
    if not ids:
        return {}
    rows = (await db.execute(select(WhatsAppInstance).where(WhatsAppInstance.id.in_(ids)))).scalars().all()
    return {int(i.id): i for i in rows}


def _avatar_url(conv: WhatsAppConversation) -> str:
    return (conv.customer_photo or "").strip()[:2000]


def _enrich_conversation_row(
    conv: WhatsAppConversation,
    *,
    dealer: Dealer | None,
    emp: Employee | None,
    inst: WhatsAppInstance | None,
    now: datetime,
    status: str = "",
    ai_tag: str = "",
) -> dict[str, Any]:
    summary_line, summary = _outline_summary_text(conv)
    outline_status = str(summary.get("conversation_status") or _outline_status(conv.ai_outline or "")).strip()
    status_out = (status or outline_status or conv.status or "Open").strip()
    since = _since_moment(conv)
    times = _timestamp_fields(since, now=now)
    prio = _priority_display(conv, summary)
    dealer_name = _dealer_display(dealer, conv)
    phone = _phone_display(dealer, conv)
    agent = _agent_display(emp, inst)
    tag = (ai_tag or str(summary.get("enquiry_subtype") or "").strip() or (conv.service_category or "").strip())
    return {
        "dealer_name": dealer_name,
        "phone": phone,
        "agent_name": agent,
        "avatar_url": _avatar_url(conv),
        "priority": prio,
        **times,
        "summary": summary_line,
        "status": status_out,
        "ai_status": outline_status,
        "ai_tag": tag,
        "contact_type": "Dealer" if dealer is not None else "Lead",
        "title": dealer_name,
        "subtitle": summary_line or f"Outline: {outline_status or status_out}".strip(),
    }


def _dm_scope() -> list[Any]:
    return [
        WhatsAppConversation.organization_id == ORG,
        newsletter_list_filter(),
        dm_inbox_filter(),
        ~WhatsAppConversation.customer_jid.ilike("%@broadcast"),
        ~WhatsAppConversation.customer_jid.ilike("status@%"),
    ]


def unreplied_filters(allowed_instance_ids: set[int] | None) -> list[Any]:
    """Same DM awaiting window Reply Stats uses for pending counts."""
    clauses = [
        *_dm_scope(),
        WhatsAppConversation.awaiting_staff_reply.is_(True),
        WhatsAppConversation.status != "Resolved",
    ]
    if allowed_instance_ids is not None:
        clauses.append(WhatsAppConversation.whatsapp_instance_id.in_(allowed_instance_ids or {-1}))
    return clauses


def _money(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def sum_open_invoice_balances(rows: list[dict[str, Any]]) -> Decimal:
    """Countable invoices with balance > 0. Void/draft/cancelled are excluded."""
    total = Decimal("0")
    for row in rows:
        if not is_countable_invoice(row.get("document_type"), row.get("status")):
            continue
        try:
            bal = Decimal(str(row.get("balance") if row.get("balance") is not None else 0))
        except Exception:
            continue
        if bal > 0:
            total += bal
    return total


def empty_funnel_shell() -> dict[str, Any]:
    labels = {
        "enquiry": "Open sales/price enquiries from WhatsApp",
        "quotation_language": "Quotation language (outline, not a Zoho quote)",
        "sales_order": "Sales order",
        "invoice_open": "Waiting",
        "invoice_settled": "Paid",
    }
    return {
        "stages": [
            {
                "key": key,
                "label": labels[key],
                "count": 0,
                "sum_balance": None,
                "ids": [],
                "items": [],
            }
            for key in FUNNEL_STAGE_KEYS
        ]
    }


def _conv_source(conv: WhatsAppConversation) -> dict[str, str]:
    name = (conv.customer_name or conv.customer_phone or "Conversation").strip()
    return {
        "type": "whatsapp_conversation",
        "id": conv.public_id,
        "label": name[:160],
        "href": f"/whatsapp?thread={conv.public_id}",
    }


async def unreplied_findings(
    db: AsyncSession,
    *,
    allowed_instance_ids: set[int] | None,
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    filters = unreplied_filters(allowed_instance_ids)
    total = int(
        (
            await db.execute(select(func.count()).select_from(WhatsAppConversation).where(*filters))
        ).scalar_one()
        or 0
    )
    rows = (
        await db.execute(
            select(WhatsAppConversation)
            .where(*filters)
            .order_by(WhatsAppConversation.awaiting_since.asc().nullsfirst(), WhatsAppConversation.id.asc())
            .limit(limit)
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    for conv in rows:
        since = conv.awaiting_since or conv.last_message_at
        hours = None
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            hours = (now - since).total_seconds() / 3600
        severity = "high" if hours is None or hours >= 4 else "medium"
        name = (conv.customer_name or conv.customer_phone or "DM").strip()
        item = {
            "finding_type": "unreplied_dm",
            "severity": severity,
            "title": f"Unreplied DM · {name}"[:240],
            "subject_type": "whatsapp_conversation",
            "subject_id": conv.public_id,
            "dealer_id": conv.dealer_id,
            "conversation_id": conv.id,
            "contact_name": name,
            "age_label": age_label(since, now),
            "detail": {
                "awaiting_since": since.isoformat() if since else None,
                "service_category": conv.service_category or "",
                "customer_phone": conv.customer_phone or "",
                "whatsapp_instance_id": conv.whatsapp_instance_id,
            },
            "sources": require_sources([_conv_source(conv)]),
            "narration": None,
            "generated_by": "sql",
            "fingerprint": "awaiting",
        }
        items.append(item)
    return total, items


async def inactive_dealer_findings(
    db: AsyncSession,
    *,
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    cutoff = _sale_cutoff_iso()
    sales_sub = _sales_by_customer_subq()
    base = (
        select(Dealer, sales_sub.c.last_sale_date, sales_sub.c.sales_total)
        .outerjoin(sales_sub, Dealer.zoho_contact_id == sales_sub.c.customer_id)
        .where(_visible_dealers_clause())
        .where(_inactive_sales_clause(sales_sub, cutoff))
    )
    total = int(
        (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one() or 0
    )
    rows = (
        await db.execute(
            base.order_by(sales_sub.c.sales_total.desc().nulls_last(), Dealer.id.asc()).limit(limit)
        )
    ).all()
    items: list[dict[str, Any]] = []
    for dealer, last_sale, sales_total in rows:
        amount = _money(sales_total)
        if amount >= float(HIGH_VALUE_SALES):
            severity = "high"
        elif amount > 0:
            severity = "medium"
        else:
            severity = "low"
        name = (dealer.company or dealer.name or "Dealer").strip()
        sources = require_sources(
            [
                {
                    "type": "dealer",
                    "id": str(dealer.id),
                    "label": name,
                    "href": f"/dealers?q={quote(name)}",
                }
            ]
        )
        items.append(
            {
                "finding_type": "inactive_dealer",
                "severity": severity,
                "title": f"Inactive dealer · {name}"[:240],
                "subject_type": "dealer",
                "subject_id": str(dealer.id),
                "dealer_id": dealer.id,
                "conversation_id": None,
                "contact_name": name,
                "age_label": (last_sale or "")[:10],
                "detail": {
                    "last_sale_date": last_sale or "",
                    "sales_total": amount,
                    "active_cutoff": cutoff,
                    "rule": f"No countable invoice on or after {cutoff} (6 months).",
                    "zoho_contact_id": dealer.zoho_contact_id or "",
                },
                "sources": sources,
                "narration": None,
                "generated_by": "sql",
                "fingerprint": "inactive",
            }
        )
    return total, items


def _invoice_filters() -> list[Any]:
    return [
        *zoho_sale_countable_clause(),
        ZohoSale.balance.is_not(None),
        ZohoSale.balance > 0,
    ]


async def invoice_open_findings(
    db: AsyncSession,
    *,
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    filters = _invoice_filters()
    total = int((await db.execute(select(func.count()).select_from(ZohoSale).where(*filters))).scalar_one() or 0)
    rows = (
        await db.execute(
            select(ZohoSale).where(*filters).order_by(ZohoSale.balance.desc(), ZohoSale.id.desc()).limit(limit)
        )
    ).scalars().all()
    customer_ids = {str(r.customer_id) for r in rows if r.customer_id}
    dealer_by_zoho: dict[str, Dealer] = {}
    if customer_ids:
        dealers = (
            await db.execute(select(Dealer).where(Dealer.zoho_contact_id.in_(customer_ids)))
        ).scalars().all()
        dealer_by_zoho = {d.zoho_contact_id: d for d in dealers if d.zoho_contact_id}
    items: list[dict[str, Any]] = []
    for sale in rows:
        bal = _money(sale.balance)
        number = (sale.invoice_number or sale.zoho_record_id or str(sale.id)).strip()
        who = (sale.customer_name or "Invoice").strip()
        severity = "high" if bal >= 50000 else "medium"
        dealer = dealer_by_zoho.get(str(sale.customer_id or ""))
        sources = require_sources(
            [
                {
                    "type": "zoho_sale",
                    "id": str(sale.id),
                    "label": number,
                    "href": "",
                }
            ]
        )
        items.append(
            {
                "finding_type": "invoice_open",
                "severity": severity,
                "title": f"Open invoice {number} · ₹{bal:,.0f}"[:240],
                "subject_type": "zoho_sale",
                "subject_id": str(sale.id),
                "dealer_id": dealer.id if dealer else None,
                "conversation_id": None,
                "contact_name": (dealer.company or dealer.name) if dealer else who,
                "age_label": (sale.date or "")[:10],
                "detail": {
                    "balance": bal,
                    "invoice_number": number,
                    "zoho_record_id": sale.zoho_record_id or "",
                    "customer_name": who,
                    "customer_id": sale.customer_id or "",
                    "date": sale.date or "",
                    "status": sale.status or "",
                    "display_only": True,
                    "note": "Open balance from Zoho. Not a collections workflow.",
                },
                "sources": sources,
                "narration": None,
                "generated_by": "sql",
                "fingerprint": "balance>0",
            }
        )
    return total, items


def _service_subject_clause() -> Any:
    """Column chip or outline label. Repair threads stay off Enquiry and on this queue."""
    return or_(
        func.lower(WhatsAppConversation.service_category) == "service",
        WhatsAppConversation.ai_outline.ilike('%"conversation_status": "Service Required"%'),
        WhatsAppConversation.ai_outline.ilike('%"conversation_status":"Service Required"%'),
        WhatsAppConversation.ai_outline.ilike('%"service_category": "service"%'),
        WhatsAppConversation.ai_outline.ilike('%"service_category":"service"%'),
    )


def _stamping_subject_clause() -> Any:
    """Category, outline, or OV/certificate message signals (Sandhya-style)."""
    from app.intel.wa_stages import stamping_msg_conv_ids_sql

    return or_(
        func.lower(WhatsAppConversation.service_category) == "stamping",
        WhatsAppConversation.ai_outline.ilike('%"service_category": "stamping"%'),
        WhatsAppConversation.ai_outline.ilike('%"service_category":"stamping"%'),
        WhatsAppConversation.id.in_(stamping_msg_conv_ids_sql()),
    )


def _unresolved_service_filters(allowed_instance_ids: set[int] | None) -> tuple[Any, Any, list[Any]]:
    """Last in/out only for service DMs, so the board does not scan every message."""
    candidate_filters = [
        *_dm_scope(),
        _service_subject_clause(),
        WhatsAppConversation.status != "Resolved",
        ~WhatsAppConversation.ai_outline.ilike('%"conversation_status": "Resolved"%'),
        ~WhatsAppConversation.ai_outline.ilike('%"conversation_status":"Resolved"%'),
    ]
    if allowed_instance_ids is not None:
        candidate_filters.append(
            WhatsAppConversation.whatsapp_instance_id.in_(allowed_instance_ids or {-1})
        )
    candidates = select(WhatsAppConversation.id).where(*candidate_filters)
    last_in = (
        select(
            WhatsAppMessage.conversation_id.label("conversation_id"),
            func.max(WhatsAppMessage.created_at).label("last_in"),
        )
        .where(
            WhatsAppMessage.direction == "in",
            WhatsAppMessage.conversation_id.in_(candidates),
        )
        .group_by(WhatsAppMessage.conversation_id)
        .subquery()
    )
    last_out = (
        select(
            WhatsAppMessage.conversation_id.label("conversation_id"),
            func.max(WhatsAppMessage.created_at).label("last_out"),
        )
        .where(
            WhatsAppMessage.direction == "out",
            WhatsAppMessage.conversation_id.in_(candidates),
        )
        .group_by(WhatsAppMessage.conversation_id)
        .subquery()
    )
    filters = [
        *_dm_scope(),
        _service_subject_clause(),
        WhatsAppConversation.status != "Resolved",
        ~WhatsAppConversation.ai_outline.ilike('%"conversation_status": "Resolved"%'),
        ~WhatsAppConversation.ai_outline.ilike('%"conversation_status":"Resolved"%'),
    ]
    if allowed_instance_ids is not None:
        filters.append(WhatsAppConversation.whatsapp_instance_id.in_(allowed_instance_ids or {-1}))
    return last_in, last_out, filters


async def unresolved_service_findings(
    db: AsyncSession,
    *,
    allowed_instance_ids: set[int] | None,
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    last_in, last_out, filters = _unresolved_service_filters(allowed_instance_ids)
    joined = (
        select(WhatsAppConversation, last_in.c.last_in)
        .join(last_in, last_in.c.conversation_id == WhatsAppConversation.id)
        .outerjoin(last_out, last_out.c.conversation_id == WhatsAppConversation.id)
        .where(*filters)
    )
    total = int((await db.execute(select(func.count()).select_from(joined.subquery()))).scalar_one() or 0)
    rows = (
        await db.execute(
            joined.order_by(last_in.c.last_in.desc().nulls_last(), WhatsAppConversation.id.desc()).limit(limit)
        )
    ).all()
    now = datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    for conv, last_in_at in rows:
        name = (conv.customer_name or conv.customer_phone or "Service").strip()
        tags = service_keyword_tags(conv.last_message_preview or "")
        items.append(
            {
                "finding_type": "unresolved_service",
                "severity": "high",
                "title": f"Unresolved service · {name}"[:240],
                "subject_type": "whatsapp_conversation",
                "subject_id": conv.public_id,
                "dealer_id": conv.dealer_id,
                "conversation_id": conv.id,
                "contact_name": name,
                "age_label": age_label(last_in_at, now),
                "detail": {
                    "tags": tags,
                    "service_category": "service",
                    "last_inbound_at": last_in_at.isoformat() if last_in_at else None,
                    "customer_phone": conv.customer_phone or "",
                },
                "sources": require_sources([_conv_source(conv)]),
                "narration": None,
                "generated_by": "sql",
                "fingerprint": "unresolved",
            }
        )
    return total, items


def _unresolved_stamping_filters(allowed_instance_ids: set[int] | None) -> tuple[Any, Any, list[Any]]:
    candidate_filters = [
        *_dm_scope(),
        _stamping_subject_clause(),
        WhatsAppConversation.status != "Resolved",
        ~WhatsAppConversation.ai_outline.ilike('%"conversation_status": "Resolved"%'),
        ~WhatsAppConversation.ai_outline.ilike('%"conversation_status":"Resolved"%'),
    ]
    if allowed_instance_ids is not None:
        candidate_filters.append(
            WhatsAppConversation.whatsapp_instance_id.in_(allowed_instance_ids or {-1})
        )
    candidates = select(WhatsAppConversation.id).where(*candidate_filters)
    last_in = (
        select(
            WhatsAppMessage.conversation_id.label("conversation_id"),
            func.max(WhatsAppMessage.created_at).label("last_in"),
        )
        .where(
            WhatsAppMessage.direction == "in",
            WhatsAppMessage.conversation_id.in_(candidates),
        )
        .group_by(WhatsAppMessage.conversation_id)
        .subquery()
    )
    last_out = (
        select(
            WhatsAppMessage.conversation_id.label("conversation_id"),
            func.max(WhatsAppMessage.created_at).label("last_out"),
        )
        .where(
            WhatsAppMessage.direction == "out",
            WhatsAppMessage.conversation_id.in_(candidates),
        )
        .group_by(WhatsAppMessage.conversation_id)
        .subquery()
    )
    filters = [
        *_dm_scope(),
        _stamping_subject_clause(),
        WhatsAppConversation.status != "Resolved",
        ~WhatsAppConversation.ai_outline.ilike('%"conversation_status": "Resolved"%'),
        ~WhatsAppConversation.ai_outline.ilike('%"conversation_status":"Resolved"%'),
        or_(last_out.c.last_out.is_(None), last_in.c.last_in > last_out.c.last_out),
    ]
    if allowed_instance_ids is not None:
        filters.append(WhatsAppConversation.whatsapp_instance_id.in_(allowed_instance_ids or {-1}))
    return last_in, last_out, filters


async def unresolved_stamping_findings(
    db: AsyncSession,
    *,
    allowed_instance_ids: set[int] | None,
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    last_in, last_out, filters = _unresolved_stamping_filters(allowed_instance_ids)
    joined = (
        select(WhatsAppConversation, last_in.c.last_in)
        .join(last_in, last_in.c.conversation_id == WhatsAppConversation.id)
        .outerjoin(last_out, last_out.c.conversation_id == WhatsAppConversation.id)
        .where(*filters)
    )
    total = int((await db.execute(select(func.count()).select_from(joined.subquery()))).scalar_one() or 0)
    rows = (
        await db.execute(
            joined.order_by(last_in.c.last_in.desc().nulls_last(), WhatsAppConversation.id.desc()).limit(limit)
        )
    ).all()
    now = datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    for conv, last_in_at in rows:
        name = (conv.customer_name or conv.customer_phone or "Stamping").strip()
        preview = conv.last_message_preview or ""
        tags = stamping_keyword_tags(preview)
        if not tags:
            summary = parse_cached_outline_summary(conv.ai_outline or "")
            tags = stamping_keyword_tags(
                " ".join(
                    str(summary.get(k) or "")
                    for k in ("topic", "customer_intent", "current_issue")
                )
            )
        items.append(
            {
                "finding_type": "unresolved_stamping",
                "severity": "high",
                "title": f"Stamping / OV · {name}"[:240],
                "subject_type": "whatsapp_conversation",
                "subject_id": conv.public_id,
                "dealer_id": conv.dealer_id,
                "conversation_id": conv.id,
                "contact_name": name,
                "age_label": age_label(last_in_at, now),
                "detail": {
                    "tags": tags,
                    "service_category": "stamping",
                    "last_inbound_at": last_in_at.isoformat() if last_in_at else None,
                    "customer_phone": conv.customer_phone or "",
                },
                "sources": require_sources([_conv_source(conv)]),
                "narration": None,
                "generated_by": "sql",
                "fingerprint": "unresolved_stamping",
            }
        )
    return total, items


async def collect_findings(
    db: AsyncSession,
    *,
    types: list[str],
    severity: str = "",
    dealer_id: int | None = None,
    allowed_instance_ids: set[int] | None,
    limit: int = 100,
) -> dict[str, Any]:
    wanted = [t for t in OBSERVE_TYPES if t in set(types)]
    counts = {t: 0 for t in wanted}
    items: list[dict[str, Any]] = []
    fetchers = {
        "unreplied_dm": lambda: unreplied_findings(
            db, allowed_instance_ids=allowed_instance_ids, limit=limit
        ),
        "inactive_dealer": lambda: inactive_dealer_findings(db, limit=limit),
        "invoice_open": lambda: invoice_open_findings(db, limit=limit),
        "unresolved_service": lambda: unresolved_service_findings(
            db, allowed_instance_ids=allowed_instance_ids, limit=limit
        ),
    }
    for key in wanted:
        total, rows = await fetchers[key]()
        counts[key] = total
        items.extend(rows)
    if dealer_id is not None:
        items = [row for row in items if row.get("dealer_id") == dealer_id]
    sev = (severity or "").strip().lower()
    if sev:
        items = [row for row in items if row.get("severity") == sev]
    return {"counts": counts, "items": items}


def _outline_status(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("conversation_status") or "").strip()


async def _dealers_for_zoho_customers(db: AsyncSession, customer_ids: set[str]) -> dict[str, Dealer]:
    if not customer_ids:
        return {}
    dealers = (
        await db.execute(select(Dealer).where(Dealer.zoho_contact_id.in_(customer_ids)))
    ).scalars().all()
    return {d.zoho_contact_id: d for d in dealers if d.zoho_contact_id}


def _dealer_href(dealer: Dealer | None, fallback_name: str) -> str:
    if dealer is not None:
        name = (dealer.company or dealer.name or "").strip()
        if name:
            return f"/dealers?q={quote(name)}"
    who = (fallback_name or "").strip()
    if who:
        return f"/dealers?q={quote(who)}"
    return ""


async def _funnel_enquiry_items(
    db: AsyncSession,
    public_ids: list[str],
    summaries: dict[str, str] | None = None,
    *,
    ai_tag: str = "Sales",
    status_label: str = "",
) -> list[dict[str, Any]]:
    if not public_ids:
        return []
    rows = (
        await db.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.public_id.in_(public_ids),
            )
        )
    ).scalars().all()
    by_pub = {c.public_id: c for c in rows}
    dealer_map = await _dealers_by_ids(db, {int(c.dealer_id) for c in rows if c.dealer_id})
    emp_map = await _employees_by_ids(db, {int(c.assigned_employee_id) for c in rows if c.assigned_employee_id})
    inst_map = await _instances_by_ids(db, {int(c.whatsapp_instance_id) for c in rows})
    now = datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    for pid in public_ids:
        conv = by_pub.get(pid)
        if conv is None:
            continue
        enriched = _enrich_conversation_row(
            conv,
            dealer=dealer_map.get(int(conv.dealer_id or 0)),
            emp=emp_map.get(int(conv.assigned_employee_id or 0)),
            inst=inst_map.get(int(conv.whatsapp_instance_id)),
            now=now,
        )
        custom = (summaries or {}).get(pid)
        if custom:
            enriched["summary"] = custom[:240]
            enriched["subtitle"] = custom[:240]
            enriched["ai_tag"] = ai_tag
        if status_label:
            enriched["status"] = status_label
        items.append(
            {
                "id": pid,
                "href": f"/whatsapp?thread={pid}",
                "conversation_id": pid,
                "customer_jid": conv.customer_jid or "",
                **enriched,
            }
        )
    from app.intel.dedupe import dedupe_intel_rows

    return sort_intel_dict_rows(dedupe_intel_rows(items, drop_blast=True))


async def _funnel_sales_order_items(db: AsyncSession, sale_ids: list[int]) -> list[dict[str, Any]]:
    if not sale_ids:
        return []
    rows = (
        await db.execute(select(ZohoSale).where(ZohoSale.id.in_(sale_ids)).order_by(ZohoSale.id.desc()))
    ).scalars().all()
    by_id = {int(r.id): r for r in rows}
    customer_ids = {str(r.customer_id) for r in rows if r.customer_id}
    dealer_by_zoho = await _dealers_for_zoho_customers(db, customer_ids)
    items: list[dict[str, Any]] = []
    for sid in sale_ids:
        sale = by_id.get(int(sid))
        if sale is None:
            continue
        number = (sale.invoice_number or sale.zoho_record_id or str(sale.id)).strip()
        who = (sale.customer_name or "Customer").strip()
        dealer = dealer_by_zoho.get(str(sale.customer_id or ""))
        dealer_name = (dealer.company or dealer.name) if dealer else who
        phone = (dealer.phone if dealer else "").strip()
        agent = (sale.salesperson_name or (dealer.assigned_salesman if dealer else "") or "").strip()
        status = (sale.status or "open").strip()
        times = _zoho_since_fields(sale.date or "")
        items.append(
            {
                "id": str(sale.id),
                "title": f"SO {number}"[:200],
                "subtitle": f"{dealer_name} · {status}"[:240],
                "href": _dealer_href(dealer, who),
                "dealer_name": dealer_name[:200],
                "phone": phone[:40],
                "agent_name": agent[:160],
                "priority": "Medium",
                **times,
                "summary": f"Open sales order · {status}"[:240],
                "status": status,
                "ai_status": "",
                "ai_tag": "Zoho",
                "contact_type": "Dealer" if dealer is not None else "Lead",
            }
        )
    return sort_intel_dict_rows(items)


async def _funnel_invoice_open_items(db: AsyncSession, sale_ids: list[int]) -> list[dict[str, Any]]:
    if not sale_ids:
        return []
    rows = (
        await db.execute(select(ZohoSale).where(ZohoSale.id.in_(sale_ids)).order_by(ZohoSale.balance.desc()))
    ).scalars().all()
    by_id = {int(r.id): r for r in rows}
    customer_ids = {str(r.customer_id) for r in rows if r.customer_id}
    dealer_by_zoho = await _dealers_for_zoho_customers(db, customer_ids)
    items: list[dict[str, Any]] = []
    for sid in sale_ids:
        sale = by_id.get(int(sid))
        if sale is None:
            continue
        bal = _money(sale.balance)
        number = (sale.invoice_number or sale.zoho_record_id or str(sale.id)).strip()
        who = (sale.customer_name or "Customer").strip()
        dealer = dealer_by_zoho.get(str(sale.customer_id or ""))
        dealer_name = (dealer.company or dealer.name) if dealer else who
        phone = (dealer.phone if dealer else "").strip()
        agent = (sale.salesperson_name or (dealer.assigned_salesman if dealer else "") or "").strip()
        status = (sale.status or "open").strip()
        times = _zoho_since_fields(sale.date or "")
        items.append(
            {
                "id": str(sale.id),
                "title": f"{number} · ₹{bal:,.0f}"[:200],
                "subtitle": f"{dealer_name} · {(sale.date or '')[:10]}".strip(" ·"),
                "href": _dealer_href(dealer, who),
                "invoice_number": number,
                "dealer_name": dealer_name[:200],
                "phone": phone[:40],
                "agent_name": agent[:160],
                "priority": _invoice_priority(bal),
                **times,
                "summary": f"Balance ₹{bal:,.0f} · {status}"[:240],
                "status": status or "Payment pending",
                "ai_status": "",
                "ai_tag": "Zoho",
                "contact_type": "Dealer" if dealer is not None else "Lead",
            }
        )
    return sort_intel_dict_rows(items)


async def _funnel_invoice_settled_items(db: AsyncSession, sale_ids: list[int]) -> list[dict[str, Any]]:
    if not sale_ids:
        return []
    rows = (
        await db.execute(select(ZohoSale).where(ZohoSale.id.in_(sale_ids)).order_by(ZohoSale.date.desc().nulls_last()))
    ).scalars().all()
    by_id = {int(r.id): r for r in rows}
    customer_ids = {str(r.customer_id) for r in rows if r.customer_id}
    dealer_by_zoho = await _dealers_for_zoho_customers(db, customer_ids)
    items: list[dict[str, Any]] = []
    for sid in sale_ids:
        sale = by_id.get(int(sid))
        if sale is None:
            continue
        bal = _money(sale.balance)
        number = (sale.invoice_number or sale.zoho_record_id or str(sale.id)).strip()
        who = (sale.customer_name or "Customer").strip()
        dealer = dealer_by_zoho.get(str(sale.customer_id or ""))
        dealer_name = (dealer.company or dealer.name) if dealer else who
        phone = (dealer.phone if dealer else "").strip()
        agent = (sale.salesperson_name or (dealer.assigned_salesman if dealer else "") or "").strip()
        status = (sale.status or "paid").strip()
        total = _money(sale.total) if getattr(sale, "total", None) is not None else bal
        times = _zoho_since_fields(sale.date or "")
        items.append(
            {
                "id": str(sale.id),
                "title": f"{number} · paid"[:200],
                "subtitle": f"{dealer_name} · {(sale.date or '')[:10]}".strip(" ·"),
                "href": _dealer_href(dealer, who),
                "invoice_number": number,
                "dealer_name": dealer_name[:200],
                "phone": phone[:40],
                "agent_name": agent[:160],
                "priority": "Low",
                **times,
                "summary": f"Paid ₹{total:,.0f}" if total > 0 else f"Settled · {status}"[:240],
                "status": status or "Paid",
                "ai_status": "",
                "ai_tag": "Zoho",
                "contact_type": "Dealer" if dealer is not None else "Lead",
            }
        )
    return sort_intel_dict_rows(items)


_ORDER_STAGES = ("sales_order", "waiting", "paid")


def _order_buckets(
    cycles: dict[str, list[CycleHit]],
    overrides: dict[int, str],
    exclude: set[int],
) -> dict[str, list[CycleHit]]:
    """Order stages stay mutually exclusive. Stamping/service/commitments may overlap."""
    buckets: dict[str, list[CycleHit]] = {name: [] for name in _ORDER_STAGES}
    for stage, hits in cycles.items():
        for hit in hits:
            if hit.conversation_id in exclude:
                continue
            override = overrides.get(hit.conversation_id, "")
            if override in {"closed", "enquiry"}:
                continue
            if override in _ORDER_STAGES and override != stage:
                continue
            buckets[stage].append(hit)
    return buckets


async def _force_order_overrides(
    db: AsyncSession,
    buckets: dict[str, list[CycleHit]],
    overrides: dict[int, str],
    exclude: set[int],
) -> None:
    present = {hit.conversation_id for hits in buckets.values() for hit in hits}
    missing = [
        cid
        for cid, stage in overrides.items()
        if stage in _ORDER_STAGES and cid not in exclude and cid not in present
    ]
    if not missing:
        return
    convs = (
        await db.execute(select(WhatsAppConversation).where(WhatsAppConversation.id.in_(missing)))
    ).scalars().all()
    labels = {
        "sales_order": "Order confirmed · sales order PDF not sent yet",
        "waiting": "Sales order sent · waiting for payment",
        "paid": "Payment confirmed on this sales order",
    }
    for conv in convs:
        stage = overrides.get(int(conv.id), "")
        if stage not in labels:
            continue
        buckets[stage].append(
            CycleHit(
                conversation_id=int(conv.id),
                public_id=conv.public_id,
                stage=stage,
                summary=labels[stage],
            )
        )


async def _force_kb_order_matches(
    db: AsyncSession,
    buckets: dict[str, list[CycleHit]],
    exclude: set[int],
    overrides: dict[int, str],
) -> None:
    """Boost sales_order / waiting / paid from human Move-to KB phrases."""
    try:
        from app.intel.stage_kb import load_kb_stage_conv_ids, load_stage_kb_patterns
    except Exception:  # noqa: BLE001
        return
    try:
        patterns = await load_stage_kb_patterns(db)
        kb_ids = await load_kb_stage_conv_ids(db, patterns, stages=list(_ORDER_STAGES))
    except Exception:  # noqa: BLE001
        return

    present = {hit.conversation_id for hits in buckets.values() for hit in hits}
    labels = {
        "sales_order": "Learned from Move-to · sales order pattern",
        "waiting": "Learned from Move-to · waiting pattern",
        "paid": "Learned from Move-to · paid pattern",
    }
    # Paid wins over waiting/sales_order when KB claims multiple.
    priority = ("paid", "waiting", "sales_order")
    claimed: set[int] = set(present)
    for stage in priority:
        ids = [cid for cid in kb_ids.get(stage, set()) if cid not in exclude and cid not in claimed]
        if not ids:
            continue
        convs = (
            await db.execute(select(WhatsAppConversation).where(WhatsAppConversation.id.in_(ids)))
        ).scalars().all()
        for conv in convs:
            cid = int(conv.id)
            if cid in claimed:
                continue
            ov = (overrides.get(cid) or "").strip().lower()
            # Respect a human override that points somewhere else.
            if ov and ov != stage:
                continue
            buckets[stage].append(
                CycleHit(
                    conversation_id=cid,
                    public_id=conv.public_id,
                    stage=stage,
                    summary=labels[stage],
                )
            )
            claimed.add(cid)


async def funnel_snapshot(
    db: AsyncSession,
    *,
    id_limit: int = 40,
    enquiry_id_limit: int = 120,
    include_items: bool = True,
    item_stages: set[str] | None = None,
) -> dict[str, Any]:
    """WhatsApp AI-read stages only. No Zoho invoice/paid lifetime dumps.

    ``include_items=False`` returns counts+ids only (fast tile path).
    ``item_stages`` limits which funnel keys get hydrated row payloads.
    """
    from app.intel.cache import get_funnel_cache, set_funnel_cache
    from app.intel.wa_stages import closed_or_blast_exclude_ids, load_stage_overrides

    stages_key = ",".join(sorted(item_stages)) if item_stages else ("all" if include_items else "none")
    cache_key = f"funnel:v2:{id_limit}:{enquiry_id_limit}:{include_items}:{stages_key}"
    cached = get_funnel_cache(cache_key)
    if cached is not None:
        return cached

    shell = empty_funnel_shell()
    by_key = {stage["key"]: stage for stage in shell["stages"]}

    # Quotation-language tile is not shown on Intelligence; skip the 400-row outline scan.
    quote_ids: list[str] = []

    overrides = await load_stage_overrides(db)
    exclude = await closed_or_blast_exclude_ids(db, overrides)
    # A sales-order PDF chat is still an order even if a broadcast line is also in the thread.
    closed_ids = {cid for cid, stage in overrides.items() if stage == "closed"}
    forced: dict[str, set[int]] = {
        key: set()
        for key in ("sales_order", "waiting", "paid", "enquiry", "stamping", "service", "commitments")
    }
    for cid, stage in overrides.items():
        if stage in forced:
            forced[stage].add(cid)

    cycles = await load_order_cycles(db)
    buckets = _order_buckets(cycles, overrides, closed_ids)
    await _force_order_overrides(db, buckets, overrides, closed_ids)
    await _force_kb_order_matches(db, buckets, exclude, overrides)

    enquiry_ids, enquiry_summaries = await select_sales_enquiries(db, allow_sync=False)
    # KB enquiry phrases → force-include similar chats (same path as manual overrides).
    try:
        from app.intel.stage_kb import load_kb_stage_conv_ids, load_stage_kb_patterns

        _kb_pat = await load_stage_kb_patterns(db)
        _kb_enq = await load_kb_stage_conv_ids(db, _kb_pat, stages=["enquiry"])
        for cid in _kb_enq.get("enquiry", set()):
            if cid not in exclude:
                forced["enquiry"].add(cid)
    except Exception:  # noqa: BLE001
        pass
    # Drop closed/blast and chats forced into other stages; add forced enquiry.
    if exclude or forced:
        enq_conv = (
            await db.execute(
                select(WhatsAppConversation.id, WhatsAppConversation.public_id).where(
                    WhatsAppConversation.public_id.in_(enquiry_ids or ["__none__"])
                )
            )
        ).all()
        pub_by_id = {int(i): p for i, p in enq_conv}
        id_by_pub = {p: int(i) for i, p in enq_conv}
        kept: list[str] = []
        for pid in enquiry_ids:
            cid = id_by_pub.get(pid)
            if cid is None:
                kept.append(pid)
                continue
            if cid in exclude:
                continue
            diverted = False
            for stage, ids in forced.items():
                if stage != "enquiry" and cid in ids:
                    diverted = True
                    break
            if diverted:
                continue
            kept.append(pid)
        for cid in forced.get("enquiry", set()):
            pub = pub_by_id.get(cid) or (
                await db.execute(
                    select(WhatsAppConversation.public_id).where(WhatsAppConversation.id == cid)
                )
            ).scalar_one_or_none()
            if pub and pub not in kept:
                kept.insert(0, pub)
                enquiry_summaries[pub] = enquiry_summaries.get(pub) or (
                    "Moved to Enquiry"
                    if overrides.get(cid) == "enquiry"
                    else "Learned from Move-to · enquiry pattern"
                )
        enquiry_ids = kept

    order_public = {hit.public_id for hits in buckets.values() for hit in hits}
    if order_public:
        enquiry_ids = [pid for pid in enquiry_ids if pid not in order_public]

    enq_cap = max(id_limit, int(enquiry_id_limit))
    by_key["enquiry"]["count"] = len(enquiry_ids)
    by_key["enquiry"]["ids"] = enquiry_ids[:enq_cap]
    by_key["enquiry"]["verified"] = False
    by_key["enquiry"]["note"] = "WhatsApp AI sales/price enquiries only."
    by_key["quotation_language"]["count"] = len(quote_ids)
    by_key["quotation_language"]["ids"] = quote_ids[:id_limit]
    by_key["quotation_language"]["verified"] = False
    by_key["quotation_language"]["note"] = "AI outline label. Not a Zoho quotation document."

    stage_notes = {
        "sales_order": "Dealer confirmed the order. Sales order PDF has not been sent yet.",
        "invoice_open": "Sales order PDF already sent. Payment is not confirmed.",
        "invoice_settled": "Payment confirmed on this sales order. Recent chats, not lifetime invoices.",
    }
    stage_tags = {
        "sales_order": ("Sales order", "Sales order"),
        "invoice_open": ("Waiting", "Waiting"),
        "invoice_settled": ("Paid", "Paid"),
    }
    funnel_key = {"sales_order": "sales_order", "waiting": "invoice_open", "paid": "invoice_settled"}
    item_cap = max(id_limit, int(enquiry_id_limit))
    want_items = set(item_stages) if item_stages is not None else (
        {"enquiry", "sales_order", "invoice_open", "invoice_settled"} if include_items else set()
    )

    order_summaries: dict[str, dict[str, str]] = {}
    for stage_name, funnel_name in funnel_key.items():
        hits = buckets[stage_name]
        by_key[funnel_name]["count"] = len(hits)
        by_key[funnel_name]["ids"] = [hit.public_id for hit in hits[:item_cap]]
        by_key[funnel_name]["verified"] = False
        by_key[funnel_name]["note"] = stage_notes[funnel_name]
        by_key[funnel_name]["sum_balance"] = None
        order_summaries[funnel_name] = {hit.public_id: hit.summary for hit in hits[:item_cap]}
        by_key[funnel_name]["items"] = []

    by_key["enquiry"]["items"] = []

    if "enquiry" in want_items:
        by_key["enquiry"]["items"] = await _funnel_enquiry_items(
            db, enquiry_ids[:enq_cap], enquiry_summaries
        )
    for funnel_name, (tag, status_label) in stage_tags.items():
        if funnel_name not in want_items:
            continue
        by_key[funnel_name]["items"] = await _funnel_enquiry_items(
            db,
            by_key[funnel_name]["ids"],
            order_summaries.get(funnel_name),
            ai_tag=tag,
            status_label=status_label,
        )

    shell["items_omitted"] = not bool(want_items) or (
        item_stages is not None and len(want_items) < 4
    )
    set_funnel_cache(cache_key, shell)
    return shell


async def service_queue(
    db: AsyncSession,
    *,
    allowed_instance_ids: set[int] | None,
    limit: int,
) -> list[dict[str, Any]]:
    from app.intel.cache import get_queue_cache, set_queue_cache
    from app.intel.wa_stages import closed_or_blast_exclude_ids, load_stage_overrides

    scope = "all" if allowed_instance_ids is None else ",".join(str(i) for i in sorted(allowed_instance_ids))
    cache_key = f"service:{limit}:{scope}"
    cached = get_queue_cache("service", cache_key)
    if cached is not None:
        return cached

    _total, items = await unresolved_service_findings(
        db, allowed_instance_ids=allowed_instance_ids, limit=limit
    )
    if not items:
        return []
    pub_ids = [str(item.get("subject_id") or "") for item in items if item.get("subject_id")]
    rows = (
        await db.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.public_id.in_(pub_ids),
            )
        )
    ).scalars().all()
    overrides = await load_stage_overrides(db)
    exclude = await closed_or_blast_exclude_ids(db, overrides)
    diverted = {cid for cid, st in overrides.items() if st and st not in {"service", "closed"}}
    rows = [c for c in rows if int(c.id) not in exclude and int(c.id) not in diverted]
    by_pub = {c.public_id: c for c in rows}
    items = [it for it in items if str(it.get("subject_id") or "") in by_pub]
    dealer_map = await _dealers_by_ids(db, {int(c.dealer_id) for c in rows if c.dealer_id})
    emp_map = await _employees_by_ids(db, {int(c.assigned_employee_id) for c in rows if c.assigned_employee_id})
    inst_map = await _instances_by_ids(db, {int(c.whatsapp_instance_id) for c in rows})
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for item in items:
        pid = str(item.get("subject_id") or "")
        conv = by_pub.get(pid)
        tags = list((item.get("detail") or {}).get("tags") or [])
        last_in = (item.get("detail") or {}).get("last_inbound_at")
        base = {
            "conversation_id": pid,
            "customer_name": item.get("contact_name") or "",
            "tags": tags,
            "severity": item.get("severity") or "high",
            "last_message_at": last_in,
            "href": (item.get("sources") or [{}])[0].get("href") or "",
        }
        if conv is None:
            out.append(base)
            continue
        since_dt = None
        if last_in:
            try:
                since_dt = datetime.fromisoformat(str(last_in).replace("Z", "+00:00"))
            except ValueError:
                since_dt = _since_moment(conv)
        else:
            since_dt = _since_moment(conv)
        enriched = _enrich_conversation_row(
            conv,
            dealer=dealer_map.get(int(conv.dealer_id or 0)),
            emp=emp_map.get(int(conv.assigned_employee_id or 0)),
            inst=inst_map.get(int(conv.whatsapp_instance_id)),
            now=now,
            status=conv.status or "Service",
            ai_tag="Service",
        )
        if since_dt is not None:
            enriched.update(_timestamp_fields(since_dt, now=now))
        if tags:
            enriched["summary"] = (enriched.get("summary") or "Service thread") + f" · {', '.join(tags[:3])}"
            enriched["subtitle"] = enriched["summary"]
        base["customer_jid"] = conv.customer_jid or ""
        out.append({**base, **enriched})
    from app.intel.dedupe import dedupe_intel_rows

    result = sort_intel_dict_rows(dedupe_intel_rows(out, drop_blast=True))
    set_queue_cache("service", cache_key, result)
    return result


async def stamping_queue(
    db: AsyncSession,
    *,
    allowed_instance_ids: set[int] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Open OV / certificate requests. Price lines that mention stamping stay off this tile."""
    from app.intel.cache import get_queue_cache, set_queue_cache
    from app.intel.order_cycle import recent_message_text
    from app.intel.wa_stages import closed_or_blast_exclude_ids, load_stage_overrides
    from app.whatsapp_service_meta import stamping_certificate_request

    scope = "all" if allowed_instance_ids is None else ",".join(str(i) for i in sorted(allowed_instance_ids))
    cache_key = f"stamping:{limit}:{scope}"
    cached = get_queue_cache("stamping", cache_key)
    if cached is not None:
        return cached

    overrides = await load_stage_overrides(db)
    pub_ids = await load_stamping_public_ids(
        db, allowed_instance_ids=allowed_instance_ids, limit=max(limit * 2, 80)
    )
    if not pub_ids and not any(stage == "stamping" for stage in overrides.values()):
        set_queue_cache("stamping", cache_key, [])
        return []
    exclude = await closed_or_blast_exclude_ids(db, overrides)
    rows = (
        await db.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.public_id.in_(pub_ids),
            )
        )
    ).scalars().all()
    # Force-include manual stamping overrides + KB-learned stamping phrases
    forced_ids = [cid for cid, st in overrides.items() if st == "stamping"]
    try:
        from app.intel.stage_kb import load_kb_stage_conv_ids, load_stage_kb_patterns

        _kb_pat = await load_stage_kb_patterns(db)
        _kb_stamp = await load_kb_stage_conv_ids(db, _kb_pat, stages=["stamping"])
        forced_ids = list({*forced_ids, *(_kb_stamp.get("stamping") or set())})
    except Exception:  # noqa: BLE001
        pass
    if forced_ids:
        extra = (
            await db.execute(select(WhatsAppConversation).where(WhatsAppConversation.id.in_(forced_ids)))
        ).scalars().all()
        seen = {c.id for c in rows}
        for c in extra:
            if c.id not in seen:
                rows.append(c)
    rows = [c for c in rows if int(c.id) not in exclude]
    # Drop chats forced to a non-stamping stage
    diverted = {
        cid
        for cid, st in overrides.items()
        if st and st not in {"stamping", "closed"}
    }
    rows = [c for c in rows if int(c.id) not in diverted]
    blobs = await recent_message_text(db, [int(c.id) for c in rows])
    certified = []
    manual_stamp = {cid for cid, st in overrides.items() if st == "stamping"}
    kb_stamp_ids = set(forced_ids) - manual_stamp
    from app.intel.wa_stages import text_has_stamping_signal

    for conv in rows:
        blob = "\n".join(
            part
            for part in (
                conv.last_message_preview or "",
                conv.ai_outline or "",
                blobs.get(int(conv.id), ""),
            )
            if part
        )
        # Manual Move-to always wins.
        if int(conv.id) in manual_stamp:
            certified.append(conv)
            continue
        # KB / auto: require a real OV / certificate signal so learned fillers
        # ("document", "voice message", …) cannot flood the Stamping tile.
        if stamping_certificate_request(blob) or (
            int(conv.id) in kb_stamp_ids and text_has_stamping_signal(blob)
        ):
            certified.append(conv)
    rows = certified
    rows = sorted(
        rows,
        key=lambda c: c.last_message_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:limit]
    if not rows:
        set_queue_cache("stamping", cache_key, [])
        return []
    dealer_map = await _dealers_by_ids(db, {int(c.dealer_id) for c in rows if c.dealer_id})
    emp_map = await _employees_by_ids(db, {int(c.assigned_employee_id) for c in rows if c.assigned_employee_id})
    inst_map = await _instances_by_ids(db, {int(c.whatsapp_instance_id) for c in rows})
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for conv in rows:
        preview = conv.last_message_preview or ""
        tags = stamping_keyword_tags(preview)
        if not tags:
            from app.whatsapp_service_meta import parse_outline_lite

            summary = parse_outline_lite(conv.ai_outline or "")
            tags = stamping_keyword_tags(
                " ".join(str(summary.get(k) or "") for k in ("topic", "customer_intent", "current_issue"))
            ) or ["ov"]
        enriched = _enrich_conversation_row(
            conv,
            dealer=dealer_map.get(int(conv.dealer_id or 0)),
            emp=emp_map.get(int(conv.assigned_employee_id or 0)),
            inst=inst_map.get(int(conv.whatsapp_instance_id or 0)),
            now=now,
            status=conv.status or "Open",
            ai_tag="Stamping",
        )
        summary_line = enriched.get("summary") or "Stamping / OV / sealed paper"
        if tags:
            summary_line = f"{summary_line} · {', '.join(tags[:3])}"
        enriched["summary"] = summary_line[:240]
        enriched["subtitle"] = enriched["summary"]
        out.append(
            {
                "conversation_id": conv.public_id,
                "customer_name": (conv.customer_name or conv.customer_phone or "").strip(),
                "customer_jid": conv.customer_jid or "",
                "tags": tags,
                "severity": "high",
                "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
                "href": f"/whatsapp?thread={conv.public_id}",
                **enriched,
            }
        )
    from app.intel.dedupe import dedupe_intel_rows

    result = sort_intel_dict_rows(dedupe_intel_rows(out, drop_blast=True))
    set_queue_cache("stamping", cache_key, result)
    return result
