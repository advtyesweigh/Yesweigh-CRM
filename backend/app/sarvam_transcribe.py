"""Sarvam Saaras speech-to-text for WhatsApp voice notes (CRM inbox).

Auto-transcribes inbound/outbound audio after media is on disk.
Sync REST for ≤30s; batch job API for longer notes (or sync 400 duration limit).
Never logs or returns the full API key. Graceful no-op if Sarvam is down / unset.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.media_store import PUBLIC_PREFIX, resolve_path
from app.models import WhatsAppMessage

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.sarvam.ai"
DEFAULT_MODEL = "saaras:v3"
MAX_AUDIO_BYTES = 16 * 1024 * 1024
# Sync REST hard-caps at 30s; batch job API handles longer notes.
SYNC_MAX_SECONDS = 30
PROCESSING_STALE_MS = 4 * 60 * 1000
BATCH_POLL_SECONDS = 3.0
BATCH_DEADLINE_SECONDS = 8 * 60
VOICE_TYPES = frozenset({"audio", "ptt", "voice"})
_DURATION_LIMIT_RE = re.compile(
    r"duration exceeds|maximum limit of\s*30|longer audio|batch API for longer",
    re.I,
)


def _trim(value: Any) -> str:
    return str(value or "").strip()


def _mask_key(key: str) -> str:
    k = _trim(key)
    if len(k) < 10:
        return "sk_…"
    return f"{k[:8]}…"


def is_voice_type(media_type: str) -> bool:
    return _trim(media_type).lower() in VOICE_TYPES


def _base_url() -> str:
    settings = get_settings()
    return (_trim(settings.sarvam_base_url) or DEFAULT_BASE_URL).rstrip("/")


def _stt_url() -> str:
    return f"{_base_url()}/speech-to-text"


def _stt_job_url() -> str:
    return f"{_base_url()}/speech-to-text/job/v1"


def _stt_model() -> str:
    settings = get_settings()
    return _trim(settings.sarvam_stt_model) or DEFAULT_MODEL


def _api_key() -> str:
    return _trim(get_settings().sarvam_api_key)


def _mime_for_path(path: Path, media_name: str = "") -> str:
    name = media_name or path.name
    guessed, _ = mimetypes.guess_type(name)
    if guessed and guessed.startswith("audio/"):
        return guessed
    suffix = path.suffix.lower()
    return {
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".webm": "audio/webm",
        ".mp3": "audio/mpeg",
        ".mpeg": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".amr": "audio/amr",
    }.get(suffix, "audio/ogg")


def _is_duration_limit_error(message: str) -> bool:
    return bool(_DURATION_LIMIT_RE.search(_trim(message)))


def load_audio_bytes(msg: WhatsAppMessage) -> tuple[bytes, str, str] | None:
    """Return (bytes, filename, mime) from local media store, or None."""
    media_url = _trim(msg.media_url)
    if not media_url.startswith(PUBLIC_PREFIX + "/"):
        return None
    stored = media_url.rsplit("/", 1)[-1]
    path = resolve_path(stored)
    if not path:
        return None
    data = path.read_bytes()
    if not data:
        return None
    filename = _trim(msg.media_name) or path.name or "voice.ogg"
    return data, filename, _mime_for_path(path, filename)


def _signed_file_url(entry: Any) -> str:
    if isinstance(entry, str):
        return _trim(entry)
    if isinstance(entry, dict):
        return _trim(entry.get("file_url"))
    return ""


async def _sarvam_job_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    api_key: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"api-subscription-key": api_key, "Accept": "application/json"}
    if body is None:
        res = await client.get(url, headers=headers)
    else:
        headers["Content-Type"] = "application/json"
        res = await client.post(url, headers=headers, json=body)
    payload = res.json() if res.content else {}
    if res.status_code >= 400:
        snippet = (res.text or "").replace("\n", " ")[:180]
        raise RuntimeError(f"Sarvam STT batch HTTP {res.status_code}: {snippet}")
    return payload if isinstance(payload, dict) else {}


async def sarvam_speech_to_text_sync(
    audio: bytes,
    *,
    filename: str,
    mime_type: str,
    api_key: str | None = None,
) -> dict[str, str]:
    """Call Sarvam Saaras sync REST STT (≤30s). Auth: api-subscription-key."""
    key = _trim(api_key) or _api_key()
    if not key:
        raise RuntimeError("SARVAM_API_KEY is not configured")
    if len(audio) > MAX_AUDIO_BYTES:
        raise RuntimeError("Voice note is larger than 16 MB")

    model = _stt_model()
    url = _stt_url()
    files = {"file": (filename or "voice.ogg", audio, mime_type or "audio/ogg")}
    data = {
        "model": model,
        "mode": "transcribe",
        "language_code": "unknown",
    }
    headers = {"api-subscription-key": key, "Accept": "application/json"}

    async with httpx.AsyncClient(timeout=60.0) as client:
        res = await client.post(url, headers=headers, files=files, data=data)

    if res.status_code >= 400:
        snippet = (res.text or "").replace("\n", " ")[:180]
        raise RuntimeError(f"Sarvam STT HTTP {res.status_code}: {snippet}")

    payload = res.json() if res.content else {}
    transcript = _trim(payload.get("transcript"))
    language = _trim(payload.get("language_code"))
    return {"transcript": transcript, "language_code": language, "model": model}


async def sarvam_speech_to_text_batch(
    audio: bytes,
    *,
    filename: str,
    mime_type: str,
    api_key: str | None = None,
) -> dict[str, str]:
    """Sarvam batch job API for voice notes longer than the sync 30s limit."""
    key = _trim(api_key) or _api_key()
    if not key:
        raise RuntimeError("SARVAM_API_KEY is not configured")
    if len(audio) > MAX_AUDIO_BYTES:
        raise RuntimeError("Voice note is larger than 16 MB")

    model = _stt_model()
    job_base = _stt_job_url()
    safe_name = Path(filename or "voice.ogg").name or "voice.ogg"
    # Keep a simple extension Sarvam accepts for signed uploads.
    if "." not in safe_name:
        safe_name = f"{safe_name}.ogg"
    raw_mime = (_trim(mime_type).split(";")[0] or "audio/ogg").lower()
    if not (raw_mime.startswith("audio/") or raw_mime.startswith("video/")):
        raw_mime = "audio/ogg"

    timeout = httpx.Timeout(120.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        created = await _sarvam_job_json(
            client,
            job_base,
            api_key=key,
            body={
                "job_parameters": {
                    "model": model,
                    "mode": "transcribe",
                    "language_code": "unknown",
                }
            },
        )
        job_id = _trim(created.get("job_id"))
        if not job_id:
            raise RuntimeError("Sarvam STT batch did not return a job_id")

        uploads = await _sarvam_job_json(
            client,
            f"{job_base}/upload-files",
            api_key=key,
            body={"job_id": job_id, "files": [safe_name]},
        )
        upload_map = uploads.get("upload_urls") if isinstance(uploads.get("upload_urls"), dict) else {}
        upload_url = _signed_file_url(upload_map.get(safe_name))
        if not upload_url:
            raise RuntimeError("Sarvam STT batch did not return an upload URL")

        put_headers: dict[str, str] = {"Content-Type": raw_mime}
        container = _trim(uploads.get("storage_container_type"))
        if container in {"", "Azure", "Azure_V1"}:
            put_headers["x-ms-blob-type"] = "BlockBlob"
        put = await client.put(upload_url, headers=put_headers, content=audio)
        if put.status_code >= 400:
            snippet = (put.text or "").replace("\n", " ")[:120]
            raise RuntimeError(f"Sarvam STT batch upload failed HTTP {put.status_code}: {snippet}")

        await _sarvam_job_json(
            client,
            f"{job_base}/{job_id}/start",
            api_key=key,
            body={},
        )

        deadline = time.monotonic() + BATCH_DEADLINE_SECONDS
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            status = await _sarvam_job_json(
                client,
                f"{job_base}/{job_id}/status",
                api_key=key,
            )
            state = _trim(status.get("job_state"))
            if state in {"Completed", "PartiallyCompleted"}:
                break
            if state == "Failed":
                raise RuntimeError("Sarvam STT batch job failed")
            await asyncio.sleep(BATCH_POLL_SECONDS)
        else:
            raise RuntimeError("Sarvam STT batch timed out waiting for result")

        state = _trim(status.get("job_state"))
        if state not in {"Completed", "PartiallyCompleted"}:
            raise RuntimeError(f"Sarvam STT batch unfinished state={state or 'unknown'}")

        result_files: list[str] = []
        for detail in status.get("job_details") or []:
            if not isinstance(detail, dict):
                continue
            detail_state = _trim(detail.get("state"))
            if detail_state and detail_state != "Success":
                continue
            for output in detail.get("outputs") or []:
                if not isinstance(output, dict):
                    continue
                name = _trim(output.get("file_name"))
                if name:
                    result_files.append(name)
        if not result_files:
            result_files = ["0.json"]

        downloads = await _sarvam_job_json(
            client,
            f"{job_base}/download-files",
            api_key=key,
            body={"job_id": job_id, "files": result_files},
        )
        download_map = (
            downloads.get("download_urls") if isinstance(downloads.get("download_urls"), dict) else {}
        )
        result_name = next(
            (name for name in result_files if _signed_file_url(download_map.get(name))),
            result_files[0],
        )
        result_url = _signed_file_url(download_map.get(result_name))
        if not result_url:
            raise RuntimeError("Sarvam STT batch did not return a result URL")

        result_res = await client.get(result_url)
        if result_res.status_code >= 400:
            raise RuntimeError(f"Sarvam STT batch result HTTP {result_res.status_code}")
        result = result_res.json() if result_res.content else {}
        if not isinstance(result, dict):
            result = {}

    transcript = _trim(result.get("transcript"))
    language = _trim(result.get("language_code"))
    return {"transcript": transcript, "language_code": language, "model": model}


async def sarvam_speech_to_text(
    audio: bytes,
    *,
    filename: str,
    mime_type: str,
    api_key: str | None = None,
) -> dict[str, str]:
    """Sync REST first (fast ≤30s); on duration-limit 400, use batch job API."""
    try:
        return await sarvam_speech_to_text_sync(
            audio, filename=filename, mime_type=mime_type, api_key=api_key
        )
    except RuntimeError as exc:
        if not _is_duration_limit_error(str(exc)):
            raise
        logger.info(
            "sarvam sync hit %ss limit; falling back to batch (%s bytes)",
            SYNC_MAX_SECONDS,
            len(audio),
        )

    return await sarvam_speech_to_text_batch(
        audio, filename=filename, mime_type=mime_type, api_key=api_key
    )


def schedule_transcription(message_db_id: int, *, force: bool = False) -> None:
    """Fire-and-forget from request/webhook handlers."""

    async def _runner() -> None:
        try:
            await process_transcription(message_db_id, force=force)
        except Exception:  # noqa: BLE001
            logger.exception("scheduled transcription failed message_id=%s", message_db_id)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_runner())


async def process_transcription(message_db_id: int, *, force: bool = False) -> dict[str, Any]:
    """Download local audio → Sarvam → store transcript on the same message row."""
    if not _api_key():
        return {"status": "skipped", "reason": "no-api-key"}

    async with SessionLocal() as db:
        msg = await db.get(WhatsAppMessage, message_db_id)
        if not msg:
            return {"status": "skipped", "reason": "not-found"}
        if not is_voice_type(msg.media_type):
            return {"status": "skipped", "reason": "not-audio"}
        if not _trim(msg.media_url):
            return {"status": "skipped", "reason": "no-media"}

        status = _trim(msg.transcription_status).lower()
        if not force and status == "completed" and _trim(msg.transcript):
            return {"status": "skipped", "reason": "already-completed"}
        if not force and status == "processing":
            # Soft skip — another worker may be mid-flight
            return {"status": "skipped", "reason": "already-processing"}

        msg.transcription_status = "processing"
        msg.transcription_error = ""
        await db.commit()

        audio_pack = load_audio_bytes(msg)
        if not audio_pack:
            msg.transcription_status = "failed"
            msg.transcription_error = "Voice media file is not available yet."
            msg.transcript = ""
            await db.commit()
            return {"status": "failed", "reason": "media-missing"}

        audio, filename, mime = audio_pack
        try:
            result = await sarvam_speech_to_text(audio, filename=filename, mime_type=mime)
        except Exception as exc:  # noqa: BLE001
            safe = str(exc).replace("\n", " ")[:180]
            # Never echo the key if somehow present
            safe = safe.replace(_api_key(), _mask_key(_api_key()))
            logger.warning("sarvam stt failed message_id=%s err=%s", message_db_id, safe)
            msg.transcription_status = "failed"
            msg.transcription_error = safe or "Could not transcribe audio"
            msg.transcript = ""
            await db.commit()
            return {"status": "failed", "reason": safe}

        transcript = result.get("transcript") or ""
        if not transcript:
            msg.transcription_status = "failed"
            msg.transcription_error = "No speech detected in this voice note."
            msg.transcript = ""
            msg.transcript_language = result.get("language_code") or ""
            await db.commit()
            return {"status": "failed", "reason": "empty-transcript"}

        msg.transcript = transcript
        msg.transcript_language = result.get("language_code") or ""
        msg.transcription_status = "completed"
        msg.transcription_error = ""
        await db.commit()
        return {
            "status": "completed",
            "language_code": msg.transcript_language,
            "chars": len(transcript),
        }


async def retry_transcription_by_public_id(public_id: str) -> dict[str, Any]:
    async with SessionLocal() as db:
        msg = (
            await db.execute(
                select(WhatsAppMessage).where(WhatsAppMessage.public_id == public_id)
            )
        ).scalar_one_or_none()
        if not msg:
            return {"ok": False, "status": "failed", "reason": "not-found"}
        mid = msg.id
        if _trim(msg.transcription_status).lower() == "completed" and _trim(msg.transcript):
            return {
                "ok": True,
                "status": "completed",
                "skipped": True,
                "transcript": msg.transcript,
                "transcript_language": msg.transcript_language or "",
            }
    result = await process_transcription(mid, force=True)
    async with SessionLocal() as db:
        msg = await db.get(WhatsAppMessage, mid)
        return {
            "ok": result.get("status") == "completed",
            **result,
            "transcript": (msg.transcript if msg else "") or "",
            "transcript_language": (msg.transcript_language if msg else "") or "",
            "transcription_error": (msg.transcription_error if msg else "") or "",
        }


async def backfill_voice_transcripts(
    *,
    customer_phone_digits: str = "",
    limit: int = 50,
    force: bool = False,
) -> dict[str, Any]:
    """Transcribe existing voice messages that have media on disk (e.g. Fahim)."""
    digits = "".join(c for c in customer_phone_digits if c.isdigit())
    async with SessionLocal() as db:
        from app.models import WhatsAppConversation

        q = (
            select(WhatsAppMessage)
            .where(WhatsAppMessage.media_type.in_(list(VOICE_TYPES)))
            .where(WhatsAppMessage.media_url != "")
            .order_by(WhatsAppMessage.id.desc())
            .limit(limit)
        )
        if digits:
            conv_ids = (
                await db.execute(
                    select(WhatsAppConversation.id).where(
                        WhatsAppConversation.customer_phone.ilike(f"%{digits[-10:]}")
                    )
                )
            ).scalars().all()
            if not conv_ids:
                return {"ok": True, "matched": 0, "completed": 0, "failed": 0, "skipped": 0}
            q = q.where(WhatsAppMessage.conversation_id.in_(list(conv_ids)))

        rows = (await db.execute(q)).scalars().all()
        ids = [r.id for r in rows]

    completed = failed = skipped = 0
    samples: list[str] = []
    for mid in reversed(ids):
        result = await process_transcription(mid, force=force)
        st = result.get("status")
        if st == "completed":
            completed += 1
            async with SessionLocal() as db:
                msg = await db.get(WhatsAppMessage, mid)
                if msg and msg.transcript and len(samples) < 3:
                    samples.append((msg.transcript or "")[:120])
        elif st == "failed":
            failed += 1
        else:
            skipped += 1

    return {
        "ok": True,
        "matched": len(ids),
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
        "samples": samples,
        "key_prefix": _mask_key(_api_key()),
        "endpoint": _stt_url(),
        "model": _stt_model(),
    }
