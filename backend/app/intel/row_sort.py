"""Sort Intel list rows: priority High → Medium → Low, then longest wait first."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models import WhatsAppConversation
from app.whatsapp_service_meta import parse_cached_outline_summary, priority_from_summary


def priority_rank(priority: str | None) -> int:
    raw = (priority or "").strip().lower()
    if raw in {"high", "urgent", "critical"}:
        return 0
    if raw in {"medium", "med", "mid"}:
        return 1
    if raw == "low":
        return 2
    return 3


def _age_seconds(conv: WhatsAppConversation, *, now: datetime | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    moment = conv.awaiting_since if conv.awaiting_staff_reply and conv.awaiting_since else conv.last_message_at
    if moment is None:
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0, int((current - moment).total_seconds()))


def conversation_priority(conv: WhatsAppConversation, summary: dict[str, Any] | None = None) -> str:
    data = summary if summary is not None else parse_cached_outline_summary(conv.ai_outline or "")
    raw = (conv.priority or "").strip() or priority_from_summary(data)
    if raw:
        if raw.lower() in {"med", "mid"}:
            return "Medium"
        return raw.title() if raw.islower() else raw
    if conv.awaiting_staff_reply:
        return "Medium"
    return "Low"


def conversation_sort_key(conv: WhatsAppConversation, *, now: datetime | None = None) -> tuple[int, int]:
    outline = parse_cached_outline_summary(conv.ai_outline or "")
    return priority_rank(conversation_priority(conv, outline)), -_age_seconds(conv, now=now)


def sort_public_ids(
    public_ids: list[str],
    by_pub: dict[str, WhatsAppConversation],
    *,
    now: datetime | None = None,
) -> list[str]:
    current = now or datetime.now(timezone.utc)

    def key(pid: str) -> tuple[int, int]:
        conv = by_pub.get(pid)
        if conv is None:
            return (3, 0)
        return conversation_sort_key(conv, now=current)

    return sorted(public_ids, key=key)


def sort_intel_dict_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(row: dict[str, Any]) -> tuple[int, int]:
        pr = priority_rank(str(row.get("priority") or ""))
        age = int(row.get("age_seconds") or 0)
        return pr, -age

    return sorted(items, key=key)