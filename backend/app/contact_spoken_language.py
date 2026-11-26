"""Customer spoken language from voice STT — not translated transcript script."""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WhatsAppConversation, WhatsAppMessage

VOICE_TYPES = frozenset({"audio", "ptt", "voice", "video"})

# ISO 639-1 (+ common Sarvam BCP-47 bases)
_CONTACT_LANG_ALIASES: dict[str, str] = {
    "ml": "ml",
    "ml-in": "ml",
    "malayalam": "ml",
    "മലയാളം": "ml",
    "en": "en",
    "en-in": "en",
    "english": "en",
    "hi": "hi",
    "hi-in": "hi",
    "hindi": "hi",
    "ta": "ta",
    "ta-in": "ta",
    "tamil": "ta",
    "தமிழ்": "ta",
    "te": "te",
    "te-in": "te",
    "telugu": "te",
    "kn": "kn",
    "kn-in": "kn",
    "kannada": "kn",
    "mr": "mr",
    "bn": "bn",
    "gu": "gu",
    "pa": "pa",
    "pa-in": "pa",
    "mr-in": "mr",
    "bn-in": "bn",
    "gu-in": "gu",
    "od": "od",
    "od-in": "od",
    "odia": "od",
    "or": "od",
    "or-in": "od",
    "ur": "ur",
    "as": "as",
    "as-in": "as",
    "assamese": "as",
}

_TAMIL_SCRIPT_RE = re.compile(r"[\u0B80-\u0BFF]")
_MALAYALAM_SCRIPT_RE = re.compile(r"[\u0D00-\u0D7F]")


def normalize_spoken_language_code(raw: str | None) -> str:
    """Map Sarvam/Gemini STT language_code to a short ISO code (ta, ml, …)."""
    token = (raw or "").strip().lower().replace("_", "-")
    if not token or token in {"unknown", "auto", "null", "none", "mixed"}:
        return ""
    if token in _CONTACT_LANG_ALIASES:
        return _CONTACT_LANG_ALIASES[token]
    base = token.split("-", 1)[0]
    return _CONTACT_LANG_ALIASES.get(base, base if len(base) == 2 else "")


def normalize_contact_language_field(raw: str | None) -> str:
    """Normalize conversation.language for storage (ISO code when possible)."""
    return normalize_spoken_language_code(raw) or (raw or "").strip()[:40]


async def latest_inbound_spoken_language(
    db: AsyncSession, conversation_id: int, *, limit: int = 30
) -> str:
    """Most recent inbound voice message with a stored spoken_language."""
    rows = (
        await db.execute(
            select(WhatsAppMessage)
            .where(WhatsAppMessage.conversation_id == conversation_id)
            .where(WhatsAppMessage.direction == "in")
            .where(WhatsAppMessage.media_type.in_(list(VOICE_TYPES)))
            .order_by(WhatsAppMessage.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    for msg in rows:
        spoken = normalize_spoken_language_code(getattr(msg, "spoken_language", None) or "")
        if spoken:
            return spoken
        # Legacy rows: STT tag before ml translate may still be on transcript_language
        legacy = normalize_spoken_language_code(msg.transcript_language or "")
        target_ml = legacy == "ml"
        if legacy and not target_ml:
            return legacy
    return ""


async def resolve_conversation_language(
    db: AsyncSession, conv: WhatsAppConversation
) -> str:
    """Display/API language: explicit contact field, else latest inbound voice STT."""
    explicit = normalize_spoken_language_code(conv.language or "")
    if explicit:
        return explicit
    return await latest_inbound_spoken_language(db, conv.id)


def should_apply_voice_language_to_conversation(
    conv: WhatsAppConversation, spoken_code: str
) -> bool:
    """Inbound customer voice updates contact language from audio ID."""
    if not spoken_code:
        return False
    current = normalize_spoken_language_code(conv.language or "")
    if not current:
        return True
    # Prefer spoken audio when transcript display language disagrees (e.g. ta → ml translate).
    if current == "ml" and spoken_code != "ml":
        return True
    return current != spoken_code


async def apply_voice_spoken_language(
    db: AsyncSession,
    msg: WhatsAppMessage,
    spoken_raw: str,
    *,
    force: bool = False,
) -> str | None:
    """After STT, persist spoken_language on the message and sync conversation.language."""
    spoken = normalize_spoken_language_code(spoken_raw)
    if spoken:
        msg.spoken_language = spoken[:40]
    if msg.direction != "in" or not spoken:
        return None
    conv = await db.get(WhatsAppConversation, msg.conversation_id)
    if not conv:
        return None
    if force or should_apply_voice_language_to_conversation(conv, spoken):
        conv.language = spoken
        return spoken
    return None


def chat_has_malayalam_from_text_only(messages: list[WhatsAppMessage]) -> bool:
    """Script detect for outline language — ignore translated voice transcripts."""
    blob_parts: list[str] = []
    for msg in messages:
        mt = (msg.media_type or "").strip().lower()
        if mt in VOICE_TYPES:
            spoken = normalize_spoken_language_code(getattr(msg, "spoken_language", None) or "")
            if spoken and spoken != "ml":
                continue
            continue
        body = (msg.body or "").strip()
        if body and body not in {"[media]", "media", "🎤 Voice message", "🎥 Video"}:
            blob_parts.append(body)
    blob = "\n".join(blob_parts)
    if not blob.strip():
        return False
    return bool(_MALAYALAM_SCRIPT_RE.search(blob))


def override_outline_customer_language(
    summary: dict, *, contact_language: str
) -> dict:
    """JSON outline `language` = customer's spoken language, not transcript script."""
    code = normalize_spoken_language_code(contact_language)
    if code:
        summary["language"] = code
    return summary


async def reconcile_conversation_languages_from_voice(
    db: AsyncSession,
    *,
    conversation_ids: list[int] | None = None,
    limit: int = 20_000,
) -> dict[str, int | list[dict[str, str]]]:
    """Sync whatsapp_conversations.language from latest inbound voice spoken_language (no STT)."""
    if conversation_ids is not None:
        ids = list(conversation_ids)[:limit]
    else:
        q = (
            select(WhatsAppMessage.conversation_id)
            .where(WhatsAppMessage.direction == "in")
            .where(WhatsAppMessage.media_type.in_(list(VOICE_TYPES)))
            .group_by(WhatsAppMessage.conversation_id)
            .order_by(WhatsAppMessage.conversation_id.asc())
            .limit(limit)
        )
        ids = list((await db.execute(q)).scalars().all())

    updated = 0
    unchanged = 0
    no_spoken = 0
    samples: list[dict[str, str]] = []

    for cid in ids:
        conv = await db.get(WhatsAppConversation, cid)
        if not conv:
            continue
        latest = await latest_inbound_spoken_language(db, cid)
        if not latest:
            no_spoken += 1
            continue
        current = normalize_spoken_language_code(conv.language or "")
        if current == latest:
            unchanged += 1
            continue
        if should_apply_voice_language_to_conversation(conv, latest):
            before = current or "—"
            conv.language = latest
            updated += 1
            if len(samples) < 12:
                phone = (conv.customer_phone or "")[-10:]
                samples.append(
                    {
                        "phone": phone,
                        "conversation_id": str(cid),
                        "before": before,
                        "after": latest,
                    }
                )

    if updated:
        await db.commit()
    return {
        "checked": len(ids),
        "updated": updated,
        "unchanged": unchanged,
        "no_spoken_language": no_spoken,
        "samples": samples,
    }