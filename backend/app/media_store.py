"""WhatsApp media files on VPS disk; Postgres stores only public paths/metadata."""
from __future__ import annotations

import base64
import os
import re
import uuid
from pathlib import Path

MEDIA_ROOT = Path("/data/wa-media")
PUBLIC_PREFIX = "/api/whatsapp/media"


def ensure_root() -> Path:
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    return MEDIA_ROOT


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", name or "file")[:180]


def _ext_for(media_type: str, filename: str = "") -> str:
    if "." in (filename or ""):
        return "." + filename.rsplit(".", 1)[-1].lower()[:12]
    if media_type == "image":
        return ".jpg"
    if media_type == "audio":
        return ".ogg"
    if media_type == "video":
        return ".mp4"
    if media_type in {"document", "sticker"}:
        return ".webp" if media_type == "sticker" else ".bin"
    return ".bin"


def save_bytes(content: bytes, *, media_type: str, filename: str = "") -> tuple[str, str]:
    """Write bytes to disk. Returns (public_url, stored_filename)."""
    ensure_root()
    ext = _ext_for(media_type, filename)
    stored = f"{uuid.uuid4().hex}{ext}"
    path = MEDIA_ROOT / stored
    path.write_bytes(content)
    return f"{PUBLIC_PREFIX}/{stored}", stored


def ensure_stable_bytes(
    content: bytes,
    *,
    stem: str,
    media_type: str,
    filename: str = "",
) -> tuple[str, str]:
    """Write once under a deterministic name; reuse when size already matches.

    Used for campaign media so paced multi-recipient sends do not create a new
    UUID copy of the same video for every recipient (was ~30MB × N on disk).
    """
    ensure_root()
    ext = _ext_for(media_type, filename)
    stored = f"{_safe_name(stem)}{ext}"
    path = MEDIA_ROOT / stored
    if path.is_file() and path.stat().st_size == len(content):
        return f"{PUBLIC_PREFIX}/{stored}", stored
    tmp = MEDIA_ROOT / f".{stored}.tmp.{uuid.uuid4().hex}"
    try:
        tmp.write_bytes(content)
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return f"{PUBLIC_PREFIX}/{stored}", stored


def ensure_stable_from_path(
    source: Path,
    *,
    stem: str,
    media_type: str,
    filename: str = "",
) -> tuple[str, str]:
    """Ensure one wa-media file for ``stem``; prefer hardlink, else copy once.

    Campaign library lives on a separate volume (``/data/campaign-media``), so
    cross-volume hardlinks often fail — we fall back to a single copy. Inbox
    still serves via ``/api/whatsapp/media/...`` (resolve_path stays in MEDIA_ROOT).
    """
    ensure_root()
    src = Path(source)
    if not src.is_file():
        raise FileNotFoundError(str(src))
    size = src.stat().st_size
    ext = _ext_for(media_type, filename or src.name)
    stored = f"{_safe_name(stem)}{ext}"
    path = MEDIA_ROOT / stored
    if path.is_file() and path.stat().st_size == size:
        return f"{PUBLIC_PREFIX}/{stored}", stored
    tmp = MEDIA_ROOT / f".{stored}.tmp.{uuid.uuid4().hex}"
    try:
        linked = False
        try:
            os.link(src, tmp)
            linked = True
        except OSError:
            linked = False
        if not linked:
            tmp.write_bytes(src.read_bytes())
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return f"{PUBLIC_PREFIX}/{stored}", stored


def save_data_url(data_url: str, *, media_type: str, filename: str = "") -> tuple[str, str, bytes]:
    """Decode data URL / bare base64, save to disk. Returns (public_url, stored_name, raw_bytes)."""
    raw = data_url or ""
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    content = base64.b64decode(raw)
    if len(content) > 25 * 1024 * 1024:
        raise ValueError("File too large (max 25MB)")
    url, name = save_bytes(content, media_type=media_type, filename=filename)
    return url, name, content


def resolve_path(stored_name: str) -> Path | None:
    name = _safe_name(stored_name)
    if not name or name.startswith("."):
        return None
    path = (MEDIA_ROOT / name).resolve()
    if not str(path).startswith(str(MEDIA_ROOT.resolve())):
        return None
    return path if path.is_file() else None


def to_evolution_base64(data_url_or_b64: str) -> str:
    raw = data_url_or_b64 or ""
    if raw.startswith("data:") and "," in raw:
        return raw.split(",", 1)[1]
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return raw
