"""Selective WhatsApp reply engine — Suggest Reply by default; auto-send OFF until admin enables."""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import corrections as corrections_mod
from app.ai import kb_service
from app.ai.budget import BudgetError, estimate_cost_inr, get_or_create_settings, reserve_budget, settle_reservation
from app.ai.provider import get_provider
from app.config import get_settings
from app.models import (
    AiUsageLog,
    User,
    WaAiSuggestion,
    WaAutoReplyConfig,
    WhatsAppConversation,
    WhatsAppMessage,
)
from app.whatsapp_outline import load_recent_messages, message_text_for_outline

log = logging.getLogger("ai.reply_engine")

ORG = "yesweigh"

HUMAN_REQUIRED_INTENTS = frozenset(
    {
        "price_negotiation",
        "discount",
        "payment_confirmation",
        "order_confirmation",
        "otp",
        "password",
        "sensitive_account",
        "unverified_technical",
        "management_complaint",
        "no_knowledge",
    }
)

DEFAULT_TOPICS = [
    "greeting",
    "product_info",
    "catalogue",
    "warranty",
    "dealer_registration",
    "troubleshooting",
    "company_info",
    "working_hours",
    "software",
]

RULE_GREETINGS = re.compile(
    r"^\s*(hi|hello|hey|namaste|namaskar|good\s+(morning|afternoon|evening)|ഹലോ|നമസ്കാരം)\b",
    re.I,
)
RULE_HOURS = re.compile(
    r"(working\s*hours|office\s*time|open\s*today|എപ്പോ[ൾഴ]?\s*തുറക്ക|സമയം)",
    re.I,
)
RULE_DEALER = re.compile(r"(dealer\s*regist|become\s*a\s*dealer|ഡീലര്?്\s*ആക)", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def suggestion_to_dict(row: WaAiSuggestion, *, kb_preview: dict | None = None) -> dict[str, Any]:
    kb_ids: list[str] = []
    try:
        raw = json.loads(row.kb_article_ids or "[]")
        if isinstance(raw, list):
            kb_ids = [str(x) for x in raw]
    except Exception:  # noqa: BLE001
        kb_ids = []
    return {
        "id": row.public_id,
        "conversation_id": row.conversation_id,
        "intent": row.intent or "",
        "product": row.product or "",
        "reply_text": row.reply_text or "",
        "reply_language": row.reply_language or "en",
        "kb_article_ids": kb_ids,
        "matched_knowledge_title": row.matched_knowledge_title or "",
        "matched_knowledge": kb_preview,
        "confidence": row.confidence or "low",
        "needs_handover": bool(row.needs_handover),
        "next_action": row.next_action or "",
        "status": row.status or "pending",
        "feedback": row.feedback or "",
        "is_automated": bool(row.is_automated),
        "block_reason": row.block_reason or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
    }


async def get_or_create_config(db: AsyncSession, instance_id: int) -> WaAutoReplyConfig:
    row = (
        await db.execute(
            select(WaAutoReplyConfig).where(WaAutoReplyConfig.whatsapp_instance_id == instance_id)
        )
    ).scalar_one_or_none()
    if row:
        return row
    row = WaAutoReplyConfig(
        organization_id=ORG,
        whatsapp_instance_id=instance_id,
        mode="suggest",
        auto_send_enabled=False,
        emergency_stop=False,
        enabled_topics=json.dumps(DEFAULT_TOPICS),
        require_approved_kb=True,
    )
    db.add(row)
    await db.flush()
    return row


def config_to_dict(row: WaAutoReplyConfig) -> dict[str, Any]:
    topics: list[str] = DEFAULT_TOPICS
    try:
        raw = json.loads(row.enabled_topics or "[]")
        if isinstance(raw, list) and raw:
            topics = [str(x) for x in raw]
    except Exception:  # noqa: BLE001
        pass
    return {
        "id": row.id,
        "whatsapp_instance_id": row.whatsapp_instance_id,
        "mode": row.mode or "suggest",
        "auto_send_enabled": bool(row.auto_send_enabled),
        "emergency_stop": bool(row.emergency_stop),
        "enabled_topics": topics,
        "require_approved_kb": bool(row.require_approved_kb),
        "max_auto_replies_per_day": int(row.max_auto_replies_per_day or 50),
        "max_auto_replies_per_conversation": int(row.max_auto_replies_per_conversation or 3),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def patch_config(
    db: AsyncSession,
    row: WaAutoReplyConfig,
    user: User,
    data: dict[str, Any],
) -> WaAutoReplyConfig:
    if "mode" in data:
        mode = str(data["mode"] or "suggest").strip().lower()
        if mode not in {"disabled", "suggest", "rule_based", "hybrid"}:
            raise ValueError("mode must be disabled|suggest|rule_based|hybrid")
        row.mode = mode
    if "auto_send_enabled" in data:
        row.auto_send_enabled = bool(data["auto_send_enabled"])
        # Safety: enabling auto_send requires non-disabled mode
        if row.auto_send_enabled and row.mode == "disabled":
            raise ValueError("Cannot enable auto-send while mode is disabled")
    if "emergency_stop" in data:
        row.emergency_stop = bool(data["emergency_stop"])
        if row.emergency_stop:
            row.auto_send_enabled = False
    if "enabled_topics" in data and isinstance(data["enabled_topics"], list):
        row.enabled_topics = json.dumps([str(x) for x in data["enabled_topics"]][:30])
    if "require_approved_kb" in data:
        row.require_approved_kb = bool(data["require_approved_kb"])
    if "max_auto_replies_per_day" in data:
        row.max_auto_replies_per_day = max(0, min(int(data["max_auto_replies_per_day"]), 500))
    if "max_auto_replies_per_conversation" in data:
        row.max_auto_replies_per_conversation = max(0, min(int(data["max_auto_replies_per_conversation"]), 20))
    row.updated_by_user_id = user.id
    await db.flush()
    return row


def _customer_language(conv: WhatsAppConversation, last_text: str) -> str:
    pref = (getattr(conv, "preferred_reply_language", None) or "").strip().lower()
    if pref in {"ml", "en", "hi", "ta", "kn"}:
        return pref
    contact = (conv.language or "").strip().lower()
    if contact in {"ml", "malayalam", "മലയാളം"}:
        return "ml"
    if contact in {"ta", "tamil", "தமிழ்"}:
        return "ta"
    if contact in {"en", "english"}:
        return "en"
    # Script detect
    if re.search(r"[\u0d00-\u0d7f]", last_text or ""):
        return "ml"
    if re.search(r"[\u0900-\u097f]", last_text or ""):
        return "hi"
    if re.search(r"[\u0b80-\u0bff]", last_text or ""):
        return "ta"
    if re.search(r"[\u0c80-\u0cff]", last_text or ""):
        return "kn"
    return "en"


def _rule_reply(text: str, lang: str) -> dict[str, Any] | None:
    t = (text or "").strip()
    if not t:
        return None
    if RULE_GREETINGS.search(t) and len(t) < 40:
        if lang == "ml":
            body = "നമസ്കാരം! YesWeigh-ൽ സ്വാഗതം. എങ്ങനെ സഹായിക്കാം?"
        else:
            body = "Hello! Welcome to YesWeigh. How can we help you today?"
        return {
            "intent": "greeting",
            "reply_text": body,
            "confidence": "high",
            "needs_handover": False,
            "next_action": "await_customer",
            "via": "rule",
        }
    if RULE_HOURS.search(t):
        if lang == "ml":
            body = "ഞങ്ങളുടെ ഓഫീസ് സമയം: തിങ്കൾ–ശനി 9:30 AM – 5:30 PM (ഞായർ അവധി). കൂടുതൽ സഹായത്തിന് ദയവായി വിശദാംശങ്ങൾ അയയ്ക്കൂ."
        else:
            body = "Our office hours are Mon–Sat 9:30 AM – 5:30 PM (Sunday closed). Please share more details if you need further help."
        return {
            "intent": "working_hours",
            "reply_text": body,
            "confidence": "medium",
            "needs_handover": False,
            "next_action": "await_customer",
            "via": "rule",
        }
    if RULE_DEALER.search(t):
        return {
            "intent": "dealer_registration",
            "reply_text": "",
            "confidence": "low",
            "needs_handover": True,
            "next_action": "hand_to_sales",
            "via": "rule",
            "block_reason": "Dealer registration needs human verification",
        }
    return None


def _looks_human_required(text: str) -> str | None:
    low = (text or "").lower()
    checks = [
        ("otp", "otp"),
        ("password", "password"),
        ("discount", "discount"),
        ("negotiate", "price_negotiation"),
        ("best price", "price_negotiation"),
        ("payment done", "payment_confirmation"),
        ("order confirm", "order_confirmation"),
        ("complaint to management", "management_complaint"),
        ("മാനേജ്മെന്റ്", "management_complaint"),
        ("ഡിസ്കൗണ്ട്", "discount"),
    ]
    for needle, intent in checks:
        if needle in low:
            return intent
    return None


async def latest_pending_suggestion(
    db: AsyncSession, conversation_id: int
) -> WaAiSuggestion | None:
    return (
        await db.execute(
            select(WaAiSuggestion)
            .where(
                WaAiSuggestion.conversation_id == conversation_id,
                WaAiSuggestion.status.in_(("pending", "edited")),
            )
            .order_by(WaAiSuggestion.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def suggest_reply(
    db: AsyncSession,
    conv: WhatsAppConversation,
    *,
    user: User | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Build a suggested reply (never auto-sends)."""
    if bool(getattr(conv, "ai_opt_out", False)):
        return {"ok": False, "error": "Customer opted out of AI", "blocked": True}

    cfg = await get_or_create_config(db, conv.whatsapp_instance_id)
    if cfg.emergency_stop or cfg.mode == "disabled":
        return {
            "ok": False,
            "error": "Auto-reply disabled for this instance" if cfg.mode == "disabled" else "Emergency stop active",
            "blocked": True,
            "config": config_to_dict(cfg),
        }

    if getattr(conv, "ai_human_takeover_at", None) and not force:
        return {
            "ok": False,
            "error": "Human takeover active — AI suggestions paused",
            "blocked": True,
            "human_takeover": True,
        }

    messages = await load_recent_messages(db, conv, limit=20)
    last_in = next((m for m in reversed(messages) if m.direction == "in"), None)
    if not last_in:
        return {"ok": False, "error": "No inbound customer message", "blocked": True}

    last_text = message_text_for_outline(last_in)
    if not last_text or last_text.startswith("[voice message"):
        return {"ok": False, "error": "Waiting for transcript / text", "blocked": True}

    lang = _customer_language(conv, last_text)
    human = _looks_human_required(last_text)
    if human:
        row = await _store_suggestion(
            db,
            conv,
            user=user,
            trigger=last_in,
            intent=human,
            product="",
            reply_text="",
            reply_language=lang,
            kb_ids=[],
            title="",
            confidence="high",
            needs_handover=True,
            next_action="hand_over",
            status="handover",
            block_reason=f"Requires human: {human}",
        )
        return {"ok": True, "suggestion": suggestion_to_dict(row), "handover": True}

    topics = []
    try:
        topics = json.loads(cfg.enabled_topics or "[]")
    except Exception:  # noqa: BLE001
        topics = DEFAULT_TOPICS

    # Rule-based path
    rule = None
    if cfg.mode in {"rule_based", "hybrid", "suggest"}:
        rule = _rule_reply(last_text, lang)
        if rule and rule.get("intent") == "greeting" and "greeting" not in topics:
            rule = None
        if rule and rule.get("intent") == "working_hours" and "working_hours" not in topics:
            rule = None

    if rule and rule.get("via") == "rule" and cfg.mode == "rule_based":
        if rule.get("needs_handover"):
            row = await _store_suggestion(
                db,
                conv,
                user=user,
                trigger=last_in,
                intent=rule["intent"],
                product="",
                reply_text=rule.get("reply_text") or "",
                reply_language=lang,
                kb_ids=[],
                title="",
                confidence=rule.get("confidence") or "low",
                needs_handover=True,
                next_action=rule.get("next_action") or "hand_over",
                status="handover",
                block_reason=rule.get("block_reason") or "",
            )
            return {"ok": True, "suggestion": suggestion_to_dict(row), "handover": True, "via": "rule"}
        row = await _store_suggestion(
            db,
            conv,
            user=user,
            trigger=last_in,
            intent=rule["intent"],
            product="",
            reply_text=rule.get("reply_text") or "",
            reply_language=lang,
            kb_ids=[],
            title="",
            confidence=rule.get("confidence") or "medium",
            needs_handover=False,
            next_action=rule.get("next_action") or "suggest_send",
            status="pending",
        )
        return {"ok": True, "suggestion": suggestion_to_dict(row), "via": "rule"}

    # KB search (approved only)
    kb_hits = await kb_service.search_approved(db, last_text, limit=5)
    kb_hits = [a for a in kb_hits if not topics or a.category in topics or a.category == "other"]

    if cfg.require_approved_kb and not kb_hits and cfg.mode == "rule_based":
        row = await _store_suggestion(
            db,
            conv,
            user=user,
            trigger=last_in,
            intent="no_knowledge",
            product="",
            reply_text="",
            reply_language=lang,
            kb_ids=[],
            title="",
            confidence="low",
            needs_handover=True,
            next_action="hand_over",
            status="handover",
            block_reason="No approved knowledge match",
        )
        return {"ok": True, "suggestion": suggestion_to_dict(row), "handover": True}

    # Hybrid: use rule greeting if no KB needed
    if rule and rule.get("intent") == "greeting" and not kb_hits:
        row = await _store_suggestion(
            db,
            conv,
            user=user,
            trigger=last_in,
            intent="greeting",
            product="",
            reply_text=rule["reply_text"],
            reply_language=lang,
            kb_ids=[],
            title="",
            confidence="high",
            needs_handover=False,
            next_action="suggest_send",
            status="pending",
        )
        return {"ok": True, "suggestion": suggestion_to_dict(row), "via": "rule"}

    # Gemini compose from approved KB (+ optional rule context)
    env = get_settings()
    settings_row = await get_or_create_settings(db)
    if not env.ai_enabled or not settings_row.enabled or not (env.gemini_api_key or "").strip():
        if kb_hits:
            # Fallback: use first approved answer verbatim
            art = kb_hits[0]
            row = await _store_suggestion(
                db,
                conv,
                user=user,
                trigger=last_in,
                intent=art.category,
                product=art.product or "",
                reply_text=art.answer,
                reply_language=lang if lang == art.language else art.language or lang,
                kb_ids=[art.public_id],
                title=art.title,
                confidence="medium",
                needs_handover=False,
                next_action="suggest_send",
                status="pending",
            )
            await kb_service.bump_usage(db, [art.id])
            return {
                "ok": True,
                "suggestion": suggestion_to_dict(row, kb_preview=kb_service.article_to_dict(art)),
                "via": "kb_verbatim",
            }
        return {"ok": False, "error": "AI not configured and no KB match", "blocked": True}

    kb_block = ""
    for i, art in enumerate(kb_hits[:4], 1):
        kb_block += f"\n[{i}] {art.title}\nQ: {art.question}\nA: {art.answer}\n"

    hints = await corrections_mod.terminology_hints(db, limit=15)
    hint_block = ""
    if hints:
        hint_block = "\nPreferred terminology corrections:\n" + "\n".join(
            f"- prefer «{h['prefer']}»" + (f" instead of «{h['instead_of']}»" if h.get("instead_of") else "")
            for h in hints[:10]
        )

    recent = []
    for m in messages[-12:]:
        t = message_text_for_outline(m)
        if not t:
            continue
        who = "Customer" if m.direction == "in" else "Agent"
        recent.append(f"{who}: {t}")

    system = f"""You draft a short WhatsApp reply for YesWeigh staff to review.
Reply in the customer's language: {lang}.
Use ONLY approved knowledge below. Never invent prices, discounts, stock, warranty terms, OTP, or payment confirmation.
If knowledge is insufficient or topic needs a human, set needs_handover=true and reply_text="".
Mark replies clearly helpful and professional. Do not claim to be human.
Return JSON only:
{{"intent":"...","product":"...","reply_text":"...","confidence":"low|medium|high","needs_handover":false,"next_action":"suggest_send|hand_over|ask_clarification"}}
{hint_block}
"""
    prompt = (
        f"Customer message:\n{last_text}\n\n"
        f"Recent chat:\n" + "\n".join(recent) + "\n\n"
        f"Approved knowledge:{kb_block or ' (none)'}\n"
    )

    reserved = Decimal("0")
    t0 = time.perf_counter()
    total_in = total_out = total_tokens = total_thoughts = 0
    try:
        reserved = await reserve_budget(db, Decimal("0.20"))
        provider = get_provider(settings_row.provider or "gemini", env.gemini_api_key)
        model = settings_row.model or env.ai_model
        resp = await provider.generate(
            model=model,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_output_tokens=min(int(settings_row.max_output_tokens or 600), 500),
        )
        total_in = resp.usage.input_tokens
        total_out = resp.usage.output_tokens
        total_thoughts = int(getattr(resp.usage, "thoughts_tokens", 0) or 0)
        total_tokens = int(getattr(resp.usage, "total_tokens", 0) or 0) or (
            total_in + total_out + total_thoughts
        )
        from app.ai.service import extract_json_object, redact_secrets

        parsed = extract_json_object(redact_secrets(resp.text or "")) or {}
        reply_text = (parsed.get("reply_text") or "").strip()
        needs_handover = bool(parsed.get("needs_handover")) or not reply_text
        # Guard: refuse invented pricing language if no KB
        if reply_text and not kb_hits and re.search(r"(₹|rs\.?|price|rate|cost)\s*\d", reply_text, re.I):
            needs_handover = True
            reply_text = ""
            parsed["next_action"] = "hand_over"

        kb_ids = [a.public_id for a in kb_hits[:3]]
        title = kb_hits[0].title if kb_hits else ""
        row = await _store_suggestion(
            db,
            conv,
            user=user,
            trigger=last_in,
            intent=(parsed.get("intent") or (kb_hits[0].category if kb_hits else "other"))[:80],
            product=(parsed.get("product") or (kb_hits[0].product if kb_hits else ""))[:160],
            reply_text=reply_text[:4000],
            reply_language=lang,
            kb_ids=kb_ids,
            title=title,
            confidence=(parsed.get("confidence") or ("medium" if kb_hits else "low"))[:20],
            needs_handover=needs_handover,
            next_action=(parsed.get("next_action") or ("hand_over" if needs_handover else "suggest_send"))[:120],
            status="handover" if needs_handover else "pending",
            block_reason="Needs human review" if needs_handover and not reply_text else "",
        )
        if kb_hits:
            await kb_service.bump_usage(db, [a.id for a in kb_hits[:3]])

        actual = estimate_cost_inr(total_in, total_out, env, thoughts_tokens=total_thoughts)
        await settle_reservation(db, reserved, actual)
        db.add(
            AiUsageLog(
                organization_id=ORG,
                user_id=user.id if user else None,
                conversation_id=conv.id,
                whatsapp_instance_id=conv.whatsapp_instance_id,
                provider=settings_row.provider or "gemini",
                model=model,
                purpose="auto_reply",
                input_tokens=total_in,
                output_tokens=total_out,
                total_tokens=total_tokens,
                thoughts_tokens=total_thoughts,
                estimated_cost_inr=actual,
                reserved_cost_inr=reserved,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                success=True,
            )
        )
        await db.flush()
        kb_preview = kb_service.article_to_dict(kb_hits[0]) if kb_hits else None
        return {
            "ok": True,
            "suggestion": suggestion_to_dict(row, kb_preview=kb_preview),
            "via": "gemini",
            # Explicit: never auto-send from suggest_reply
            "auto_sent": False,
        }
    except BudgetError as exc:
        if reserved:
            await settle_reservation(db, reserved, Decimal("0"))
        return {"ok": False, "error": exc.message}
    except Exception as exc:  # noqa: BLE001
        log.exception("suggest_reply failed")
        if reserved:
            try:
                await settle_reservation(db, reserved, Decimal("0"))
            except Exception:  # noqa: BLE001
                pass
        db.add(
            AiUsageLog(
                organization_id=ORG,
                user_id=user.id if user else None,
                conversation_id=conv.id,
                whatsapp_instance_id=conv.whatsapp_instance_id,
                provider="gemini",
                model=env.ai_model or "",
                purpose="auto_reply",
                input_tokens=total_in,
                output_tokens=total_out,
                total_tokens=total_tokens or (total_in + total_out + total_thoughts),
                thoughts_tokens=total_thoughts,
                estimated_cost_inr=estimate_cost_inr(
                    total_in, total_out, env, thoughts_tokens=total_thoughts
                ),
                reserved_cost_inr=reserved,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                success=False,
                error=str(exc)[:2000],
            )
        )
        await db.flush()
        return {"ok": False, "error": str(exc)[:500]}


async def _store_suggestion(
    db: AsyncSession,
    conv: WhatsAppConversation,
    *,
    user: User | None,
    trigger: WhatsAppMessage | None,
    intent: str,
    product: str,
    reply_text: str,
    reply_language: str,
    kb_ids: list[str],
    title: str,
    confidence: str,
    needs_handover: bool,
    next_action: str,
    status: str,
    block_reason: str = "",
) -> WaAiSuggestion:
    # Supersede older pending
    old = await latest_pending_suggestion(db, conv.id)
    if old:
        old.status = "discarded"
    row = WaAiSuggestion(
        public_id=str(uuid.uuid4()),
        organization_id=ORG,
        conversation_id=conv.id,
        whatsapp_instance_id=conv.whatsapp_instance_id,
        trigger_message_id=trigger.id if trigger else None,
        intent=intent,
        product=product,
        reply_text=reply_text,
        reply_language=reply_language,
        kb_article_ids=json.dumps(kb_ids),
        matched_knowledge_title=title[:240],
        confidence=confidence,
        needs_handover=needs_handover,
        next_action=next_action,
        status=status,
        is_automated=False,
        block_reason=block_reason[:255],
        created_by_user_id=user.id if user else None,
    )
    db.add(row)
    await db.flush()
    return row


async def mark_human_takeover(db: AsyncSession, conv: WhatsAppConversation) -> None:
    conv.ai_human_takeover_at = _now()
    pending = await latest_pending_suggestion(db, conv.id)
    if pending:
        pending.status = "handover"
        pending.needs_handover = True
        pending.next_action = "human_takeover"
    await db.flush()


async def clear_human_takeover(db: AsyncSession, conv: WhatsAppConversation) -> None:
    conv.ai_human_takeover_at = None
    await db.flush()


async def set_suggestion_feedback(
    db: AsyncSession, row: WaAiSuggestion, feedback: str
) -> WaAiSuggestion:
    fb = (feedback or "").strip().lower()
    if fb not in {"helpful", "incorrect", ""}:
        raise ValueError("feedback must be helpful|incorrect|empty")
    row.feedback = fb
    # Bump KB quality counters
    try:
        ids = json.loads(row.kb_article_ids or "[]")
    except Exception:  # noqa: BLE001
        ids = []
    if ids:
        from app.models import KbArticle

        arts = (
            await db.execute(select(KbArticle).where(KbArticle.public_id.in_([str(x) for x in ids])))
        ).scalars().all()
        for a in arts:
            if fb == "helpful":
                a.helpful_count = int(a.helpful_count or 0) + 1
            elif fb == "incorrect":
                a.incorrect_count = int(a.incorrect_count or 0) + 1
                a.needs_review = True
    await db.flush()
    return row


async def can_auto_send(db: AsyncSession, conv: WhatsAppConversation) -> tuple[bool, str]:
    """Strict gate — default False until admin enables auto_send on instance config."""
    cfg = await get_or_create_config(db, conv.whatsapp_instance_id)
    if cfg.emergency_stop:
        return False, "emergency_stop"
    if not cfg.auto_send_enabled:
        return False, "auto_send_disabled"
    if cfg.mode in {"disabled", "suggest"}:
        # suggest mode never auto-sends
        return False, f"mode_{cfg.mode}"
    if getattr(conv, "ai_human_takeover_at", None):
        return False, "human_takeover"
    if getattr(conv, "ai_opt_out", False):
        return False, "opt_out"
    # Daily cap
    day_start = datetime(_now().year, _now().month, _now().day, tzinfo=timezone.utc)
    sent_today = (
        await db.execute(
            select(func.count()).where(
                WaAiSuggestion.whatsapp_instance_id == conv.whatsapp_instance_id,
                WaAiSuggestion.status.in_(("auto_sent", "sent")),
                WaAiSuggestion.sent_at >= day_start,
                WaAiSuggestion.is_automated.is_(True),
            )
        )
    ).scalar_one()
    if int(sent_today or 0) >= int(cfg.max_auto_replies_per_day or 0):
        return False, "daily_cap"
    return True, "ok"


async def learning_analytics(db: AsyncSession) -> dict[str, Any]:
    """Real metrics only for Phase 5 scaffold."""
    from app.models import AiCorrection, KbArticle, KbLearningCheckpoint

    async def _count(model, *filters):
        q = select(func.count()).select_from(model)
        for f in filters:
            q = q.where(f)
        return int((await db.execute(q)).scalar_one() or 0)

    kb_stats = await kb_service.stats(db)
    analysed = await _count(KbLearningCheckpoint, KbLearningCheckpoint.status == "done")
    sugg = await _count(WaAiSuggestion)
    ai_sent = await _count(WaAiSuggestion, WaAiSuggestion.status.in_(("sent", "auto_sent")))
    auto_sent = await _count(WaAiSuggestion, WaAiSuggestion.status == "auto_sent")
    handovers = await _count(WaAiSuggestion, WaAiSuggestion.needs_handover.is_(True))
    corrections = await _count(AiCorrection)
    failed_learn = await _count(KbLearningCheckpoint, KbLearningCheckpoint.status == "failed")

    usage = (
        await db.execute(
            select(
                AiUsageLog.purpose,
                func.count(),
                func.coalesce(func.sum(AiUsageLog.estimated_cost_inr), 0),
                func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
            )
            .where(
                AiUsageLog.organization_id == ORG,
                AiUsageLog.purpose.in_(
                    ("knowledge_learning", "auto_reply", "knowledge_search", "whatsapp_outline")
                ),
            )
            .group_by(AiUsageLog.purpose)
        )
    ).all()
    by_purpose = {
        str(p): {
            "requests": int(c or 0),
            "spend_inr": round(float(s or 0), 4),
            "tokens": int(t or 0),
        }
        for p, c, s, t in usage
    }

    return {
        "kb": kb_stats,
        "conversations_analysed": analysed,
        "suggestions_total": sugg,
        "replies_sent": ai_sent,
        "auto_replies_sent": auto_sent,
        "handovers": handovers,
        "corrections": corrections,
        "learning_failures": failed_learn,
        "by_purpose": by_purpose,
        "auto_send_default": False,
        "note": "Customer auto-send remains off until admin enables per-instance auto_send_enabled.",
    }
