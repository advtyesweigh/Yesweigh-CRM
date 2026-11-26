"""Chat-driven sales-order cycle for the Intelligence tiles.

Stages are mutually exclusive:

- sales_order: dealer confirmed the order; our sales-order PDF is not sent yet
- waiting: staff already sent that PDF; payment is not confirmed
- paid: payment against that sales order is confirmed

Zoho invoices are used only to corroborate a chat that already has a sales-order
PDF, and only for invoices dated around that send. Lifetime invoice totals are
not a tile. Stamping, service, and commitments stay separate lists.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.whatsapp_send_guards import load_org_staff_phone_keys, phone_key
from app.models import Dealer, WhatsAppConversation, WhatsAppMessage, ZohoSale
from app.sales_amount import is_countable_invoice
from app.whatsapp_chat_types import dm_inbox_filter, newsletter_list_filter
from app.whatsapp_service_meta import parse_outline_lite

ORG = "yesweigh"
OPERATIONAL_DAYS = 120
ZOHO_WINDOW_BEFORE = timedelta(days=2)
ZOHO_WINDOW_AFTER = timedelta(days=75)
_PATCHABLE_STATUSES = {
    "",
    "New Enquiry",
    "Awaiting Reply",
    "Interested",
    "Quotation Required",
    "Order Confirmed",
    "Payment Pending",
    "Paid",
}
_STAGE_STATUS = {
    "sales_order": "Order Confirmed",
    "waiting": "Payment Pending",
    "paid": "Paid",
}

_SO_FILE = re.compile(r"(?i)(?:^|[^A-Za-z0-9])SO[-_ ]?\d{4,}(?:\.pdf)?\b")
_SO_WORDS = re.compile(r"(?i)sales\s*order")
_ORDER_CONFIRM = re.compile(
    r"(?i)("
    r"order\s+confirmed|"
    r"confirmed\s+(?:the\s+|my\s+|your\s+)?order|"
    r"order\s+is\s+confirmed|"
    r"(?:please\s+)?(?:send|share)\s+(?:me\s+|us\s+|the\s+)?sales\s+order|"
    r"sales\s+order\s+(?:please|pdf|copy)|"
    r"waiting\s+for\s+(?:the\s+)?sales\s+order"
    r")"
    r"|ഓർഡർ\s*കൺഫേം|സെയിൽസ്\s*ഓർഡർ"
)
_RENEW = re.compile(r"(?i)\b(renew|subscription|per\s+scale)\b|ലൈസൻസ്|പുതുക്ക")
_PAY_ASK = re.compile(
    r"(?i)("
    r"please\s+pay|kindly\s+(?:pay|make\s+the\s+payment)|"
    r"payment\s+pending|awaiting\s+payment|need\s+payment|"
    r"payment\s+to\s+be|pay\s+the\s+balance|"
    r"will\s+(?:pay|transfer)|to\s+confirm\s+(?:the\s+)?order"
    r")"
)
_RECEIPT = re.compile(
    r"(?i)("
    r"paid\s+using|"
    r"payment\s+(?:done|received|successful|completed|credited)|"
    r"(?:i|we)\s+(?:have\s+)?paid\b|"
    r"amount\s+(?:has\s+been\s+)?(?:transferred|credited)|"
    r"received\s+(?:the\s+)?payment|"
    r"thanks\s+for\s+(?:the\s+)?payment|"
    r"google\s+pay|phonepe|\bgpay\b|\bpaytm\b|\butr\b"
    r")"
    r"|പേയ്മെന്റ്\s*നടത്തി|അടച്ചു\s*കഴിഞ്ഞ|പണം\s*അടച്ച"
)
_STAFF_RECEIVED = re.compile(
    r"(?i)(payment\s+received|received\s+(?:the\s+)?payment|thanks\s+for\s+(?:the\s+)?payment)"
)
_BANK = re.compile(r"(?i)(?:debited|credited)\s+from|\bacbal\b|upi\s*ref")
_HARD_STATUSES = {"Order Confirmed"}


@dataclass(frozen=True)
class OrderCycle:
    stage: str
    summary: str
    so_label: str = ""


@dataclass(frozen=True)
class CycleHit:
    conversation_id: int
    public_id: str
    stage: str
    summary: str


def so_pdf_exists_clause():
    """Outbound sales-order PDF (SO-#####) already shared on this DM."""
    return exists(
        select(WhatsAppMessage.id).where(
            WhatsAppMessage.conversation_id == WhatsAppConversation.id,
            WhatsAppMessage.direction == "out",
            or_(
                WhatsAppMessage.media_name.op("~*")(r"SO[-_ ]?[0-9]{4,}"),
                WhatsAppMessage.body.op("~*")(r"SO[-_ ]?[0-9]{4,}"),
            ),
        )
    )


def _msg_blob(message: dict[str, Any]) -> str:
    parts = [
        str(message.get("body") or ""),
        str(message.get("media_name") or ""),
        str(message.get("transcript") or ""),
        str(message.get("text") or ""),
    ]
    return " ".join(part.strip() for part in parts if part and part.strip())


def _outline_text(outline: dict[str, Any] | None) -> str:
    data = outline or {}
    bits = [
        str(data.get(key) or "")
        for key in ("topic", "customer_intent", "current_issue", "recommended_action", "conversation_status")
    ]
    for key in ("important_information", "product_interest"):
        for item in data.get(key) or []:
            bits.append(str(item or ""))
    return " ".join(bit.strip() for bit in bits if bit and bit.strip())


def _is_so_message(message: dict[str, Any]) -> bool:
    if (message.get("direction") or "").lower() not in {"out", "outbound"}:
        return False
    blob = _msg_blob(message)
    if _SO_FILE.search(blob):
        return True
    media = (message.get("media_type") or "").lower()
    if _SO_WORDS.search(blob) and ("pdf" in blob.lower() or "document" in media or "document" in blob.lower()):
        return True
    return False


def _so_label(message: dict[str, Any]) -> str:
    match = _SO_FILE.search(_msg_blob(message))
    if not match:
        return "Sales order PDF"
    token = match.group(0).upper().replace("_", "-").replace(" ", "")
    token = re.sub(r"^[^A-Z0-9]+", "", token).strip()
    if not token.startswith("SO"):
        token = f"SO-{token}"
    if token.endswith(".PDF"):
        token = token[:-4]
    return token


def _is_payment_done(message: dict[str, Any]) -> bool:
    blob = _msg_blob(message)
    if not blob.strip() or _RENEW.search(blob):
        return False
    direction = (message.get("direction") or "").lower()
    receipt = bool(_RECEIPT.search(blob))
    if _PAY_ASK.search(blob) and not receipt:
        return False
    inbound = direction in {"in", "inbound"}
    outbound = direction in {"out", "outbound"}
    if receipt and inbound:
        return True
    if receipt and outbound and _STAFF_RECEIVED.search(blob):
        return True
    if inbound and _BANK.search(blob) and not _PAY_ASK.search(blob):
        return True
    return False


def _payment_state_after(messages: list[dict[str, Any]], index: int) -> str:
    """Last payment signal after the sales-order PDF: paid, open, or unknown."""
    state = "unknown"
    for message in messages[index + 1 :]:
        blob = _msg_blob(message)
        if _is_payment_done(message):
            state = "paid"
        elif _PAY_ASK.search(blob) and not _RENEW.search(blob):
            state = "open"
    return state


def _hard_confirm(messages: list[dict[str, Any]], outline: dict[str, Any] | None) -> bool:
    status = str((outline or {}).get("conversation_status") or "").strip()
    if status in _HARD_STATUSES:
        return True
    if _ORDER_CONFIRM.search(_outline_text(outline)):
        return True
    return any(_ORDER_CONFIRM.search(_msg_blob(message)) for message in messages)


def classify_order_cycle(
    messages: list[dict[str, Any]] | None,
    outline: dict[str, Any] | None = None,
    *,
    zoho_settled: bool | None = None,
) -> OrderCycle | None:
    """One stage, or None when this chat is not in the order-payment cycle."""
    rows = list(messages or [])
    so_indexes = [index for index, message in enumerate(rows) if _is_so_message(message)]
    if so_indexes:
        last = so_indexes[-1]
        label = _so_label(rows[last])
        state = _payment_state_after(rows, last)
        if state == "paid" or (state != "open" and zoho_settled is True):
            how = "payment confirmed in chat" if state == "paid" else "Zoho invoice settled for this order"
            return OrderCycle("paid", f"{label} · {how}"[:240], label)
        return OrderCycle("waiting", f"{label} sent · waiting for payment"[:240], label)

    status = str((outline or {}).get("conversation_status") or "").strip()
    if status == "Renewal Required":
        return None
    outline_blob = _outline_text(outline)
    message_blob = " ".join(_msg_blob(message) for message in rows)
    combined = f"{outline_blob}\n{message_blob}"
    if _RENEW.search(combined) and not _hard_confirm(rows, outline):
        return None
    if not _hard_confirm(rows, outline):
        return None
    return OrderCycle("sales_order", "Order confirmed · sales order PDF not sent yet", "")


def enquiry_blocked_by_order_cycle(
    messages: list[dict[str, Any]] | None,
    outline: dict[str, Any] | None = None,
) -> bool:
    """Confirmed orders leave Enquiry. Price talk before confirmation stays."""
    return classify_order_cycle(messages, outline) is not None


def zoho_settled_for_cycle(invoices: list[Any], sent_on: date) -> bool | None:
    """True when a linked invoice in the SO window is settled and none are open.

    Invoices outside the window (older lifetime paid rows) are ignored.
    Returns None when Zoho has nothing in the window.
    """
    start = sent_on - ZOHO_WINDOW_BEFORE
    end = sent_on + ZOHO_WINDOW_AFTER
    relevant: list[tuple[bool, bool]] = []
    for row in invoices:
        raw_date = str(getattr(row, "date", "") or "")
        parsed = _parse_sale_date(raw_date)
        if parsed is None or parsed < start or parsed > end:
            continue
        if not is_countable_invoice(getattr(row, "document_type", ""), getattr(row, "status", "")):
            continue
        balance = getattr(row, "balance", None)
        try:
            open_balance = balance is not None and float(balance) > 0
        except (TypeError, ValueError):
            open_balance = False
        status = str(getattr(row, "status", "") or "").strip().lower()
        settled = (not open_balance) and (balance == 0 or status in {"paid", "closed", ""})
        if open_balance:
            settled = False
        relevant.append((open_balance, settled))
    if not relevant:
        return None
    if any(open_balance for open_balance, _settled in relevant):
        return False
    if any(settled for _open, settled in relevant):
        return True
    return None


def _parse_sale_date(raw: str) -> date | None:
    text = (raw or "").strip()[:10]
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def status_patch_for_stage(summary: dict[str, Any] | None, stage: str) -> dict[str, Any] | None:
    """Outline status fix for the order cycle. Leaves service/renewal labels alone."""
    data = dict(summary or {})
    current = str(data.get("conversation_status") or "").strip()
    if current not in _PATCHABLE_STATUSES:
        return None
    target = _STAGE_STATUS.get(stage)
    if not target or current == target:
        return None
    data["conversation_status"] = target
    return data


def _outline_order_hit():
    return or_(
        WhatsAppConversation.ai_outline.ilike("%Order Confirmed%"),
        WhatsAppConversation.ai_outline.ilike("%Payment Pending%"),
        WhatsAppConversation.ai_outline.ilike('%"conversation_status": "Paid"%'),
    )


def _candidate_clause(cutoff: datetime, message_conv_ids: set[int] | None = None):
    """Prefer precomputed message-signal IDs over a correlated EXISTS (much faster)."""
    outline_hit = _outline_order_hit()
    if message_conv_ids:
        signal = or_(outline_hit, WhatsAppConversation.id.in_(message_conv_ids))
    else:
        message_hit = exists(
            select(WhatsAppMessage.id).where(
                WhatsAppMessage.conversation_id == WhatsAppConversation.id,
                or_(
                    WhatsAppMessage.media_name.op("~*")(r"SO[-_ ]?[0-9]{4,}"),
                    WhatsAppMessage.body.op("~*")(
                        r"SO[-_ ]?[0-9]{4,}|sales[[:space:]]*order|order[[:space:]]+confirmed|confirmed[[:space:]]+the[[:space:]]+order"
                    ),
                    WhatsAppMessage.transcript.op("~*")(
                        r"sales[[:space:]]*order|order[[:space:]]+confirmed|സെയിൽസ്[[:space:]]*ഓർഡർ|ഓർഡർ[[:space:]]*കൺഫേം"
                    ),
                ),
            )
        )
        signal = or_(message_hit, outline_hit)
    return (
        WhatsAppConversation.organization_id == ORG,
        newsletter_list_filter(),
        dm_inbox_filter(),
        ~WhatsAppConversation.customer_jid.ilike("%@broadcast"),
        ~WhatsAppConversation.customer_jid.ilike("status@%"),
        WhatsAppConversation.last_message_at.is_not(None),
        WhatsAppConversation.last_message_at >= cutoff,
        signal,
    )


async def _order_message_signal_ids(db: AsyncSession, cutoff: datetime) -> set[int]:
    """Conversation IDs with SO / order-confirm language in messages (one scan, not per-row EXISTS)."""
    rows = (
        await db.execute(
            select(WhatsAppMessage.conversation_id)
            .join(WhatsAppConversation, WhatsAppConversation.id == WhatsAppMessage.conversation_id)
            .where(
                WhatsAppConversation.organization_id == ORG,
                newsletter_list_filter(),
                dm_inbox_filter(),
                WhatsAppConversation.last_message_at.is_not(None),
                WhatsAppConversation.last_message_at >= cutoff,
                or_(
                    WhatsAppMessage.media_name.op("~*")(r"SO[-_ ]?[0-9]{4,}"),
                    WhatsAppMessage.body.op("~*")(
                        r"SO[-_ ]?[0-9]{4,}|sales[[:space:]]*order|order[[:space:]]+confirmed|confirmed[[:space:]]+the[[:space:]]+order"
                    ),
                    WhatsAppMessage.transcript.op("~*")(
                        r"sales[[:space:]]*order|order[[:space:]]+confirmed|സെയിൽസ്[[:space:]]*ഓർഡർ|ഓർഡർ[[:space:]]*കൺഫേം"
                    ),
                ),
            )
            .group_by(WhatsAppMessage.conversation_id)
        )
    ).scalars().all()
    return {int(i) for i in rows}


async def _message_rows(db: AsyncSession, conv_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not conv_ids:
        return {}
    ranked_rn = func.row_number().over(
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
            WhatsAppMessage.created_at.label("created_at"),
            WhatsAppMessage.id.label("id"),
            ranked_rn,
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
                ranked.c.created_at,
                ranked.c.id,
            )
            .where(ranked.c.rn <= 80)
            .order_by(ranked.c.conversation_id.asc(), ranked.c.id.asc())
        )
    ).all()
    so_extra = (
        await db.execute(
            select(
                WhatsAppMessage.conversation_id,
                WhatsAppMessage.direction,
                WhatsAppMessage.body,
                WhatsAppMessage.transcript,
                WhatsAppMessage.media_type,
                WhatsAppMessage.media_name,
                WhatsAppMessage.created_at,
                WhatsAppMessage.id,
            ).where(
                WhatsAppMessage.conversation_id.in_(conv_ids),
                WhatsAppMessage.direction == "out",
                or_(
                    WhatsAppMessage.media_name.op("~*")(r"SO[-_ ]?[0-9]{4,}"),
                    WhatsAppMessage.body.op("~*")(r"SO[-_ ]?[0-9]{4,}"),
                ),
            )
        )
    ).all()
    pay_extra = (
        await db.execute(
            select(
                WhatsAppMessage.conversation_id,
                WhatsAppMessage.direction,
                WhatsAppMessage.body,
                WhatsAppMessage.transcript,
                WhatsAppMessage.media_type,
                WhatsAppMessage.media_name,
                WhatsAppMessage.created_at,
                WhatsAppMessage.id,
            ).where(
                WhatsAppMessage.conversation_id.in_(conv_ids),
                or_(
                    WhatsAppMessage.body.op("~*")(
                        r"(paid[[:space:]]+using|phonepe|google[[:space:]]*pay|payment[[:space:]]+received|പേയ്മെന്റ്[[:space:]]*നടത്തി)"
                    ),
                    WhatsAppMessage.transcript.op("~*")(
                        r"(paid[[:space:]]+using|phonepe|google[[:space:]]*pay|പേയ്മെന്റ്[[:space:]]*നടത്തി|പണം[[:space:]]*അടച്ച)"
                    ),
                ),
            )
        )
    ).all()
    grouped: dict[int, dict[int, dict[str, Any]]] = {}
    for cid, direction, body, transcript, media_type, media_name, created_at, mid in list(fetched) + list(so_extra) + list(pay_extra):
        grouped.setdefault(int(cid), {})[int(mid)] = {
            "direction": direction or "",
            "body": body or "",
            "transcript": transcript or "",
            "media_type": media_type or "",
            "media_name": media_name or "",
            "at": created_at,
            "id": int(mid),
        }
    return {
        cid: [row for _mid, row in sorted(rows.items())]
        for cid, rows in grouped.items()
    }


def _sent_on(messages: list[dict[str, Any]]) -> date | None:
    for message in reversed(messages):
        if not _is_so_message(message):
            continue
        moment = message.get("at")
        if isinstance(moment, datetime):
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.date()
    return None


async def _zoho_settled_by_dealer(
    db: AsyncSession,
    waiting: list[tuple[WhatsAppConversation, list[dict[str, Any]]]],
) -> dict[int, bool | None]:
    dealer_ids = {int(conv.dealer_id) for conv, _messages in waiting if conv.dealer_id}
    if not dealer_ids:
        return {}
    dealers = (
        await db.execute(select(Dealer.id, Dealer.zoho_contact_id).where(Dealer.id.in_(dealer_ids)))
    ).all()
    zoho_by_dealer = {int(dealer_id): str(zoho_id) for dealer_id, zoho_id in dealers if zoho_id}
    customer_ids = set(zoho_by_dealer.values())
    if not customer_ids:
        return {}
    invoices = (
        await db.execute(select(ZohoSale).where(ZohoSale.customer_id.in_(customer_ids)))
    ).scalars().all()
    by_customer: dict[str, list[ZohoSale]] = {}
    for invoice in invoices:
        by_customer.setdefault(str(invoice.customer_id or ""), []).append(invoice)
    out: dict[int, bool | None] = {}
    for conv, messages in waiting:
        zoho_id = zoho_by_dealer.get(int(conv.dealer_id or 0))
        sent_on = _sent_on(messages)
        if not zoho_id or sent_on is None:
            continue
        out[int(conv.id)] = zoho_settled_for_cycle(by_customer.get(zoho_id, []), sent_on)
    return out


async def load_order_cycles(db: AsyncSession) -> dict[str, list[CycleHit]]:
    """Operational chats in the last 120 days, split into the three order stages."""
    from app.intel.cache import get_order_cycles_cache, set_order_cycles_cache

    cached = get_order_cycles_cache()
    if cached is not None:
        return cached

    cutoff = datetime.now(timezone.utc) - timedelta(days=OPERATIONAL_DAYS)
    msg_ids = await _order_message_signal_ids(db, cutoff)
    convs = (
        await db.execute(
            select(WhatsAppConversation)
            .where(*_candidate_clause(cutoff, msg_ids))
            .order_by(WhatsAppConversation.last_message_at.desc().nulls_last())
        )
    ).scalars().all()
    staff_phones = await load_org_staff_phone_keys(db)
    kept = []
    for conv in convs:
        key = phone_key(conv.customer_phone or "")
        if key and key in staff_phones:
            continue
        kept.append(conv)
    messages_by_id = await _message_rows(db, [int(conv.id) for conv in kept])
    preliminary: list[tuple[WhatsAppConversation, list[dict[str, Any]], OrderCycle]] = []
    waiting_for_zoho: list[tuple[WhatsAppConversation, list[dict[str, Any]]]] = []
    outlines: dict[int, dict[str, Any]] = {}
    for conv in kept:
        outline = parse_outline_lite(conv.ai_outline or "")
        outlines[int(conv.id)] = outline
        messages = messages_by_id.get(int(conv.id), [])
        cycle = classify_order_cycle(messages, outline)
        if cycle is None:
            continue
        preliminary.append((conv, messages, cycle))
        if cycle.stage == "waiting":
            waiting_for_zoho.append((conv, messages))
    zoho_flags = await _zoho_settled_by_dealer(db, waiting_for_zoho)
    grouped: dict[str, list[CycleHit]] = {"sales_order": [], "waiting": [], "paid": []}
    for conv, messages, cycle in preliminary:
        if cycle.stage == "waiting":
            flag = zoho_flags.get(int(conv.id))
            revised = classify_order_cycle(
                messages,
                outlines.get(int(conv.id)) or parse_outline_lite(conv.ai_outline or ""),
                zoho_settled=flag,
            )
            if revised is not None:
                cycle = revised
        grouped[cycle.stage].append(
            CycleHit(
                conversation_id=int(conv.id),
                public_id=conv.public_id,
                stage=cycle.stage,
                summary=cycle.summary,
            )
        )
    set_order_cycles_cache(grouped)
    return grouped


async def reclassify_order_outlines(db: AsyncSession) -> dict[str, int]:
    """Write Order Confirmed / Payment Pending / Paid where the chat cycle is clear.

    Does not change service, renewal, or resolved labels, and does not call Gemini.
    """
    cycles = await load_order_cycles(db)
    hits = [hit for rows in cycles.values() for hit in rows]
    if not hits:
        return {"updated": 0, **{key: len(rows) for key, rows in cycles.items()}}
    by_id = {hit.conversation_id: hit for hit in hits}
    convs = (
        await db.execute(select(WhatsAppConversation).where(WhatsAppConversation.id.in_(list(by_id))))
    ).scalars().all()
    updated = 0
    for conv in convs:
        hit = by_id.get(int(conv.id))
        if hit is None:
            continue
        raw = (conv.ai_outline or "").strip()
        if not raw.startswith("{"):
            summary: dict[str, Any] = {}
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            summary = parsed
        patched = status_patch_for_stage(summary, hit.stage)
        if patched is None:
            continue
        conv.ai_outline = json.dumps(patched, ensure_ascii=False)
        updated += 1
    counts = {key: len(rows) for key, rows in cycles.items()}
    counts["updated"] = updated
    return counts


async def recent_message_text(db: AsyncSession, conv_ids: list[int], *, per: int = 30) -> dict[int, str]:
    """Plain text of recent messages, for stamping/service checks outside the order cycle."""
    rows = await _message_rows(db, conv_ids)
    out: dict[int, str] = {}
    for cid, messages in rows.items():
        tail = messages[-per:]
        out[cid] = "\n".join(_msg_blob(message) for message in tail if _msg_blob(message))
    return out
