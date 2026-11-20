"""Fetch & cache WhatsApp group / channel (newsletter) subject + icon."""
from __future__ import annotations

from typing import Any

import httpx

from app.evolution_service import EvolutionService
from app.media_store import save_bytes, save_data_url
from app.models import WhatsAppConversation
from app.whatsapp_chat_types import is_group_jid, is_newsletter_jid, parse_group_rows


def chat_kind_from_jid(jid: str) -> str:
    lower = (jid or "").lower()
    if is_group_jid(lower):
        return "group"
    if is_newsletter_jid(lower):
        return "channel"
    if "@broadcast" in lower or lower.startswith("status@"):
        return "broadcast"
    return "dm"


def _digits(value: str) -> str:
    return "".join(c for c in (value or "") if c.isdigit())


def _name_needs_refresh(conv: WhatsAppConversation) -> bool:
    name = (conv.customer_name or "").strip()
    phone = (conv.customer_phone or "").strip()
    if not name:
        return True
    if phone and name == phone:
        return True
    jid_user = (conv.customer_jid or "").split("@", 1)[0].split(":", 1)[0]
    if jid_user and name == jid_user:
        return True
    # Raw @LID / long digit ids are not usable group titles
    digits = _digits(name.lstrip("@"))
    if name.startswith("@") and len(digits) >= 14 and digits == name.lstrip("@"):
        return True
    if name.isdigit() and len(name) >= 14:
        return True
    return False


def _photo_needs_refresh(conv: WhatsAppConversation) -> bool:
    return not (conv.customer_photo or "").strip()


def _pick_str(*vals: Any) -> str:
    for v in vals:
        if isinstance(v, dict):
            text = v.get("text") or v.get("Text") or v.get("name") or v.get("Name")
            if text:
                return str(text).strip()
            continue
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _media_url_from_picture(pic: Any) -> str:
    if not isinstance(pic, dict):
        return ""
    url = _pick_str(pic.get("url"), pic.get("URL"), pic.get("profilePictureUrl"), pic.get("picture"))
    if url.startswith("http://") or url.startswith("https://"):
        return url
    direct = _pick_str(pic.get("direct_path"), pic.get("directPath"), pic.get("DirectPath"))
    if direct:
        if not direct.startswith("/"):
            direct = "/" + direct
        return f"https://mmg.whatsapp.net{direct}"
    return ""


def _parse_newsletter_meta(payload: dict[str, Any]) -> tuple[str, str]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return "", ""
    meta = data.get("thread_metadata") if isinstance(data.get("thread_metadata"), dict) else {}
    if not meta:
        meta = data.get("ThreadMetadata") if isinstance(data.get("ThreadMetadata"), dict) else data
    name = _pick_str(
        meta.get("name") if isinstance(meta, dict) else None,
        data.get("name"),
        data.get("Name"),
        data.get("subject"),
        data.get("Subject"),
        data.get("newsletterName"),
    )
    pic_url = ""
    if isinstance(meta, dict):
        pic_url = _media_url_from_picture(meta.get("preview")) or _media_url_from_picture(meta.get("picture"))
    if not pic_url:
        pic_url = _media_url_from_picture(data.get("preview")) or _media_url_from_picture(data.get("picture"))
    return name[:160], pic_url


def _parse_group_meta(payload: dict[str, Any]) -> tuple[str, str]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return "", ""
    name = _pick_str(
        data.get("Name"),
        data.get("name"),
        data.get("Subject"),
        data.get("subject"),
        data.get("GroupName"),
        data.get("groupName"),
        data.get("Topic"),
        data.get("topic"),
    )
    pic_url = _media_url_from_picture(data.get("Picture")) or _media_url_from_picture(data.get("picture"))
    pic_url = pic_url or _pick_str(
        data.get("PictureUrl"),
        data.get("pictureUrl"),
        data.get("profilePictureUrl"),
        data.get("GroupPicture"),
    )
    return name[:160], pic_url


async def _cache_photo(conv: WhatsAppConversation, url: str) -> str:
    if not url:
        return ""
    try:
        if url.startswith("http://") or url.startswith("https://"):
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(url)
                r.raise_for_status()
                public_url, _ = save_bytes(
                    r.content,
                    media_type="image",
                    filename=f"{_digits(conv.customer_phone) or 'chat'}.jpg",
                )
                return public_url
        data_url = url if url.startswith("data:") else f"data:image/jpeg;base64,{url}"
        public_url, _, _ = save_data_url(
            data_url,
            media_type="image",
            filename=f"{_digits(conv.customer_phone) or 'chat'}.jpg",
        )
        return public_url
    except Exception:  # noqa: BLE001
        if url.startswith("http"):
            return url[:2000]
        return ""


def _group_user(jid: str) -> str:
    return (jid or "").strip().split("@", 1)[0].split(":", 1)[0]


def _looks_digit_stripped_classic_group(jid: str) -> bool:
    """True when user looks like a mangled classic ``{creator}-{ts}@g.us`` (hyphen stripped).

    Modern groups use ids like ``120363…@g.us`` (no hyphen) — those are valid as-is.
    """
    user = _group_user(jid)
    if not user or "-" in user or not user.isdigit() or len(user) < 18:
        return False
    # WhatsApp's newer group ids commonly start with 120363…
    if user.startswith("120363") or user.startswith("12036"):
        return False
    return True


async def _resolve_group_jid_from_list(
    svc: EvolutionService, token: str, jid: str
) -> tuple[str, str]:
    """Match digit-stripped / exact @g.us against Evolution group/list → (jid, name)."""
    want = _digits(_group_user(jid))
    if not want:
        return "", ""
    try:
        remote = await svc.list_groups(token)
    except Exception:  # noqa: BLE001
        return "", ""
    for row in parse_group_rows(remote):
        remote_jid = (row.get("jid") or "").strip()
        if _digits(_group_user(remote_jid)) != want:
            continue
        return remote_jid, (row.get("name") or "").strip()[:160]
    return "", ""


async def _avatar_url(svc: EvolutionService, token: str, jid: str) -> str:
    try:
        remote = await svc.get_avatar(token, jid, preview=True)
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(remote, dict):
        return ""
    data = remote.get("data") if isinstance(remote.get("data"), dict) else remote
    if isinstance(data, dict):
        return _pick_str(
            data.get("url"),
            data.get("profilePictureUrl"),
            data.get("picture"),
            data.get("avatar"),
            data.get("base64"),
        )
    if isinstance(data, str):
        return data
    return _pick_str(remote.get("avatar"))


async def enrich_conversation_meta(
    *,
    svc: EvolutionService,
    token: str,
    conv: WhatsAppConversation,
    force: bool = False,
) -> dict[str, Any]:
    """Fill customer_name / customer_photo for group (@g.us) and channel (@newsletter) chats.

    Does not commit.
    """
    jid = (conv.customer_jid or "").strip()
    kind = chat_kind_from_jid(jid)
    changed = False
    reason = ""

    if kind not in {"group", "channel"}:
        return {
            "ok": True,
            "chat_kind": kind,
            "customer_name": conv.customer_name or "",
            "customer_photo": conv.customer_photo or "",
            "changed": False,
            "reason": "not_group_or_channel",
        }

    want_name = force or _name_needs_refresh(conv)
    want_photo = force or _photo_needs_refresh(conv)
    if not want_name and not want_photo:
        return {
            "ok": True,
            "chat_kind": kind,
            "customer_name": conv.customer_name or "",
            "customer_photo": conv.customer_photo or "",
            "changed": False,
            "reason": "already_set",
        }

    name = ""
    pic_url = ""
    resolved_jid = ""
    try:
        if kind == "channel":
            remote = await svc.get_newsletter_info(token, jid)
            name, pic_url = _parse_newsletter_meta(remote if isinstance(remote, dict) else {})
        elif _looks_digit_stripped_classic_group(jid):
            # /group/info hangs on mangled creator-ts JIDs; recover hyphen + Name from list.
            resolved_jid, list_name = await _resolve_group_jid_from_list(svc, token, jid)
            if list_name:
                name = list_name
            info_jid = resolved_jid or jid
            if (want_photo or not name) and info_jid:
                try:
                    remote = await svc.get_group_info(token, info_jid)
                    info_name, pic_url = _parse_group_meta(
                        remote if isinstance(remote, dict) else {}
                    )
                    name = name or info_name
                except Exception as info_exc:  # noqa: BLE001
                    if not name:
                        reason = str(info_exc)[:300]
            if want_photo and not pic_url:
                pic_url = await _avatar_url(svc, token, info_jid)
            if not name and not pic_url and reason:
                return {
                    "ok": False,
                    "chat_kind": kind,
                    "customer_name": conv.customer_name or "",
                    "customer_photo": conv.customer_photo or "",
                    "changed": False,
                    "reason": reason,
                }
        else:
            remote = await svc.get_group_info(token, jid)
            name, pic_url = _parse_group_meta(remote if isinstance(remote, dict) else {})
            if want_photo and not pic_url:
                pic_url = await _avatar_url(svc, token, jid)
    except Exception as exc:  # noqa: BLE001
        reason = str(exc)[:300]
        return {
            "ok": False,
            "chat_kind": kind,
            "customer_name": conv.customer_name or "",
            "customer_photo": conv.customer_photo or "",
            "changed": False,
            "reason": reason,
        }

    if resolved_jid and is_group_jid(resolved_jid) and resolved_jid != jid:
        conv.customer_jid = resolved_jid
        changed = True

    if want_name and name:
        conv.customer_name = name[:160]
        changed = True

    if want_photo and pic_url:
        cached = await _cache_photo(conv, pic_url)
        if cached:
            conv.customer_photo = cached
            changed = True

    if not reason:
        if not name and not pic_url:
            reason = "no_meta"
        elif not changed:
            reason = "nothing_new"
        else:
            reason = "updated"

    return {
        "ok": True,
        "chat_kind": kind,
        "customer_name": conv.customer_name or "",
        "customer_photo": conv.customer_photo or "",
        "changed": changed,
        "reason": reason,
    }
