"""Sarvam Instant Voice Cloning — create saved voices + TTS with voice_id.

Official API (beta):
  POST /voices/create  — upload employee reference clip → voice_id (svc-…)
  POST /voices/clone   — text + voice_id → base64 audio
  GET  /voices         — list workspace clones
  DELETE /voices/delete/{voice_id}

Consent: only clone employee *outbound* WhatsApp samples; never customer inbound.
Admin-only create/delete. TTS reply to customers requires explicit per-instance opt-in.
"""
from __future__ import annotations

import base64
import logging
import struct
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings
from app.media_store import MEDIA_ROOT, resolve_path

logger = logging.getLogger("yesweigh.sarvam_voice")

DEFAULT_BASE_URL = "https://api.sarvam.ai"
# Sarvam recommends 10–15s single-speaker reference for create.
TARGET_DURATION_S = (10.0, 15.0)
ACCEPTABLE_DURATION_S = (8.0, 22.0)
MAX_CREATE_BYTES = 50 * 1024 * 1024
MAX_TTS_CHARS = 1000
CLONE_TIMEOUT_S = 120.0
TTS_TIMEOUT_S = 90.0


def _trim(value: str | None) -> str:
    return (value or "").strip()


def _api_key() -> str:
    return _trim(get_settings().sarvam_api_key)


def _base_url() -> str:
    settings = get_settings()
    return (_trim(settings.sarvam_base_url) or DEFAULT_BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    key = _api_key()
    if not key:
        raise RuntimeError("SARVAM_API_KEY is not configured")
    return {"api-subscription-key": key}


def ogg_opus_duration_seconds(path: Path) -> float | None:
    """Estimate Opus-in-Ogg duration from the last positive granule (48 kHz)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    last: int | None = None
    i = 0
    while True:
        j = data.find(b"OggS", i)
        if j < 0 or j + 14 > len(data):
            break
        granule = struct.unpack_from("<q", data, j + 6)[0]
        if granule > 0:
            last = granule
        i = j + 4
    if last is None:
        return None
    return last / 48000.0


def media_filename_from_url(media_url: str) -> str:
    raw = _trim(media_url)
    if "/api/whatsapp/media/" in raw:
        return raw.rsplit("/", 1)[-1]
    return Path(raw).name


def resolve_media_file(media_url: str) -> Path | None:
    name = media_filename_from_url(media_url)
    if not name:
        return None
    path = resolve_path(name)
    if path:
        return path
    # Fallback for scripts running with MEDIA_ROOT mounted
    alt = (MEDIA_ROOT / name).resolve()
    if alt.is_file() and str(alt).startswith(str(MEDIA_ROOT.resolve())):
        return alt
    return None


def duration_score(seconds: float | None) -> float:
    """Higher is better; prefer Sarvam's 10–15s band."""
    if seconds is None or seconds <= 0:
        return -1.0
    lo, hi = TARGET_DURATION_S
    alo, ahi = ACCEPTABLE_DURATION_S
    if lo <= seconds <= hi:
        # Prefer center (~12.5s)
        return 100.0 - abs(seconds - 12.5)
    if alo <= seconds <= ahi:
        return 50.0 - abs(seconds - 12.5)
    if 5.0 <= seconds < alo:
        return 10.0 - (alo - seconds)
    if ahi < seconds <= 35.0:
        return 10.0 - (seconds - ahi)
    return -1.0


async def create_voice(
    *,
    file_path: Path,
    name: str,
    language: str = "ml-IN",
    gender: str | None = "male",
    style: str | None = "conversational",
    remove_background_noise: bool = True,
) -> dict[str, Any]:
    """POST /voices/create — returns data dict with voice_id."""
    if not file_path.is_file():
        raise RuntimeError(f"Reference audio missing: {file_path}")
    size = file_path.stat().st_size
    if size <= 0 or size > MAX_CREATE_BYTES:
        raise RuntimeError(f"Reference audio size invalid ({size} bytes)")

    filename = file_path.name or "reference.ogg"
    mime = "audio/ogg" if filename.lower().endswith((".ogg", ".opus")) else "application/octet-stream"
    form: dict[str, Any] = {
        "name": _trim(name)[:100] or "employee-voice",
        "language": _trim(language) or "ml-IN",
        "remove_background_noise": "true" if remove_background_noise else "false",
    }
    if gender:
        form["gender"] = gender
    if style:
        form["style"] = style

    async with httpx.AsyncClient(timeout=CLONE_TIMEOUT_S) as client:
        with file_path.open("rb") as fh:
            files = {"file": (filename, fh, mime)}
            res = await client.post(
                f"{_base_url()}/voices/create",
                headers=_headers(),
                data=form,
                files=files,
            )
    if res.status_code not in {200, 201}:
        snippet = (res.text or "")[:400]
        raise RuntimeError(f"Sarvam create voice HTTP {res.status_code}: {snippet}")
    payload = res.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data.get("voice_id"):
        raise RuntimeError(f"Sarvam create voice missing voice_id: {payload!r}"[:400])
    return data


async def synthesize_clone(
    *,
    text: str,
    voice_id: str,
    language_code: str | None = "ml-IN",
) -> bytes:
    """POST /voices/clone with saved voice_id → decoded audio bytes (WAV/default)."""
    body = _trim(text)
    if not body:
        raise RuntimeError("TTS text is empty")
    if len(body) > MAX_TTS_CHARS:
        raise RuntimeError(f"TTS text exceeds {MAX_TTS_CHARS} characters — split client-side")
    vid = _trim(voice_id)
    if not vid:
        raise RuntimeError("voice_id is required")

    form: dict[str, Any] = {"text": body, "voice_id": vid}
    if language_code:
        form["language_code"] = _trim(language_code)

    async with httpx.AsyncClient(timeout=TTS_TIMEOUT_S) as client:
        res = await client.post(
            f"{_base_url()}/voices/clone",
            headers=_headers(),
            data=form,
        )
    if res.status_code != 200:
        snippet = (res.text or "")[:400]
        raise RuntimeError(f"Sarvam clone TTS HTTP {res.status_code}: {snippet}")
    payload = res.json()
    b64 = ""
    if isinstance(payload, dict):
        b64 = _trim(str(payload.get("audio_b64") or payload.get("audio") or ""))
    if not b64:
        raise RuntimeError("Sarvam clone TTS returned empty audio")
    try:
        return base64.b64decode(b64)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Sarvam clone TTS audio decode failed: {exc}") from exc


async def list_voices(*, search: str = "", limit: int = 50) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
    if _trim(search):
        params["search"] = _trim(search)
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.get(f"{_base_url()}/voices", headers=_headers(), params=params)
    if res.status_code != 200:
        snippet = (res.text or "")[:400]
        raise RuntimeError(f"Sarvam list voices HTTP {res.status_code}: {snippet}")
    payload = res.json()
    return payload if isinstance(payload, dict) else {"status": "error", "data": {"voices": [], "total": 0}}


async def delete_voice(voice_id: str) -> None:
    vid = _trim(voice_id)
    if not vid:
        raise RuntimeError("voice_id is required")
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.delete(f"{_base_url()}/voices/delete/{vid}", headers=_headers())
    if res.status_code not in {200, 204}:
        snippet = (res.text or "")[:400]
        raise RuntimeError(f"Sarvam delete voice HTTP {res.status_code}: {snippet}")
