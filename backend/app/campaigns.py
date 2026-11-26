"""Campaign media library + lightweight campaign CRUD (phased; no Meta ads)."""
from __future__ import annotations

import json
import logging
import re
import subprocess
import uuid
from datetime import timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.lead_market_language import parse_language_filter
from app.models import Campaign, CampaignMedia, CampaignMediaFolder, CampaignRun, User, WhatsAppInstance

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])
Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

MEDIA_ROOT = Path("/data/campaign-media")
COVER_ROOT = MEDIA_ROOT / "covers"
PUBLIC_FILE_PREFIX = "/api/campaigns/media"
# Soft OS/safety ceiling only — no product 80MB cap. Prefer unlimited via proxies.
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024  # 2GB
MAX_COVER_BYTES = 8 * 1024 * 1024
ALLOWED_VIDEO_TYPES = {
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/ogg",
    "video/x-msvideo",
    "video/x-matroska",
}
ALLOWED_VIDEO_EXT = {".mp4", ".webm", ".mov", ".ogg", ".ogv", ".avi", ".mkv"}
ALLOWED_IMAGE_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
}
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_DOC_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain",
}
ALLOWED_DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt"}
ALLOWED_COVER_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
ALLOWED_COVER_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MediaKind = Literal["video", "image", "document"]
CampaignStatus = Literal["draft", "active", "completed"]


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", name or "media")[:180]


def _parse_media_ids(raw: str | None) -> list[int]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[int] = []
    for item in data:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _parse_tags(raw: str | None) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        s = str(item or "").strip().lower()[:40]
        if s and s not in out:
            out.append(s)
    return out[:24]


def _tags_json(tags: list[str]) -> str:
    return json.dumps(tags, ensure_ascii=False)


def _tags_from_topic(topic: str) -> list[str]:
    parts = re.split(r"[\s,;/|_\-]+", (topic or "").lower())
    out: list[str] = []
    for p in parts:
        t = re.sub(r"[^a-z0-9.+]", "", p)[:40]
        if len(t) >= 2 and t not in out:
            out.append(t)
    return out[:8]


def _detect_kind(mime: str, ext: str) -> MediaKind | None:
    if mime in ALLOWED_VIDEO_TYPES or ext in ALLOWED_VIDEO_EXT:
        return "video"
    if mime in ALLOWED_IMAGE_TYPES or ext in ALLOWED_IMAGE_EXT:
        return "image"
    if mime in ALLOWED_DOC_TYPES or ext in ALLOWED_DOC_EXT:
        return "document"
    return None


def _normalize_mime(kind: MediaKind, mime: str, ext: str) -> str:
    if mime and mime in (
        ALLOWED_VIDEO_TYPES | ALLOWED_IMAGE_TYPES | ALLOWED_DOC_TYPES
    ):
        return mime
    if kind == "video":
        return {
            ".mp4": "video/mp4",
            ".webm": "video/webm",
            ".mov": "video/quicktime",
            ".ogg": "video/ogg",
            ".ogv": "video/ogg",
            ".avi": "video/x-msvideo",
            ".mkv": "video/x-matroska",
        }.get(ext, "video/mp4")
    if kind == "image":
        return {
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }.get(ext, "image/jpeg")
    return {
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".txt": "text/plain",
    }.get(ext, "application/octet-stream")


def _media_path(row: CampaignMedia) -> Path | None:
    path = Path(row.stored_path) if row.stored_path else MEDIA_ROOT / row.stored_name
    if path.is_file():
        return path
    alt = MEDIA_ROOT / row.stored_name
    return alt if alt.is_file() else None


def _cover_path(row: CampaignMedia) -> Path | None:
    if row.cover_path:
        p = Path(row.cover_path)
        if p.is_file():
            return p
    if row.cover_name:
        p = COVER_ROOT / row.cover_name
        if p.is_file():
            return p
    return None


def _cover_ext_for(mime: str, filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if mime == "image/png" or suffix == ".png":
        return ".png"
    if mime == "image/webp" or suffix == ".webp":
        return ".webp"
    return ".jpg"


def _probe_duration_seconds(path: Path) -> int:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False, text=True)
        if proc.returncode != 0:
            return 0
        val = float((proc.stdout or "").strip() or "0")
        return max(0, int(round(val)))
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        return 0


def _extract_first_frame(video: Path, dest: Path) -> bool:
    """Extract strictly the first decoded frame (t≈0). Never seek mid-clip."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-vf",
        r"select=eq(n\,0)",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=90, check=False)
        if proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 0:
            return True
        if dest.exists():
            dest.unlink(missing_ok=True)
        proc2 = subprocess.run(
            ["ffmpeg", "-y", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(dest)],
            capture_output=True,
            timeout=90,
            check=False,
        )
        ok = proc2.returncode == 0 and dest.is_file() and dest.stat().st_size > 0
        if not ok:
            logger.warning(
                "First-frame cover failed for %s: rc=%s err=%s",
                video,
                proc2.returncode,
                (proc2.stderr or b"")[-400:],
            )
            if dest.exists():
                dest.unlink(missing_ok=True)
        return ok
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("First-frame cover failed for %s: %s", video, exc)
        if dest.exists():
            dest.unlink(missing_ok=True)
        return False


def _set_cover_from_first_frame(row: CampaignMedia, *, force: bool = False) -> bool:
    """Write/replace cover from video first frame. Mutates row cover fields on success."""
    if not force and _cover_path(row):
        return True
    video = _media_path(row)
    if not video:
        return False
    COVER_ROOT.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.jpg"
    dest = COVER_ROOT / name
    if not _extract_first_frame(video, dest):
        return False
    if force:
        _unlink_cover(row)
    row.cover_name = name
    row.cover_path = str(dest)
    return True


def _set_cover_from_image_file(row: CampaignMedia, src: Path) -> bool:
    """Use the image itself as the library cover thumbnail."""
    COVER_ROOT.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower() if src.suffix.lower() in ALLOWED_COVER_EXT else ".jpg"
    name = f"{uuid.uuid4().hex}{ext}"
    dest = COVER_ROOT / name
    try:
        dest.write_bytes(src.read_bytes())
    except OSError as exc:
        logger.warning("Image cover copy failed for %s: %s", src, exc)
        return False
    _unlink_cover(row)
    row.cover_name = name
    row.cover_path = str(dest)
    return True


async def _store_uploaded_cover(row: CampaignMedia, file: UploadFile) -> None:
    """Optional manual cover replace (API kept; UI prefers first-frame)."""
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty cover")
    if len(content) > MAX_COVER_BYTES:
        raise HTTPException(400, "Cover too large")
    mime = (file.content_type or "").lower().split(";")[0].strip()
    suffix = Path(file.filename or "").suffix.lower()
    if mime not in ALLOWED_COVER_TYPES and suffix not in ALLOWED_COVER_EXT:
        raise HTTPException(400, "Cover must be jpeg, png, or webp")
    if mime not in ALLOWED_COVER_TYPES:
        mime = {
            ".png": "image/png",
            ".webp": "image/webp",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }.get(suffix, "image/jpeg")
    COVER_ROOT.mkdir(parents=True, exist_ok=True)
    _unlink_cover(row)
    name = f"{uuid.uuid4().hex}{_cover_ext_for(mime, file.filename)}"
    dest = COVER_ROOT / name
    dest.write_bytes(content)
    row.cover_name = name
    row.cover_path = str(dest)


def _unlink_cover(row: CampaignMedia) -> None:
    for candidate in (
        Path(row.cover_path) if row.cover_path else None,
        COVER_ROOT / row.cover_name if row.cover_name else None,
    ):
        if candidate and candidate.is_file():
            candidate.unlink(missing_ok=True)


def _unlink_media_file(row: CampaignMedia) -> None:
    path = _media_path(row)
    if path and path.is_file():
        path.unlink(missing_ok=True)


def _media_kind_of(row: CampaignMedia) -> MediaKind:
    kind = (getattr(row, "media_kind", None) or "").strip().lower()
    if kind in {"video", "image", "document"}:
        return kind  # type: ignore[return-value]
    mime = (row.mime_type or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/") or not mime:
        return "video"
    return "document"


async def _allocate_media_uid(db: AsyncSession) -> int:
    """Next free 4-digit public UID in 1000–9999."""
    used = {
        int(v)
        for v in (
            await db.execute(
                select(CampaignMedia.uid).where(CampaignMedia.uid.is_not(None))
            )
        )
        .scalars()
        .all()
        if v is not None
    }
    for candidate in range(1000, 10000):
        if candidate not in used:
            return candidate
    raise HTTPException(500, "No free media UIDs (1000–9999) remaining")


async def _media_send_counts(db: AsyncSession) -> dict[int, int]:
    """Sum campaign-report Success counts per media (not Total/Failed/Skipped)."""
    rows = (
        await db.execute(
            select(
                CampaignRun.media_id,
                func.coalesce(func.sum(CampaignRun.success_count), 0),
            )
            .where(CampaignRun.media_id.isnot(None))
            .group_by(CampaignRun.media_id)
        )
    ).all()
    out: dict[int, int] = {}
    for media_id, success in rows:
        if media_id is None:
            continue
        out[int(media_id)] = int(success or 0)
    return out


async def _media_send_count_one(db: AsyncSession, media_id: int) -> int:
    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(CampaignRun.success_count), 0),
            ).where(CampaignRun.media_id == media_id)
        )
    ).one()
    return int(row[0] or 0)


def _stored_media_language(row: CampaignMedia) -> str:
    return parse_language_filter(getattr(row, "language", None) or "")


def _normalize_media_language_input(raw: str | None) -> str:
    """Empty clears; otherwise validate against CRM market language buckets."""
    if raw is None:
        return ""
    token = (raw or "").strip()
    if not token:
        return ""
    normalized = parse_language_filter(token)
    if not normalized:
        raise HTTPException(400, "Unknown language — use a CRM market code (ml, ta, hi, …)")
    return normalized


def _cover_public_url(row: CampaignMedia) -> str:
    """Poster/thumbnail path for gallery cards (not the full media file)."""
    kind = _media_kind_of(row)
    if kind in ("video", "image"):
        return f"{PUBLIC_FILE_PREFIX}/{row.id}/cover"
    return ""


def _file_response_cached(
    path: Path,
    *,
    media_type: str,
    filename: str,
    max_age: int,
) -> FileResponse:
    resp = FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline",
    )
    resp.headers["Cache-Control"] = f"private, max-age={max_age}"
    return resp


def _media_out(row: CampaignMedia, uploader_name: str = "", *, count: int = 0) -> CampaignMediaOut:
    has_cover = _cover_path(row) is not None
    topic = (row.title or row.file_name or "").strip()
    kind = _media_kind_of(row)
    uid_val = getattr(row, "uid", None)
    return CampaignMediaOut(
        id=row.id,
        uid=int(uid_val) if uid_val is not None else None,
        title=topic,
        topic=topic,
        file_name=row.file_name,
        mime_type=row.mime_type,
        media_kind=kind,
        tags=_parse_tags(getattr(row, "tags", None)),
        folder_id=getattr(row, "folder_id", None),
        duration_seconds=int(getattr(row, "duration_seconds", 0) or 0),
        file_size=row.file_size,
        uploaded_by=row.uploaded_by,
        uploaded_by_name=uploader_name,
        created_at=row.created_at.isoformat() if row.created_at else "",
        play_url=f"{PUBLIC_FILE_PREFIX}/{row.id}/file",
        cover_url=_cover_public_url(row),
        has_cover=has_cover,
        language=_stored_media_language(row),
        count=int(count or 0),
    )


def _folder_out(row: CampaignMediaFolder, creator_name: str = "") -> CampaignFolderOut:
    return CampaignFolderOut(
        id=row.id,
        name=row.name,
        created_by=row.created_by,
        created_by_name=creator_name,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


def _campaign_out(row: Campaign, creator_name: str = "") -> CampaignOut:
    return CampaignOut(
        id=row.id,
        name=row.name,
        status=row.status if row.status in {"draft", "active", "completed"} else "draft",
        description=row.description or "",
        media_ids=_parse_media_ids(row.media_ids),
        created_by=row.created_by,
        created_by_name=creator_name,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


def _run_out(row: CampaignRun, media_row: CampaignMedia | None = None) -> CampaignRunOut:
    from app.campaign_reports import format_media_display_name

    occurred = row.occurred_at
    # Prefer live campaign_media title + uid over stored media_name.
    media_name = format_media_display_name(media_row, fallback=row.media_name or "")
    if media_name == "—":
        media_name = row.media_name or ""
    status = (getattr(row, "status", None) or "completed").strip() or "completed"
    total_hours = int(getattr(row, "total_hours", 0) or 0)
    note = row.note or ""
    if status == "running" and total_hours:
        note = note or f"Paced {total_hours}h in progress"
    estimated_end_at = ""
    if status in {"running", "queued"}:
        from app.paced_media_batch import estimate_paced_run_end_at

        end = estimate_paced_run_end_at(row)
        if end is not None:
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            estimated_end_at = end.isoformat()
    uid_val = getattr(row, "uid", None)
    uid_int = int(uid_val) if uid_val is not None else None
    status_l = status.strip().lower()
    can_restart = status_l in {"failed", "stopped"}
    return CampaignRunOut(
        id=row.id,
        uid=uid_int,
        campaign_number=uid_int,
        campaign_id=row.campaign_id,
        batch_id=row.batch_id or "",
        tool_name=row.tool_name or "",
        agent_name=row.agent_name or "",
        media_name=media_name,
        media_id=row.media_id,
        audience_name=row.audience_name or "",
        total_count=int(row.total_count or 0),
        success_count=int(row.success_count or 0),
        failed_count=int(row.failed_count or 0),
        skipped_count=int(row.skipped_count or 0),
        replied_count=int(getattr(row, "replied_count", 0) or 0),
        from_instance_public_id=(getattr(row, "from_instance_public_id", None) or "")[:80],
        note=note,
        status=status,
        total_hours=total_hours,
        occurred_at=occurred.isoformat() if occurred else "",
        date=occurred.strftime("%Y-%m-%d") if occurred else "",
        time=occurred.strftime("%H:%M") if occurred else "",
        created_at=row.created_at.isoformat() if row.created_at else "",
        estimated_end_at=estimated_end_at,
        can_restart=can_restart,
    )


class CampaignMediaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    uid: int | None = None
    title: str = ""
    topic: str = ""
    file_name: str = ""
    mime_type: str = ""
    media_kind: MediaKind = "video"
    tags: list[str] = Field(default_factory=list)
    folder_id: int | None = None
    duration_seconds: int = 0
    file_size: int = 0
    uploaded_by: int | None = None
    uploaded_by_name: str = ""
    created_at: str = ""
    play_url: str = ""
    cover_url: str = ""
    has_cover: bool = False
    language: str = ""
    # Successful sends for this media (sum of campaign-report Success only).
    count: int = 0


class MediaUsageCampaignOut(BaseModel):
    """One campaign_run that sent (or is sending) this media."""

    run_id: int
    campaign_number: int | None = None
    campaign_id: int | None = None
    status: str = "completed"
    details: str = ""
    note: str = ""
    total_hours: int = 0
    occurred_at: str = ""
    audience_name: str = ""
    audience_uid: int | None = None
    audience_size: int = 0
    total_sent: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    from_agent: str = ""
    from_number: str = ""
    from_instance_public_id: str = ""


class MediaUsageOut(BaseModel):
    media_id: int
    media_uid: int | None = None
    media_title: str = ""
    total_sent: int = 0
    campaigns: list[MediaUsageCampaignOut] = Field(default_factory=list)


class CampaignFolderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    created_by: int | None = None
    created_by_name: str = ""
    created_at: str = ""


class CampaignFolderIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: str = "draft"
    description: str = ""
    media_ids: list[int] = Field(default_factory=list)
    created_by: int | None = None
    created_by_name: str = ""
    created_at: str = ""
    updated_at: str = ""


class CampaignRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    uid: int | None = None
    campaign_number: int | None = None
    campaign_id: int | None = None
    batch_id: str = ""
    tool_name: str = ""
    agent_name: str = ""
    media_name: str = ""
    media_id: int | None = None
    audience_name: str = ""
    total_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    replied_count: int = 0
    from_instance_public_id: str = ""
    note: str = ""
    status: str = "completed"
    total_hours: int = 0
    occurred_at: str = ""
    date: str = ""
    time: str = ""
    created_at: str = ""
    estimated_end_at: str = ""
    can_restart: bool = False


class CampaignResumeIn(BaseModel):
    """Resume a failed/stopped paced blast on a different Evolution instance."""

    from_instance_public_id: str = Field(min_length=1, max_length=80)


class CampaignSendInstanceOut(BaseModel):
    public_id: str
    display_name: str = ""
    employee_name: str = ""
    employee_code: str = ""
    connected_phone: str = ""
    connection_status: str = ""
    label: str = ""


class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    status: CampaignStatus = "draft"
    description: str = ""
    media_ids: list[int] = Field(default_factory=list)


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: CampaignStatus | None = None
    description: str | None = None
    media_ids: list[int] | None = None


class CampaignMediaUpdate(BaseModel):
    """Topic rename + optional tags / folder / language. Duplicate topics are allowed."""

    topic: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    tags: list[str] | None = None
    folder_id: int | None = None
    language: str | None = Field(default=None, max_length=20)


async def _user_names(db: AsyncSession, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    rows = (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
    return {u.id: (u.name or u.email or f"User {u.id}") for u in rows}


async def _validate_media_ids(db: AsyncSession, media_ids: list[int]) -> list[int]:
    unique = sorted({int(i) for i in media_ids if isinstance(i, int) or str(i).isdigit()})
    if not unique:
        return []
    found = (
        await db.execute(select(CampaignMedia.id).where(CampaignMedia.id.in_(unique)))
    ).scalars().all()
    found_set = set(found)
    missing = [i for i in unique if i not in found_set]
    if missing:
        raise HTTPException(400, f"Unknown media id(s): {', '.join(map(str, missing))}")
    return unique


async def _validate_folder_id(db: AsyncSession, folder_id: int | None) -> int | None:
    if folder_id is None:
        return None
    row = await db.get(CampaignMediaFolder, folder_id)
    if not row:
        raise HTTPException(400, "Unknown folder")
    return folder_id


def _parse_folder_id_form(raw: str | None) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError as exc:
        raise HTTPException(400, "Invalid folder_id") from exc


def _parse_tags_form(raw: str | None) -> list[str] | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        return [t.strip().lower() for t in s.split(",") if t.strip()][:24]
    if isinstance(data, list):
        return _parse_tags(json.dumps(data))
    return None


async def _persist_upload(
    *,
    db: AsyncSession,
    user: User,
    file: UploadFile,
    display_title: str,
    folder_id: int | None,
    tags_override: list[str] | None,
    language: str = "",
) -> CampaignMedia:
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(400, "File too large (max 2GB)")

    mime = (file.content_type or "").lower().split(";")[0].strip()
    original = file.filename or "media.bin"
    ext = Path(original).suffix.lower()
    kind = _detect_kind(mime, ext)
    if not kind:
        raise HTTPException(
            400,
            "Unsupported file. Upload video (mp4/webm/mov), image (jpg/png/webp/gif), "
            "or document (pdf/doc/xls/ppt/txt).",
        )
    mime = _normalize_mime(kind, mime, ext)
    if not ext:
        ext = {
            "video": ".mp4",
            "image": ".jpg",
            "document": ".pdf",
        }[kind]

    folder_id = await _validate_folder_id(db, folder_id)
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}{ext}"
    dest = MEDIA_ROOT / stored
    dest.write_bytes(content)

    tags = tags_override if tags_override is not None else _tags_from_topic(display_title)
    uid = await _allocate_media_uid(db)
    row = CampaignMedia(
        uid=uid,
        title=display_title[:200],
        file_name=_safe_name(original),
        stored_name=stored,
        stored_path=str(dest),
        cover_name="",
        cover_path="",
        mime_type=mime,
        media_kind=kind,
        tags=_tags_json(tags),
        folder_id=folder_id,
        duration_seconds=0,
        file_size=len(content),
        language=language,
        uploaded_by=user.id,
    )
    if kind == "video":
        _set_cover_from_first_frame(row, force=True)
        row.duration_seconds = _probe_duration_seconds(dest)
    elif kind == "image":
        _set_cover_from_image_file(row, dest)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/folders", response_model=list[CampaignFolderOut])
async def list_folders(db: Db, user: CurrentUser):
    _ = user
    rows = (
        await db.execute(select(CampaignMediaFolder).order_by(CampaignMediaFolder.name.asc()))
    ).scalars().all()
    names = await _user_names(db, {r.created_by for r in rows if r.created_by})
    return [_folder_out(r, names.get(r.created_by or 0, "")) for r in rows]


@router.post("/folders", response_model=CampaignFolderOut)
async def create_folder(body: CampaignFolderIn, db: Db, user: CurrentUser):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Folder name is required")
    row = CampaignMediaFolder(name=name[:200], created_by=user.id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _folder_out(row, user.name or user.email)


@router.get("/media", response_model=list[CampaignMediaOut])
async def list_media(db: Db, user: CurrentUser, language: str = Query("")):
    _ = user
    rows = (
        await db.execute(select(CampaignMedia).order_by(CampaignMedia.id.desc()))
    ).scalars().all()
    lang_filter = parse_language_filter(language)
    if lang_filter:
        rows = [r for r in rows if _stored_media_language(r) == lang_filter]
    names = await _user_names(db, {r.uploaded_by for r in rows if r.uploaded_by})
    send_counts = await _media_send_counts(db)
    return [
        _media_out(r, names.get(r.uploaded_by or 0, ""), count=send_counts.get(r.id, 0))
        for r in rows
    ]


@router.post("/media", response_model=CampaignMediaOut)
async def upload_media(
    db: Db,
    user: CurrentUser,
    file: UploadFile = File(...),
    title: str = Form(""),
    topic: str = Form(""),
    folder_id: str = Form(""),
    tags: str = Form(""),
    language: str = Form(""),
):
    display_title = (topic or title or "").strip()
    if not display_title:
        raise HTTPException(400, "Topic is required")
    lang = ""
    if (language or "").strip():
        lang = _normalize_media_language_input(language)
    row = await _persist_upload(
        db=db,
        user=user,
        file=file,
        display_title=display_title,
        folder_id=_parse_folder_id_form(folder_id),
        tags_override=_parse_tags_form(tags),
        language=lang,
    )
    return _media_out(row, user.name or user.email, count=0)


def _parse_filter_meta(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _audience_uid_from_meta(meta: dict) -> int | None:
    for key in ("audience_uid", "audience_uid_label"):
        val = meta.get(key)
        if val is None or val == "":
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            continue
    return None


def _format_from_number(inst: WhatsAppInstance | None, fallback_agent: str = "") -> str:
    if not inst:
        return (fallback_agent or "").strip()
    phone = (inst.connected_phone_number or inst.expected_phone_number or "").strip()
    label = (inst.employee_name or inst.display_name or fallback_agent or "").strip()
    if phone and label:
        return f"{label} · {phone}"
    return phone or label or (fallback_agent or "").strip()


def _usage_details(row: CampaignRun) -> str:
    status = (getattr(row, "status", None) or "completed").strip() or "completed"
    hours = int(getattr(row, "total_hours", 0) or 0)
    parts: list[str] = []
    if status:
        parts.append(status.capitalize())
    if hours:
        parts.append(f"{hours}h paced")
    note = (row.note or "").strip()
    if note and len(note) < 120 and "Paced send scheduled" not in note:
        parts.append(note)
    return " · ".join(parts) if parts else status.capitalize()


@router.get("/media/{media_id}/usage", response_model=MediaUsageOut)
async def media_usage(media_id: int, db: Db, user: CurrentUser):
    """Campaigns / paced sends that used this media (for media-card eye popup)."""
    _ = user
    media = await db.get(CampaignMedia, media_id)
    if not media:
        raise HTTPException(404, "Media not found")

    runs = (
        await db.execute(
            select(CampaignRun)
            .where(CampaignRun.media_id == media_id)
            .order_by(CampaignRun.occurred_at.desc(), CampaignRun.id.desc())
        )
    ).scalars().all()

    instance_ids = {
        (r.from_instance_public_id or "").strip()
        for r in runs
        if (r.from_instance_public_id or "").strip()
    }
    inst_map: dict[str, WhatsAppInstance] = {}
    if instance_ids:
        inst_rows = (
            await db.execute(
                select(WhatsAppInstance).where(WhatsAppInstance.public_id.in_(instance_ids))
            )
        ).scalars().all()
        inst_map = {str(i.public_id): i for i in inst_rows}

    campaigns: list[MediaUsageCampaignOut] = []
    total_sent = 0
    for r in runs:
        sent = int(r.success_count or 0)
        total_sent += sent
        meta = _parse_filter_meta(getattr(r, "filter_meta_json", None))
        uid_val = getattr(r, "uid", None)
        campaign_number = int(uid_val) if uid_val is not None else None
        from_pid = (r.from_instance_public_id or "").strip()
        campaigns.append(
            MediaUsageCampaignOut(
                run_id=r.id,
                campaign_number=campaign_number,
                campaign_id=r.campaign_id,
                status=(getattr(r, "status", None) or "completed").strip() or "completed",
                details=_usage_details(r),
                note=(r.note or "")[:500],
                total_hours=int(getattr(r, "total_hours", 0) or 0),
                occurred_at=r.occurred_at.isoformat() if r.occurred_at else "",
                audience_name=(r.audience_name or "").strip(),
                audience_uid=_audience_uid_from_meta(meta),
                audience_size=int(r.total_count or 0),
                total_sent=sent,
                failed_count=int(r.failed_count or 0),
                skipped_count=int(r.skipped_count or 0),
                from_agent=(r.agent_name or "").strip(),
                from_number=_format_from_number(inst_map.get(from_pid), r.agent_name or ""),
                from_instance_public_id=from_pid,
            )
        )

    title = (media.title or media.file_name or "").strip()
    uid_m = getattr(media, "uid", None)
    return MediaUsageOut(
        media_id=media.id,
        media_uid=int(uid_m) if uid_m is not None else None,
        media_title=title,
        total_sent=total_sent,
        campaigns=campaigns,
    )


@router.get("/media/{media_id}/file")
async def play_media(media_id: int, db: Db, user: CurrentUser):
    _ = user
    row = await db.get(CampaignMedia, media_id)
    if not row:
        raise HTTPException(404, "Media not found")
    path = _media_path(row)
    if not path:
        raise HTTPException(404, "File missing on server")
    resolved = path.resolve()
    if not str(resolved).startswith(str(MEDIA_ROOT.resolve())):
        raise HTTPException(404, "File missing on server")
    return _file_response_cached(
        resolved,
        media_type=row.mime_type or "application/octet-stream",
        filename=row.file_name or resolved.name,
        max_age=3600,
    )


@router.get("/media/{media_id}/cover")
async def get_cover(media_id: int, db: Db, user: CurrentUser):
    _ = user
    row = await db.get(CampaignMedia, media_id)
    if not row:
        raise HTTPException(404, "Media not found")
    path = _cover_path(row)
    if not path and _media_kind_of(row) == "video":
        if _set_cover_from_first_frame(row, force=False):
            await db.commit()
            await db.refresh(row)
            path = _cover_path(row)
    if not path and _media_kind_of(row) == "image":
        src = _media_path(row)
        if src and _set_cover_from_image_file(row, src):
            await db.commit()
            await db.refresh(row)
            path = _cover_path(row)
    if not path:
        raise HTTPException(404, "Cover not available")
    resolved = path.resolve()
    cover_root = COVER_ROOT.resolve()
    cover_root.mkdir(parents=True, exist_ok=True)
    # Allow covers under COVER_ROOT, or (for image kind) the media file itself if used as cover
    media_root = MEDIA_ROOT.resolve()
    ok = str(resolved).startswith(str(cover_root)) or str(resolved).startswith(str(media_root))
    if not ok:
        raise HTTPException(404, "Cover not available")
    suffix = resolved.suffix.lower()
    media_type = {
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(suffix, "image/jpeg")
    return _file_response_cached(
        resolved,
        media_type=media_type,
        filename=resolved.name,
        max_age=86_400,
    )


@router.post("/media/covers/backfill")
async def backfill_media_covers(db: Db, user: CurrentUser):
    """Generate missing poster JPEGs for library videos/images (ffmpeg once per file)."""
    _ = user
    rows = (await db.execute(select(CampaignMedia).order_by(CampaignMedia.id.asc()))).scalars().all()
    generated = 0
    skipped = 0
    for row in rows:
        if _cover_path(row):
            skipped += 1
            continue
        kind = _media_kind_of(row)
        ok = False
        if kind == "video":
            ok = _set_cover_from_first_frame(row, force=False)
        elif kind == "image":
            src = _media_path(row)
            if src:
                ok = _set_cover_from_image_file(row, src)
        if ok:
            generated += 1
    if generated:
        await db.commit()
    return {"generated": generated, "skipped": skipped, "total": len(rows)}


@router.patch("/media/{media_id}", response_model=CampaignMediaOut)
async def update_media(media_id: int, body: CampaignMediaUpdate, db: Db, user: CurrentUser):
    row = await db.get(CampaignMedia, media_id)
    if not row:
        raise HTTPException(404, "Media not found")
    next_topic = body.topic if body.topic is not None else body.title
    if next_topic is not None:
        display = next_topic.strip()
        if not display:
            raise HTTPException(400, "Topic is required")
        row.title = display[:200]  # no uniqueness check — uid is the public identifier
    if body.tags is not None:
        row.tags = _tags_json(_parse_tags(json.dumps(body.tags)))
    if "folder_id" in body.model_fields_set:
        row.folder_id = await _validate_folder_id(db, body.folder_id)
    if "language" in body.model_fields_set:
        if body.language is None or not str(body.language).strip():
            row.language = ""
        else:
            row.language = _normalize_media_language_input(body.language)
    await db.commit()
    await db.refresh(row)
    return _media_out(
        row, user.name or user.email, count=await _media_send_count_one(db, row.id)
    )


@router.post("/media/{media_id}/replace", response_model=CampaignMediaOut)
async def replace_media(
    media_id: int,
    db: Db,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    """Replace the underlying file; keep topic/tags/folder when possible."""
    row = await db.get(CampaignMedia, media_id)
    if not row:
        raise HTTPException(404, "Media not found")
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(400, "File too large (max 2GB)")
    mime = (file.content_type or "").lower().split(";")[0].strip()
    original = file.filename or "media.bin"
    ext = Path(original).suffix.lower()
    kind = _detect_kind(mime, ext)
    if not kind:
        raise HTTPException(400, "Unsupported replacement file type")
    mime = _normalize_mime(kind, mime, ext)
    if not ext:
        ext = {"video": ".mp4", "image": ".jpg", "document": ".pdf"}[kind]

    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}{ext}"
    dest = MEDIA_ROOT / stored
    dest.write_bytes(content)
    _unlink_media_file(row)
    _unlink_cover(row)
    row.file_name = _safe_name(original)
    row.stored_name = stored
    row.stored_path = str(dest)
    row.mime_type = mime
    row.media_kind = kind
    row.file_size = len(content)
    row.cover_name = ""
    row.cover_path = ""
    row.duration_seconds = 0
    if kind == "video":
        _set_cover_from_first_frame(row, force=True)
        row.duration_seconds = _probe_duration_seconds(dest)
    elif kind == "image":
        _set_cover_from_image_file(row, dest)
    await db.commit()
    await db.refresh(row)
    return _media_out(
        row, user.name or user.email, count=await _media_send_count_one(db, row.id)
    )


@router.post("/media/{media_id}/cover/regenerate", response_model=CampaignMediaOut)
async def regenerate_cover(media_id: int, db: Db, user: CurrentUser):
    """Replace cover with the video's first frame (fixes wrong mid-clip thumbs)."""
    row = await db.get(CampaignMedia, media_id)
    if not row:
        raise HTTPException(404, "Media not found")
    if _media_kind_of(row) != "video":
        raise HTTPException(400, "Only video covers can be regenerated from first frame")
    if not _set_cover_from_first_frame(row, force=True):
        raise HTTPException(500, "Could not extract first-frame cover")
    await db.commit()
    await db.refresh(row)
    return _media_out(
        row, user.name or user.email, count=await _media_send_count_one(db, row.id)
    )


@router.post("/media/{media_id}/cover", response_model=CampaignMediaOut)
async def upload_cover(
    media_id: int,
    db: Db,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    """Optional manual cover replace; UI prefers first-frame regenerate."""
    row = await db.get(CampaignMedia, media_id)
    if not row:
        raise HTTPException(404, "Media not found")
    await _store_uploaded_cover(row, file)
    await db.commit()
    await db.refresh(row)
    return _media_out(
        row, user.name or user.email, count=await _media_send_count_one(db, row.id)
    )


@router.delete("/media/{media_id}", status_code=204)
async def delete_media(media_id: int, db: Db, user: CurrentUser):
    _ = user
    row = await db.get(CampaignMedia, media_id)
    if not row:
        raise HTTPException(404, "Media not found")
    _unlink_media_file(row)
    _unlink_cover(row)
    campaigns = (await db.execute(select(Campaign))).scalars().all()
    for c in campaigns:
        ids = _parse_media_ids(c.media_ids)
        if media_id in ids:
            c.media_ids = json.dumps([i for i in ids if i != media_id])
    await db.delete(row)
    await db.commit()


async def _authorized_send_instances(db: AsyncSession, user: User) -> list[WhatsAppInstance]:
    """Evolution instances the user may send from (admin = all YesWeigh)."""
    from app.ai.tools import ORG, authorized_instance_ids

    allowed = await authorized_instance_ids(db, user)
    q = (
        select(WhatsAppInstance)
        .where(
            WhatsAppInstance.organization_id == ORG,
            WhatsAppInstance.deleted_at.is_(None),
            WhatsAppInstance.provider != "meta_cloud",
        )
        .order_by(WhatsAppInstance.employee_name, WhatsAppInstance.display_name)
    )
    if allowed is not None:
        if not allowed:
            return []
        q = q.where(WhatsAppInstance.id.in_(allowed))
    return list((await db.execute(q)).scalars().all())


def _send_instance_out(inst: WhatsAppInstance) -> CampaignSendInstanceOut:
    name = (inst.employee_name or inst.display_name or "agent").strip()
    phone = (inst.connected_phone_number or inst.expected_phone_number or "").strip()
    label = name
    if phone:
        label = f"{name} · {phone}"
    return CampaignSendInstanceOut(
        public_id=inst.public_id or "",
        display_name=inst.display_name or "",
        employee_name=inst.employee_name or "",
        employee_code=inst.employee_code or "",
        connected_phone=phone,
        connection_status=inst.connection_status or "Disconnected",
        label=label,
    )


@router.get("/send-instances", response_model=list[CampaignSendInstanceOut])
async def list_campaign_send_instances(db: Db, user: CurrentUser):
    """Connected Evolution lines available for Resume-on-new-instance (Campaigns ACL)."""
    rows = await _authorized_send_instances(db, user)
    return [_send_instance_out(r) for r in rows]


@router.post("/runs/{run_id}/restart", response_model=CampaignRunOut)
async def restart_campaign_run(run_id: int, db: Db, user: CurrentUser):
    """Restart a Failed/Stopped paced blast on the same Evolution instance after reconnect.

    Skips already-successful recipients. Does not touch other active campaigns.
    """
    from app.paced_media_batch import resume_paced_campaign_run, wake_paced_worker

    run = await db.get(CampaignRun, run_id)
    if not run:
        raise HTTPException(404, "Campaign run not found")
    try:
        await resume_paced_campaign_run(db, run, user=user, from_instance=None, mode="restart")
        await db.commit()
        await db.refresh(run)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    wake_paced_worker()
    media_row = await db.get(CampaignMedia, run.media_id) if run.media_id else None
    return _run_out(run, media_row)


@router.post("/runs/{run_id}/resume", response_model=CampaignRunOut)
async def resume_campaign_run_on_instance(run_id: int, body: CampaignResumeIn, db: Db, user: CurrentUser):
    """Resume a Failed/Stopped blast on a different connected Evolution / WhatsApp number.

    Rebinds the run, clears failure/block stuck state, continues from unsent + failed only
    (never re-blasts Success). Other healthy active campaigns are left alone.
    """
    from app.paced_media_batch import resume_paced_campaign_run, wake_paced_worker

    run = await db.get(CampaignRun, run_id)
    if not run:
        raise HTTPException(404, "Campaign run not found")

    pid = (body.from_instance_public_id or "").strip()
    if not pid:
        raise HTTPException(400, "from_instance_public_id is required")

    instances = await _authorized_send_instances(db, user)
    inst = next((i for i in instances if (i.public_id or "") == pid), None)
    if not inst:
        raise HTTPException(404, "Evolution instance not found or not authorized")

    try:
        await resume_paced_campaign_run(
            db, run, user=user, from_instance=inst, mode="resume_new_instance"
        )
        await db.commit()
        await db.refresh(run)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    wake_paced_worker()
    media_row = await db.get(CampaignMedia, run.media_id) if run.media_id else None
    return _run_out(run, media_row)


@router.get("/runs", response_model=list[CampaignRunOut])
async def list_campaign_runs(db: Db, user: CurrentUser):
    """Campaign Report rows (batch send summaries), newest first."""
    from datetime import datetime, timedelta, timezone

    from app.campaign_reports import refresh_campaign_run_replied_count

    _ = user
    rows = (
        await db.execute(select(CampaignRun).order_by(CampaignRun.occurred_at.desc(), CampaignRun.id.desc()))
    ).scalars().all()

    # Refresh replied cache for live runs + recent completed sends (replies arrive later).
    now = datetime.now(timezone.utc)
    refresh_budget = 25
    for r in rows:
        if refresh_budget <= 0:
            break
        status = (r.status or "").strip().lower()
        success = int(r.success_count or 0)
        if success <= 0 and status not in {"running", "queued"}:
            continue
        occurred = r.occurred_at or r.started_at or r.finished_at
        if occurred is not None and occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        recent = occurred is not None and occurred >= now - timedelta(days=14)
        if status in {"running", "queued"} or recent:
            await refresh_campaign_run_replied_count(db, r)
            refresh_budget -= 1
    if refresh_budget < 25:
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            try:
                await db.flush()
            except Exception:  # noqa: BLE001
                pass

    media_ids = {int(r.media_id) for r in rows if r.media_id is not None}
    media_map: dict[int, CampaignMedia] = {}
    if media_ids:
        media_rows = (
            await db.execute(select(CampaignMedia).where(CampaignMedia.id.in_(media_ids)))
        ).scalars().all()
        media_map = {int(m.id): m for m in media_rows}
    return [_run_out(r, media_map.get(int(r.media_id)) if r.media_id is not None else None) for r in rows]


@router.get("", response_model=list[CampaignOut])
async def list_campaigns(db: Db, user: CurrentUser):
    _ = user
    rows = (await db.execute(select(Campaign).order_by(Campaign.id.desc()))).scalars().all()
    names = await _user_names(db, {r.created_by for r in rows if r.created_by})
    return [_campaign_out(r, names.get(r.created_by or 0, "")) for r in rows]


@router.post("", response_model=CampaignOut)
async def create_campaign(body: CampaignIn, db: Db, user: CurrentUser):
    media_ids = await _validate_media_ids(db, body.media_ids)
    row = Campaign(
        name=body.name.strip(),
        status=body.status,
        description=(body.description or "").strip(),
        media_ids=json.dumps(media_ids),
        created_by=user.id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _campaign_out(row, user.name or user.email)


@router.patch("/{campaign_id}", response_model=CampaignOut)
async def update_campaign(campaign_id: int, body: CampaignUpdate, db: Db, user: CurrentUser):
    row = await db.get(Campaign, campaign_id)
    if not row:
        raise HTTPException(404, "Campaign not found")
    if body.name is not None:
        row.name = body.name.strip()
    if body.status is not None:
        row.status = body.status
    if body.description is not None:
        row.description = body.description.strip()
    if body.media_ids is not None:
        row.media_ids = json.dumps(await _validate_media_ids(db, body.media_ids))
    await db.commit()
    await db.refresh(row)
    names = await _user_names(db, {row.created_by} if row.created_by else set())
    return _campaign_out(row, names.get(row.created_by or 0, user.name or user.email))


@router.delete("/{campaign_id}", status_code=204)
async def delete_campaign(campaign_id: int, db: Db, user: CurrentUser):
    _ = user
    row = await db.get(Campaign, campaign_id)
    if not row:
        raise HTTPException(404, "Campaign not found")
    # Remove report rows first (FK campaign_runs.campaign_id → campaigns.id).
    await db.execute(delete(CampaignRun).where(CampaignRun.campaign_id == campaign_id))
    await db.delete(row)
    await db.commit()
