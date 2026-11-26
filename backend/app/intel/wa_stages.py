"""WhatsApp-signal stage classification for Intelligence funnel tiles.

Sales order / Waiting / Paid / Stamping are AI-read chat findings only.
Zoho invoices are never used for these stage lists or tile totals.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Select, and_, func, literal_column, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.models import WhatsAppConversation, WhatsAppMessage
from app.whatsapp_chat_types import dm_inbox_filter, newsletter_list_filter
from app.whatsapp_service_meta import stamping_signal

ORG = "yesweigh"

# Dealer confirmed / asked for SO work — spare parts, freight add, "S" / sales order.
_SALES_ORDER_MSG = re.compile(
    r"(?i)("
    r"sales\s*order|order\s*confirm(?:ed|ation)?|place(?:d)?\s+the\s+order|"
    r"spare\s*parts?|printer\s*head|display\s*board|add\s+freight|freight\s+add|"
    r"സെയിൽസ്\s*ഓർഡർ|ഫ്രൈറ്റ്|എസ്\s*ഉണ്ടാക്ക|ഓർഡർ\s*ഉണ്ടാക്ക"
    r")"
)

# Staff sent SO PDF / document on WA.
_SO_PDF_OUT = re.compile(
    r"(?i)(^|[^a-z0-9])so[-_ ]?\d{3,}|\bsales\s*order\b.*\.pdf|media_name.*so[-_ ]?\d",
)

# Payment proof shared with an image (UPI / PhonePe / GPay style captions).
_PAYMENT_PROOF = re.compile(
    r"(?i)("
    r"paid\s+using|payment\s+via\s+upi|sent\s+you\s+payment|"
    r"phonepe|google\s*pay|\bgpay\b|paytm|super\.money|"
    r"ഫോൺപേ|യു\.?\s*പി\.?\s*ഐ|പേയ്മെന്റ്\s*നടത്ത|പണം\s*അടച്ച"
    r")"
)
_PAYMENT_SPAM = re.compile(
    r"(?i)(jee\s*\(?main\)?|niftee|nift\s+entrance|national\s+testing|registration\s+form|രജിസ്ട്രേഷൻ)"
)

# OV / stamping / sealed-paper certificate language in messages.
_STAMPING_MSG = re.compile(
    r"(?i)("
    r"(^|[^a-z])ov([^a-z]|$)|ov\s*send|ov\s*please\s*send|insitu\s*ov|"
    r"certificate\s+of\s+verification|verified\s+and\s+stamped|"
    r"verification\s*certificate|legal\s*metrology|stamping|stamped|"
    r"sealed?\s*(?:paper|certificate|copy)|stamp(?:ed|ing)?\s*paper|"
    r"yesgatc\.in/download|"
    r"യ\d{4,6}.{0,20}ov|y0?\d{4,6}.{0,12}ov|"
    r"\b(?:y|yo|yz)\s*0*\d{3,6}(?:\s*[,/;\-]+\s*(?:y|yo|yz)?\s*0*\d{3,6}){0,12}\s*[,.]?\s*ov\b|"
    r"\bgo\s*\d{1,3}\s*/\s*\d{1,3}\b.{0,120}\bov\b|"
    r"ayachilla|അയച്ചില്ല|"
    r"ഓവി|സ്റ്റാമ്പ്|പരിശോധന|ഒവി|സീൽ(?:\s*ചെയ്ത)?(?:\s*പേപ്പർ)?"
    r")"
)

# One-way campaign / mass announcement — keep out of every Intelligence stage.
_BLAST_ONLY = re.compile(
    r"(?i)("
    r"dear\s+customer|container\s+strike|resume\s+dispatching|"
    r"we\s+will\s+resume\s+dispatch|mass\s+notification|broadcast\s+message|"
    r"പ്രിയ\s*ഉപഭോക്താവ്|കണ്ടൈനർ\s*സ്ട്രൈക്ക്|ഡിസ്പാച്ച്"
    r")"
)

STAGE_KEYS = (
    "enquiry",
    "sales_order",
    "waiting",
    "paid",
    "stamping",
    "service",
    "commitments",
    "closed",
)

# Frontend / API stage → funnel/queue key
STAGE_TO_FUNNEL = {
    "enquiry": "enquiry",
    "sales_order": "sales_order",
    "waiting": "invoice_open",
    "paid": "invoice_settled",
    "stamping": "stamping",
    "service": "service",
    "commitments": "commitments",
    "closed": "closed",
}
FUNNEL_TO_STAGE = {
    "enquiry": "enquiry",
    "sales_order": "sales_order",
    "invoice_open": "waiting",
    "invoice_settled": "paid",
    "stamping": "stamping",
    "service": "service",
    "commitments": "commitments",
}


def dm_scope_clauses() -> list[Any]:
    return [
        WhatsAppConversation.organization_id == ORG,
        newsletter_list_filter(),
        dm_inbox_filter(),
        ~WhatsAppConversation.customer_jid.ilike("%@broadcast"),
        ~WhatsAppConversation.customer_jid.ilike("status@%"),
    ]


def _msg_text_expr():
    """body + transcript + media_name for signal matching."""
    return func.concat(
        func.coalesce(WhatsAppMessage.body, ""),
        literal_column("' '"),
        func.coalesce(WhatsAppMessage.transcript, ""),
        literal_column("' '"),
        func.coalesce(WhatsAppMessage.media_name, ""),
    )


def _is_inbound_image() -> ColumnElement[bool]:
    mt = func.lower(func.coalesce(WhatsAppMessage.media_type, ""))
    mn = func.lower(func.coalesce(WhatsAppMessage.media_name, ""))
    return and_(
        WhatsAppMessage.direction == "in",
        or_(
            mt.like("%image%"),
            mt.like("%jpeg%"),
            mt.like("%jpg%"),
            mt.like("%png%"),
            mt.like("%webp%"),
            mn.op("~*")(r"\.(jpe?g|png|webp)$"),
        ),
    )


def payment_screenshot_conv_ids_sql() -> Select[Any]:
    """DMs where dealer paid AND shared a payment screenshot (image + proof caption)."""
    blob = _msg_text_expr()
    return (
        select(WhatsAppMessage.conversation_id)
        .join(WhatsAppConversation, WhatsAppConversation.id == WhatsAppMessage.conversation_id)
        .where(
            *dm_scope_clauses(),
            _is_inbound_image(),
            blob.op("~*")(
                r"(paid\s+using|payment\s+via\s+upi|sent\s+you\s+payment|"
                r"phonepe|google\s*pay|\bgpay\b|paytm|super\.money|"
                r"ഫോൺപേ|യു\.?\s*പി\.?\s*ഐ|പേയ്മെന്റ്\s*നടത്ത|പണം\s*അടച്ച)"
            ),
            ~blob.op("~*")(r"(jee|niftee|nift\s+entrance|national\s+testing|registration\s+form|രജിസ്ട്രേഷൻ)"),
            ~func.coalesce(WhatsAppConversation.customer_name, "").ilike("%national testing%"),
            ~func.coalesce(WhatsAppConversation.customer_name, "").ilike("%nta%"),
        )
        .group_by(WhatsAppMessage.conversation_id)
    )


def so_pdf_out_conv_ids_sql() -> Select[Any]:
    """Staff sent an SO-like PDF/document on WhatsApp."""
    return (
        select(WhatsAppMessage.conversation_id)
        .join(WhatsAppConversation, WhatsAppConversation.id == WhatsAppMessage.conversation_id)
        .where(
            *dm_scope_clauses(),
            WhatsAppMessage.direction == "out",
            or_(
                func.coalesce(WhatsAppMessage.media_name, "").op("~*")(r"^SO[-_ ]?[0-9]{3,}"),
                func.coalesce(WhatsAppMessage.body, "").op("~*")(r"SO[-_ ]?[0-9]{4,}\.pdf"),
                and_(
                    or_(
                        func.lower(func.coalesce(WhatsAppMessage.media_type, "")).like("%document%"),
                        func.lower(func.coalesce(WhatsAppMessage.media_name, "")).like("%.pdf"),
                    ),
                    or_(
                        func.coalesce(WhatsAppMessage.media_name, "").ilike("%sales%order%"),
                        func.coalesce(WhatsAppMessage.body, "").ilike("%sales order%"),
                        func.coalesce(WhatsAppMessage.media_name, "").op("~*")(r"^SO[-_ ]?[0-9]"),
                    ),
                ),
            ),
        )
        .group_by(WhatsAppMessage.conversation_id)
    )


def sales_order_msg_conv_ids_sql() -> Select[Any]:
    """Spare-parts / freight / sales-order document work signals (Meezan-style)."""
    blob = _msg_text_expr()
    return (
        select(WhatsAppMessage.conversation_id)
        .join(WhatsAppConversation, WhatsAppConversation.id == WhatsAppMessage.conversation_id)
        .where(
            *dm_scope_clauses(),
            or_(
                blob.op("~*")(
                    r"(sales\s*order|order\s*confirm|add\s+freight|freight\s+add|"
                    r"spare\s*parts?|printer\s*head|"
                    r"സെയിൽസ്\s*ഓർഡർ|ഫ്രൈറ്റ്|എസ്\s*ഉണ്ടാക്ക|ഓർഡർ\s*ഉണ്ടാക്ക)"
                ),
                and_(
                    blob.op("~*")(r"(spare|printer\s*head|display|ഡിസ്പ്ലേ|പ്രിന്റർ)"),
                    blob.op("~*")(r"(order|ഓർഡർ|എസ്\s*ഉണ്ടാക്ക|invoice|ഇൻവോയ്സ്)"),
                ),
            ),
            WhatsAppConversation.status != "Resolved",
            ~WhatsAppConversation.ai_outline.ilike('%"conversation_status": "Resolved"%'),
            ~WhatsAppConversation.ai_outline.ilike('%"conversation_status": "Not Interested"%'),
        )
        .group_by(WhatsAppMessage.conversation_id)
    )


def stamping_msg_conv_ids_sql() -> Select[Any]:
    """OV / Certificate of Verification / sealed-paper / stamping language in WA messages."""
    blob = _msg_text_expr()
    return (
        select(WhatsAppMessage.conversation_id)
        .join(WhatsAppConversation, WhatsAppConversation.id == WhatsAppMessage.conversation_id)
        .where(
            *dm_scope_clauses(),
            or_(
                blob.op("~*")(
                    r"((^|[^a-z0-9])ov([^a-z0-9]|$)|ov\s*send|ov\s*please\s*send|insitu\s*ov|"
                    r"certificate\s+of\s+verification|verified\s+and\s+stamped|"
                    r"verification\s*certificate|legal\s*metrology|"
                    r"stamp\s*certificate|stamping\s*certificate|"
                    r"sealed?\s*(paper|certificate|copy)|stamp(ed|ing)?\s*paper|"
                    r"yesgatc\.in/download|"
                    r"ayachilla|അയച്ചില്ല|"
                    r"ഓവി|സ്റ്റാമ്പ്|പരിശോധന|ഒവി|സീൽ)"
                ),
                blob.op("~*")(
                    r"\b[yY][oOzZ]?\s*0*\d{3,6}([,/; -]+[yY]?[oOzZ]?\s*0*\d{3,6}){0,24}\s*,?\s*ov\b"
                ),
                blob.op("~*")(r"\bgo\s*\d{1,3}\s*/\s*\d{1,3}\b.{0,160}\bov\b"),
                blob.op("~*")(r"\byzoo\s*\d{3,6}\s*ov\b"),
                func.coalesce(WhatsAppMessage.media_name, "").op("~*")(
                    r"(ov|stamp|verification|certificate).*\.pdf"
                ),
            ),
            WhatsAppConversation.status != "Resolved",
            ~WhatsAppConversation.ai_outline.ilike('%"conversation_status": "Resolved"%'),
        )
        .group_by(WhatsAppMessage.conversation_id)
    )


def blast_only_conv_ids_sql() -> Select[Any]:
    """True blast-only threads (strike / dear-customer), not dealers who also chat.

    A strike notice in last_message_preview alone must NOT exclude an active OV /
    sales thread that also received the All-India blast.
    """
    preview = func.coalesce(WhatsAppConversation.last_message_preview, "")
    blast_text = or_(
        preview.op("~*")(
            r"(dear\s+customer|container\s+strike|resume\s+dispatch|"
            r"പ്രിയ\s*ഉപഭോക്താവ|കണ്ടൈനർ\s*സ്ട്രൈക്ക്|കണ്ടെയ്നർ\s*സമരം)"
        ),
        WhatsAppConversation.ai_outline.ilike("%Dear Customer%"),
        WhatsAppConversation.ai_outline.ilike("%container strike%"),
        WhatsAppConversation.ai_outline.ilike("%resume dispatching%"),
        WhatsAppConversation.ai_outline.ilike("%കണ്ടെയ്നർ സമരം%"),
    )
    # Any inbound customer message → keep the chat in Intelligence stages.
    has_inbound = (
        select(WhatsAppMessage.conversation_id)
        .where(WhatsAppMessage.direction == "in")
        .group_by(WhatsAppMessage.conversation_id)
    )
    # Threads whose only activity is campaign media / strike notice (no inbound).
    campaign_only = (
        select(WhatsAppMessage.conversation_id)
        .join(WhatsAppConversation, WhatsAppConversation.id == WhatsAppMessage.conversation_id)
        .where(
            *dm_scope_clauses(),
            or_(
                func.coalesce(WhatsAppMessage.media_name, "").ilike("campaign_media:%"),
                func.coalesce(WhatsAppMessage.body, "").op("~*")(
                    r"(dear\s+customer|container\s+strike|resume\s+dispatch|"
                    r"പ്രിയ\s*ഉപഭോക്താവ|കണ്ടെയ്നർ\s*സമരം)"
                ),
                func.coalesce(WhatsAppMessage.transcript, "").op("~*")(
                    r"(dear\s+customer|container\s+strike|പ്രിയ\s*ഉപഭോക്താവ|കണ്ടെയ്നർ\s*സമരം)"
                ),
            ),
        )
        .group_by(WhatsAppMessage.conversation_id)
        # Use count().filter (not func.case(..., else_=…); that raises TypeError).
        .having(func.count().filter(WhatsAppMessage.direction == "in") == 0)
    )
    return select(WhatsAppConversation.id).where(
        *dm_scope_clauses(),
        or_(
            and_(blast_text, ~WhatsAppConversation.id.in_(has_inbound)),
            WhatsAppConversation.id.in_(campaign_only),
        ),
    )


async def load_stage_overrides(db: AsyncSession) -> dict[int, str]:
    """conversation_id -> stage_override (including closed)."""
    from app.models import AiThreadInsight

    rows = (
        await db.execute(
            select(AiThreadInsight.conversation_id, AiThreadInsight.stage_override).where(
                AiThreadInsight.stage_override != "",
                AiThreadInsight.stage_override.is_not(None),
            )
        )
    ).all()
    out: dict[int, str] = {}
    for cid, stage in rows:
        key = (stage or "").strip().lower()
        if key in STAGE_KEYS:
            out[int(cid)] = key
    return out


async def public_ids_for_override_stage(
    db: AsyncSession,
    overrides: dict[int, str],
    stage: str,
    *,
    limit: int,
) -> list[str]:
    ids = [cid for cid, st in overrides.items() if st == stage]
    if not ids:
        return []
    return await fetch_public_ids_for_conv_ids(db, ids, order_limit=limit)


async def _blast_only_conv_ids_fast(db: AsyncSession) -> set[int]:
    """Blast exclude IDs via two cheaper queries + short TTL cache.

    Full body/transcript regex over all campaign messages was ~700ms and only found
    a handful of IDs beyond ``campaign_media:%``. Prefer media-name + preview/outline.
    """
    from app.intel.cache import get_blast_cache, set_blast_cache

    cached = get_blast_cache()
    if cached is not None:
        return cached

    preview = func.coalesce(WhatsAppConversation.last_message_preview, "")
    # ILIKE beats ~* for these fixed phrases on a seq scan of conversations.
    blast_text = or_(
        preview.ilike("%dear customer%"),
        preview.ilike("%container strike%"),
        preview.ilike("%resume dispatch%"),
        preview.ilike("%പ്രിയ ഉപഭോക്താവ%"),
        preview.ilike("%കണ്ടൈനർ സ്ട്രൈക്ക്%"),
        preview.ilike("%കണ്ടെയ്നർ സമരം%"),
        WhatsAppConversation.ai_outline.ilike("%Dear Customer%"),
        WhatsAppConversation.ai_outline.ilike("%container strike%"),
        WhatsAppConversation.ai_outline.ilike("%resume dispatching%"),
        WhatsAppConversation.ai_outline.ilike("%കണ്ടെയ്നർ സമരം%"),
    )
    has_inbound = (
        select(WhatsAppMessage.conversation_id)
        .where(WhatsAppMessage.direction == "in")
        .group_by(WhatsAppMessage.conversation_id)
    )
    preview_blast = set(
        (
            await db.execute(
                select(WhatsAppConversation.id).where(
                    *dm_scope_clauses(),
                    blast_text,
                    ~WhatsAppConversation.id.in_(has_inbound),
                )
            )
        )
        .scalars()
        .all()
    )
    # Campaign blasts are tagged on media_name — avoid scanning every message body.
    campaign_only = set(
        (
            await db.execute(
                select(WhatsAppMessage.conversation_id)
                .join(WhatsAppConversation, WhatsAppConversation.id == WhatsAppMessage.conversation_id)
                .where(
                    *dm_scope_clauses(),
                    func.coalesce(WhatsAppMessage.media_name, "").ilike("campaign_media:%"),
                )
                .group_by(WhatsAppMessage.conversation_id)
                .having(func.count().filter(WhatsAppMessage.direction == "in") == 0)
            )
        )
        .scalars()
        .all()
    )
    out = {int(i) for i in (preview_blast | campaign_only)}
    set_blast_cache(out)
    return out


async def closed_or_blast_exclude_ids(db: AsyncSession, overrides: dict[int, str]) -> set[int]:
    closed = {cid for cid, st in overrides.items() if st == "closed"}
    blast = await _blast_only_conv_ids_fast(db)
    # Human Closed moves teach exclude phrases (weight ≥ 2) — apply to similar chats.
    try:
        from app.intel.stage_kb import load_kb_exclude_conv_ids, load_stage_kb_patterns

        patterns = await load_stage_kb_patterns(db)
        kb_exclude = await load_kb_exclude_conv_ids(db, patterns)
    except Exception:  # noqa: BLE001 — never break funnel on KB miss
        kb_exclude = set()
    exclude = closed | blast | kb_exclude
    # Manual Move-to (any stage except Closed) always wins over blast/KB exclude.
    forced_open = {cid for cid, st in overrides.items() if st and st != "closed"}
    return exclude - forced_open


def outline_status_is(status: str) -> ColumnElement[bool]:
    return or_(
        WhatsAppConversation.ai_outline.ilike(f'%"conversation_status": "{status}"%'),
        WhatsAppConversation.ai_outline.ilike(f'%"conversation_status":"{status}"%'),
    )


def text_has_sales_order_signal(blob: str) -> bool:
    return bool(_SALES_ORDER_MSG.search(blob or ""))


def text_has_payment_proof(blob: str) -> bool:
    text = blob or ""
    if _PAYMENT_SPAM.search(text):
        return False
    return bool(_PAYMENT_PROOF.search(text))


def text_has_stamping_signal(blob: str) -> bool:
    return stamping_signal(blob or "") or bool(_STAMPING_MSG.search(blob or ""))


async def fetch_public_ids_for_conv_ids(
    db: AsyncSession,
    conv_ids: set[int] | list[int],
    *,
    order_limit: int | None = None,
) -> list[str]:
    ids = [int(i) for i in conv_ids if i]
    if not ids:
        return []
    q = (
        select(WhatsAppConversation.public_id)
        .where(WhatsAppConversation.id.in_(ids), *dm_scope_clauses())
        .order_by(WhatsAppConversation.last_message_at.desc().nulls_last())
    )
    if order_limit is not None:
        q = q.limit(order_limit)
    return list((await db.execute(q)).scalars().all())


async def load_wa_funnel_stages(
    db: AsyncSession,
    *,
    id_limit: int = 120,
) -> dict[str, dict[str, Any]]:
    """Return sales_order / waiting / paid stage id lists from WhatsApp only."""
    overrides = await load_stage_overrides(db)
    exclude = await closed_or_blast_exclude_ids(db, overrides)

    # Manual overrides force membership (except closed, which only excludes).
    forced: dict[str, set[int]] = {k: set() for k in ("sales_order", "waiting", "paid", "enquiry", "stamping", "service", "commitments")}
    for cid, stage in overrides.items():
        if stage == "closed":
            continue
        mapped = {
            "sales_order": "sales_order",
            "waiting": "waiting",
            "paid": "paid",
            "enquiry": "enquiry",
            "stamping": "stamping",
            "service": "service",
            "commitments": "commitments",
        }.get(stage)
        if mapped:
            forced[mapped].add(cid)

    # Paid first — payment screenshot is authoritative and excludes from earlier stages.
    paid_ids = set((await db.execute(payment_screenshot_conv_ids_sql())).scalars().all())
    so_pdf_ids = set((await db.execute(so_pdf_out_conv_ids_sql())).scalars().all())
    so_msg_ids = set((await db.execute(sales_order_msg_conv_ids_sql())).scalars().all())

    order_confirmed = set(
        (
            await db.execute(
                select(WhatsAppConversation.id).where(
                    *dm_scope_clauses(),
                    outline_status_is("Order Confirmed"),
                    WhatsAppConversation.status != "Resolved",
                )
            )
        )
        .scalars()
        .all()
    )
    payment_pending = set(
        (
            await db.execute(
                select(WhatsAppConversation.id).where(
                    *dm_scope_clauses(),
                    outline_status_is("Payment Pending"),
                    WhatsAppConversation.status != "Resolved",
                )
            )
        )
        .scalars()
        .all()
    )

    stamp_ids = set((await db.execute(stamping_msg_conv_ids_sql())).scalars().all())

    # Human Move-to labels → KB phrases boost similar chats into the chosen stage.
    kb_by_stage: dict[str, set[int]] = {}
    try:
        from app.intel.stage_kb import load_kb_stage_conv_ids, load_stage_kb_patterns

        kb_patterns = await load_stage_kb_patterns(db)
        kb_by_stage = await load_kb_stage_conv_ids(
            db,
            kb_patterns,
            stages=["sales_order", "waiting", "paid", "stamping", "enquiry", "service", "commitments"],
        )
    except Exception:  # noqa: BLE001
        kb_by_stage = {}

    # Apply closed/blast exclusion and remove chats forced into other stages.
    def _clean(ids: set[int], stage: str) -> set[int]:
        out = {i for i in ids if i not in exclude}
        for other, forced_ids in forced.items():
            if other == stage:
                continue
            out -= forced_ids
        # KB positives for other stages should not steal a forced override, but may
        # divert auto-classified chats when this stage also claims them — paid wins later.
        out |= forced.get(stage, set())
        out |= kb_by_stage.get(stage, set())
        out -= exclude
        return out

    waiting_ids = _clean((payment_pending | (so_pdf_ids - paid_ids)) - paid_ids, "waiting")
    paid_ids = _clean(paid_ids | kb_by_stage.get("paid", set()), "paid")
    sales_order_ids = _clean((order_confirmed | so_msg_ids) - waiting_ids - paid_ids, "sales_order")
    sales_order_ids = {
        cid
        for cid in sales_order_ids
        if cid not in stamp_ids
        or cid in so_msg_ids
        or cid in order_confirmed
        or cid in forced["sales_order"]
        or cid in kb_by_stage.get("sales_order", set())
    }
    stamp_ids = _clean(stamp_ids | forced["stamping"] | kb_by_stage.get("stamping", set()), "stamping")

    paid_public = await fetch_public_ids_for_conv_ids(db, paid_ids, order_limit=id_limit)
    waiting_public = await fetch_public_ids_for_conv_ids(db, waiting_ids, order_limit=id_limit)
    so_public = await fetch_public_ids_for_conv_ids(db, sales_order_ids, order_limit=id_limit)

    return {
        "sales_order": {
            "count": len(sales_order_ids),
            "ids": so_public,
            "summaries": {},
            "note": "WhatsApp: confirmed order / spare-parts SO / freight — awaiting our SO action.",
        },
        "invoice_open": {
            "count": len(waiting_ids),
            "ids": waiting_public,
            "summaries": {},
            "note": "WhatsApp: SO sent; awaiting payment confirmation in chat (not Zoho balance).",
        },
        "invoice_settled": {
            "count": len(paid_ids),
            "ids": paid_public,
            "summaries": {},
            "note": "WhatsApp: dealer paid and shared a payment screenshot (image + proof).",
        },
        "stamping_msg_ids": stamp_ids,
        "paid_conv_ids": paid_ids,
        "overrides": overrides,
        "exclude_ids": exclude,
        "forced": forced,
        "kb_by_stage": {k: sorted(v)[:40] for k, v in kb_by_stage.items() if v},
    }


async def _stamping_msg_conv_ids_cached(db: AsyncSession) -> set[int]:
    from app.intel.cache import get_stamp_msg_ids_cache, set_stamp_msg_ids_cache

    cached = get_stamp_msg_ids_cache()
    if cached is not None:
        return cached
    ids = {int(i) for i in (await db.execute(stamping_msg_conv_ids_sql())).scalars().all()}
    set_stamp_msg_ids_cache(ids)
    return ids


async def load_stamping_public_ids(
    db: AsyncSession,
    *,
    allowed_instance_ids: set[int] | None,
    limit: int,
) -> list[str]:
    """Open stamping/OV chats from category, outline, or message signals."""
    msg_ids = await _stamping_msg_conv_ids_cached(db)
    stamp_match = or_(
        func.lower(WhatsAppConversation.service_category) == "stamping",
        WhatsAppConversation.ai_outline.ilike('%"service_category": "stamping"%'),
        WhatsAppConversation.ai_outline.ilike('%"service_category":"stamping"%'),
    )
    if msg_ids:
        stamp_match = or_(stamp_match, WhatsAppConversation.id.in_(msg_ids))
    clauses = [
        *dm_scope_clauses(),
        WhatsAppConversation.status != "Resolved",
        ~WhatsAppConversation.ai_outline.ilike('%"conversation_status": "Resolved"%'),
        ~WhatsAppConversation.ai_outline.ilike('%"conversation_status":"Resolved"%'),
        stamp_match,
    ]
    if allowed_instance_ids is not None:
        clauses.append(WhatsAppConversation.whatsapp_instance_id.in_(allowed_instance_ids or {-1}))
    rows = (
        await db.execute(
            select(WhatsAppConversation.public_id)
            .where(*clauses)
            .order_by(WhatsAppConversation.last_message_at.desc().nulls_last())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def count_stamping_chats(
    db: AsyncSession,
    *,
    allowed_instance_ids: set[int] | None,
) -> int:
    clauses = [
        *dm_scope_clauses(),
        WhatsAppConversation.status != "Resolved",
        ~WhatsAppConversation.ai_outline.ilike('%"conversation_status": "Resolved"%'),
        or_(
            func.lower(WhatsAppConversation.service_category) == "stamping",
            WhatsAppConversation.ai_outline.ilike('%"service_category": "stamping"%'),
            WhatsAppConversation.ai_outline.ilike('%"service_category":"stamping"%'),
            WhatsAppConversation.id.in_(stamping_msg_conv_ids_sql()),
        ),
    ]
    if allowed_instance_ids is not None:
        clauses.append(WhatsAppConversation.whatsapp_instance_id.in_(allowed_instance_ids or {-1}))
    return int((await db.execute(select(func.count()).select_from(WhatsAppConversation).where(*clauses))).scalar_one() or 0)
