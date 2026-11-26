"""Shared WhatsApp AI outline (Gemini structured summary) helpers.

Used by inbox POST/GET outline endpoints, Care webhook auto-refresh, and Care backfill.
Fingerprint gating keeps Gemini cost to one summary per conversation content version.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.evolution import primary_owner_employee
from app.models import Employee, User, WhatsAppConversation, WhatsAppInstance, WhatsAppMessage

log = logging.getLogger("whatsapp_outline")

PROVIDER_META_CLOUD = "meta_cloud"


def message_text_for_outline(msg: WhatsAppMessage) -> str:
    """Prefer Malayalam transcript for voice; for text include original + ml translation."""
    from app.ai.service import redact_secrets

    media_type = (msg.media_type or "").strip().lower()
    is_voice = media_type in {"audio", "ptt", "voice"} or bool(getattr(msg, "is_voice", False))
    transcript = (getattr(msg, "transcript", None) or "").strip()
    body = (msg.body or "").strip()
    if is_voice:
        if transcript:
            return f"[voice transcript] {redact_secrets(transcript)}"
        return "[voice message — no transcript yet]"
    if body in {"[media]", "media", "📷 Photo", "🎥 Video", "🎤 Voice message", "📄 Document", "Sticker", "Attachment"}:
        return ""
    if body.startswith("📷") or body.startswith("🎥") or body.startswith("📄"):
        return ""
    if transcript and transcript != body:
        return redact_secrets(f"{body}\n[ml] {transcript}")
    return redact_secrets(body)


def outline_fingerprint(
    messages: list[WhatsAppMessage],
    *,
    preferred_language: str = "en",
) -> str:
    from app.ai.service import OUTLINE_PROMPT_VERSION, normalize_staff_preferred_language

    lang = normalize_staff_preferred_language(preferred_language)
    if not messages:
        return f"{OUTLINE_PROMPT_VERSION}:{lang}:empty"
    last = messages[-1]
    tail = (
        f"{last.public_id}:"
        f"{int(last.created_at.timestamp()) if last.created_at else 0}"
    )
    digest = hashlib.sha256(tail.encode("utf-8")).hexdigest()[:16]
    # Keep under DB VARCHAR(128); UUID+timestamp in cleartext exceeded VARCHAR(80).
    return f"{OUTLINE_PROMPT_VERSION}:{lang}:{len(messages)}:{digest}"


async def preferred_outline_language(db: AsyncSession, conv: WhatsAppConversation) -> str:
    """Outline language for Gemini narrative fields.

    Priority:
    1. Contact Language = Malayalam → ml
    2. Assigned agent / instance owner prefers Malayalam → ml
    3. Care (Meta Cloud) line → ml by default (Kerala customer care)
    4. Recent chat text already uses Malayalam script → ml
    5. Else staff preference (default English)
    """
    from app.ai.service import normalize_staff_preferred_language

    from app.contact_spoken_language import (
        chat_has_malayalam_from_text_only,
        latest_inbound_spoken_language,
        normalize_spoken_language_code,
    )

    contact_raw = (getattr(conv, "language", None) or "").strip()
    contact_lang = normalize_staff_preferred_language(contact_raw)
    if contact_lang == "ml":
        return "ml"

    emp: Employee | None = None
    if conv.assigned_employee_id:
        emp = await db.get(Employee, conv.assigned_employee_id)
    if emp is None:
        emp = await primary_owner_employee(db, conv.whatsapp_instance_id)
    staff_raw = (getattr(emp, "languages", None) if emp else None) or ""
    staff_lang = normalize_staff_preferred_language(staff_raw)
    if staff_lang == "ml":
        return "ml"

    # Care Cloud WhatsApp: default Malayalam unless staff explicitly set English.
    inst = await db.get(WhatsAppInstance, conv.whatsapp_instance_id) if conv.whatsapp_instance_id else None
    is_care = bool(inst and (inst.provider or "") == PROVIDER_META_CLOUD)
    if is_care:
        explicit_en = staff_lang == "en" and bool(staff_raw.strip())
        if not explicit_en:
            return "ml"

    spoken_contact = normalize_spoken_language_code(contact_raw)
    if not spoken_contact:
        spoken_contact = await latest_inbound_spoken_language(db, conv.id)
    if spoken_contact and spoken_contact not in {"", "ml"}:
        # Customer speaks Tamil/Telugu/etc.; translated voice transcripts must not force ml.
        return staff_lang

    # Plain-text Malayalam in chat (not translated voice) → summarise in Malayalam.
    try:
        messages = await load_recent_messages(db, conv, limit=20)
        if chat_has_malayalam_from_text_only(messages):
            return "ml"
    except Exception:  # noqa: BLE001
        pass

    return staff_lang


async def load_recent_messages(
    db: AsyncSession,
    conv: WhatsAppConversation,
    *,
    limit: int = 40,
) -> list[WhatsAppMessage]:
    rows = (
        await db.execute(
            select(WhatsAppMessage)
            .where(WhatsAppMessage.conversation_id == conv.id)
            .order_by(WhatsAppMessage.created_at.desc(), WhatsAppMessage.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(reversed(rows))


def cached_summary_payload(conv: WhatsAppConversation) -> dict[str, Any]:
    from app.ai.service import normalize_outline_summary

    cached = (getattr(conv, "ai_outline", None) or "").strip()
    generated_at = getattr(conv, "ai_outline_at", None)
    updated = generated_at.isoformat() if generated_at else ""
    summary = (
        normalize_outline_summary(cached, updated_at=updated)
        if cached
        else normalize_outline_summary({}, updated_at=updated)
    )
    return {
        "outline": cached,
        "summary": summary,
        "generated_at": generated_at,
        "last_message_id": (getattr(conv, "ai_outline_fingerprint", None) or "").split(":")[-2]
        if (getattr(conv, "ai_outline_fingerprint", None) or "").count(":") >= 2
        else None,
    }


async def refresh_conversation_outline(
    db: AsyncSession,
    conv: WhatsAppConversation,
    *,
    user: User | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Generate or return cached Gemini structured summary for a conversation.

    Skips Gemini when fingerprint matches (unless force) or there is no summarisable text.
    """
    from app.ai.budget import BudgetError
    from app.ai.service import summarize_whatsapp_thread

    messages = await load_recent_messages(db, conv)
    preferred_language = await preferred_outline_language(db, conv)
    fp = outline_fingerprint(messages, preferred_language=preferred_language)
    cached = (getattr(conv, "ai_outline", None) or "").strip()
    cached_fp = getattr(conv, "ai_outline_fingerprint", None) or ""
    legacy = bool(cached) and not cached.lstrip().startswith("{")

    from app.ai.service import outline_lacks_malayalam_for_ml, normalize_outline_summary

    wrong_ml_script = False
    if preferred_language == "ml" and cached:
        cached_summary = normalize_outline_summary(cached)
        wrong_ml_script = outline_lacks_malayalam_for_ml(cached_summary) or outline_lacks_malayalam_for_ml(
            cached
        )
    if wrong_ml_script:
        log.info("invalidating non-Malayalam / wrong-script ml outline conv=%s", conv.id)

    from app.intel.enquiry_list import rewrite_outline_if_needed, verdict_for

    gate = await verdict_for(db, conv, [{"direction": m.direction or "", "text": message_text_for_outline(m) or ""} for m in messages])
    if rewrite_outline_if_needed(conv, gate):
        conv.ai_outline_fingerprint = fp
        conv.ai_outline_at = datetime.now(timezone.utc)
        await db.flush()
        from app.whatsapp_service_meta import parse_cached_outline_summary as _parse_outline

        summary = _parse_outline(conv.ai_outline or "")
        return {
            "outline": conv.ai_outline,
            "summary": summary,
            "stale": False,
            "fingerprint": fp,
            "preferred_language": preferred_language,
            "generated_at": conv.ai_outline_at,
            "last_message_id": messages[-1].public_id if messages else None,
            "cached": False,
            "skipped": False,
            "model": None,
            "ok": True,
        }

    if cached and cached_fp == fp and not force and not legacy and not wrong_ml_script:
        payload = cached_summary_payload(conv)
        return {
            **payload,
            "stale": False,
            "fingerprint": fp,
            "preferred_language": preferred_language,
            "cached": True,
            "skipped": True,
            "model": None,
            "ok": True,
        }

    lines: list[str] = []
    for m in messages:
        text = message_text_for_outline(m)
        if not text:
            continue
        if m.direction == "note":
            who = "Note"
        elif m.direction == "out":
            who = m.sender_name or "Agent"
        else:
            who = conv.customer_name or "Customer"
        lines.append(f"{who}: {text}")
    if not lines:
        return {
            **cached_summary_payload(conv),
            "stale": True,
            "fingerprint": fp,
            "preferred_language": preferred_language,
            "cached": bool(cached),
            "skipped": True,
            "ok": False,
            "error": "No text or voice transcripts to summarise yet",
        }

    try:
        result = await summarize_whatsapp_thread(
            db,
            user,
            transcript_block="\n".join(lines),
            customer_label=f"{conv.customer_name or ''} {conv.customer_phone}".strip(),
            conversation_id=conv.id,
            whatsapp_instance_id=getattr(conv, "whatsapp_instance_id", None),
            preferred_language=preferred_language,
        )
    except BudgetError as exc:
        return {
            **cached_summary_payload(conv),
            "stale": True,
            "fingerprint": fp,
            "preferred_language": preferred_language,
            "cached": bool(cached),
            "skipped": False,
            "ok": False,
            "error": exc.message,
            "budget": True,
        }
    except (ValueError, RuntimeError) as exc:
        if cached and not force:
            return {
                **cached_summary_payload(conv),
                "stale": True,
                "fingerprint": fp,
                "preferred_language": preferred_language,
                "cached": True,
                "skipped": False,
                "ok": False,
                "error": str(exc),
            }
        return {
            **cached_summary_payload(conv),
            "stale": True,
            "fingerprint": fp,
            "preferred_language": preferred_language,
            "cached": False,
            "skipped": False,
            "ok": False,
            "error": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        if cached and not force:
            return {
                **cached_summary_payload(conv),
                "stale": True,
                "fingerprint": fp,
                "preferred_language": preferred_language,
                "cached": True,
                "skipped": False,
                "ok": False,
                "error": f"Outline failed: {exc}",
            }
        return {
            **cached_summary_payload(conv),
            "stale": True,
            "fingerprint": fp,
            "preferred_language": preferred_language,
            "cached": False,
            "skipped": False,
            "ok": False,
            "error": f"Outline failed: {exc}",
        }

    summary = result.get("summary") or {}
    outline = result.get("outline") or json.dumps(summary, ensure_ascii=False)
    conv.ai_outline = outline
    conv.ai_outline_fingerprint = fp
    conv.ai_outline_at = datetime.now(timezone.utc)
    from app.whatsapp_service_meta import apply_service_meta_to_conversation

    apply_service_meta_to_conversation(
        conv,
        summary,
        fallback_text=conv.last_message_preview or "",
    )
    from app.whatsapp_dealer_enquiry import apply_dealer_enquiry_actions, patch_outline_json

    summary = await apply_dealer_enquiry_actions(db, conv, summary, messages)
    apply_service_meta_to_conversation(
        conv,
        summary,
        fallback_text=conv.last_message_preview or "",
    )
    outline = json.dumps(summary, ensure_ascii=False)
    patch_outline_json(conv, summary)
    await db.flush()
    return {
        "outline": outline,
        "summary": summary,
        "stale": False,
        "fingerprint": fp,
        "preferred_language": preferred_language,
        "generated_at": conv.ai_outline_at,
        "last_message_id": messages[-1].public_id if messages else None,
        "cached": False,
        "skipped": False,
        "model": result.get("model"),
        "usage": result.get("usage"),
        "ok": True,
    }


async def _refresh_outline_bg(conversation_id: int) -> None:
    try:
        async with SessionLocal() as db:
            conv = await db.get(WhatsAppConversation, conversation_id)
            if not conv:
                return
            result = await refresh_conversation_outline(db, conv, user=None, force=False)
            if result.get("ok") and not result.get("skipped"):
                await db.commit()
            elif result.get("ok") and result.get("skipped"):
                await db.rollback()
            else:
                await db.rollback()
                if result.get("error"):
                    log.info(
                        "care outline refresh skipped/failed conv=%s: %s",
                        conversation_id,
                        result.get("error"),
                    )
    except Exception as exc:  # noqa: BLE001
        log.warning("care outline refresh error conv=%s: %s", conversation_id, exc)


def schedule_outline_refresh(conversation_ids: Iterable[int]) -> None:
    """Fire-and-forget outline refresh after Care webhook commit (deduped)."""
    seen: set[int] = set()
    for raw in conversation_ids:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid <= 0 or cid in seen:
            continue
        seen.add(cid)
        try:
            asyncio.get_running_loop().create_task(_refresh_outline_bg(cid))
        except RuntimeError:
            log.warning("no running loop for outline refresh conv=%s", cid)


async def backfill_care_outlines(
    *,
    sleep_s: float = 1.5,
    limit: int = 0,
    force: bool = False,
    only_missing: bool = True,
) -> dict[str, Any]:
    """Summarise Care (meta_cloud) conversations that already have CRM-stored messages."""
    summarized = 0
    skipped = 0
    failed = 0
    errors: list[str] = []

    async with SessionLocal() as db:
        care_ids = (
            await db.execute(
                select(WhatsAppInstance.id).where(
                    WhatsAppInstance.provider == PROVIDER_META_CLOUD,
                    WhatsAppInstance.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        if not care_ids:
            return {
                "ok": True,
                "care_instances": 0,
                "candidates": 0,
                "summarized": 0,
                "skipped": 0,
                "failed": 0,
            }

        has_msgs = exists().where(WhatsAppMessage.conversation_id == WhatsAppConversation.id)
        q = (
            select(WhatsAppConversation)
            .where(
                WhatsAppConversation.whatsapp_instance_id.in_(list(care_ids)),
                has_msgs,
            )
            .order_by(WhatsAppConversation.last_message_at.desc().nullslast())
        )
        if only_missing and not force:
            q = q.where(
                or_(
                    WhatsAppConversation.ai_outline == "",
                    WhatsAppConversation.ai_outline.is_(None),
                    ~WhatsAppConversation.ai_outline.startswith("{"),
                )
            )
        if limit and limit > 0:
            q = q.limit(limit)
        convs = (await db.execute(q)).scalars().all()
        candidates = len(convs)

    for conv_row in convs:
        cid = conv_row.id
        try:
            async with SessionLocal() as db:
                conv = await db.get(WhatsAppConversation, cid)
                if not conv:
                    skipped += 1
                    continue
                result = await refresh_conversation_outline(db, conv, user=None, force=force)
                if result.get("ok") and not result.get("skipped"):
                    await db.commit()
                    summarized += 1
                elif result.get("ok") and result.get("skipped"):
                    await db.rollback()
                    skipped += 1
                else:
                    await db.rollback()
                    failed += 1
                    err = result.get("error") or "unknown"
                    errors.append(f"{cid}:{err[:120]}")
                    if result.get("budget"):
                        break
        except Exception as exc:  # noqa: BLE001
            failed += 1
            errors.append(f"{cid}:{exc}"[:160])
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)

    return {
        "ok": True,
        "care_instances": len(care_ids),
        "candidates": candidates,
        "summarized": summarized,
        "skipped": skipped,
        "failed": failed,
        "errors": errors[:20],
        "sleep_s": sleep_s,
        "force": force,
        "only_missing": only_missing,
    }
