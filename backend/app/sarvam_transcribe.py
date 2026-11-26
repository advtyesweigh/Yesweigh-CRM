"""Sarvam Saaras speech-to-text + Malayalam translate for WhatsApp (CRM inbox).

Voice / video: auto-transcribes inbound/outbound speech after media is on disk
(video → ffmpeg audio extract), then (by default) translates the transcript into
Malayalam (ml-IN) via Sarvam /translate.

Text: translates plain / caption bodies (Hinglish, Hindi, English, …) into
Malayalam and stores the result on ``transcript`` while leaving ``body`` as the
original. Gemini is used when Sarvam is unset or out of quota.

Sync REST for ≤30s; batch job API for longer notes (or sync 400 duration limit).
Never logs or returns the full API key. Graceful no-op if translators are down.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import shutil
import subprocess
import tempfile
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
DEFAULT_STT_MODE = "transcribe"
DEFAULT_TARGET_LANGUAGE = "ml-IN"
DEFAULT_TRANSLATE_MODEL = "mayura:v1"
MAX_AUDIO_BYTES = 16 * 1024 * 1024
# Mayura:v1 = 1000 chars; sarvam-translate:v1 = 2000. Chunk conservatively for mayura.
TRANSLATE_MAX_CHARS = {
    "mayura:v1": 1000,
    "sarvam-translate:v1": 2000,
}
# Sync REST hard-caps at 30s; batch job API handles longer notes.
SYNC_MAX_SECONDS = 30
PROCESSING_STALE_MS = 4 * 60 * 1000
BATCH_POLL_SECONDS = 3.0
BATCH_DEADLINE_SECONDS = 8 * 60
VOICE_TYPES = frozenset({"audio", "ptt", "voice"})
VIDEO_TYPES = frozenset({"video"})
SPEECH_MEDIA_TYPES = VOICE_TYPES | VIDEO_TYPES
VIDEO_FILE_SUFFIXES = frozenset(
    {".mp4", ".3gp", ".3gpp", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
)
MEDIA_PLACEHOLDER_BODIES = frozenset(
    {
        "[media]",
        "media",
        "📷 Photo",
        "🎥 Video",
        "🎤 Voice message",
        "📄 Document",
        "Sticker",
        "Attachment",
        "Unsupported message",
    }
)
_PLACEHOLDER_BODY_RE = re.compile(
    r"^(📷|🎥|🎤|📄)?\s*(Photo|Video|Voice message|Document|Sticker|\[media\]|Attachment)$",
    re.I,
)
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


def is_speech_media_type(media_type: str) -> bool:
    """Voice notes and videos with spoken audio — eligible for Sarvam STT."""
    return _trim(media_type).lower() in SPEECH_MEDIA_TYPES


def is_media_placeholder_body(text: str) -> bool:
    cleaned = _trim(text)
    if not cleaned:
        return True
    if cleaned in MEDIA_PLACEHOLDER_BODIES:
        return True
    return bool(_PLACEHOLDER_BODY_RE.match(cleaned))


def is_text_translatable_message(msg: WhatsAppMessage) -> bool:
    """True for plain text / media captions that should get a Malayalam ``transcript``."""
    if is_speech_media_type(msg.media_type):
        return False
    if _trim(msg.direction).lower() == "note":
        return False
    if _trim(msg.media_type).lower() == "unsupported":
        return False
    return not is_media_placeholder_body(msg.body)


def _can_translate() -> bool:
    """Sarvam and/or Gemini can produce Malayalam translations."""
    settings = get_settings()
    return bool(_api_key() or _trim(getattr(settings, "gemini_api_key", "")))


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


def _stt_mode() -> str:
    settings = get_settings()
    return _trim(getattr(settings, "sarvam_stt_mode", "")) or DEFAULT_STT_MODE


def _target_language() -> str:
    """BCP-47 target for displayed transcripts (empty = keep STT source language)."""
    settings = get_settings()
    return _trim(getattr(settings, "sarvam_transcript_target_language", DEFAULT_TARGET_LANGUAGE))


def _translate_model() -> str:
    settings = get_settings()
    return _trim(getattr(settings, "sarvam_translate_model", "")) or DEFAULT_TRANSLATE_MODEL


def _translate_url() -> str:
    return f"{_base_url()}/translate"


def _api_key() -> str:
    return _trim(get_settings().sarvam_api_key)


def _lang_base(code: str) -> str:
    """hi-IN / hi → hi; unknown/auto → empty."""
    raw = _trim(code).lower().replace("_", "-")
    if not raw or raw in {"unknown", "auto", "null", "none"}:
        return ""
    return raw.split("-", 1)[0]


def _same_language(a: str, b: str) -> bool:
    ba, bb = _lang_base(a), _lang_base(b)
    return bool(ba and bb and ba == bb)


# Tamil script must not remain when display target is Malayalam (STT/tag can be wrong).
_TAMIL_SCRIPT_RE = re.compile(r"[\u0B80-\u0BFF]")
_MALAYALAM_SCRIPT_RE = re.compile(r"[\u0D00-\u0D7F]")
_LETTERISH_RE = re.compile(r"[A-Za-z\u0900-\u0D7F]")


def _has_tamil_script(text: str) -> bool:
    return bool(_TAMIL_SCRIPT_RE.search(text or ""))


def _has_malayalam_script(text: str) -> bool:
    return bool(_MALAYALAM_SCRIPT_RE.search(text or ""))


def _needs_target_translation(text: str, source_language_code: str, target: str) -> bool:
    """True when stored/STT text is not yet suitable as the configured display language."""
    if not _trim(text) or not _trim(target):
        return False
    # Untagged Care/group posts that are already dominant Malayalam — skip API.
    if _lang_base(target) == "ml" and _has_malayalam_script(text) and not _has_tamil_script(text):
        ml_n = len(_MALAYALAM_SCRIPT_RE.findall(text))
        latin_n = len(re.findall(r"[A-Za-z]", text or ""))
        if ml_n >= 6 and ml_n >= latin_n:
            return False
    if not _same_language(source_language_code, target):
        return True
    if _lang_base(target) == "ml":
        # Tagged ml-IN but body still has Tamil glyphs (pure Tamil or mixed leftovers).
        if _has_tamil_script(text):
            return True
        # Tagged ml-IN but no Malayalam script (e.g. English fallback mistake).
        if _LETTERISH_RE.search(text) and not _has_malayalam_script(text):
            return True
        return False


def _translate_source_hint(text: str, source_language_code: str, target: str) -> str:
    """Prefer an explicit source when forcing rewrite of mis-tagged / wrong-script text."""
    source = _trim(source_language_code)
    if _lang_base(target) != "ml":
        return source or "auto"
    if _has_tamil_script(text) and not _has_malayalam_script(text):
        return "ta-IN"
    if _has_tamil_script(text) and (_same_language(source, target) or not source):
        return "auto"
    # Tagged ml but Latin/other with no Malayalam glyphs → treat as English/auto.
    if (not source or _same_language(source, target)) and _LETTERISH_RE.search(text) and not _has_malayalam_script(text):
        return "en-IN" if re.search(r"[A-Za-z]", text or "") else "auto"
    return source or "auto"


# Residual STT mixups: Tamil lookalike glyphs inside otherwise-Malayalam text.
_TA_TO_ML_SPECIAL = str.maketrans(
    {
        "\u0B85": "\u0D05",
        "\u0B86": "\u0D06",
        "\u0B87": "\u0D07",
        "\u0B88": "\u0D08",
        "\u0B89": "\u0D09",
        "\u0B8A": "\u0D0A",
        "\u0B8E": "\u0D0E",
        "\u0B8F": "\u0D0F",
        "\u0B90": "\u0D10",
        "\u0B92": "\u0D12",
        "\u0B93": "\u0D13",
        "\u0B94": "\u0D14",
        "\u0B95": "\u0D15",  # க → ക
        "\u0B99": "\u0D19",  # ங → ങ
        "\u0B9A": "\u0D1A",  # ச → ച
        "\u0B9C": "\u0D1C",  # ஜ → ജ
        "\u0B9E": "\u0D1E",  # ஞ → ഞ
        "\u0B9F": "\u0D1F",  # ட → ട
        "\u0BA3": "\u0D23",  # ண → ണ
        "\u0BA4": "\u0D24",  # த → ത
        "\u0BA8": "\u0D28",  # ந → ന
        "\u0BA9": "\u0D28",  # ன → ന
        "\u0BAA": "\u0D2A",  # ப → പ
        "\u0BAE": "\u0D2E",  # ம → മ
        "\u0BAF": "\u0D2F",  # ய → യ
        "\u0BB0": "\u0D30",  # ர → ര
        "\u0BB1": "\u0D31",  # ற → റ
        "\u0BB2": "\u0D32",  # ல → ല
        "\u0BB3": "\u0D33",  # ள → ള
        "\u0BB4": "\u0D34",  # ழ → ഴ
        "\u0BB5": "\u0D35",  # வ → വ
        "\u0BB7": "\u0D37",  # ஷ → ഷ
        "\u0BB8": "\u0D38",  # ஸ → സ
        "\u0BB9": "\u0D39",  # ஹ → ഹ
        "\u0BBE": "\u0D3E",  # ா
        "\u0BBF": "\u0D3F",  # ி
        "\u0BC0": "\u0D40",  # ீ
        "\u0BC1": "\u0D41",  # ு
        "\u0BC2": "\u0D42",  # ூ
        "\u0BC6": "\u0D46",  # ெ
        "\u0BC7": "\u0D47",  # ே
        "\u0BC8": "\u0D48",  # ை
        "\u0BCA": "\u0D4A",  # ொ
        "\u0BCB": "\u0D4B",  # ோ
        "\u0BCC": "\u0D4C",  # ௌ
        "\u0BCD": "\u0D4D",  # ்
    }
)


def _scrub_tamil_script_to_malayalam(text: str) -> str:
    """Map leftover Tamil-block glyphs to Malayalam lookalikes (no API call)."""
    if not _has_tamil_script(text):
        return text
    out: list[str] = []
    for ch in text:
        if "\u0B80" <= ch <= "\u0BFF":
            mapped = ch.translate(_TA_TO_ML_SPECIAL)
            if mapped == ch:
                cand = chr(ord(ch) + 0x80)
                mapped = cand if "\u0D00" <= cand <= "\u0D7F" else ch
            out.append(mapped)
        else:
            out.append(ch)
    return "".join(out)


def _mostly_malayalam_with_tamil_noise(text: str) -> bool:
    ml = len(_MALAYALAM_SCRIPT_RE.findall(text or ""))
    ta = len(_TAMIL_SCRIPT_RE.findall(text or ""))
    return ta > 0 and ml >= max(5, ta * 5)


def _chunk_text_for_translate(text: str, max_chars: int) -> list[str]:
    """Split long transcripts so each Mayura/Sarvam-Translate request stays under the cap."""
    cleaned = _trim(text)
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]
    chunks: list[str] = []
    remaining = cleaned
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        window = remaining[:max_chars]
        cut = max(window.rfind("\n"), window.rfind(". "), window.rfind("। "), window.rfind(" "))
        if cut < max_chars // 3:
            cut = max_chars
        piece = remaining[:cut].strip()
        if piece:
            chunks.append(piece)
        remaining = remaining[cut:].lstrip()
    return chunks


def _is_sarvam_quota_error(message: str) -> bool:
    low = _trim(message).lower()
    return "insufficient_quota" in low or "no credits available" in low or "http 402" in low


def _target_language_name(code: str) -> str:
    base = _lang_base(code)
    return {
        "ml": "Malayalam",
        "ta": "Tamil",
        "hi": "Hindi",
        "en": "English",
        "kn": "Kannada",
        "te": "Telugu",
    }.get(base, code or "Malayalam")


async def _gemini_translate_text(
    text: str,
    *,
    source_language_code: str = "auto",
    target_language_code: str = DEFAULT_TARGET_LANGUAGE,
) -> str:
    """Fallback translator via Gemini when Sarvam /translate is unavailable."""
    cleaned = _trim(text)
    if not cleaned:
        return ""
    settings = get_settings()
    gemini_key = _trim(getattr(settings, "gemini_api_key", ""))
    if not gemini_key:
        raise RuntimeError("GEMINI_API_KEY is not configured for translate fallback")

    target = _trim(target_language_code) or DEFAULT_TARGET_LANGUAGE
    target_name = _target_language_name(target)
    source = _trim(source_language_code) or "auto"
    source_name = _target_language_name(source) if _lang_base(source) else "the source language"
    model = _trim(getattr(settings, "ai_model", "")) or "gemini-2.5-flash-lite"

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=gemini_key)
    script_rule = ""
    if _lang_base(target) == "ml":
        script_rule = (
            " Write the entire translation in Malayalam script (Unicode Malayalam / മലയാളം) only. "
            "Do not use English prose, Tamil script (தமிழ்), Telugu, or Kannada. "
            "Keep product codes, SKUs, and digits as-is."
        )
    prompt = (
        f"Translate the following text from {source_name} into {target_name} ({target})."
        f"{script_rule} "
        "Return only the translated text with no quotes, labels, or commentary.\n\n"
        f"{cleaned}"
    )

    def _call():
        return client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=min(2048, max(256, len(cleaned) * 3)),
            ),
        )

    response = await asyncio.to_thread(_call)
    out = _trim(getattr(response, "text", "") or "")
    if not out and getattr(response, "candidates", None):
        parts = []
        for part in (response.candidates[0].content.parts or []):
            t = getattr(part, "text", None)
            if t:
                parts.append(str(t))
        out = _trim("".join(parts))
    if not out:
        raise RuntimeError("Gemini translate fallback returned empty text")
    return out


async def sarvam_translate_text(
    text: str,
    *,
    source_language_code: str = "auto",
    target_language_code: str = DEFAULT_TARGET_LANGUAGE,
    api_key: str | None = None,
) -> str:
    """POST /translate — any supported Indic/English text → target (default Malayalam).

    Falls back to Gemini when Sarvam returns insufficient credits / HTTP 402.
    """
    key = _trim(api_key) or _api_key()
    cleaned = _trim(text)
    if not cleaned:
        return ""
    target = _trim(target_language_code) or DEFAULT_TARGET_LANGUAGE
    if _same_language(source_language_code, target):
        return cleaned

    if not key:
        logger.warning("Sarvam API key missing; using Gemini translate fallback")
        return await _gemini_translate_text(
            cleaned,
            source_language_code=source_language_code,
            target_language_code=target,
        )

    model = _translate_model()
    max_chars = TRANSLATE_MAX_CHARS.get(model, 1000)
    source = _trim(source_language_code) or "auto"
    if not _lang_base(source):
        source = "auto"

    parts: list[str] = []
    headers = {
        "api-subscription-key": key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            for chunk in _chunk_text_for_translate(cleaned, max_chars):
                body = {
                    "input": chunk,
                    "source_language_code": source,
                    "target_language_code": target,
                    "model": model,
                }
                res = await client.post(_translate_url(), headers=headers, json=body)
                if res.status_code >= 400:
                    snippet = (res.text or "").replace("\n", " ")[:180]
                    raise RuntimeError(f"Sarvam translate HTTP {res.status_code}: {snippet}")
                payload = res.json() if res.content else {}
                translated = _trim(
                    payload.get("translated_text")
                    if isinstance(payload, dict)
                    else ""
                )
                if not translated:
                    raise RuntimeError("Sarvam translate returned empty translated_text")
                parts.append(translated)
        return " ".join(parts).strip()
    except RuntimeError as exc:
        if not _is_sarvam_quota_error(str(exc)):
            raise
        logger.warning(
            "Sarvam translate quota exhausted; falling back to Gemini (%s → %s)",
            source or "auto",
            target,
        )
        return await _gemini_translate_text(
            cleaned,
            source_language_code=source_language_code,
            target_language_code=target,
        )


async def ensure_transcript_in_target_language(
    transcript: str,
    *,
    source_language_code: str = "",
    api_key: str | None = None,
) -> dict[str, str]:
    """Apply configured post-STT translation (default ml-IN). No-op if target unset or already matches."""
    target = _target_language()
    cleaned = _trim(transcript)
    source = _trim(source_language_code)
    if not cleaned or not target:
        return {
            "transcript": cleaned,
            "language_code": source or target,
            "translated": "0",
        }
    if not _needs_target_translation(cleaned, source, target):
        return {"transcript": cleaned, "language_code": target, "translated": "0"}

    # Cheap path: mostly Malayalam with a few Tamil lookalike glyphs — scrub locally.
    if _lang_base(target) == "ml" and _mostly_malayalam_with_tamil_noise(cleaned):
        scrubbed = _scrub_tamil_script_to_malayalam(cleaned)
        return {
            "transcript": scrubbed,
            "language_code": target,
            "translated": "1" if scrubbed != cleaned else "0",
        }

    translated = await sarvam_translate_text(
        cleaned,
        source_language_code=_translate_source_hint(cleaned, source, target),
        target_language_code=target,
        api_key=api_key,
    )
    final_text = translated or cleaned
    if _lang_base(target) == "ml":
        final_text = _scrub_tamil_script_to_malayalam(final_text)
    return {
        "transcript": final_text,
        "language_code": target,
        "translated": "1",
    }


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


def _is_video_file(path: Path, media_name: str = "", media_type: str = "") -> bool:
    if _trim(media_type).lower() == "video":
        return True
    name = media_name or path.name
    suffix = path.suffix.lower()
    if suffix in VIDEO_FILE_SUFFIXES:
        return True
    guessed, _ = mimetypes.guess_type(name)
    return bool(guessed and guessed.startswith("video/"))


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def extract_audio_from_video(
    data: bytes, *, source_name: str = "video.mp4"
) -> tuple[bytes, str, str] | None:
    """Strip audio track from a video container via ffmpeg (mono opus @ 16 kHz)."""
    if not data or not _ffmpeg_available():
        return None
    suffix = Path(source_name).suffix.lower() or ".mp4"
    if suffix not in VIDEO_FILE_SUFFIXES and suffix != ".webm":
        suffix = ".mp4"
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / f"in{suffix}"
        outp = Path(td) / "speech.ogg"
        inp.write_bytes(data)
        try:
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(inp),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "libopus",
                    str(outp),
                ],
                capture_output=True,
                timeout=180,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("ffmpeg extract timeout/error: %s", exc)
            return None
        if proc.returncode != 0 or not outp.is_file():
            err = (proc.stderr or b"").decode("utf-8", errors="replace")[-400:]
            logger.warning("ffmpeg extract failed rc=%s: %s", proc.returncode, err)
            return None
        out = outp.read_bytes()
        if not out:
            return None
        stem = Path(source_name).stem or "video"
        return out, f"{stem}.ogg", "audio/ogg"


def _is_duration_limit_error(message: str) -> bool:
    return bool(_DURATION_LIMIT_RE.search(_trim(message)))


def load_audio_bytes(msg: WhatsAppMessage) -> tuple[bytes, str, str] | None:
    """Return (bytes, filename, mime) from local media — ffmpeg for video containers."""
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
    media_type = _trim(msg.media_type).lower()
    if _is_video_file(path, filename, media_type):
        extracted = extract_audio_from_video(data, source_name=filename)
        if extracted:
            return extracted
        if media_type == "video":
            return None
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
    mode = _stt_mode()
    url = _stt_url()
    files = {"file": (filename or "voice.ogg", audio, mime_type or "audio/ogg")}
    data = {
        "model": model,
        "mode": mode,
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
                    "mode": _stt_mode(),
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


async def _run_after_commit_visible(
    process_fn,
    message_db_id: int,
    *,
    force: bool = False,
    label: str = "transcription",
) -> None:
    """Run STT/translate after the inserting transaction commits.

    Webhook ingest schedules AI work before ``db.commit()``. A naive
    ``create_task`` often opens a new session, gets ``not-found``, and leaves
    ``transcription_status`` empty forever (Transcribe button, no auto STT).
    Retry briefly so the row is visible; still no-op if the id is truly gone.
    """
    delays = (0.05, 0.1, 0.2, 0.4, 0.8, 1.5)
    last: dict[str, Any] = {}
    for attempt, delay in enumerate((0.0, *delays)):
        if delay:
            await asyncio.sleep(delay)
        try:
            last = await process_fn(message_db_id, force=force)
        except Exception:  # noqa: BLE001
            logger.exception("scheduled %s failed message_id=%s", label, message_db_id)
            return
        if last.get("reason") != "not-found":
            return
        logger.info(
            "scheduled %s waiting for commit message_id=%s attempt=%s",
            label,
            message_db_id,
            attempt + 1,
        )
    logger.warning(
        "scheduled %s gave up (not-found) message_id=%s last=%s",
        label,
        message_db_id,
        last,
    )


def schedule_transcription(message_db_id: int, *, force: bool = False) -> None:
    """Fire-and-forget from request/webhook handlers."""

    async def _runner() -> None:
        await _run_after_commit_visible(
            process_transcription, message_db_id, force=force, label="transcription"
        )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_runner())


def schedule_text_translation(message_db_id: int, *, force: bool = False) -> None:
    """Fire-and-forget Malayalam translate for plain text / captions."""

    async def _runner() -> None:
        await _run_after_commit_visible(
            process_text_translation, message_db_id, force=force, label="text-translation"
        )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_runner())


async def process_text_translation(message_db_id: int, *, force: bool = False) -> dict[str, Any]:
    """Translate message ``body`` → Malayalam into ``transcript`` (body stays original)."""
    target = _target_language()
    if not target:
        return {"status": "skipped", "reason": "no-target-language"}
    if not _can_translate():
        return {"status": "skipped", "reason": "no-translate-key"}

    async with SessionLocal() as db:
        msg = await db.get(WhatsAppMessage, message_db_id)
        if not msg:
            return {"status": "skipped", "reason": "not-found"}
        if not is_text_translatable_message(msg):
            return {"status": "skipped", "reason": "not-text"}

        body = _trim(msg.body)
        prior = _trim(msg.transcript)
        prior_lang = _trim(msg.transcript_language)
        status = _trim(msg.transcription_status).lower()

        if not force and status == "completed" and prior:
            if not _needs_target_translation(prior, prior_lang, target):
                if prior_lang != target:
                    msg.transcript_language = target
                    await db.commit()
                return {"status": "skipped", "reason": "already-completed"}
        if not force and status == "processing":
            return {"status": "skipped", "reason": "already-processing"}

        # Existing Malayalam transcript from Care/YesOne — just normalize tags.
        if prior and not _needs_target_translation(prior, prior_lang, target):
            msg.transcript_language = target
            msg.transcription_status = "completed"
            if _trim(msg.transcription_error).lower().startswith("translate skipped"):
                msg.transcription_error = ""
            await db.commit()
            return {
                "status": "skipped",
                "reason": "already-target-transcript",
                "language_code": target,
            }

        # Body already Malayalam — no separate translation line needed.
        if not _needs_target_translation(body, "", target):
            msg.transcription_status = "completed"
            msg.transcript_language = target
            msg.transcription_error = ""
            await db.commit()
            return {"status": "skipped", "reason": "already-target", "language_code": target}

        msg.transcription_status = "processing"
        msg.transcription_error = ""
        await db.commit()

        try:
            final = await ensure_transcript_in_target_language(
                body, source_language_code="auto"
            )
        except Exception as exc:  # noqa: BLE001
            safe = str(exc).replace("\n", " ")[:180]
            safe = safe.replace(_api_key(), _mask_key(_api_key())) if _api_key() else safe
            logger.warning(
                "text translate failed message_id=%s err=%s", message_db_id, safe
            )
            msg.transcription_status = "failed"
            msg.transcription_error = (safe or "Could not translate")[:255]
            await db.commit()
            return {"status": "failed", "reason": safe}

        translated_text = _trim(final.get("transcript"))
        if final.get("translated") != "1" or not translated_text:
            msg.transcription_status = "completed"
            msg.transcript_language = target
            msg.transcription_error = ""
            await db.commit()
            return {"status": "skipped", "reason": "already-target", "language_code": target}

        # Avoid duplicating identical text under the bubble.
        if translated_text == body:
            msg.transcript = ""
            msg.transcript_language = target
            msg.transcription_status = "completed"
            msg.transcription_error = ""
            await db.commit()
            return {"status": "skipped", "reason": "already-target", "language_code": target}

        msg.transcript = translated_text
        msg.transcript_language = final.get("language_code") or target
        msg.transcription_status = "completed"
        msg.transcription_error = ""
        await db.commit()
        return {
            "status": "completed",
            "language_code": msg.transcript_language,
            "chars": len(msg.transcript or ""),
            "translated": True,
        }


async def process_transcription(message_db_id: int, *, force: bool = False) -> dict[str, Any]:
    """Download local speech media → Sarvam STT → optional translate to target (ml-IN) → store."""
    if not _api_key():
        return {"status": "skipped", "reason": "no-api-key"}

    async with SessionLocal() as db:
        msg = await db.get(WhatsAppMessage, message_db_id)
        if not msg:
            return {"status": "skipped", "reason": "not-found"}
        if not is_speech_media_type(msg.media_type):
            return {"status": "skipped", "reason": "not-audio"}
        if not _trim(msg.media_url):
            return {"status": "skipped", "reason": "no-media"}

        status = _trim(msg.transcription_status).lower()
        if not force and status == "completed" and _trim(msg.transcript):
            return {"status": "skipped", "reason": "already-completed"}
        if not force and status == "processing":
            # Another worker may be mid-flight; empty transcript usually means a crashed worker
            # left the row stuck after an API restart (Care manual Transcribe included).
            if _trim(msg.transcript):
                return {"status": "skipped", "reason": "already-processing"}

        prior_transcript = _trim(msg.transcript)
        prior_language = _trim(msg.transcript_language)
        prior_completed = status == "completed" and bool(prior_transcript)

        msg.transcription_status = "processing"
        msg.transcription_error = ""
        await db.commit()

        audio_pack = load_audio_bytes(msg)
        if not audio_pack:
            if prior_completed:
                # Keep last good transcript if a forced retry cannot read media.
                msg.transcription_status = "completed"
                msg.transcript = prior_transcript
                msg.transcript_language = prior_language
                msg.transcription_error = "Speech media file is not available yet."
            else:
                msg.transcription_status = "failed"
                msg.transcription_error = "Speech media file is not available yet."
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
            if prior_completed:
                # Do not wipe a completed transcript when force re-STT hits quota/errors.
                msg.transcription_status = "completed"
                msg.transcript = prior_transcript
                msg.transcript_language = prior_language
                msg.transcription_error = safe or "Could not transcribe audio"
            else:
                msg.transcription_status = "failed"
                msg.transcription_error = safe or "Could not transcribe audio"
                msg.transcript = ""
            await db.commit()
            return {"status": "failed", "reason": safe}

        transcript = result.get("transcript") or ""
        source_lang = result.get("language_code") or ""
        from app.contact_spoken_language import apply_voice_spoken_language

        if source_lang:
            await apply_voice_spoken_language(db, msg, source_lang)
        if not transcript:
            msg.transcription_status = "failed"
            msg.transcription_error = "No speech detected in this recording."
            msg.transcript = ""
            msg.transcript_language = source_lang
            await db.commit()
            return {"status": "failed", "reason": "empty-transcript"}

        try:
            final = await ensure_transcript_in_target_language(
                transcript, source_language_code=source_lang
            )
        except Exception as exc:  # noqa: BLE001
            safe = str(exc).replace("\n", " ")[:180]
            safe = safe.replace(_api_key(), _mask_key(_api_key()))
            logger.warning(
                "sarvam translate failed message_id=%s src=%s err=%s",
                message_db_id,
                source_lang or "?",
                safe,
            )
            # Keep native STT transcript rather than failing the whole voice note.
            final = {
                "transcript": transcript,
                "language_code": source_lang,
                "translated": "0",
            }
            msg.transcription_error = f"Translate skipped: {safe}"[:255]

        msg.transcript = final.get("transcript") or transcript
        msg.transcript_language = final.get("language_code") or source_lang
        msg.transcription_status = "completed"
        if final.get("translated") == "1":
            msg.transcription_error = ""
        await db.commit()
        return {
            "status": "completed",
            "language_code": msg.transcript_language,
            "chars": len(msg.transcript or ""),
            "translated": final.get("translated") == "1",
            "source_language_code": source_lang,
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
        is_text = is_text_translatable_message(msg)
        is_speech = is_speech_media_type(msg.media_type)
        if _trim(msg.transcription_status).lower() == "completed" and _trim(msg.transcript):
            return {
                "ok": True,
                "status": "completed",
                "skipped": True,
                "transcript": msg.transcript,
                "transcript_language": msg.transcript_language or "",
                "spoken_language": getattr(msg, "spoken_language", None) or "",
                "source_language_code": getattr(msg, "spoken_language", None) or "",
            }
    if is_text and not is_speech:
        result = await process_text_translation(mid, force=True)
    else:
        result = await process_transcription(mid, force=True)
    async with SessionLocal() as db:
        msg = await db.get(WhatsAppMessage, mid)
        spoken = (getattr(msg, "spoken_language", None) if msg else "") or ""
        return {
            "ok": result.get("status") == "completed",
            **result,
            "transcript": (msg.transcript if msg else "") or "",
            "transcript_language": (msg.transcript_language if msg else "") or "",
            "spoken_language": spoken,
            "source_language_code": result.get("source_language_code") or spoken,
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
            .where(WhatsAppMessage.media_type.in_(list(SPEECH_MEDIA_TYPES)))
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
        "target_language": _target_language(),
        "translate_model": _translate_model(),
    }


async def translate_stored_transcript(message_db_id: int) -> dict[str, Any]:
    """Translate an existing completed transcript to target language (no re-STT)."""
    target = _target_language()
    if not target:
        return {"status": "skipped", "reason": "no-target-language"}
    if not _api_key():
        return {"status": "skipped", "reason": "no-api-key"}

    async with SessionLocal() as db:
        msg = await db.get(WhatsAppMessage, message_db_id)
        if not msg:
            return {"status": "skipped", "reason": "not-found"}
        if not is_speech_media_type(msg.media_type):
            return {"status": "skipped", "reason": "not-audio"}
        text = _trim(msg.transcript)
        if not text:
            return {"status": "skipped", "reason": "empty-transcript"}
        source = _trim(msg.transcript_language)
        if not _needs_target_translation(text, source, target):
            # Already suitable as target; still rewrite lang code to canonical target.
            if source != target:
                msg.transcript_language = target
                await db.commit()
            return {
                "status": "skipped",
                "reason": "already-target",
                "language_code": target,
            }

        try:
            # Pass stored language tag; ensure_* picks translate source hint internally.
            final = await ensure_transcript_in_target_language(
                text,
                source_language_code=source or "auto",
            )
        except Exception as exc:  # noqa: BLE001
            safe = str(exc).replace("\n", " ")[:180]
            safe = safe.replace(_api_key(), _mask_key(_api_key()))
            logger.warning(
                "stored-transcript translate failed message_id=%s src=%s err=%s",
                message_db_id,
                source or "?",
                safe,
            )
            return {"status": "failed", "reason": safe}

        if final.get("translated") != "1" and _same_language(
            final.get("language_code") or "", target
        ):
            msg.transcript_language = target
            await db.commit()
            return {"status": "skipped", "reason": "already-target", "language_code": target}

        msg.transcript = final.get("transcript") or text
        msg.transcript_language = final.get("language_code") or target
        if final.get("translated") == "1":
            # Clear stale translate-skip notes from older STT runs.
            err = _trim(msg.transcription_error)
            if err.lower().startswith("translate skipped"):
                msg.transcription_error = ""
        await db.commit()
        return {
            "status": "completed",
            "language_code": msg.transcript_language,
            "chars": len(msg.transcript or ""),
            "translated": final.get("translated") == "1",
            "source_language_code": source,
        }


async def _list_non_target_voice_transcript_ids(
    *,
    customer_phone_digits: str = "",
    limit: int = 500,
) -> list[int]:
    """Completed voice rows whose stored transcript is not yet in the target language."""
    target = _target_language()
    digits = "".join(c for c in customer_phone_digits if c.isdigit())

    async with SessionLocal() as db:
        from app.models import WhatsAppConversation

        q = (
            select(WhatsAppMessage)
            .where(WhatsAppMessage.media_type.in_(list(SPEECH_MEDIA_TYPES)))
            .where(WhatsAppMessage.transcription_status == "completed")
            .where(WhatsAppMessage.transcript != "")
            .order_by(WhatsAppMessage.id.asc())
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
                return []
            q = q.where(WhatsAppMessage.conversation_id.in_(list(conv_ids)))

        rows = (await db.execute(q)).scalars().all()
        backlog: list[int] = []
        for row in rows:
            lang = _trim(row.transcript_language)
            body = row.transcript or ""
            if _needs_target_translation(body, lang, target):
                backlog.append(row.id)
            if len(backlog) >= limit:
                break
        return backlog


async def backfill_translate_stored_transcripts(
    *,
    customer_phone_digits: str = "",
    limit: int = 500,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Translate completed non-target voice transcripts to configured language (default ml-IN).

    Uses Sarvam /translate on stored text — no audio re-STT.
    """
    target = _target_language()
    if not target:
        return {"ok": False, "reason": "no-target-language"}

    backlog = await _list_non_target_voice_transcript_ids(
        customer_phone_digits=customer_phone_digits,
        limit=limit,
    )

    if dry_run:
        return {
            "ok": True,
            "matched": len(backlog),
            "translated": 0,
            "failed": 0,
            "skipped": 0,
            "remaining": len(backlog),
            "dry_run": True,
            "ids": backlog[:50],
            "target_language": target,
            "translate_model": _translate_model(),
        }

    translated = failed = skipped = 0
    samples: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    # Always keep the screenshot / known Tamil sample if present.
    for mid in backlog:
        result = await translate_stored_transcript(mid)
        st = result.get("status")
        if st == "completed" and result.get("translated"):
            translated += 1
            if len(samples) < 5:
                async with SessionLocal() as db:
                    msg = await db.get(WhatsAppMessage, mid)
                    if msg:
                        samples.append(
                            {
                                "id": mid,
                                "language_code": msg.transcript_language or "",
                                "preview": (msg.transcript or "")[:140],
                            }
                        )
        elif st == "failed":
            failed += 1
            if len(failures) < 10:
                failures.append({"id": str(mid), "reason": str(result.get("reason") or "")})
        else:
            skipped += 1

    remaining_ids = await _list_non_target_voice_transcript_ids(
        customer_phone_digits=customer_phone_digits,
        limit=10_000,
    )

    return {
        "ok": failed == 0,
        "matched": len(backlog),
        "translated": translated,
        "failed": failed,
        "skipped": skipped,
        "remaining": len(remaining_ids),
        "dry_run": False,
        "samples": samples,
        "failures": failures,
        "target_language": target,
        "translate_model": _translate_model(),
        "key_prefix": _mask_key(_api_key()),
    }


async def _list_untranslated_text_message_ids(
    *,
    customer_phone_digits: str = "",
    limit: int = 200,
) -> list[int]:
    """Recent text/caption rows that still need a Malayalam ``transcript``."""
    target = _target_language()
    digits = "".join(c for c in customer_phone_digits if c.isdigit())

    async with SessionLocal() as db:
        from app.models import WhatsAppConversation

        q = (
            select(WhatsAppMessage)
            .where(WhatsAppMessage.direction != "note")
            .where(~WhatsAppMessage.media_type.in_(list(SPEECH_MEDIA_TYPES)))
            .where(WhatsAppMessage.body != "")
            .order_by(WhatsAppMessage.id.desc())
            .limit(max(limit * 8, limit))
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
                return []
            q = q.where(WhatsAppMessage.conversation_id.in_(list(conv_ids)))

        rows = (await db.execute(q)).scalars().all()
        backlog: list[int] = []
        for row in rows:
            if not is_text_translatable_message(row):
                continue
            body = _trim(row.body)
            prior = _trim(row.transcript)
            lang = _trim(row.transcript_language)
            status = _trim(row.transcription_status).lower()
            if prior and not _needs_target_translation(prior, lang, target):
                continue
            if status == "completed" and prior:
                continue
            if not _needs_target_translation(body, "", target):
                continue
            backlog.append(row.id)
            if len(backlog) >= limit:
                break
        return backlog


async def backfill_text_translations(
    *,
    customer_phone_digits: str = "",
    limit: int = 200,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Translate recent untranslated text/caption messages to Malayalam (ml-IN)."""
    target = _target_language()
    if not target:
        return {"ok": False, "reason": "no-target-language"}
    if not _can_translate():
        return {"ok": False, "reason": "no-translate-key"}

    backlog = await _list_untranslated_text_message_ids(
        customer_phone_digits=customer_phone_digits,
        limit=limit,
    )

    if dry_run:
        return {
            "ok": True,
            "matched": len(backlog),
            "translated": 0,
            "failed": 0,
            "skipped": 0,
            "dry_run": True,
            "ids": backlog[:50],
            "target_language": target,
            "translate_model": _translate_model(),
        }

    translated = failed = skipped = 0
    samples: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for mid in backlog:
        result = await process_text_translation(mid, force=force)
        st = result.get("status")
        if st == "completed" and result.get("translated"):
            translated += 1
            if len(samples) < 5:
                async with SessionLocal() as db:
                    msg = await db.get(WhatsAppMessage, mid)
                    if msg:
                        samples.append(
                            {
                                "id": mid,
                                "body_preview": (msg.body or "")[:80],
                                "ml_preview": (msg.transcript or "")[:140],
                                "language_code": msg.transcript_language or "",
                            }
                        )
        elif st == "failed":
            failed += 1
            if len(failures) < 10:
                failures.append({"id": str(mid), "reason": str(result.get("reason") or "")})
        else:
            skipped += 1

    return {
        "ok": failed == 0,
        "matched": len(backlog),
        "translated": translated,
        "failed": failed,
        "skipped": skipped,
        "dry_run": False,
        "samples": samples,
        "failures": failures,
        "target_language": target,
        "translate_model": _translate_model(),
        "key_prefix": _mask_key(_api_key()),
        "gemini_fallback": not bool(_api_key()),
    }


async def process_voice_spoken_language(message_db_id: int, *, force: bool = False) -> dict[str, Any]:
    """STT inbound voice for Sarvam language_code; persist spoken_language + conv.language."""
    if not _api_key():
        return {"status": "skipped", "reason": "no-api-key"}

    from app.contact_spoken_language import (
        apply_voice_spoken_language,
        normalize_spoken_language_code,
    )

    async with SessionLocal() as db:
        msg = await db.get(WhatsAppMessage, message_db_id)
        if not msg:
            return {"status": "skipped", "reason": "not-found"}
        if msg.direction != "in":
            return {"status": "skipped", "reason": "not-inbound"}
        if not is_speech_media_type(msg.media_type):
            return {"status": "skipped", "reason": "not-audio"}
        if not _trim(msg.media_url):
            return {"status": "skipped", "reason": "no-media"}

        existing = normalize_spoken_language_code(getattr(msg, "spoken_language", None) or "")
        if not force and existing:
            return {
                "status": "skipped",
                "reason": "already-spoken",
                "language_code": existing,
                "message_id": message_db_id,
            }

        audio_pack = load_audio_bytes(msg)
        if not audio_pack:
            return {"status": "failed", "reason": "media-missing", "message_id": message_db_id}

        audio, filename, mime = audio_pack
        try:
            result = await sarvam_speech_to_text(audio, filename=filename, mime_type=mime)
        except Exception as exc:  # noqa: BLE001
            safe = str(exc).replace("\n", " ")[:180]
            safe = safe.replace(_api_key(), _mask_key(_api_key()))
            logger.warning(
                "spoken-language STT failed message_id=%s err=%s", message_db_id, safe
            )
            st = "quota" if _is_sarvam_quota_error(safe) else "failed"
            return {"status": st, "reason": safe, "message_id": message_db_id}

        source_lang = result.get("language_code") or ""
        spoken = normalize_spoken_language_code(source_lang)
        if not spoken:
            return {
                "status": "failed",
                "reason": "empty-language-code",
                "message_id": message_db_id,
            }

        conv_updated = await apply_voice_spoken_language(db, msg, source_lang, force=True)
        await db.commit()
        return {
            "status": "completed",
            "language_code": spoken,
            "conversation_updated": bool(conv_updated),
            "conversation_id": msg.conversation_id,
            "message_id": message_db_id,
        }


async def _list_inbound_voice_spoken_backlog(
    *,
    customer_phone_digits: str = "",
    after_id: int = 0,
    limit: int = 200,
    all_voices: bool = False,
) -> list[int]:
    """Inbound voice rows needing spoken_language (ascending id for resume)."""
    from sqlalchemy import or_

    from app.models import WhatsAppConversation

    digits = "".join(c for c in customer_phone_digits if c.isdigit())

    async with SessionLocal() as db:
        q = (
            select(WhatsAppMessage.id)
            .where(WhatsAppMessage.direction == "in")
            .where(WhatsAppMessage.media_type.in_(list(SPEECH_MEDIA_TYPES)))
            .where(WhatsAppMessage.media_url != "")
            .where(WhatsAppMessage.id > after_id)
            .order_by(WhatsAppMessage.id.asc())
            .limit(limit)
        )
        if not all_voices:
            q = q.where(
                or_(
                    WhatsAppMessage.spoken_language == "",
                    WhatsAppMessage.spoken_language.is_(None),
                )
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
                return []
            q = q.where(WhatsAppMessage.conversation_id.in_(list(conv_ids)))

        return list((await db.execute(q)).scalars().all())


async def backfill_voice_spoken_language(
    *,
    customer_phone_digits: str = "",
    limit: int = 200,
    after_id: int = 0,
    force: bool = False,
    all_voices: bool = False,
    dry_run: bool = False,
    sleep_s: float = 1.25,
    reconcile: bool = True,
) -> dict[str, Any]:
    """Batch STT for inbound voice spoken_language; rate-limit safe; resume via after_id."""
    from app.contact_spoken_language import reconcile_conversation_languages_from_voice

    backlog = await _list_inbound_voice_spoken_backlog(
        customer_phone_digits=customer_phone_digits,
        after_id=after_id,
        limit=limit,
        all_voices=all_voices or force,
    )

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "matched": len(backlog),
            "after_id": after_id,
            "next_after_id": backlog[-1] if backlog else after_id,
            "ids_head": backlog[:30],
            "key_prefix": _mask_key(_api_key()),
        }

    completed = failed = skipped = quota_hit = 0
    conv_updates = 0
    last_id = after_id
    language_counts: dict[str, int] = {}
    conv_change_samples: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    stopped_quota = False

    for mid in backlog:
        result = await process_voice_spoken_language(mid, force=force or all_voices)
        st = result.get("status")
        last_id = max(last_id, int(mid))
        if st == "completed":
            completed += 1
            code = str(result.get("language_code") or "")
            if code:
                language_counts[code] = language_counts.get(code, 0) + 1
            if result.get("conversation_updated"):
                conv_updates += 1
                if len(conv_change_samples) < 10:
                    conv_change_samples.append(
                        {
                            "message_id": mid,
                            "conversation_id": result.get("conversation_id"),
                            "language_code": code,
                        }
                    )
        elif st == "quota":
            quota_hit += 1
            stopped_quota = True
            if len(failures) < 5:
                failures.append({"id": str(mid), "reason": str(result.get("reason") or "quota")})
            break
        elif st == "failed":
            failed += 1
            if len(failures) < 10:
                failures.append({"id": str(mid), "reason": str(result.get("reason") or "")})
        else:
            skipped += 1

        if sleep_s > 0 and not stopped_quota:
            await asyncio.sleep(sleep_s)

    reconcile_stats: dict[str, Any] = {}
    if reconcile and not stopped_quota:
        async with SessionLocal() as db:
            reconcile_stats = await reconcile_conversation_languages_from_voice(db)

    remaining = await _list_inbound_voice_spoken_backlog(
        customer_phone_digits=customer_phone_digits,
        after_id=last_id,
        limit=10_000,
        all_voices=all_voices or force,
    )

    return {
        "ok": failed == 0 and not stopped_quota,
        "matched": len(backlog),
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
        "quota_stopped": stopped_quota,
        "quota_hits": quota_hit,
        "conversation_updates_during_stt": conv_updates,
        "reconcile": reconcile_stats,
        "remaining_after_last_id": len(remaining),
        "last_message_id": last_id,
        "resume_after_id": last_id,
        "language_counts": language_counts,
        "conversation_change_samples": conv_change_samples,
        "failures": failures,
        "key_prefix": _mask_key(_api_key()),
        "sleep_s": sleep_s,
    }
