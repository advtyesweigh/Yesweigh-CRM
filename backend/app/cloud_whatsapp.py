"""Care = YesOne Meta Cloud WhatsApp → CRM inbox.

Inbound realtime (no YesOne redeploy):
  Meta → POST /api/webhooks/meta-whatsapp
  CRM dual-forwards raw body to YesOne ingestWhatsAppCloudWebhook, then Care ingest.

Inbound alternate (after YesOne Care forward deploy):
  Meta → YesOne → POST /api/webhooks/cloud-whatsapp (X-Care-Webhook-Secret)

Backfill: POST /api/whatsapp/care/sync

Outbound: sendWhatsAppCloudMessage callable (preferred), optional CRM proxy, or Graph token.

Evolution GO personal accounts (Biju/Shibin/Visakh) are untouched.
"""
from __future__ import annotations

import hmac
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from starlette.requests import Request as StarletteRequest
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import get_settings
from app.db import get_db
from app.evolution import ORG
from app.media_store import PUBLIC_PREFIX, save_bytes
from app.models import User, WhatsAppConversation, WhatsAppInstance, WhatsAppMessage
from app.sync_firebase_catalog import (
    DEFAULT_API_KEY,
    DEFAULT_PROJECT,
    auth_email_from_login,
    decode_doc,
    sign_in,
)

router = APIRouter(tags=["cloud-whatsapp"])

CARE_EVO_NAME = "care_meta_cloud"
CARE_DISPLAY_NAME = "Care"
PROVIDER_META_CLOUD = "meta_cloud"
YESONE_CLOUD_WEBHOOK = (
    "https://asia-south1-yesweigh-service.cloudfunctions.net/ingestWhatsAppCloudWebhook"
)
YESONE_CLOUD_SEND_CALLABLE = (
    "https://asia-south1-yesweigh-service.cloudfunctions.net/sendWhatsAppCloudMessage"
)
CARE_MEDIA_KINDS = {"image", "video", "audio", "document", "sticker", "voice", "ptt", "gif"}
# Match media_store / YesOne practical cap; Meta Cloud inbound media ids expire ~30 days.
MAX_CARE_MEDIA_BYTES = 25 * 1024 * 1024


def is_meta_cloud_instance(inst: WhatsAppInstance | None) -> bool:
    if not inst:
        return False
    return (inst.provider or "").strip().lower() == PROVIDER_META_CLOUD


# Meta Cloud API customer care / service window (free-form replies).
CARE_CUSTOMER_WINDOW = timedelta(hours=24)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def care_customer_care_window(
    last_inbound_at: datetime | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Care (meta_cloud) only: Open vs Expired from last inbound customer message.

    Meta's free-form reply window is typically 24h from the customer's last message.
    No inbound → Expired (template-only until the customer writes again).
    """
    clock = _as_utc(now) or datetime.now(timezone.utc)
    inbound = _as_utc(last_inbound_at)
    if inbound is None:
        return {
            "provider": PROVIDER_META_CLOUD,
            "care_window": "expired",
            "care_window_open": False,
            "can_freeform_reply": False,
            "last_inbound_at": None,
            "care_window_expires_at": None,
        }
    expires = inbound + CARE_CUSTOMER_WINDOW
    open_window = clock < expires
    return {
        "provider": PROVIDER_META_CLOUD,
        "care_window": "open" if open_window else "expired",
        "care_window_open": open_window,
        "can_freeform_reply": open_window,
        "last_inbound_at": inbound.isoformat(),
        "care_window_expires_at": expires.isoformat(),
    }


def _phone_digits(value: str) -> str:
    digits = re.sub(r"\D+", "", value or "")
    if len(digits) == 10:
        return f"91{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"91{digits[1:]}"
    return digits


def cloud_preview_label(msg_type: str, text: str) -> str:
    """Match YesOne conversationPreviewLabel for unsupported/empty types."""
    kind = (msg_type or "").strip().lower()
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if kind in {"image", "sticker", "gif"}:
        if cleaned and cleaned.lower() not in {"photo", "sticker", "gif"}:
            return cleaned[:255]
        if kind == "sticker" or cleaned.lower() == "sticker":
            return "Sticker"
        if cleaned.lower() == "gif":
            return "GIF"
        return "Photo"
    if kind == "video":
        return cleaned if cleaned and cleaned.lower() != "video" else "Video"
    if kind in {"audio", "voice", "ptt"}:
        return cleaned if cleaned and cleaned.lower() != "voice message" else "Voice message"
    if kind in {"document", "file"}:
        return cleaned if cleaned and cleaned.lower() != "document" else "Document"
    if kind in {"unsupported", "unknown"}:
        if cleaned and cleaned.lower() not in {"unsupported", "unknown"}:
            return cleaned[:255]
        return "Unsupported message"
    return cleaned[:255] if cleaned else ""


def cloud_message_body(msg_type: str, text: str) -> str:
    """Match YesOne MessageBody placeholder for empty / unsupported Cloud API payloads."""
    kind = (msg_type or "").strip().lower()
    cleaned = (text or "").strip()
    if kind in {"unsupported", "unknown"}:
        if cleaned and cleaned.lower() not in {"unsupported", "unknown", ""}:
            return cleaned
        # Meta omits body for type=unsupported (e.g. 131051). Not recoverable via Graph.
        return "Unsupported message"
    if cleaned:
        return cleaned
    if kind == "image":
        return "📷 Photo"
    if kind == "video":
        return "🎥 Video"
    if kind in {"audio", "voice", "ptt"}:
        return "🎤 Voice message"
    if kind in {"document", "file"}:
        return "📄 Document"
    if kind == "sticker":
        return "Sticker"
    if kind == "location":
        return "Location"
    if kind == "contacts":
        return "Contact"
    if kind == "order":
        return "Order"
    if kind == "system":
        return "System"
    if kind == "reaction":
        return "Reaction"
    return "Unsupported message"


def is_local_media_url(url: str) -> bool:
    return (url or "").startswith(PUBLIC_PREFIX + "/")


def _care_media_type(msg_type: str = "", mime_type: str = "") -> str:
    kind = (msg_type or "").strip().lower()
    if kind in {"image", "gif"}:
        return "image"
    if kind == "sticker":
        return "sticker"
    if kind in {"video", "audio", "document"}:
        return kind
    if kind in {"voice", "ptt"}:
        return "audio"
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if mime in {"application/pdf", "application/msword"} or mime.startswith("application/"):
        return "document"
    return ""


def _care_media_filename(media_type: str, media_name: str = "", mime_type: str = "") -> str:
    if (media_name or "").strip():
        return media_name.strip()[:180]
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if "jpeg" in mime or mime == "image/jpg":
        return "photo.jpg"
    if "png" in mime:
        return "photo.png"
    if "webp" in mime:
        return "sticker.webp" if media_type == "sticker" else "image.webp"
    if "ogg" in mime:
        return "voice.ogg"
    if "mpeg" in mime or "mp3" in mime:
        return "voice.mp3"
    if "mp4" in mime and media_type == "audio":
        return "voice.mp4"
    if "mp4" in mime:
        return "video.mp4"
    if "pdf" in mime:
        return "file.pdf"
    return {
        "image": "photo.jpg",
        "video": "video.mp4",
        "audio": "voice.ogg",
        "document": "file.bin",
        "sticker": "sticker.webp",
    }.get(media_type or "", "file.bin")


async def download_meta_graph_media(
    media_id: str,
    *,
    access_token: str = "",
) -> tuple[bytes, str]:
    """Meta Graph: GET /{media-id} → temporary URL → binary (Authorization required)."""
    mid = (media_id or "").strip()
    token = (access_token or get_settings().whatsapp_cloud_access_token or "").strip()
    if not mid or not token:
        return b"", ""
    version = (get_settings().whatsapp_cloud_graph_version or "v21.0").strip() or "v21.0"
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        info_r = await client.get(f"https://graph.facebook.com/{version}/{mid}", headers=headers)
        info = info_r.json() if info_r.content else {}
        if not info_r.is_success or not isinstance(info, dict) or not info.get("url"):
            return b"", ""
        dl = await client.get(str(info["url"]), headers=headers)
        if not dl.is_success:
            return b"", ""
        mime = str(dl.headers.get("content-type") or info.get("mime_type") or "").split(";")[0].strip()
        return dl.content, mime


async def persist_care_media(
    *,
    media_url: str = "",
    media_id: str = "",
    media_type: str = "",
    media_name: str = "",
    mime_type: str = "",
    msg_type: str = "",
) -> dict[str, str]:
    """
    Download Care media to VPS disk (same /data/wa-media as Evolution).

    Prefer durable HTTP sources (YesOne Firebase Storage URLs). Fall back to Meta
    media id via Graph when the inbound webhook still only has an id.
    """
    mt = media_type or _care_media_type(msg_type, mime_type)
    name = _care_media_filename(mt, media_name, mime_type)
    url = (media_url or "").strip()
    mid = (media_id or "").strip()

    if is_local_media_url(url):
        return {"media_type": mt, "media_name": name or media_name, "media_url": url}

    if not url and not mid:
        if mt:
            return {"media_type": mt, "media_name": name, "media_url": ""}
        return {}

    try:
        content = b""
        detected_mime = (mime_type or "").strip()
        if url.startswith(("http://", "https://")):
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                r = await client.get(url)
                r.raise_for_status()
                content = r.content
                if not detected_mime:
                    detected_mime = str(r.headers.get("content-type") or "").split(";")[0].strip()
        elif mid:
            content, g_mime = await download_meta_graph_media(mid)
            if not detected_mime:
                detected_mime = g_mime

        if not content:
            # Keep remote URL so inbox can still load slowly until a later backfill.
            return {
                "media_type": mt,
                "media_name": name,
                "media_url": url if url.startswith(("http://", "https://")) else "",
            }
        if len(content) > MAX_CARE_MEDIA_BYTES:
            print(f"care media too large ({len(content)} bytes)")
            return {
                "media_type": mt,
                "media_name": name,
                "media_url": url if url.startswith(("http://", "https://")) else "",
            }
        if not mt:
            mt = _care_media_type(msg_type, detected_mime) or "document"
        name = _care_media_filename(mt, media_name or name, detected_mime)
        public, _ = save_bytes(content, media_type=mt or "document", filename=name)
        return {"media_type": mt, "media_name": name, "media_url": public}
    except Exception as exc:  # noqa: BLE001
        print(f"care media persist failed: {exc}")
        return {
            "media_type": mt,
            "media_name": name,
            "media_url": url if url.startswith(("http://", "https://")) else "",
        }


async def resolve_care_media_for_ingest(
    *,
    msg_type: str = "",
    media_url: str = "",
    media_id: str = "",
    media_name: str = "",
    mime_type: str = "",
) -> tuple[str, str]:
    """Persist when message looks like media; returns (media_url, media_name)."""
    kind = (msg_type or "").strip().lower()
    url = (media_url or "").strip()
    mid = (media_id or "").strip()
    if kind not in CARE_MEDIA_KINDS and not url and not mid:
        return url, media_name
    if is_local_media_url(url):
        return url, media_name
    saved = await persist_care_media(
        media_url=url,
        media_id=mid,
        media_name=media_name,
        mime_type=mime_type,
        msg_type=msg_type,
    )
    return saved.get("media_url") or url, saved.get("media_name") or media_name


def _parse_firestore_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Firestore Timestamp may already be decoded as seconds or ms via other paths
        n = float(value)
        if n > 1e12:
            n /= 1000.0
        elif n > 1e10:
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, dict):
        # Admin/REST sometimes leave {seconds, nanoseconds}
        secs = value.get("seconds") or value.get("_seconds")
        if secs is not None:
            try:
                return datetime.fromtimestamp(float(secs), tz=timezone.utc)
            except (OverflowError, OSError, ValueError, TypeError):
                return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def get_care_instance(db: AsyncSession) -> WhatsAppInstance | None:
    return (
        await db.execute(
            select(WhatsAppInstance).where(
                WhatsAppInstance.organization_id == ORG,
                WhatsAppInstance.deleted_at.is_(None),
                or_(
                    WhatsAppInstance.provider == PROVIDER_META_CLOUD,
                    WhatsAppInstance.evolution_instance_name == CARE_EVO_NAME,
                    WhatsAppInstance.display_name == CARE_DISPLAY_NAME,
                ),
            )
        )
    ).scalars().first()


async def require_care_instance(db: AsyncSession) -> WhatsAppInstance:
    inst = await get_care_instance(db)
    if not inst:
        raise HTTPException(404, "Care (Meta Cloud) WhatsApp instance is not configured")
    return inst


async def upsert_care_conversation(
    db: AsyncSession,
    inst: WhatsAppInstance,
    *,
    wa_id: str,
    sender_name: str = "",
    last_text: str = "",
    last_type: str = "",
    last_at: datetime | None = None,
    unread_count: int | None = None,
    closed: bool = False,
    channel_phone: str = "",
    profile_image: str = "",
) -> WhatsAppConversation:
    phone = _phone_digits(wa_id)
    jid = f"{phone}@s.whatsapp.net" if phone else ""
    existing = (
        await db.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.whatsapp_instance_id == inst.id,
                or_(
                    WhatsAppConversation.customer_phone == phone,
                    WhatsAppConversation.customer_jid == jid,
                    WhatsAppConversation.customer_phone == wa_id,
                ),
            )
        )
    ).scalars().first()

    preview = cloud_preview_label(last_type, last_text) or (last_text or "")[:255]
    name = (sender_name or "").strip() or (existing.customer_name if existing else "") or phone

    if existing:
        if name and name != existing.customer_name:
            existing.customer_name = name
        if phone:
            existing.customer_phone = phone
        if jid:
            existing.customer_jid = jid
        if preview:
            existing.last_message_preview = preview[:255]
        if last_at and (not existing.last_message_at or last_at >= existing.last_message_at):
            existing.last_message_at = last_at
        if unread_count is not None:
            existing.unread_count = max(0, int(unread_count))
        if profile_image and not existing.customer_photo:
            existing.customer_photo = profile_image[:2000]
        if closed:
            existing.status = "Resolved"
        elif existing.status == "Resolved":
            pass
        elif existing.status in {"", "New"}:
            existing.status = "Open"
        conv = existing
    else:
        conv = WhatsAppConversation(
            public_id=str(uuid.uuid4()),
            organization_id=ORG,
            whatsapp_instance_id=inst.id,
            customer_phone=phone or wa_id,
            customer_name=name,
            customer_photo=(profile_image or "")[:2000],
            customer_jid=jid,
            status="Resolved" if closed else "Open",
            tag="Care",
            last_message_preview=preview[:255],
            last_message_at=last_at,
            unread_count=max(0, int(unread_count or 0)),
            customer_type="Customer",
        )
        db.add(conv)
        await db.flush()

    if channel_phone:
        digits = _phone_digits(channel_phone)
        if digits and digits != (inst.connected_phone_number or ""):
            inst.connected_phone_number = digits
    return conv


async def upsert_care_message(
    db: AsyncSession,
    inst: WhatsAppInstance,
    conv: WhatsAppConversation,
    *,
    external_id: str,
    direction: str,
    msg_type: str = "text",
    text: str = "",
    status: str = "",
    media_url: str = "",
    media_name: str = "",
    mime_type: str = "",
    sender_name: str = "",
    created_at: datetime | None = None,
    transcript: str = "",
) -> tuple[WhatsAppMessage | None, bool]:
    """Upsert Care message. Returns ``(message, content_changed)`` for outline refresh gating."""
    ext = (external_id or "").strip()
    if not ext:
        return None, False
    existing = (
        await db.execute(
            select(WhatsAppMessage).where(
                WhatsAppMessage.whatsapp_instance_id == inst.id,
                WhatsAppMessage.external_message_id == ext,
            )
        )
    ).scalars().first()

    body = cloud_message_body(msg_type, text)
    dir_norm = "in" if str(direction).lower() in {"in", "inbound", "incoming"} else "out"
    media_type = ""
    kind = (msg_type or "").lower()
    if kind in {"image", "sticker", "gif"}:
        media_type = "image" if kind != "sticker" else "sticker"
    elif kind in {"video", "audio", "document"}:
        media_type = kind
    elif kind in {"voice", "ptt"}:
        media_type = "audio"
    elif kind in {"unsupported", "unknown"}:
        # Preserve Meta type so Care UI can treat these differently from empty text.
        media_type = "unsupported"
    elif mime_type.startswith("image/"):
        media_type = "image"
    elif mime_type.startswith("audio/"):
        media_type = "audio"
    elif mime_type.startswith("video/"):
        media_type = "video"

    status_map = {
        "received": "delivered",
        "sent": "sent",
        "delivered": "delivered",
        "read": "read",
        "failed": "failed",
        "pending": "sending",
    }
    st = status_map.get((status or "").lower(), "delivered" if dir_norm == "in" else "sent")

    if existing:
        content_changed = False
        old_body = (existing.body or "").strip()
        placeholderish = (
            not old_body
            or old_body.startswith("WhatsApp did not")
            or old_body.lower() in {"unsupported message", "unsupported", "unknown"}
        )
        if body and placeholderish and body != old_body:
            existing.body = body
            content_changed = True
        # Upgrade empty/remote Firebase URLs to durable VPS /api/whatsapp/media/... paths.
        if media_url and (
            not existing.media_url
            or (is_local_media_url(media_url) and not is_local_media_url(existing.media_url))
        ):
            existing.media_url = media_url
        if media_type and not existing.media_type:
            existing.media_type = media_type
        if media_name and not existing.media_name:
            existing.media_name = media_name
        if transcript and not existing.transcript:
            existing.transcript = transcript
            content_changed = True
        if st:
            existing.status = st
        if created_at and existing.created_at and created_at < existing.created_at:
            existing.created_at = created_at
        return existing, content_changed

    msg = WhatsAppMessage(
        public_id=str(uuid.uuid4()),
        organization_id=ORG,
        conversation_id=conv.id,
        whatsapp_instance_id=inst.id,
        direction=dir_norm,
        body=body,
        media_url=media_url or "",
        media_type=media_type,
        media_name=media_name or "",
        transcript=transcript or "",
        external_message_id=ext,
        status=st,
        sender_name=(sender_name or ("Care" if dir_norm == "out" else conv.customer_name or ""))[:120],
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(msg)
    await db.flush()
    # New message rows always change the outline fingerprint (count / last id).
    return msg, True


class CareWebhookMessage(BaseModel):
    external_id: str = ""
    wa_id: str
    direction: str = "inbound"
    type: str = "text"
    text: str = ""
    status: str = ""
    media_url: str = ""
    media_id: str = ""
    media_name: str = ""
    mime_type: str = ""
    sender_name: str = ""
    created_at: str | None = None
    transcript: str = ""


class CareWebhookConversation(BaseModel):
    wa_id: str
    sender_name: str = ""
    last_text: str = ""
    last_type: str = ""
    last_at: str | None = None
    unread_count: int = 0
    closed: bool = False
    channel_phone: str = ""
    profile_image: str = ""


class CareWebhookIn(BaseModel):
    conversations: list[CareWebhookConversation] = Field(default_factory=list)
    messages: list[CareWebhookMessage] = Field(default_factory=list)


class CareSendIn(BaseModel):
    to: str
    text: str = ""
    client_message_id: str = ""


def _check_care_secret(provided: str | None) -> None:
    expected = (get_settings().care_webhook_secret or "").strip()
    if not expected:
        raise HTTPException(503, "CARE_WEBHOOK_SECRET is not configured")
    got = (provided or "").strip()
    if not got or not hmac.compare_digest(got, expected):
        raise HTTPException(401, "Unauthorized webhook")


def _firebase_email(settings) -> str:
    email = (settings.firebase_email or "").strip()
    if email:
        return email
    login = (settings.firebase_login_id or "").strip()
    if login:
        return auth_email_from_login(login)
    raise HTTPException(503, "FIREBASE_EMAIL or FIREBASE_LOGIN_ID not configured for Care sync")


async def _firebase_id_token(client: httpx.AsyncClient, settings) -> tuple[str, str]:
    password = (settings.firebase_password or "").strip()
    if not password:
        raise HTTPException(503, "FIREBASE_PASSWORD not configured for Care sync")
    api_key = (settings.firebase_api_key or DEFAULT_API_KEY).strip() or DEFAULT_API_KEY
    project = (settings.firebase_project_id or DEFAULT_PROJECT).strip() or DEFAULT_PROJECT
    email = _firebase_email(settings)
    token = await sign_in(client, api_key, email, password)
    return token, project


async def _run_query(
    client: httpx.AsyncClient,
    *,
    project: str,
    id_token: str,
    structured_query: dict[str, Any],
) -> list[dict[str, Any]]:
    url = f"https://firestore.googleapis.com/v1/projects/{project}/databases/(default)/documents:runQuery"
    r = await client.post(
        url,
        headers={"Authorization": f"Bearer {id_token}"},
        json={"structuredQuery": structured_query},
        timeout=60.0,
    )
    if not r.is_success:
        raise HTTPException(502, f"Firestore query failed: {r.status_code} {r.text[:240]}")
    payload = r.json()
    rows: list[dict[str, Any]] = []
    for item in payload if isinstance(payload, list) else []:
        doc = item.get("document")
        if doc:
            rows.append(decode_doc(doc))
    return rows


async def sync_care_from_firestore(
    db: AsyncSession,
    *,
    conversation_limit: int | None = None,
    message_wa_id: str | None = None,
    message_limit: int | None = None,
) -> dict[str, Any]:
    """Backfill Care chats from YesOne Firestore (catalog-sync style Auth)."""
    settings = get_settings()
    inst = await require_care_instance(db)
    conv_limit = conversation_limit or settings.care_sync_conversation_limit
    msg_limit = message_limit or settings.care_sync_message_limit

    async with httpx.AsyncClient(timeout=60.0) as client:
        id_token, project = await _firebase_id_token(client, settings)
        try:
            meta = await client.post(
                f"https://asia-south1-{project}.cloudfunctions.net/getWhatsAppCloudSettingsFn",
                headers={"Authorization": f"Bearer {id_token}", "Content-Type": "application/json"},
                json={"data": None},
                timeout=30.0,
            )
            if meta.is_success:
                result = (meta.json() or {}).get("result") or {}
                if isinstance(result, dict) and result.get("configured"):
                    display = str(result.get("displayPhoneNumber") or "").strip()
                    phone_id = str(result.get("phoneNumberId") or "").strip()
                    if display:
                        inst.connected_phone_number = _phone_digits(display) or display
                    if phone_id:
                        inst.evolution_instance_id = phone_id
                    inst.connection_status = "Connected"
        except Exception:  # noqa: BLE001
            pass

        conv_docs = await _run_query(
            client,
            project=project,
            id_token=id_token,
            structured_query={
                "from": [{"collectionId": "whatsappConversations"}],
                "orderBy": [{"field": {"fieldPath": "lastAt"}, "direction": "DESCENDING"}],
                "limit": max(1, min(int(conv_limit), 300)),
            },
        )

        conversations_upserted = 0
        messages_upserted = 0
        target_wa = _phone_digits(message_wa_id or "") if message_wa_id else ""

        for doc in conv_docs:
            wa_id = _phone_digits(str(doc.get("waId") or doc.get("_id") or ""))
            if not wa_id:
                continue
            if target_wa and wa_id != target_wa:
                continue
            last_at = _parse_firestore_time(doc.get("lastAt") or doc.get("lastInboundAt") or doc.get("updatedAt"))
            conv = await upsert_care_conversation(
                db,
                inst,
                wa_id=wa_id,
                sender_name=str(doc.get("senderName") or doc.get("softwareShopName") or ""),
                last_text=str(doc.get("lastText") or ""),
                last_type=str(doc.get("lastType") or ""),
                last_at=last_at,
                unread_count=int(doc.get("unreadCount") or 0),
                closed=bool(doc.get("closed")),
                channel_phone=str(doc.get("channelPhoneNumber") or ""),
                profile_image=str(doc.get("profileImage") or ""),
            )
            conversations_upserted += 1

            pull_messages = bool(target_wa) or conversations_upserted <= 40
            if not pull_messages:
                continue

            msg_docs = await _run_query(
                client,
                project=project,
                id_token=id_token,
                structured_query={
                    "from": [{"collectionId": "whatsappMessages"}],
                    "where": {
                        "fieldFilter": {
                            "field": {"fieldPath": "waId"},
                            "op": "EQUAL",
                            "value": {"stringValue": wa_id},
                        }
                    },
                    "orderBy": [{"field": {"fieldPath": "createdAt"}, "direction": "ASCENDING"}],
                    "limit": max(1, min(int(msg_limit), 500)),
                },
            )
            latest_preview = ""
            latest_type = ""
            latest_at = conv.last_message_at
            for m in msg_docs:
                ext = str(m.get("whatsappMessageId") or m.get("_id") or "").strip()
                created = _parse_firestore_time(m.get("createdAt"))
                text = str(m.get("text") or "")
                translated = str(m.get("translatedText") or "").strip()
                transcript = str(m.get("transcript") or m.get("malayalamText") or "").strip()
                display_text = translated or text
                msg_type = str(m.get("type") or "text")
                raw_media_url = str(m.get("mediaUrl") or m.get("translatedMediaUrl") or "")
                raw_media_name = str(m.get("fileName") or "")
                raw_mime = str(m.get("mimeType") or "")
                media_url, media_name = await resolve_care_media_for_ingest(
                    msg_type=msg_type,
                    media_url=raw_media_url,
                    media_name=raw_media_name,
                    mime_type=raw_mime,
                )
                await upsert_care_message(
                    db,
                    inst,
                    conv,
                    external_id=ext,
                    direction=str(m.get("direction") or "inbound"),
                    msg_type=msg_type,
                    text=display_text,
                    status=str(m.get("status") or ""),
                    media_url=media_url,
                    media_name=media_name,
                    mime_type=raw_mime,
                    sender_name=str(m.get("senderName") or m.get("sentByName") or ""),
                    created_at=created,
                    transcript=transcript,
                )
                messages_upserted += 1
                if created and (not latest_at or created >= latest_at):
                    latest_at = created
                    latest_preview = display_text
                    latest_type = msg_type
            if latest_preview or latest_type:
                preview = cloud_preview_label(latest_type, latest_preview)
                if preview:
                    conv.last_message_preview = preview[:255]
                if latest_at:
                    conv.last_message_at = latest_at

        inst.history_sync_status = "ok"
        inst.history_sync_at = datetime.now(timezone.utc)
        inst.history_sync_stats = (
            f'{{"conversations":{conversations_upserted},"messages":{messages_upserted}}}'
        )
        inst.history_sync_error = ""
        if inst.connection_status != "Connected":
            inst.connection_status = "Connected"
        await db.commit()
        return {
            "ok": True,
            "instance_id": inst.public_id,
            "conversations": conversations_upserted,
            "messages": messages_upserted,
            "connection_status": inst.connection_status,
            "connected_phone_number": inst.connected_phone_number,
        }


@router.post("/api/whatsapp/care/sync")
async def care_sync(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    wa_id: str = Query("", description="Optional single chat waId to sync deeply"),
):
    """Pull Care conversations/messages from YesOne Firestore into CRM."""
    _ = user
    try:
        return await sync_care_from_firestore(db, message_wa_id=wa_id or None)
    except HTTPException:
        raise
    except SystemExit as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Care sync failed: {exc}") from exc


@router.post("/api/webhooks/cloud-whatsapp")
async def cloud_whatsapp_webhook(
    body: CareWebhookIn,
    db: AsyncSession = Depends(get_db),
    x_care_webhook_secret: str | None = Header(default=None, alias="X-Care-Webhook-Secret"),
):
    """Ingest normalized Care events forwarded from YesOne Cloud webhook."""
    from app.whatsapp_outline import schedule_outline_refresh

    _check_care_secret(x_care_webhook_secret)
    inst = await require_care_instance(db)
    conv_by_wa: dict[str, WhatsAppConversation] = {}
    outline_conv_ids: set[int] = set()
    for c in body.conversations:
        wa = _phone_digits(c.wa_id)
        if not wa:
            continue
        conv = await upsert_care_conversation(
            db,
            inst,
            wa_id=wa,
            sender_name=c.sender_name,
            last_text=c.last_text,
            last_type=c.last_type,
            last_at=_parse_firestore_time(c.last_at),
            unread_count=c.unread_count,
            closed=c.closed,
            channel_phone=c.channel_phone,
            profile_image=c.profile_image,
        )
        conv_by_wa[wa] = conv
    for m in body.messages:
        wa = _phone_digits(m.wa_id)
        if not wa:
            continue
        conv = conv_by_wa.get(wa)
        if not conv:
            conv = await upsert_care_conversation(
                db,
                inst,
                wa_id=wa,
                sender_name=m.sender_name,
                last_text=m.text,
                last_type=m.type,
                last_at=_parse_firestore_time(m.created_at),
            )
            conv_by_wa[wa] = conv
        media_url, media_name = await resolve_care_media_for_ingest(
            msg_type=m.type,
            media_url=m.media_url,
            media_id=m.media_id,
            media_name=m.media_name,
            mime_type=m.mime_type,
        )
        _msg, content_changed = await upsert_care_message(
            db,
            inst,
            conv,
            external_id=m.external_id or str(uuid.uuid4()),
            direction=m.direction,
            msg_type=m.type,
            text=m.text,
            status=m.status,
            media_url=media_url,
            media_name=media_name,
            mime_type=m.mime_type,
            sender_name=m.sender_name,
            created_at=_parse_firestore_time(m.created_at),
            transcript=m.transcript,
        )
        if content_changed:
            outline_conv_ids.add(conv.id)
        preview = cloud_preview_label(m.type, m.text)
        if preview:
            conv.last_message_preview = preview[:255]
        created = _parse_firestore_time(m.created_at)
        if created:
            conv.last_message_at = created
        if str(m.direction).lower() in {"in", "inbound", "incoming"}:
            conv.unread_count = int(conv.unread_count or 0) + 1
            if conv.status == "Resolved":
                conv.status = "Open"
    inst.connection_status = "Connected"
    await db.commit()
    if outline_conv_ids:
        schedule_outline_refresh(outline_conv_ids)
    return {"ok": True, "conversations": len(body.conversations), "messages": len(body.messages)}


async def _send_via_yesone_callable(
    client: httpx.AsyncClient,
    *,
    phone: str,
    text: str,
    client_message_id: str,
) -> str:
    settings = get_settings()
    id_token, _project = await _firebase_id_token(client, settings)
    r = await client.post(
        YESONE_CLOUD_SEND_CALLABLE,
        headers={
            "Authorization": f"Bearer {id_token}",
            "Content-Type": "application/json",
        },
        json={
            "data": {
                "waId": phone,
                "text": text,
                "clientMessageId": client_message_id or "",
                "skipTranslation": True,
            }
        },
    )
    data = r.json() if r.content else {}
    if not r.is_success:
        err = data.get("error") if isinstance(data, dict) else None
        detail = ""
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("status") or "")
        detail = detail or (data.get("message") if isinstance(data, dict) else "") or r.text[:300]
        status = 409 if re.search(r"24 hours|re-engagement|131047", str(detail), re.I) else 502
        raise HTTPException(status, f"Care send failed: {detail}")
    result = data.get("result") if isinstance(data, dict) else None
    if isinstance(result, dict):
        return str(result.get("id") or result.get("messageId") or "")
    return ""


async def send_care_cloud_text(
    *,
    to_wa_id: str = "",
    to: str = "",
    text: str,
    client_message_id: str = "",
) -> str:
    """Send via YesOne callable, optional proxy, or direct Graph. Returns Meta message id."""
    settings = get_settings()
    phone = _phone_digits(to_wa_id or to)
    if not phone:
        raise HTTPException(400, "Invalid recipient phone")
    body_text = (text or "").strip()
    if not body_text:
        raise HTTPException(400, "Message body required")

    async with httpx.AsyncClient(timeout=45.0) as client:
        # 1) Existing YesOne staff callable (token stays in Firestore). Preferred.
        try:
            return await _send_via_yesone_callable(
                client,
                phone=phone,
                text=body_text,
                client_message_id=client_message_id,
            )
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            pass

        # 2) Dedicated CRM proxy (when crmCareSendWhatsApp is deployed).
        proxy = (settings.yesone_care_send_url or "").strip()
        secret = (settings.care_webhook_secret or "").strip()
        if proxy and secret:
            r = await client.post(
                proxy,
                headers={
                    "Content-Type": "application/json",
                    "X-Care-Webhook-Secret": secret,
                },
                json={
                    "to": phone,
                    "text": body_text,
                    "clientMessageId": client_message_id or "",
                },
            )
            if r.status_code != 404:
                data = r.json() if r.content else {}
                if not r.is_success:
                    detail = data.get("error") or data.get("message") or r.text[:300]
                    raise HTTPException(502, f"Care send proxy failed: {detail}")
                return str(data.get("messageId") or data.get("id") or "")

        # 3) Direct Graph (optional env token).
        token = (settings.whatsapp_cloud_access_token or "").strip()
        phone_number_id = (settings.whatsapp_cloud_phone_number_id or "").strip()
        if not token or not phone_number_id:
            raise HTTPException(
                503,
                "Care reply is not configured. Need Firebase catalog login for "
                "sendWhatsAppCloudMessage, or CARE proxy / Graph token env.",
            )
        version = (settings.whatsapp_cloud_graph_version or "v21.0").strip()
        url = f"https://graph.facebook.com/{version}/{phone_number_id}/messages"
        r = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": phone,
                "type": "text",
                "text": {"preview_url": False, "body": body_text},
            },
        )
        data = r.json() if r.content else {}
        if not r.is_success or data.get("error"):
            err = data.get("error") or {}
            detail = err.get("error_user_msg") or err.get("message") or r.text[:300]
            raise HTTPException(502, f"Meta Cloud send failed: {detail}")
        messages = data.get("messages") or []
        if messages and isinstance(messages[0], dict):
            return str(messages[0].get("id") or "")
        return ""


def _meta_errors(message: dict[str, Any]) -> list[dict[str, Any]]:
    rows = message.get("errors") if isinstance(message.get("errors"), list) else []
    out: list[dict[str, Any]] = []
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue
        code = row.get("code")
        title = str(row.get("title") or row.get("message") or "").strip()
        err_data = row.get("error_data") if isinstance(row.get("error_data"), dict) else {}
        details = str(err_data.get("details") or row.get("details") or "").strip()
        if code is None and not title and not details:
            continue
        try:
            code_n = int(code) if code is not None else None
        except (TypeError, ValueError):
            code_n = None
        out.append({"code": code_n, "title": title, "details": details})
    return out


def _meta_message_text(message: dict[str, Any]) -> str:
    msg_type = str(message.get("type") or "")
    if msg_type == "text":
        return str((message.get("text") or {}).get("body") or "").strip()
    if msg_type == "button":
        button = message.get("button") or {}
        return str(button.get("text") or button.get("payload") or "").strip()
    if msg_type == "interactive":
        interactive = message.get("interactive") or {}
        reply = (
            interactive.get("button_reply")
            or interactive.get("list_reply")
            or interactive.get("nfm_reply")
            or {}
        )
        return str(
            reply.get("title")
            or reply.get("description")
            or reply.get("name")
            or reply.get("response_json")
            or interactive.get("type")
            or ""
        ).strip()
    if msg_type == "reaction":
        return str((message.get("reaction") or {}).get("emoji") or "").strip()
    if msg_type == "system":
        system = message.get("system") or {}
        return str(system.get("body") or system.get("type") or "").strip()
    if msg_type == "order":
        order = message.get("order") or {}
        items = order.get("product_items") if isinstance(order.get("product_items"), list) else []
        if items:
            n = len(items)
            return f"Order ({n} item{'s' if n != 1 else ''})"
        catalog = str(order.get("catalog_id") or "").strip()
        return f"Order {catalog}".strip() if catalog else "Order"
    if msg_type == "contacts":
        contacts = message.get("contacts") if isinstance(message.get("contacts"), list) else []
        for c in contacts:
            if not isinstance(c, dict):
                continue
            profile = c.get("profile") if isinstance(c.get("profile"), dict) else {}
            name = c.get("name") if isinstance(c.get("name"), dict) else {}
            label = str(profile.get("name") or name.get("formatted_name") or "").strip()
            if label:
                return label
        return "Contact" if contacts else ""
    if msg_type == "location":
        loc = message.get("location") or {}
        return str(loc.get("name") or loc.get("address") or "").strip()
    if msg_type in {"unsupported", "unknown"}:
        errors = _meta_errors(message)
        for err in errors:
            detail = str(err.get("details") or err.get("title") or "").strip()
            if detail:
                return detail
        return ""
    part = message.get(msg_type)
    if isinstance(part, dict):
        return str(part.get("caption") or part.get("body") or "").strip()
    return ""


def _meta_iso_ts(value: Any) -> str:
    try:
        if value is None or value == "":
            return datetime.now(timezone.utc).isoformat()
        n = int(value)
        ms = n if n > 1e12 else n * 1000
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc).isoformat()


def _normalize_meta_payload(body: dict[str, Any]) -> CareWebhookIn:
    conversations: list[CareWebhookConversation] = []
    messages: list[CareWebhookMessage] = []
    if str(body.get("object") or "") != "whatsapp_business_account":
        return CareWebhookIn()
    for entry in body.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value") if isinstance(change.get("value"), dict) else {}
            metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
            channel = str(metadata.get("display_phone_number") or "").strip()
            contacts = value.get("contacts") if isinstance(value.get("contacts"), list) else []
            contact_names: dict[str, str] = {}
            for c in contacts:
                if not isinstance(c, dict):
                    continue
                wa = _phone_digits(str(c.get("wa_id") or ""))
                profile = c.get("profile") if isinstance(c.get("profile"), dict) else {}
                name = str(profile.get("name") or "").strip()
                if wa and name:
                    contact_names[wa] = name
            for message in (value.get("messages") or [])[:40]:
                if not isinstance(message, dict):
                    continue
                wa = _phone_digits(str(message.get("from") or ""))
                external_id = str(message.get("id") or "").strip()
                if not wa or not external_id:
                    continue
                msg_type = str(message.get("type") or "text") or "text"
                text = _meta_message_text(message)
                created = _meta_iso_ts(message.get("timestamp"))
                sender = contact_names.get(wa, "")
                part = message.get(msg_type) if isinstance(message.get(msg_type), dict) else {}
                conversations.append(
                    CareWebhookConversation(
                        wa_id=wa,
                        sender_name=sender,
                        last_text=text or cloud_preview_label(msg_type, text),
                        last_type=msg_type,
                        last_at=created,
                        unread_count=1,
                        closed=False,
                        channel_phone=channel,
                    )
                )
                messages.append(
                    CareWebhookMessage(
                        external_id=external_id,
                        wa_id=wa,
                        direction="inbound",
                        type=msg_type,
                        text=text,
                        status="received",
                        media_id=str(part.get("id") or ""),
                        media_name=str(part.get("filename") or ""),
                        mime_type=str(part.get("mime_type") or ""),
                        sender_name=sender,
                        created_at=created,
                    )
                )
            echoes = []
            if isinstance(value.get("message_echoes"), list):
                echoes.extend(value["message_echoes"])
            if isinstance(value.get("smb_message_echoes"), list):
                echoes.extend(value["smb_message_echoes"])
            for message in echoes[:40]:
                if not isinstance(message, dict):
                    continue
                wa = _phone_digits(str(message.get("to") or message.get("recipient_id") or ""))
                external_id = str(message.get("id") or "").strip()
                if not wa or not external_id:
                    continue
                msg_type = str(message.get("type") or "text") or "text"
                text = _meta_message_text(message)
                created = _meta_iso_ts(message.get("timestamp"))
                part = message.get(msg_type) if isinstance(message.get(msg_type), dict) else {}
                conversations.append(
                    CareWebhookConversation(
                        wa_id=wa,
                        sender_name="",
                        last_text=text or cloud_preview_label(msg_type, text),
                        last_type=msg_type,
                        last_at=created,
                        unread_count=0,
                        closed=False,
                        channel_phone=channel,
                    )
                )
                messages.append(
                    CareWebhookMessage(
                        external_id=external_id,
                        wa_id=wa,
                        direction="outbound",
                        type=msg_type,
                        text=text,
                        status="sent",
                        media_id=str(part.get("id") or ""),
                        media_name=str(part.get("filename") or ""),
                        mime_type=str(part.get("mime_type") or ""),
                        sender_name="WhatsApp",
                        created_at=created,
                    )
                )
    return CareWebhookIn(conversations=conversations, messages=messages)


async def _resolve_meta_verify_token(client: httpx.AsyncClient) -> str:
    settings = get_settings()
    env_token = (settings.care_meta_verify_token or "").strip()
    if env_token:
        return env_token
    try:
        id_token, _project = await _firebase_id_token(client, settings)
        r = await client.post(
            f"https://asia-south1-{DEFAULT_PROJECT}.cloudfunctions.net/getWhatsAppCloudSettingsFn",
            headers={"Authorization": f"Bearer {id_token}", "Content-Type": "application/json"},
            json={"data": None},
            timeout=20.0,
        )
        if r.is_success:
            result = (r.json() or {}).get("result") or {}
            token = str(result.get("verifyToken") or "").strip()
            if token:
                return token
    except Exception:  # noqa: BLE001
        pass
    return ""


@router.get("/api/webhooks/meta-whatsapp")
async def meta_whatsapp_webhook_verify(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
):
    """Meta Cloud webhook verification challenge."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        expected = await _resolve_meta_verify_token(client)
    if not expected:
        raise HTTPException(503, "Care Meta verify token is not configured")
    if hub_mode == "subscribe" and hub_verify_token and hmac.compare_digest(hub_verify_token, expected):
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(403, "Forbidden")


@router.post("/api/webhooks/meta-whatsapp")
async def meta_whatsapp_webhook(
    request: StarletteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Meta Cloud webhook entry: dual-forward to YesOne, then Care ingest."""
    settings = get_settings()
    raw = await request.body()
    signature = request.headers.get("x-hub-signature-256") or request.headers.get("X-Hub-Signature-256") or ""
    yesone_url = (settings.yesone_cloud_webhook_url or YESONE_CLOUD_WEBHOOK).strip() or YESONE_CLOUD_WEBHOOK
    async with httpx.AsyncClient(timeout=60.0) as client:
        headers = {"Content-Type": "application/json"}
        if signature:
            headers["X-Hub-Signature-256"] = signature
        try:
            forward = await client.post(yesone_url, content=raw, headers=headers)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"YesOne Cloud webhook forward failed: {exc}") from exc
        # Require YesOne signature validation to succeed (app secret stays in YesOne).
        if forward.status_code >= 400:
            detail = (forward.text or "")[:240]
            raise HTTPException(forward.status_code, f"YesOne rejected webhook: {detail}")

    try:
        body = json.loads(raw.decode("utf-8") or "{}") if raw else {}
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    normalized = _normalize_meta_payload(body)
    if not normalized.conversations and not normalized.messages:
        return {"ok": True, "forwarded": True, "conversations": 0, "messages": 0}

    from app.whatsapp_outline import schedule_outline_refresh

    inst = await require_care_instance(db)
    conv_by_wa: dict[str, WhatsAppConversation] = {}
    outline_conv_ids: set[int] = set()
    for c in normalized.conversations:
        wa = _phone_digits(c.wa_id)
        if not wa:
            continue
        conv = await upsert_care_conversation(
            db,
            inst,
            wa_id=wa,
            sender_name=c.sender_name,
            last_text=c.last_text,
            last_type=c.last_type,
            last_at=_parse_firestore_time(c.last_at),
            unread_count=c.unread_count,
            closed=c.closed,
            channel_phone=c.channel_phone,
            profile_image=c.profile_image,
        )
        conv_by_wa[wa] = conv
    for m in normalized.messages:
        wa = _phone_digits(m.wa_id)
        if not wa:
            continue
        conv = conv_by_wa.get(wa)
        if not conv:
            conv = await upsert_care_conversation(
                db,
                inst,
                wa_id=wa,
                sender_name=m.sender_name,
                last_text=m.text,
                last_type=m.type,
                last_at=_parse_firestore_time(m.created_at),
            )
            conv_by_wa[wa] = conv
        media_url, media_name = await resolve_care_media_for_ingest(
            msg_type=m.type,
            media_url=m.media_url,
            media_id=m.media_id,
            media_name=m.media_name,
            mime_type=m.mime_type,
        )
        _msg, content_changed = await upsert_care_message(
            db,
            inst,
            conv,
            external_id=m.external_id or str(uuid.uuid4()),
            direction=m.direction,
            msg_type=m.type,
            text=m.text,
            status=m.status,
            media_url=media_url,
            media_name=media_name,
            mime_type=m.mime_type,
            sender_name=m.sender_name,
            created_at=_parse_firestore_time(m.created_at),
            transcript=m.transcript,
        )
        if content_changed:
            outline_conv_ids.add(conv.id)
        preview = cloud_preview_label(m.type, m.text)
        if preview:
            conv.last_message_preview = preview[:255]
        created = _parse_firestore_time(m.created_at)
        if created:
            conv.last_message_at = created
        if str(m.direction).lower() in {"in", "inbound", "incoming"}:
            conv.unread_count = int(conv.unread_count or 0) + 1
            if conv.status == "Resolved":
                conv.status = "Open"
    inst.connection_status = "Connected"
    await db.commit()
    if outline_conv_ids:
        schedule_outline_refresh(outline_conv_ids)
    return {
        "ok": True,
        "forwarded": True,
        "conversations": len(normalized.conversations),
        "messages": len(normalized.messages),
    }
