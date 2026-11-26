"""Dealer-enquiry WhatsApp flows: classify, CRM Lead upsert, sales-line attendance."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealers import _normalized_phone_sql
from app.models import Dealer, Lead, WhatsAppConversation, WhatsAppInstance, WhatsAppMessage
from app.phone_util import normalize_in_mobile, to_evolution_number

PROVIDER_EVOLUTION = "evolution"

_DEALER_ENQUIRY_KEYWORDS = re.compile(
    r"(?i)\b("
    r"dealer\s*price|dealer\s*prices|dealer\s*rate|dealer\s*margin|dealer\s*models?|"
    r"dealer\s*enquiry|dealer\s*inquiry|become\s*a?\s*dealer|dealership|"
    r"wholesale\s*price|distributor\s*price|agency\s*price|"
    r"dealer\s*list|dealer\s*catalog|dealer\s*details"
    r")\b",
)

_DEALER_ENQUIRY_PHRASES = re.compile(
    r"(?i)(may\s+i\s+know\s+(?:the\s+)?dealer|"
    r"dealer\s+price\s+details|want\s+to\s+become\s+(?:a\s+)?dealer|"
    r"dealership\s+enquiry|wholesale\s+for\s+machines)",
)

_MALAYALAM_DEALER = re.compile(
    r"ഡീലർ|ഡീല്ലർ|വിതരണ|ഏജന്റ്\s*വില|ഡീലർ\s*വില",
)


def dealer_enquiry_text_signal(*texts: str | None) -> bool:
    blob = " ".join((t or "").strip() for t in texts if (t or "").strip())
    if not blob:
        return False
    if _MALAYALAM_DEALER.search(blob):
        return True
    return bool(_DEALER_ENQUIRY_KEYWORDS.search(blob) or _DEALER_ENQUIRY_PHRASES.search(blob))


def is_dealer_enquiry_summary(summary: dict[str, Any] | None, *, fallback_text: str = "") -> bool:
    if not summary:
        return dealer_enquiry_text_signal(fallback_text)
    subtype = (summary.get("enquiry_subtype") or "").strip().lower()
    if subtype in {"dealer enquiry", "dealer_enquiry"}:
        return True
    blob = " ".join(
        str(summary.get(k) or "")
        for k in (
            "topic",
            "customer_intent",
            "current_issue",
            "recommended_action",
            "quantity_requirement",
        )
    )
    products = " ".join(str(p) for p in (summary.get("product_interest") or []))
    info = " ".join(str(x) for x in (summary.get("important_information") or []))
    cat = (summary.get("service_category") or "").strip().lower()
    if cat and cat not in {"sales", ""}:
        # Cloud/service chats mentioning "dealer" in passing — require explicit price/enquiry signal.
        return dealer_enquiry_text_signal(blob, products, info, fallback_text)
    return dealer_enquiry_text_signal(blob, products, info, fallback_text)


def _merge_tags(existing: str, add: str) -> str:
    parts = [p.strip() for p in (existing or "").split(",") if p.strip()]
    seen = {p.casefold() for p in parts}
    if add.strip() and add.casefold() not in seen:
        parts.append(add.strip())
    return ", ".join(parts)


def _note_line(existing: str, line: str) -> str:
    line = (line or "").strip()
    if not line:
        return (existing or "").strip()
    base = (existing or "").strip()
    if line in base:
        return base
    return f"{base}\n{line}".strip() if base else line


async def _find_dealer_by_phone(db: AsyncSession, phone: str) -> Dealer | None:
    from app.inbox import _find_dealer_by_phone

    return await _find_dealer_by_phone(db, phone)


async def _find_lead_by_phone(db: AsyncSession, phone: str) -> Lead | None:
    from app.inbox import _find_lead_by_phone

    return await _find_lead_by_phone(db, phone)


def _lead_phone_storage(phone: str) -> str:
    key = normalize_in_mobile(phone)
    if not key:
        return (phone or "").strip()
    return to_evolution_number(key) or f"91{key}"


def _state_from_summary(summary: dict[str, Any] | None, conv: WhatsAppConversation) -> str:
    region = (getattr(conv, "region", None) or "").strip()
    if region:
        return region[:80]
    if not summary:
        return ""
    for item in summary.get("important_information") or []:
        text = str(item)
        m = re.search(
            r"(?i)\b(?:from|dealer\s+from|state)\s+([A-Za-z][A-Za-z\s]{1,40})\b",
            text,
        )
        if m:
            return m.group(1).strip()[:80]
        for place in ("Nellore", "Kerala", "Tamil Nadu", "Karnataka", "Andhra Pradesh"):
            if place.lower() in text.lower():
                return place
    return ""


def _product_from_summary(summary: dict[str, Any] | None) -> str:
    if not summary:
        return ""
    items = [str(x).strip() for x in (summary.get("product_interest") or []) if str(x).strip()]
    if items:
        return ", ".join(items)[:160]
    topic = (summary.get("topic") or "").strip()
    if re.search(r"(?i)platform|weighing\s*scale|bench|label", topic):
        return "Electronic Weighing Scales"
    return ""


async def upsert_lead_for_dealer_enquiry(
    db: AsyncSession,
    conv: WhatsAppConversation,
    summary: dict[str, Any] | None,
) -> Lead | None:
    """Create or enrich CRM Lead for dealer-price chats (skip existing dealers)."""
    if conv.dealer_id:
        return None
    if await _find_dealer_by_phone(db, conv.customer_phone):
        return None

    phone_key = normalize_in_mobile(conv.customer_phone)
    if not phone_key:
        return None

    lead = await _find_lead_by_phone(db, conv.customer_phone)
    name = (conv.customer_name or "").strip() or "WhatsApp Lead"
    state = _state_from_summary(summary, conv)
    product = _product_from_summary(summary)
    note = "Dealer enquiry — dealer / wholesale price request (WhatsApp Care or sales line)."
    if summary:
        topic = (summary.get("topic") or "").strip()
        if topic:
            note = f"Dealer enquiry: {topic[:240]}"

    if lead is None:
        lead = Lead(
            name=name[:160],
            phone=_lead_phone_storage(conv.customer_phone),
            company=name[:160] if name != "WhatsApp Lead" else "",
            source="WhatsApp",
            status="new",
            priority="high",
            state=state,
            product_category=product,
            tags=_merge_tags("", "Dealer enquiry"),
            notes=note[:4000],
        )
        db.add(lead)
        await db.flush()
        return lead

    changed = False
    if not (lead.name or "").strip() or lead.name == "WhatsApp Lead":
        if name and name != "WhatsApp Lead":
            lead.name = name[:160]
            changed = True
    if not (lead.phone or "").strip():
        lead.phone = _lead_phone_storage(conv.customer_phone)
        changed = True
    if state and not (lead.state or "").strip():
        lead.state = state[:80]
        changed = True
    if product and not (lead.product_category or "").strip():
        lead.product_category = product[:160]
        changed = True
    new_tags = _merge_tags(lead.tags or "", "Dealer enquiry")
    if new_tags != (lead.tags or ""):
        lead.tags = new_tags
        changed = True
    new_notes = _note_line(lead.notes or "", note)
    if new_notes != (lead.notes or ""):
        lead.notes = new_notes[:4000]
        changed = True
    if (lead.priority or "").lower() in {"", "medium", "low"}:
        lead.priority = "high"
        changed = True
    if changed:
        await db.flush()
    return lead


async def first_dealer_enquiry_inbound_at(
    db: AsyncSession,
    conv: WhatsAppConversation,
    messages: list[WhatsAppMessage],
) -> datetime | None:
    for m in messages:
        if m.direction != "in":
            continue
        from app.whatsapp_outline import message_text_for_outline

        text = message_text_for_outline(m)
        if dealer_enquiry_text_signal(text, conv.last_message_preview):
            return m.created_at
    key = normalize_in_mobile(conv.customer_phone)
    if not key:
        return conv.created_at
    in_rows = (
        await db.execute(
            select(WhatsAppMessage.created_at, WhatsAppMessage.body, WhatsAppMessage.transcript)
            .join(WhatsAppConversation, WhatsAppMessage.conversation_id == WhatsAppConversation.id)
            .where(
                _normalized_phone_sql(WhatsAppConversation.customer_phone) == key,
                WhatsAppMessage.direction == "in",
            )
            .order_by(WhatsAppMessage.created_at.asc())
            .limit(80)
        )
    ).all()
    for ts, body, transcript in in_rows:
        if dealer_enquiry_text_signal(body, transcript):
            return ts
    for ts, _body, _tr in in_rows:
        if ts:
            return ts
    return conv.created_at


async def sales_attendance_cross_instance(
    db: AsyncSession,
    conv: WhatsAppConversation,
    *,
    since: datetime | None,
) -> dict[str, Any]:
    """Outbound from Evolution (sales) lines to this phone after the enquiry."""
    key = normalize_in_mobile(conv.customer_phone)
    if not key:
        return {"sales_attended": "no", "sales_attended_by": "", "sales_attended_at": None}

    since_utc = since
    if since_utc and since_utc.tzinfo is None:
        since_utc = since_utc.replace(tzinfo=timezone.utc)

    rows = (
        await db.execute(
            select(
                WhatsAppMessage.created_at,
                WhatsAppMessage.sender_name,
                WhatsAppMessage.body,
                WhatsAppInstance.display_name,
                WhatsAppInstance.employee_name,
                WhatsAppInstance.provider,
            )
            .join(WhatsAppConversation, WhatsAppMessage.conversation_id == WhatsAppConversation.id)
            .join(WhatsAppInstance, WhatsAppMessage.whatsapp_instance_id == WhatsAppInstance.id)
            .where(
                _normalized_phone_sql(WhatsAppConversation.customer_phone) == key,
                WhatsAppMessage.direction == "out",
            )
            .order_by(WhatsAppMessage.created_at.asc())
            .limit(300)
        )
    ).all()

    for created_at, sender_name, body, display_name, employee_name, provider in rows:
        prov = (provider or "").strip().lower()
        if prov != PROVIDER_EVOLUTION:
            continue
        if since_utc and created_at and created_at.replace(tzinfo=timezone.utc) < since_utc:
            continue
        agent = (sender_name or employee_name or display_name or "").strip()
        at_iso = created_at.isoformat() if created_at else ""
        return {
            "sales_attended": "yes",
            "sales_attended_by": agent,
            "sales_attended_at": at_iso,
            "sales_attendance_detail": f"{agent or 'Sales'} ({display_name or 'Evolution'})",
        }

    return {"sales_attended": "no", "sales_attended_by": "", "sales_attended_at": None}


def enrich_dealer_enquiry_recommended_action(
    summary: dict[str, Any],
    *,
    attended: bool,
) -> None:
    if attended:
        return
    status = (summary.get("conversation_status") or "").lower()
    issue = (summary.get("current_issue") or "").lower()
    no_callback = any(
        x in issue
        for x in (
            "no call",
            "nobody",
            "no body",
            "not called",
            "waiting",
            "did not call",
        )
    ) or any(
        x in " ".join(str(i).lower() for i in (summary.get("important_information") or []))
        for x in ("no call", "not called", "waiting", "nobody")
    )
    if not no_callback and status not in {"awaiting reply", "new enquiry", "quotation required"}:
        return
    lang = (summary.get("language") or "").strip().lower()
    if lang == "ml":
        summary["recommended_action"] = (
            "ഉടൻ ഫോൺ വിളിച്ച് ഡീലർ വില വിവരങ്ങൾ അയയ്ക്കുക; "
            "ഏതെങ്കിലും സെയിൽസ് ലൈൻ (Biju / Visakh / Shibin) കോൺടാക്റ്റ് സ്ഥിരീകരിക്കുക."
        )
    else:
        summary["recommended_action"] = (
            "Call the customer immediately with dealer price list; assign/follow up from a "
            "sales WhatsApp line (Biju, Visakh, or Shibin) — Care handoff alone does not count."
        )
    if not (summary.get("followup_priority") or "").strip():
        summary["followup_priority"] = "High"


async def apply_dealer_enquiry_actions(
    db: AsyncSession,
    conv: WhatsAppConversation,
    summary: dict[str, Any],
    messages: list[WhatsAppMessage],
) -> dict[str, Any]:
    """Classify dealer enquiry, upsert Lead, attach cross-instance sales attendance."""
    fallback = conv.last_message_preview or ""
    if not is_dealer_enquiry_summary(summary, fallback_text=fallback):
        return summary

    summary = dict(summary)
    summary["enquiry_subtype"] = "dealer enquiry"
    cat = (summary.get("service_category") or "").strip().lower()
    if not cat or cat == "support":
        summary["service_category"] = "sales"

    await upsert_lead_for_dealer_enquiry(db, conv, summary)

    since = await first_dealer_enquiry_inbound_at(db, conv, messages)
    att = await sales_attendance_cross_instance(db, conv, since=since)
    summary.update(att)

    attended = (summary.get("sales_attended") or "").strip().lower() == "yes"
    detail = att.get("sales_attendance_detail") or ""
    info = list(summary.get("important_information") or [])
    if attended and detail:
        line = f"Sales attended: yes — {detail}"
        if since:
            line += f" (after enquiry)"
    else:
        line = "Sales attended: no — no outbound from Biju/Visakh/Shibin Evolution lines after enquiry"
    if line.casefold() not in " ".join(info).casefold():
        info.append(line)
    summary["important_information"] = info

    enrich_dealer_enquiry_recommended_action(summary, attended=attended)
    if not (summary.get("followup_priority") or "").strip():
        summary["followup_priority"] = "High"

    return summary


def patch_outline_json(conv: WhatsAppConversation, summary: dict[str, Any]) -> None:
    conv.ai_outline = json.dumps(summary, ensure_ascii=False)