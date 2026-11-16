"""WhatsApp media files on VPS disk; Postgres stores only public paths/metadata."""
from __future__ import annotations

import base64
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


def save_bytes(content: bytes, *, media_type: str, filename: str = "") -> tuple[str, str]:
    """Write bytes to disk. Returns (public_url, stored_filename)."""
    ensure_root()
    ext = ""
    if "." in (filename or ""):
        ext = "." + filename.rsplit(".", 1)[-1].lower()[:12]
    elif media_type == "image":
        ext = ".jpg"
    elif media_type == "audio":
        ext = ".ogg"
    elif media_type == "video":
        ext = ".mp4"
    elif media_type in {"document", "sticker"}:
        ext = ".webp" if media_type == "sticker" else ".bin"
    stored = f"{uuid.uuid4().hex}{ext}"
    path = MEDIA_ROOT / stored
    path.write_bytes(content)
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
