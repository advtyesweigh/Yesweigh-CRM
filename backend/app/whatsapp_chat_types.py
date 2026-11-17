"""WhatsApp JID helpers — DM vs group vs channel/newsletter."""
from __future__ import annotations

from typing import Any


def jid_lower(jid: str | None) -> str:
    return (jid or "").strip().lower()


def is_group_jid(jid: str | None) -> bool:
    return "@g.us" in jid_lower(jid)


def is_newsletter_jid(jid: str | None) -> bool:
    return "@newsletter" in jid_lower(jid)


def is_broadcast_or_status_jid(jid: str | None) -> bool:
    low = jid_lower(jid)
    return "@broadcast" in low or low.startswith("status@") or "status@broadcast" in low


def is_inbox_excluded_jid(jid: str | None) -> bool:
    """Status / broadcast stay out. Groups + channels stay in (name/icon via meta APIs)."""
    return is_broadcast_or_status_jid(jid)


def newsletter_list_filter():
    """Hide WhatsApp Channels (@newsletter). Named @g.us groups stay in the inbox."""
    from app.models import WhatsAppConversation

    return ~WhatsAppConversation.customer_jid.ilike("%@newsletter")


def parse_group_name(payload: Any) -> str:
    """Best-effort group subject from Evolution /group/info or /group/list row."""
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return ""
    for key in ("Name", "name", "Subject", "subject", "Topic", "topic"):
        val = str(data.get(key) or "").strip()
        if val:
            return val[:160]
    return ""


def parse_group_rows(payload: Any) -> list[dict[str, Any]]:
    """Normalize Evolution GET /group/list (or myall) into [{jid, name}, ...]."""
    if not isinstance(payload, dict):
        raw = payload
    else:
        raw = payload.get("data", payload)
    rows: list[Any]
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        inner = raw.get("groups") or raw.get("Groups") or raw.get("data")
        if isinstance(inner, list):
            rows = inner
        elif isinstance(inner, dict):
            rows = list(inner.values())
        else:
            rows = []
    else:
        rows = []
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        jid = str(
            item.get("JID")
            or item.get("jid")
            or item.get("groupJid")
            or item.get("GroupJid")
            or item.get("id")
            or ""
        ).strip()
        if not jid or not is_group_jid(jid):
            continue
        name = parse_group_name(item)
        out.append({"jid": jid, "name": name, "raw": item})
    return out
