"""Admin Voice Cloning settings + opt-in TTS reply (Sarvam Instant Voice Cloning).

Safety:
- Only outbound employee voice notes are eligible as clone samples.
- Create / delete / map voice_id: admin-only.
- Sending cloned TTS to a customer requires per-instance voice_tts_enabled=true
  AND an explicit request (never auto-blast).
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.media_store import save_bytes
from app.models import User, WhatsAppInstance, WhatsAppMessage
from app.sarvam_voice import (
    ACCEPTABLE_DURATION_S,
    TARGET_DURATION_S,
    create_voice,
    delete_voice,
    duration_score,
    list_voices,
    media_filename_from_url,
    ogg_opus_duration_seconds,
    resolve_media_file,
    synthesize_clone,
)

logger = logging.getLogger("yesweigh.voice_clones")

router = APIRouter(prefix="/api/voice-clones", tags=["voice-clones"])
Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

ORG = "yesweigh"
VOICE_TYPES = frozenset({"audio", "ptt", "voice"})


def _require_admin(user: User) -> None:
    if user.role.lower() not in {"admin", "super_admin", "super admin", "management"}:
        raise HTTPException(status_code=403, detail="Admin or Management role required")


class CreateCloneIn(BaseModel):
    whatsapp_instance_id: int
    message_id: int | None = None
    media_filename: str | None = None
    name: str | None = Field(default=None, max_length=100)
    language: str = Field(default="ml-IN", max_length=20)
    gender: str | None = Field(default="male", max_length=20)
    replace_existing: bool = False


class PatchCloneIn(BaseModel):
    voice_tts_enabled: bool | None = None
    sarvam_voice_id: str | None = Field(default=None, max_length=120)
    clear_voice: bool | None = None


class PreviewTtsIn(BaseModel):
    whatsapp_instance_id: int
    text: str = Field(min_length=1, max_length=1000)
    language_code: str = Field(default="ml-IN", max_length=20)


class SendVoiceTtsIn(BaseModel):
    conversation_public_id: str = Field(min_length=8, max_length=80)
    text: str = Field(min_length=1, max_length=1000)
    language_code: str = Field(default="ml-IN", max_length=20)
    # Must be true — blocks accidental customer sends
    confirm_send: bool = False


def _instance_out(inst: WhatsAppInstance) -> dict[str, Any]:
    return {
        "id": inst.id,
        "public_id": inst.public_id,
        "display_name": inst.display_name,
        "employee_name": inst.employee_name,
        "connected_phone_number": inst.connected_phone_number,
        "connection_status": inst.connection_status,
        "provider": inst.provider,
        "sarvam_voice_id": inst.sarvam_voice_id or "",
        "sarvam_voice_name": inst.sarvam_voice_name or "",
        "sarvam_voice_sample_url": inst.sarvam_voice_sample_url or "",
        "sarvam_voice_sample_message_id": inst.sarvam_voice_sample_message_id,
        "sarvam_voice_created_at": inst.sarvam_voice_created_at.isoformat()
        if inst.sarvam_voice_created_at
        else None,
        "voice_tts_enabled": bool(inst.voice_tts_enabled),
    }


@router.get("/status")
async def voice_clone_status(db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    from app.config import get_settings

    key_ok = bool((get_settings().sarvam_api_key or "").strip())
    rows = (
        await db.execute(
            select(WhatsAppInstance)
            .where(
                WhatsAppInstance.organization_id == ORG,
                WhatsAppInstance.deleted_at.is_(None),
                WhatsAppInstance.provider == "evolution",
            )
            .order_by(WhatsAppInstance.display_name)
        )
    ).scalars().all()
    return {
        "sarvam_configured": key_ok,
        "pricing_note": (
            "Instant Voice Cloning TTS ≈ ₹2.00 / 1,000 characters. "
            "Creating/listing/deleting saved voices is not billed per character. "
            "Reference: docs.sarvam.ai pricing (text_to_speech_voice_cloning)."
        ),
        "consent_note": (
            "Clones use employee outbound WhatsApp voice notes only. "
            "Admin-only create. Never clone customer inbound audio. "
            "Customer TTS send requires per-line Voice TTS enabled + explicit confirm."
        ),
        "target_duration_s": list(TARGET_DURATION_S),
        "acceptable_duration_s": list(ACCEPTABLE_DURATION_S),
        "instances": [_instance_out(i) for i in rows],
    }


@router.get("/samples/{instance_id}")
async def list_clone_samples(
    instance_id: int,
    db: Db,
    user: CurrentUser,
    limit: int = 20,
) -> dict[str, Any]:
    """Outbound employee voice notes suitable for Sarvam clone (duration-scored)."""
    _require_admin(user)
    inst = await db.get(WhatsAppInstance, instance_id)
    if not inst or inst.deleted_at or inst.organization_id != ORG:
        raise HTTPException(404, "WhatsApp instance not found")

    msgs = (
        await db.execute(
            select(WhatsAppMessage)
            .where(
                WhatsAppMessage.whatsapp_instance_id == instance_id,
                WhatsAppMessage.direction == "out",
                WhatsAppMessage.media_type.in_(list(VOICE_TYPES)),
                WhatsAppMessage.media_url.like("/api/whatsapp/media/%"),
            )
            .order_by(WhatsAppMessage.created_at.desc())
            .limit(min(max(limit * 4, 40), 200))
        )
    ).scalars().all()

    scored: list[dict[str, Any]] = []
    for msg in msgs:
        path = resolve_media_file(msg.media_url)
        if not path:
            continue
        dur = ogg_opus_duration_seconds(path)
        score = duration_score(dur)
        if score < 0:
            continue
        scored.append(
            {
                "message_id": msg.id,
                "media_url": msg.media_url,
                "media_filename": media_filename_from_url(msg.media_url),
                "duration_s": round(dur, 2) if dur is not None else None,
                "score": round(score, 2),
                "transcript_preview": (msg.transcript or "")[:120],
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
            }
        )
    scored.sort(key=lambda r: (-float(r["score"]), -(r["duration_s"] or 0)))
    return {
        "instance": _instance_out(inst),
        "samples": scored[: max(1, min(limit, 50))],
        "hint": "Prefer 10–15s clean single-speaker outbound clips (Sarvam recommendation).",
    }


@router.post("/create")
async def create_instance_clone(body: CreateCloneIn, db: Db, user: CurrentUser) -> dict[str, Any]:
    _require_admin(user)
    inst = await db.get(WhatsAppInstance, body.whatsapp_instance_id)
    if not inst or inst.deleted_at or inst.organization_id != ORG:
        raise HTTPException(404, "WhatsApp instance not found")
    if inst.provider != "evolution":
        raise HTTPException(400, "Voice clones are for Evolution personal lines only")
    if inst.sarvam_voice_id and not body.replace_existing:
        raise HTTPException(
            409,
            f"Instance already has voice_id {inst.sarvam_voice_id}. Pass replace_existing=true to recreate.",
        )

    msg: WhatsAppMessage | None = None
    media_url = ""
    if body.message_id:
        msg = await db.get(WhatsAppMessage, body.message_id)
        if (
            not msg
            or msg.whatsapp_instance_id != inst.id
            or msg.direction != "out"
            or (msg.media_type or "").lower() not in VOICE_TYPES
        ):
            raise HTTPException(400, "message_id must be an outbound voice note on this instance")
        media_url = msg.media_url
    elif body.media_filename:
        media_url = f"/api/whatsapp/media/{body.media_filename.strip()}"
    else:
        # Auto-pick best scored outbound sample
        samples = await list_clone_samples(inst.id, db, user, limit=5)
        picks = samples.get("samples") or []
        if not picks:
            raise HTTPException(400, "No suitable outbound voice samples found on disk")
        best = picks[0]
        media_url = str(best["media_url"])
        msg = await db.get(WhatsAppMessage, int(best["message_id"]))

    # Safety: refuse inbound paths (direction already checked when message_id set)
    if msg and msg.direction != "out":
        raise HTTPException(400, "Refusing to clone non-outbound audio (customers never cloned)")

    path = resolve_media_file(media_url)
    if not path:
        raise HTTPException(404, f"Media file not on disk: {media_url}")

    dur = ogg_opus_duration_seconds(path)
    if duration_score(dur) < 0:
        raise HTTPException(
            400,
            f"Sample duration {dur}s outside usable range {ACCEPTABLE_DURATION_S[0]}–{ACCEPTABLE_DURATION_S[1]}s",
        )

    voice_name = (body.name or "").strip() or f"YW {inst.display_name}".strip()[:100]
    old_id = (inst.sarvam_voice_id or "").strip()

    try:
        data = await create_voice(
            file_path=path,
            name=voice_name,
            language=body.language or "ml-IN",
            gender=body.gender,
        )
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc

    voice_id = str(data.get("voice_id") or "")
    if not voice_id:
        raise HTTPException(502, "Sarvam returned empty voice_id")

    if body.replace_existing and old_id and old_id != voice_id:
        try:
            await delete_voice(old_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to delete old voice %s: %s", old_id, exc)

    inst.sarvam_voice_id = voice_id
    inst.sarvam_voice_name = voice_name
    inst.sarvam_voice_sample_url = media_url
    inst.sarvam_voice_sample_message_id = msg.id if msg else None
    inst.sarvam_voice_created_at = datetime.now(timezone.utc)
    # Keep TTS off until admin explicitly enables
    if not body.replace_existing:
        inst.voice_tts_enabled = False
    await db.commit()
    await db.refresh(inst)

    return {
        "ok": True,
        "instance": _instance_out(inst),
        "sarvam": {
            "voice_id": voice_id,
            "status": data.get("status"),
            "reference_text": data.get("reference_text"),
            "background_noise_removed": data.get("background_noise_removed"),
            "sample_duration_s": round(dur, 2) if dur is not None else None,
            "sample_file": path.name,
        },
    }


@router.patch("/instances/{instance_id}")
async def patch_instance_clone(
    instance_id: int,
    body: PatchCloneIn,
    db: Db,
    user: CurrentUser,
) -> dict[str, Any]:
    _require_admin(user)
    inst = await db.get(WhatsAppInstance, instance_id)
    if not inst or inst.deleted_at or inst.organization_id != ORG:
        raise HTTPException(404, "WhatsApp instance not found")

    if body.clear_voice:
        old = (inst.sarvam_voice_id or "").strip()
        if old:
            try:
                await delete_voice(old)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Sarvam delete during clear failed for %s: %s", old, exc)
        inst.sarvam_voice_id = ""
        inst.sarvam_voice_name = ""
        inst.sarvam_voice_sample_url = ""
        inst.sarvam_voice_sample_message_id = None
        inst.sarvam_voice_created_at = None
        inst.voice_tts_enabled = False
    else:
        if body.sarvam_voice_id is not None:
            inst.sarvam_voice_id = body.sarvam_voice_id.strip()
        if body.voice_tts_enabled is not None:
            if body.voice_tts_enabled and not (inst.sarvam_voice_id or "").strip():
                raise HTTPException(400, "Cannot enable Voice TTS without a sarvam_voice_id")
            inst.voice_tts_enabled = bool(body.voice_tts_enabled)

    await db.commit()
    await db.refresh(inst)
    return {"ok": True, "instance": _instance_out(inst)}


@router.get("/sarvam-library")
async def sarvam_library(db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = db
    _require_admin(user)
    try:
        return await list_voices(limit=50)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/preview-tts")
async def preview_tts(body: PreviewTtsIn, db: Db, user: CurrentUser) -> dict[str, Any]:
    """Synthesize with the instance clone; returns audio as data URL (does not send WhatsApp)."""
    _require_admin(user)
    inst = await db.get(WhatsAppInstance, body.whatsapp_instance_id)
    if not inst or inst.deleted_at:
        raise HTTPException(404, "WhatsApp instance not found")
    vid = (inst.sarvam_voice_id or "").strip()
    if not vid:
        raise HTTPException(400, "No sarvam_voice_id on this instance — create a clone first")
    try:
        audio = await synthesize_clone(
            text=body.text,
            voice_id=vid,
            language_code=body.language_code or "ml-IN",
        )
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc

    # Persist briefly under wa-media so admin can download/play
    public_url, stored = save_bytes(audio, media_type="audio", filename="clone-preview.wav")
    b64 = base64.b64encode(audio).decode("ascii")
    return {
        "ok": True,
        "voice_id": vid,
        "media_url": public_url,
        "stored_filename": stored,
        "audio_data_url": f"data:audio/wav;base64,{b64}",
        "bytes": len(audio),
        "sent": False,
        "note": "Preview only — not sent to any customer.",
    }


@router.post("/send-tts")
async def send_tts_voice(body: SendVoiceTtsIn, db: Db, user: CurrentUser) -> dict[str, Any]:
    """Opt-in: TTS with instance clone → Evolution voice note.

    Requires:
    - confirm_send=true
    - instance.voice_tts_enabled=true
    - instance.sarvam_voice_id set
    """
    if not body.confirm_send:
        raise HTTPException(400, "confirm_send must be true to send cloned voice to a customer")

    from app.inbox import _get_conversation, _send_and_store

    conv = await _get_conversation(db, body.conversation_public_id, user)
    inst = await db.get(WhatsAppInstance, conv.whatsapp_instance_id)
    if not inst or inst.deleted_at:
        raise HTTPException(404, "WhatsApp instance missing")
    if not inst.voice_tts_enabled:
        raise HTTPException(
            403,
            "Voice TTS is disabled for this line. Enable it in Settings → Voice Clones (admin).",
        )
    vid = (inst.sarvam_voice_id or "").strip()
    if not vid:
        raise HTTPException(400, "No sarvam_voice_id — create a clone first")
    if inst.connection_status != "Connected":
        raise HTTPException(409, f"WhatsApp instance is {inst.connection_status}")

    try:
        audio = await synthesize_clone(
            text=body.text,
            voice_id=vid,
            language_code=body.language_code or "ml-IN",
        )
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc

    public_url, _stored = save_bytes(audio, media_type="audio", filename="clone-tts.wav")
    data_url = "data:audio/wav;base64," + base64.b64encode(audio).decode("ascii")

    msg = await _send_and_store(
        db,
        user,
        conv,
        inst,
        body.text,
        media_url=data_url,
        media_type="audio",
        media_name="voice-clone.wav",
    )
    # Prefer stored public path if send_and_store rewrote it
    if not msg.media_url:
        msg.media_url = public_url
    await db.commit()
    await db.refresh(msg)
    return {
        "ok": True,
        "message_id": msg.id,
        "public_id": msg.public_id,
        "media_url": msg.media_url,
        "voice_id": vid,
        "status": msg.status,
    }
