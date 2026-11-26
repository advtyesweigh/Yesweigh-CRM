"""Conversation learning engine — extract KB suggestions from WhatsApp threads.

Lightweight, incremental, Gemini-backed. Does NOT fine-tune models or auto-publish.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import kb_service
from app.ai.budget import BudgetError, estimate_cost_inr, reserve_budget, settle_reservation
from app.ai.provider import get_provider
from app.config import get_settings
from app.models import (
    AiUsageLog,
    KbLearningCheckpoint,
    User,
    WhatsAppConversation,
    WhatsAppMessage,
)
from app.whatsapp_outline import message_text_for_outline

log = logging.getLogger("ai.learning")

ORG = "yesweigh"
LEARN_PROMPT_VERSION = "learn-v1"
# Cap concurrency / batch to protect 8GB VPS
DEFAULT_BATCH = 5
MAX_MESSAGES = 30
MAX_BLOCK_CHARS = 6000

_GROUPISH = re.compile(r"@g\.us|group", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _content_fingerprint(messages: list[WhatsAppMessage]) -> str:
    if not messages:
        return f"{LEARN_PROMPT_VERSION}:empty"
    last = messages[-1]
    raw = f"{LEARN_PROMPT_VERSION}:{len(messages)}:{last.id}:{int(last.created_at.timestamp()) if last.created_at else 0}"
    return hashlib.sha1(raw.encode()).hexdigest()[:40]


def _is_skippable_conversation(conv: WhatsAppConversation) -> bool:
    if bool(getattr(conv, "ai_opt_out", False)):
        return True
    jid = (conv.customer_jid or "").lower()
    ctype = (conv.customer_type or "").lower()
    if "@g.us" in jid or "group" in ctype:
        return True
    phone = (conv.customer_phone or "").strip()
    if not phone or phone.startswith("status"):
        return True
    return False


def _build_transcript(conv: WhatsAppConversation, messages: list[WhatsAppMessage]) -> str:
    lines: list[str] = []
    for m in messages:
        text = message_text_for_outline(m)
        if not text:
            continue
        if m.direction == "note":
            continue
        who = "Customer" if m.direction == "in" else (m.sender_name or "Employee")
        lines.append(f"{who}: {text}")
    block = "\n".join(lines)
    if len(block) > MAX_BLOCK_CHARS:
        block = block[-MAX_BLOCK_CHARS:]
    return block


def _extract_json_list(text: str) -> list[dict[str, Any]]:
    from app.ai.service import extract_json_object, redact_secrets

    cleaned = redact_secrets(text or "")
    # Prefer array
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start >= 0 and end > start:
        try:
            data = json.loads(cleaned[start : end + 1])
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except Exception:  # noqa: BLE001
            pass
    obj = extract_json_object(cleaned)
    if not obj:
        return []
    if isinstance(obj.get("articles"), list):
        return [x for x in obj["articles"] if isinstance(x, dict)]
    if isinstance(obj.get("suggestions"), list):
        return [x for x in obj["suggestions"] if isinstance(x, dict)]
    if obj.get("question") or obj.get("answer") or obj.get("title"):
        return [obj]
    return []


LEARN_SYSTEM = """You extract reusable YesWeigh business knowledge from WhatsApp support/sales chats.
YesWeigh sells weighing scales, POS, billing software, and related products in India.

Return ONLY JSON: {"articles":[...]} where each article has:
- title (short)
- question (customer question paraphrase)
- answer (employee's useful verified explanation — do NOT invent prices, discounts, OTP, or warranties)
- category: one of greeting, product_info, catalogue, warranty, dealer_registration, troubleshooting, company_info, working_hours, software, sales, complaint, other
- product (sku/name if any, else "")
- keywords (comma-separated)
- language: ml|en|hi|ta|kn|mixed
- useful: true/false

Rules:
- Only extract knowledge that would help answer FUTURE customers.
- Skip greetings-only, personal chatter, confidential account/OTP/payment details.
- Prefer employee answers that resolved the issue.
- If nothing useful, return {"articles":[]}.
- Do not fabricate specs or prices not present in the chat.
"""


async def _get_or_create_checkpoint(db: AsyncSession, conv: WhatsAppConversation) -> KbLearningCheckpoint:
    row = (
        await db.execute(
            select(KbLearningCheckpoint).where(KbLearningCheckpoint.conversation_id == conv.id)
        )
    ).scalar_one_or_none()
    if row:
        return row
    row = KbLearningCheckpoint(
        organization_id=ORG,
        conversation_id=conv.id,
        status="idle",
    )
    db.add(row)
    await db.flush()
    return row


async def analyse_conversation(
    db: AsyncSession,
    conv: WhatsAppConversation,
    *,
    user: User | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Analyse one conversation; create suggested KB articles. Idempotent via fingerprint."""
    if _is_skippable_conversation(conv):
        return {"ok": True, "skipped": True, "reason": "group_or_opt_out", "created": 0}

    messages = (
        await db.execute(
            select(WhatsAppMessage)
            .where(WhatsAppMessage.conversation_id == conv.id)
            .order_by(WhatsAppMessage.created_at.desc(), WhatsAppMessage.id.desc())
            .limit(MAX_MESSAGES)
        )
    ).scalars().all()
    messages = list(reversed(messages))
    fp = _content_fingerprint(messages)
    ck = await _get_or_create_checkpoint(db, conv)

    if not force and ck.content_fingerprint == fp and ck.status in {"done", "skipped"}:
        return {"ok": True, "skipped": True, "reason": "unchanged", "created": 0, "fingerprint": fp}

    # Need at least one inbound + one outbound with text
    has_in = any(m.direction == "in" and message_text_for_outline(m) for m in messages)
    has_out = any(m.direction == "out" and message_text_for_outline(m) for m in messages)
    if not (has_in and has_out):
        ck.content_fingerprint = fp
        ck.status = "skipped"
        ck.last_error = "insufficient_dialogue"
        ck.last_analysed_at = _now()
        ck.last_message_id = messages[-1].id if messages else None
        await db.flush()
        return {"ok": True, "skipped": True, "reason": "insufficient_dialogue", "created": 0}

    block = _build_transcript(conv, messages)
    if len(block) < 40:
        ck.content_fingerprint = fp
        ck.status = "skipped"
        ck.last_error = "too_short"
        ck.last_analysed_at = _now()
        await db.flush()
        return {"ok": True, "skipped": True, "reason": "too_short", "created": 0}

    env = get_settings()
    if not env.ai_enabled or not (env.gemini_api_key or "").strip():
        return {"ok": False, "error": "AI not configured", "created": 0}

    from app.ai.budget import get_or_create_settings

    row_settings = await get_or_create_settings(db)
    if not row_settings.enabled:
        return {"ok": False, "error": "AI disabled in settings", "created": 0}

    ck.status = "processing"
    await db.flush()

    reserved = Decimal("0")
    t0 = time.perf_counter()
    total_in = total_out = total_tokens = total_thoughts = 0
    created_rows: list[dict[str, Any]] = []
    try:
        reserved = await reserve_budget(db, Decimal("0.30"))
        provider = get_provider(row_settings.provider or "gemini", env.gemini_api_key)
        model = row_settings.model or env.ai_model or "gemini-3.5-flash-lite"
        prompt = (
            f"Customer: {(conv.customer_name or '')} {(conv.customer_phone or '')}\n"
            f"Chat (oldest → newest):\n\n{block}\n\n"
            "Extract useful knowledge articles now as JSON."
        )
        resp = await provider.generate(
            model=model,
            system=LEARN_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_output_tokens=min(int(row_settings.max_output_tokens or 1024), 1200),
        )
        total_in = resp.usage.input_tokens
        total_out = resp.usage.output_tokens
        total_thoughts = int(getattr(resp.usage, "thoughts_tokens", 0) or 0)
        total_tokens = int(getattr(resp.usage, "total_tokens", 0) or 0) or (
            total_in + total_out + total_thoughts
        )
        articles = _extract_json_list(resp.text or "")
        msg_ids = [m.public_id for m in messages if m.direction in {"in", "out"}][-12:]
        source_emp = None
        for m in reversed(messages):
            if m.direction == "out" and m.sender_employee_id:
                source_emp = m.sender_employee_id
                break

        for art in articles[:5]:
            if art.get("useful") is False:
                continue
            question = (art.get("question") or art.get("title") or "").strip()
            answer = (art.get("answer") or "").strip()
            if len(question) < 8 or len(answer) < 12:
                continue
            # Skip confidential-looking answers
            low = answer.lower()
            if any(x in low for x in ("otp", "password", "upi pin", "cvv", "account number")):
                continue

            similar = await kb_service.find_similar_approved(db, question, limit=5)
            similar_id = similar[0].id if similar else None
            contradiction = await kb_service.detect_contradiction(db, answer, similar)
            # Near-duplicate of existing suggested/approved → skip create
            if similar and _near_duplicate(question, answer, similar[0]):
                continue

            row = await kb_service.create_article(
                db,
                user,
                {
                    "title": (art.get("title") or question)[:240],
                    "question": question[:4000],
                    "answer": answer[:8000],
                    "category": art.get("category") or "other",
                    "product": art.get("product") or "",
                    "keywords": art.get("keywords") or "",
                    "language": (art.get("language") or "en"),
                    "source_employee_id": source_emp,
                    "source_conversation_id": conv.id,
                    "source_message_ids": msg_ids,
                    "similar_to_id": similar_id,
                    "contradiction_of_id": contradiction.id if contradiction else None,
                    "needs_review": bool(contradiction),
                },
                status="suggested",
            )
            created_rows.append(kb_service.article_to_dict(row))

        actual = estimate_cost_inr(total_in, total_out, env, thoughts_tokens=total_thoughts)
        await settle_reservation(db, reserved, actual)
        db.add(
            AiUsageLog(
                organization_id=ORG,
                user_id=user.id if user else None,
                conversation_id=conv.id,
                whatsapp_instance_id=conv.whatsapp_instance_id,
                provider=row_settings.provider or "gemini",
                model=model,
                purpose="knowledge_learning",
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
        ck.content_fingerprint = fp
        ck.status = "done"
        ck.suggestions_created = int(ck.suggestions_created or 0) + len(created_rows)
        ck.last_error = ""
        ck.last_analysed_at = _now()
        ck.last_message_id = messages[-1].id if messages else None
        await db.flush()
        return {
            "ok": True,
            "skipped": False,
            "created": len(created_rows),
            "articles": created_rows,
            "fingerprint": fp,
        }
    except BudgetError as exc:
        ck.status = "failed"
        ck.last_error = exc.message[:500]
        await db.flush()
        if reserved:
            await settle_reservation(db, reserved, Decimal("0"))
        return {"ok": False, "error": exc.message, "created": 0}
    except Exception as exc:  # noqa: BLE001
        log.exception("learning failed conv=%s", conv.id)
        if reserved:
            try:
                await settle_reservation(db, reserved, Decimal("0"))
            except Exception:  # noqa: BLE001
                pass
        duration_ms = int((time.perf_counter() - t0) * 1000)
        db.add(
            AiUsageLog(
                organization_id=ORG,
                user_id=user.id if user else None,
                conversation_id=conv.id,
                whatsapp_instance_id=conv.whatsapp_instance_id,
                provider="gemini",
                model=get_settings().ai_model or "",
                purpose="knowledge_learning",
                input_tokens=total_in,
                output_tokens=total_out,
                total_tokens=total_tokens or (total_in + total_out + total_thoughts),
                thoughts_tokens=total_thoughts,
                estimated_cost_inr=estimate_cost_inr(
                    total_in, total_out, get_settings(), thoughts_tokens=total_thoughts
                ),
                reserved_cost_inr=reserved,
                duration_ms=duration_ms,
                success=False,
                error=str(exc)[:2000],
            )
        )
        ck.status = "failed"
        ck.last_error = str(exc)[:500]
        ck.last_analysed_at = _now()
        await db.flush()
        return {"ok": False, "error": str(exc)[:500], "created": 0}


def _near_duplicate(question: str, answer: str, existing) -> bool:
    q = re.sub(r"\s+", " ", question.lower()).strip()
    eq = re.sub(r"\s+", " ", (existing.question or existing.title or "").lower()).strip()
    if q and eq and (q == eq or q in eq or eq in q):
        a = re.sub(r"\s+", " ", answer.lower()).strip()[:200]
        ea = re.sub(r"\s+", " ", (existing.answer or "").lower()).strip()[:200]
        if a and ea and (a == ea or a in ea or ea in a):
            return True
        # Same question already exists approved/suggested
        if existing.status in {"approved", "suggested", "under_review"}:
            return True
    return False


async def run_learning_batch(
    db: AsyncSession,
    *,
    user: User | None = None,
    limit: int = DEFAULT_BATCH,
    conversation_public_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Process up to `limit` conversations that need learning."""
    lim = max(1, min(int(limit or DEFAULT_BATCH), 20))
    results: list[dict[str, Any]] = []
    created_total = 0
    skipped = 0
    failed = 0

    if conversation_public_id:
        conv = (
            await db.execute(
                select(WhatsAppConversation).where(
                    WhatsAppConversation.public_id == conversation_public_id,
                    WhatsAppConversation.organization_id == ORG,
                )
            )
        ).scalar_one_or_none()
        if not conv:
            return {"ok": False, "error": "Conversation not found", "processed": 0}
        r = await analyse_conversation(db, conv, user=user, force=force)
        results.append({"conversation_id": conv.public_id, **r})
        created_total += int(r.get("created") or 0)
        if r.get("skipped"):
            skipped += 1
        elif not r.get("ok"):
            failed += 1
        return {
            "ok": True,
            "processed": 1,
            "created": created_total,
            "skipped": skipped,
            "failed": failed,
            "results": results,
        }

    # Prefer recent 1:1 chats with activity; left-join checkpoint to find stale fingerprints
    convs = (
        await db.execute(
            select(WhatsAppConversation)
            .where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.last_message_at.is_not(None),
            )
            .order_by(WhatsAppConversation.last_message_at.desc())
            .limit(lim * 4)
        )
    ).scalars().all()

    processed = 0
    for conv in convs:
        if processed >= lim:
            break
        if _is_skippable_conversation(conv):
            continue
        ck = await _get_or_create_checkpoint(db, conv)
        # Quick skip if recently done without force
        if (
            not force
            and ck.status == "done"
            and ck.last_analysed_at
            and (_now() - ck.last_analysed_at).total_seconds() < 3600
        ):
            # Still check fingerprint cheaply via last message id
            last = (
                await db.execute(
                    select(WhatsAppMessage.id)
                    .where(WhatsAppMessage.conversation_id == conv.id)
                    .order_by(WhatsAppMessage.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if last and ck.last_message_id == last:
                skipped += 1
                continue
        r = await analyse_conversation(db, conv, user=user, force=force)
        results.append({"conversation_id": conv.public_id, **r})
        created_total += int(r.get("created") or 0)
        if r.get("skipped"):
            skipped += 1
        elif not r.get("ok"):
            failed += 1
        processed += 1
        # Small yield between Gemini calls
        await db.commit()

    return {
        "ok": True,
        "processed": processed,
        "created": created_total,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }
