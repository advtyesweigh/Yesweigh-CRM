"""New-messenger WhatsApp onboarding (dealer vs customer → business card → Lead).

Runs async after Evolution / Care ingest commits. Idempotent per normalized phone
across instances so Care + Evolution staff lines never re-ask forever.

Included instances (documented):
  - All Evolution GO personal lines (provider=evolution) — e.g. Biju / Shibin / Visakh
  - Care Meta Cloud (provider=meta_cloud, display Care)
First live inbound on an included instance wins; later instances see the same phone
row and either skip (terminal) or continue the state machine on the reply line.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.dealers import _normalized_phone_sql
from app.models import (
    Lead,
    WhatsAppConversation,
    WhatsAppInstance,
    WhatsAppMessage,
    WhatsAppNewMessengerOnboarding,
)
from app.phone_util import normalize_in_mobile, to_evolution_number
from app.whatsapp_chat_meta import chat_kind_from_jid
from app.whatsapp_chat_types import is_group_jid

log = logging.getLogger("whatsapp_new_messenger")

ORG = "yesweigh"

# Steps (authoritative state machine)
STEP_ASKED_ROLE = "asked_role"
STEP_ASKED_CARD = "asked_card"
STEP_LEAD_CREATED = "lead_created"
STEP_CUSTOMER = "customer"
STEP_SKIPPED = "skipped"
TERMINAL_STEPS = frozenset({STEP_LEAD_CREATED, STEP_CUSTOMER, STEP_SKIPPED})

MSG_ASK_ROLE = (
    "Hi! Welcome to YesWeigh. Are you a weighing scale *dealer* or a *customer*? "
    "Please reply with Dealer or Customer."
)
MSG_ASK_CARD = (
    "Thanks! Please share a photo of your *business card* so we can add your details as a lead."
)
MSG_CUSTOMER_ACK = "Thank you! Our team will assist you shortly."
MSG_LEAD_CREATED = (
    "Thanks — we've saved your business card details. Our sales team will follow up soon."
)
MSG_CARD_RETRY = (
    "Please send a clear *photo* of your business visiting card (image), "
    "or reply Customer if you are not a dealer."
)

_DEALER_RE = re.compile(
    r"(?i)(?:^\s*(dealer|dealership|wholesale|distributor|agency|"
    r"weighing\s*scale\s*dealer|yes\s*,?\s*dealer|i\s*am\s*(a\s*)?dealer|"
    r"ഡീലർ|ഡീല്ലർ|വിതരണക്കാരൻ)\s*[.!]?\s*$"
    r"|\b(i\s*am\s*(a\s*)?dealer|we\s*are\s*dealers?)\b)"
)
_CUSTOMER_RE = re.compile(
    r"(?i)(?:^\s*(customer|end\s*user|buyer|retail\s*customer|user|"
    r"not\s*a?\s*dealer|i\s*am\s*(a\s*)?customer|"
    r"കസ്റ്റമർ|ഉപഭോക്താവ്)\s*[.!]?\s*$"
    r"|\b(i\s*am\s*(a\s*)?customer|not\s*a?\s*dealer)\b)"
)

_LIVE_MAX_AGE = timedelta(minutes=15)
_DEDUP_CLIENT_PREFIX = "newmsg-"

# In-flight conversation ids (process-local)
_pending: set[int] = set()


def instance_included(inst: WhatsAppInstance | None) -> bool:
    """Evolution staff lines + Care Meta Cloud only."""
    if not inst or getattr(inst, "deleted_at", None):
        return False
    provider = (getattr(inst, "provider", None) or "evolution").strip().lower()
    return provider in {"evolution", "meta_cloud"}


def included_instances_doc() -> dict[str, Any]:
    return {
        "providers": ["evolution", "meta_cloud"],
        "note": (
            "All Evolution GO personal lines (Biju/Shibin/Visakh etc.) plus Care Meta Cloud. "
            "Phone-keyed state: first live inbound wins; other lines do not re-ask."
        ),
    }


def parse_role_reply(text: str) -> str | None:
    """Return 'dealer' | 'customer' | None."""
    raw = (text or "").strip()
    if not raw:
        return None
    # Ignore pure media placeholders
    if raw.startswith(("📷", "🎥", "🎤", "📄", "[")) or raw.lower() in {
        "media",
        "[media]",
        "sticker",
        "attachment",
    }:
        return None
    if _DEALER_RE.search(raw):
        return "dealer"
    if _CUSTOMER_RE.search(raw):
        return "customer"
    # Soft single-token match
    token = re.sub(r"[^\w\u0d00-\u0d7f]+", "", raw, flags=re.UNICODE).casefold()
    if token in {"dealer", "dealership", "wholesale", "distributor", "ഡീലർ", "ഡീല്ലർ"}:
        return "dealer"
    if token in {"customer", "buyer", "user", "കസ്റ്റമർ", "ഉപഭോക്താവ്"}:
        return "customer"
    return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _append_log(row: WhatsAppNewMessengerOnboarding, event: str, **extra: Any) -> None:
    try:
        entries = json.loads(row.log or "[]")
        if not isinstance(entries, list):
            entries = []
    except Exception:  # noqa: BLE001
        entries = []
    entries.append({"at": _utcnow().isoformat(), "event": event, **extra})
    row.log = json.dumps(entries[-40:], ensure_ascii=False)


def _lead_phone_storage(phone: str) -> str:
    key = normalize_in_mobile(phone)
    if not key:
        return (phone or "").strip()
    return to_evolution_number(key) or f"91{key}"


def _tags_str(*parts: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        for bit in re.split(r"[,|]", p or ""):
            t = bit.strip()
            if not t:
                continue
            key = t.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
    return ", ".join(out)


async def _find_dealer(db: AsyncSession, phone: str):
    from app.inbox import _find_dealer_by_phone

    return await _find_dealer_by_phone(db, phone)


async def _find_lead(db: AsyncSession, phone: str):
    from app.inbox import _find_lead_by_phone

    return await _find_lead_by_phone(db, phone)


async def _prior_dm_history(
    db: AsyncSession,
    phone_key: str,
    *,
    current_message_id: int,
    current_created_at: datetime | None,
) -> bool:
    """True if this phone already has earlier inbound DM messages (any instance).

    Near-simultaneous Care + Evolution first messages (within 2 minutes) do NOT count
    as prior history — phone-keyed state + send dedupe prevent double-ask.
    """
    from app.whatsapp_chat_types import dm_inbox_filter, newsletter_list_filter

    created = _aware(current_created_at) or _utcnow()
    # Only messages clearly before this first contact window
    cutoff = created - timedelta(minutes=2)

    inbound_before = (
        await db.execute(
            select(func.count())
            .select_from(WhatsAppMessage)
            .join(WhatsAppConversation, WhatsAppMessage.conversation_id == WhatsAppConversation.id)
            .where(
                WhatsAppConversation.organization_id == ORG,
                _normalized_phone_sql(WhatsAppConversation.customer_phone) == phone_key,
                WhatsAppMessage.direction == "in",
                WhatsAppMessage.id != current_message_id,
                WhatsAppMessage.created_at < cutoff,
                dm_inbox_filter(),
                newsletter_list_filter(),
            )
        )
    ).scalar_one()
    return int(inbound_before or 0) > 0


async def _get_or_create_row(
    db: AsyncSession,
    phone_key: str,
    *,
    conv: WhatsAppConversation,
    inst: WhatsAppInstance,
) -> WhatsAppNewMessengerOnboarding:
    row = (
        await db.execute(
            select(WhatsAppNewMessengerOnboarding)
            .where(
                WhatsAppNewMessengerOnboarding.organization_id == ORG,
                WhatsAppNewMessengerOnboarding.phone_key == phone_key,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if row:
        return row
    row = WhatsAppNewMessengerOnboarding(
        organization_id=ORG,
        phone_key=phone_key,
        step="",
        conversation_id=conv.id,
        whatsapp_instance_id=inst.id,
        log="[]",
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except Exception:  # noqa: BLE001 — unique race with parallel Care/Evolution ingest
        existing = (
            await db.execute(
                select(WhatsAppNewMessengerOnboarding)
                .where(
                    WhatsAppNewMessengerOnboarding.organization_id == ORG,
                    WhatsAppNewMessengerOnboarding.phone_key == phone_key,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing:
            return existing
        raise
    return row


def _mirror_conv(
    conv: WhatsAppConversation,
    row: WhatsAppNewMessengerOnboarding,
    *,
    note: str = "",
) -> None:
    conv.onboarding_step = (row.step or "")[:40]
    conv.onboarding_at = row.updated_at or _utcnow()
    if row.lead_id:
        conv.onboarding_lead_id = row.lead_id
    if note:
        base = (conv.notes_summary or "").strip()
        line = f"[New messenger] {note}"
        if line not in base:
            conv.notes_summary = f"{base}\n{line}".strip() if base else line
    # Soft tag while in-flight (don't stomp Resolved etc.)
    if row.step in {STEP_ASKED_ROLE, STEP_ASKED_CARD} and not (conv.tag or "").strip():
        conv.tag = "New messenger"
    elif row.step == STEP_LEAD_CREATED:
        conv.tag = "New messenger lead"
    elif row.step == STEP_CUSTOMER and (conv.tag or "").strip() in {"", "New messenger"}:
        conv.tag = "New messenger customer"


async def _send_automation_text(
    db: AsyncSession,
    conv: WhatsAppConversation,
    inst: WhatsAppInstance,
    text: str,
    *,
    client_suffix: str,
) -> WhatsAppMessage | None:
    """Outbound without a staff user; reuses Evolution/Care gateways."""
    from app.cloud_whatsapp import is_meta_cloud_instance, send_care_cloud_text
    from app.evolution import assign_conversation_to_instance_owner, decrypt_secret
    from app.inbox import _send_recipient, _svc
    from app.whatsapp_reply_latency import complete_staff_reply

    body = (text or "").strip()
    if not body:
        return None
    client_message_id = f"{_DEDUP_CLIENT_PREFIX}{client_suffix}-{uuid.uuid4().hex[:12]}"
    dup = (
        await db.execute(
            select(WhatsAppMessage).where(WhatsAppMessage.client_message_id == client_message_id)
        )
    ).scalar_one_or_none()
    if dup:
        return dup

    # Soft dedupe: same automation text already sent to this phone recently (any instance)
    phone_key = normalize_in_mobile(conv.customer_phone)
    recent_q = (
        select(WhatsAppMessage)
        .join(WhatsAppConversation, WhatsAppMessage.conversation_id == WhatsAppConversation.id)
        .where(
            WhatsAppMessage.direction == "out",
            WhatsAppMessage.body == body,
            WhatsAppMessage.sender_name == "YesWeigh CRM",
            WhatsAppMessage.created_at >= _utcnow() - timedelta(hours=24),
        )
        .order_by(WhatsAppMessage.id.desc())
        .limit(1)
    )
    if phone_key:
        recent_q = recent_q.where(_normalized_phone_sql(WhatsAppConversation.customer_phone) == phone_key)
    else:
        recent_q = recent_q.where(WhatsAppMessage.conversation_id == conv.id)
    recent = (await db.execute(recent_q)).scalar_one_or_none()
    if recent:
        return recent

    care = is_meta_cloud_instance(inst)
    msg = WhatsAppMessage(
        public_id=str(uuid.uuid4()),
        organization_id=ORG,
        conversation_id=conv.id,
        whatsapp_instance_id=inst.id,
        direction="out",
        body=body,
        client_message_id=client_message_id,
        status="sending",
        sender_user_id=None,
        sender_employee_id=None,
        sender_name="YesWeigh CRM",
    )
    db.add(msg)
    await db.flush()
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.flush()

    recipient = _send_recipient(conv)
    if not recipient:
        msg.status = "failed"
        await db.commit()
        return msg
    try:
        if care:
            ext = await send_care_cloud_text(
                to_wa_id=recipient,
                text=body,
                client_message_id=msg.client_message_id,
            )
            msg.external_message_id = ext
            msg.status = "delivered"
        else:
            _, svc = await _svc(db)
            token = decrypt_secret(inst.encrypted_instance_token)
            remote = await svc.send_text(token, recipient, body, msg.client_message_id)
            ext = ""
            if isinstance(remote, dict):
                data = remote.get("data") if isinstance(remote.get("data"), dict) else remote
                if isinstance(data, dict):
                    key = data.get("key") if isinstance(data.get("key"), dict) else {}
                    info = data.get("Info") if isinstance(data.get("Info"), dict) else {}
                    ext = str(
                        data.get("id")
                        or data.get("messageId")
                        or key.get("id")
                        or info.get("ID")
                        or ""
                    )
            msg.external_message_id = ext
            msg.status = "delivered"
    except Exception as exc:  # noqa: BLE001
        log.warning("new-messenger send failed conv=%s: %s", conv.id, exc)
        msg.status = "failed"
        msg.body = f"[send failed] {exc}"[:2000]
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            pass
        return msg

    now = _utcnow()
    conv.last_message_preview = body[:255]
    conv.last_message_at = now
    await complete_staff_reply(db, conv, replied_at=now, source="crm", inst=inst, msg=msg)
    if not conv.assigned_employee_id:
        await assign_conversation_to_instance_owner(db, conv, inst.id)
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.flush()
    return msg


async def _create_lead_from_card(
    db: AsyncSession,
    conv: WhatsAppConversation,
    *,
    image_url: str,
) -> Lead | None:
    from app.visiting_card import extract_visiting_card
    from app.state_normalize import canon_state

    result = await extract_visiting_card(
        image_url=image_url,
        resolve_places=True,
        source="Visiting Card",
    )
    prefill = dict(result.get("lead_prefill") or {})
    name = (prefill.get("name") or "").strip() or (conv.customer_name or "").strip() or "WhatsApp Lead"
    company = (prefill.get("company") or "").strip()
    phone = (prefill.get("phone") or "").strip() or _lead_phone_storage(conv.customer_phone)
    tags = prefill.get("tags") or []
    if isinstance(tags, list):
        tag_s = _tags_str(*[str(t) for t in tags], "New Messenger", "Dealer")
    else:
        tag_s = _tags_str(str(tags), "New Messenger", "Dealer")
    state = canon_state(str(prefill.get("state") or "")) or (prefill.get("state") or "")
    notes = (prefill.get("notes") or "").strip()
    notes = (notes + "\nAuto-created from new-messenger business card OCR.").strip()

    # Prefer existing lead enrichment over duplicate
    existing = await _find_lead(db, phone) or await _find_lead(db, conv.customer_phone)
    if existing:
        changed = False
        if company and not (existing.company or "").strip():
            existing.company = company[:160]
            changed = True
        if name and (not (existing.name or "").strip() or existing.name == "WhatsApp Lead"):
            existing.name = name[:160]
            changed = True
        if (prefill.get("address") or "") and not (existing.address or "").strip():
            existing.address = str(prefill.get("address") or "")[:4000]
            changed = True
        for field in ("city", "district", "state", "pin", "contact_person"):
            val = str(prefill.get(field) or "").strip()
            if field == "state":
                val = (canon_state(val) or val)[:80]
            if val and not (getattr(existing, field) or "").strip():
                setattr(existing, field, val[:160] if field != "address" else val)
                changed = True
        new_tags = _tags_str(existing.tags or "", tag_s)
        if new_tags != (existing.tags or ""):
            existing.tags = new_tags[:500]
            changed = True
        if notes and notes not in (existing.notes or ""):
            existing.notes = f"{(existing.notes or '').strip()}\n{notes}".strip()[:4000]
            changed = True
        if changed:
            await db.flush()
        return existing

    lead = Lead(
        name=name[:160],
        company=company[:160],
        phone=_lead_phone_storage(phone),
        contact_person=str(prefill.get("contact_person") or "")[:160],
        address=str(prefill.get("address") or "")[:4000],
        city=str(prefill.get("city") or "")[:80],
        district=str(prefill.get("district") or "")[:80],
        state=str(state or "")[:80],
        pin=str(prefill.get("pin") or "")[:20],
        source="Visiting Card",
        status="new",
        priority="high",
        business_type="Weighing Equipment Dealer",
        product_category="Electronic Weighing Scales",
        tags=tag_s[:500],
        notes=notes[:4000],
    )
    db.add(lead)
    await db.flush()
    return lead


async def process_new_messenger(
    db: AsyncSession,
    *,
    conversation_id: int,
    message_id: int,
) -> dict[str, Any]:
    conv = await db.get(WhatsAppConversation, conversation_id)
    msg = await db.get(WhatsAppMessage, message_id)
    if not conv or not msg:
        return {"ok": False, "reason": "missing"}
    if msg.direction != "in":
        return {"ok": False, "reason": "not_inbound"}
    if getattr(conv, "is_spam", False):
        return {"ok": False, "reason": "spam"}

    jid = conv.customer_jid or ""
    if is_group_jid(jid) or chat_kind_from_jid(jid) in {"group", "channel"}:
        return {"ok": False, "reason": "not_dm"}

    inst = await db.get(WhatsAppInstance, conv.whatsapp_instance_id)
    if not instance_included(inst):
        return {"ok": False, "reason": "instance_excluded", "doc": included_instances_doc()}

    phone_key = normalize_in_mobile(conv.customer_phone)
    if not phone_key:
        return {"ok": False, "reason": "bad_phone"}

    # Ignore history-sync / delayed dumps
    created = _aware(msg.created_at) or _utcnow()
    if _utcnow() - created > _LIVE_MAX_AGE:
        return {"ok": False, "reason": "stale_message"}

    row = await _get_or_create_row(db, phone_key, conv=conv, inst=inst)
    row.conversation_id = conv.id
    row.whatsapp_instance_id = inst.id

    # Already finished or permanently skipped
    if (row.step or "") in TERMINAL_STEPS:
        _mirror_conv(conv, row)
        await db.commit()
        return {"ok": True, "step": row.step, "skipped": True, "reason": "terminal"}

    # Mid-flow: handle role reply or card image
    if row.step == STEP_ASKED_ROLE:
        text = (msg.body or "").strip()
        # Prefer transcript when body is media placeholder
        if msg.transcript and (not text or text.startswith(("📷", "["))):
            text = (msg.transcript or "").strip()
        role = parse_role_reply(text)
        if role == "customer":
            row.step = STEP_CUSTOMER
            row.updated_at = _utcnow()
            _append_log(row, "role_customer", message_id=msg.id)
            _mirror_conv(conv, row, note="Confirmed customer — automation stopped.")
            await _send_automation_text(
                db, conv, inst, MSG_CUSTOMER_ACK, client_suffix=f"{phone_key}-cust"
            )
            await db.commit()
            return {"ok": True, "step": STEP_CUSTOMER}
        if role == "dealer":
            row.step = STEP_ASKED_CARD
            row.updated_at = _utcnow()
            _append_log(row, "role_dealer", message_id=msg.id)
            conv.customer_type = "Dealer"
            _mirror_conv(conv, row, note="Confirmed dealer — asked for business card.")
            await _send_automation_text(
                db, conv, inst, MSG_ASK_CARD, client_suffix=f"{phone_key}-card"
            )
            await db.commit()
            return {"ok": True, "step": STEP_ASKED_CARD}
        # Unparsed — wait (do not re-ask forever)
        _append_log(row, "role_unparsed", body=(msg.body or "")[:120])
        await db.commit()
        return {"ok": True, "step": STEP_ASKED_ROLE, "waiting": "role"}

    if row.step == STEP_ASKED_CARD:
        media_type = (msg.media_type or "").strip().lower()
        media_url = (msg.media_url or "").strip()
        is_image = media_type in {"image", "photo", "sticker", "jpeg", "jpg", "png", "webp"} or (
            media_url and media_type.startswith("image")
        )
        # Some gateways store type empty but URL is /api/whatsapp/media/...
        if not is_image and media_url.startswith("/api/whatsapp/media/"):
            # Treat as image only if body looks like photo or type empty/image-ish
            if media_type in {"", "document"} and (
                (msg.body or "").startswith("📷") or not media_type
            ):
                # Prefer explicit image; documents might be PDF cards — skip OCR for non-image
                if media_type == "document":
                    is_image = False
                else:
                    is_image = True

        if is_image and media_url:
            try:
                lead = await _create_lead_from_card(db, conv, image_url=media_url)
            except Exception as exc:  # noqa: BLE001
                log.warning("card OCR failed conv=%s: %s", conv.id, exc)
                _append_log(row, "ocr_failed", error=str(exc)[:200])
                await _send_automation_text(
                    db, conv, inst, MSG_CARD_RETRY, client_suffix=f"{phone_key}-retry"
                )
                await db.commit()
                return {"ok": False, "step": STEP_ASKED_CARD, "error": "ocr_failed"}

            if lead:
                row.step = STEP_LEAD_CREATED
                row.lead_id = lead.id
                row.updated_at = _utcnow()
                _append_log(row, "lead_created", lead_id=lead.id, message_id=msg.id)
                conv.customer_type = "Dealer"
                _mirror_conv(
                    conv,
                    row,
                    note=f"Lead #{lead.id} created from business card OCR.",
                )
                await _send_automation_text(
                    db, conv, inst, MSG_LEAD_CREATED, client_suffix=f"{phone_key}-lead"
                )
                await db.commit()
                return {"ok": True, "step": STEP_LEAD_CREATED, "lead_id": lead.id}

        # Maybe they switched to customer
        role = parse_role_reply((msg.body or "").strip())
        if role == "customer":
            row.step = STEP_CUSTOMER
            row.updated_at = _utcnow()
            _append_log(row, "role_customer_after_card_ask", message_id=msg.id)
            _mirror_conv(conv, row, note="Switched to customer after card ask.")
            await _send_automation_text(
                db, conv, inst, MSG_CUSTOMER_ACK, client_suffix=f"{phone_key}-cust2"
            )
            await db.commit()
            return {"ok": True, "step": STEP_CUSTOMER}

        _append_log(row, "waiting_card", media_type=media_type)
        await db.commit()
        return {"ok": True, "step": STEP_ASKED_CARD, "waiting": "card"}

    # Fresh start — only when step is empty
    if await _find_dealer(db, conv.customer_phone) or conv.dealer_id:
        row.step = STEP_SKIPPED
        row.updated_at = _utcnow()
        _append_log(row, "skip_existing_dealer")
        _mirror_conv(conv, row, note="Skipped — already a dealer.")
        await db.commit()
        return {"ok": True, "step": STEP_SKIPPED, "reason": "dealer"}

    if await _find_lead(db, conv.customer_phone):
        row.step = STEP_SKIPPED
        row.updated_at = _utcnow()
        _append_log(row, "skip_existing_lead")
        _mirror_conv(conv, row, note="Skipped — already a lead.")
        await db.commit()
        return {"ok": True, "step": STEP_SKIPPED, "reason": "lead"}

    if await _prior_dm_history(
        db, phone_key, current_message_id=msg.id, current_created_at=msg.created_at
    ):
        row.step = STEP_SKIPPED
        row.updated_at = _utcnow()
        _append_log(row, "skip_prior_chat_history")
        _mirror_conv(conv, row, note="Skipped — prior WhatsApp chat history exists.")
        await db.commit()
        return {"ok": True, "step": STEP_SKIPPED, "reason": "prior_history"}

    # Claim + ask role (idempotent if parallel Care/Evolution workers race)
    if (row.step or "").strip():
        _mirror_conv(conv, row)
        await db.commit()
        return {"ok": True, "step": row.step, "skipped": True, "reason": "already_claimed"}

    row.step = STEP_ASKED_ROLE
    row.updated_at = _utcnow()
    _append_log(
        row,
        "asked_role",
        instance=inst.display_name or inst.evolution_instance_name,
        provider=(inst.provider or "evolution"),
        message_id=msg.id,
    )
    await db.flush()
    _mirror_conv(conv, row, note="Automation asked dealer vs customer.")
    await _send_automation_text(
        db, conv, inst, MSG_ASK_ROLE, client_suffix=f"{phone_key}-role"
    )
    await db.commit()
    return {"ok": True, "step": STEP_ASKED_ROLE, "asked": True}


async def _run_safe(*, message_id: int, conversation_id: int | None = None) -> None:
    lock_key = conversation_id or message_id
    if lock_key in _pending:
        return
    _pending.add(lock_key)
    try:
        await asyncio.sleep(0.35)  # let webhook commit settle; stay off critical path
        async with SessionLocal() as db:
            try:
                cid = conversation_id
                if not cid:
                    msg = await db.get(WhatsAppMessage, message_id)
                    if not msg:
                        return
                    cid = int(msg.conversation_id)
                result = await process_new_messenger(
                    db, conversation_id=cid, message_id=message_id
                )
                log.info(
                    "new_messenger conv=%s msg=%s → %s",
                    cid,
                    message_id,
                    result,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "new_messenger failed msg=%s: %s",
                    message_id,
                    exc,
                )
    finally:
        _pending.discard(lock_key)


def schedule_new_messenger(
    message_id: int | None = None,
    conversation_id: int | None = None,
) -> None:
    """Fire-and-forget after Evolution/Care webhook commit."""
    try:
        mid = int(message_id or 0)
        cid = int(conversation_id or 0) or None
    except (TypeError, ValueError):
        return
    if mid <= 0:
        return
    try:
        asyncio.get_running_loop().create_task(_run_safe(message_id=mid, conversation_id=cid))
    except RuntimeError:
        log.warning("no running loop for new_messenger msg=%s", mid)


def onboarding_public(row: WhatsAppNewMessengerOnboarding | None, conv: WhatsAppConversation | None = None) -> dict[str, Any] | None:
    """Admin-visible payload for inbox API."""
    step = ""
    lead_id = None
    at = None
    log_tail: list[Any] = []
    if row:
        step = row.step or ""
        lead_id = row.lead_id
        at = row.updated_at or row.created_at
        try:
            entries = json.loads(row.log or "[]")
            if isinstance(entries, list):
                log_tail = entries[-8:]
        except Exception:  # noqa: BLE001
            log_tail = []
    elif conv and (getattr(conv, "onboarding_step", None) or ""):
        step = conv.onboarding_step or ""
        lead_id = getattr(conv, "onboarding_lead_id", None)
        at = getattr(conv, "onboarding_at", None)
    if not step:
        return None
    return {
        "step": step,
        "lead_id": lead_id,
        "at": at.isoformat() if at else None,
        "ran": True,
        "log": log_tail,
        "instances": included_instances_doc(),
    }


async def load_onboarding_for_conv(
    db: AsyncSession, conv: WhatsAppConversation
) -> dict[str, Any] | None:
    phone_key = normalize_in_mobile(conv.customer_phone)
    row = None
    if phone_key:
        row = (
            await db.execute(
                select(WhatsAppNewMessengerOnboarding)
                .where(
                    WhatsAppNewMessengerOnboarding.organization_id == ORG,
                    WhatsAppNewMessengerOnboarding.phone_key == phone_key,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    return onboarding_public(row, conv)
