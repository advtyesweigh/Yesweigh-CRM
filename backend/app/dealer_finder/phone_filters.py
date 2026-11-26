"""Reusable Indian mobile-only filters for Dealer Finder phone-NEW lists."""
from __future__ import annotations

from typing import Any

from app.models import DealerSearchPlace
from app.phone_util import PhoneKind, classify_indian_phone, is_indian_mobile


def phone_removal_reason(kind: PhoneKind) -> str:
    if kind == "landline":
        return "landline"
    if kind == "empty":
        return "no_phone"
    if kind == "invalid":
        return "invalid_phone"
    return ""


def filter_places_mobile_only(
    rows: list[Any],
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Keep only rows with Indian mobile numbers; return (kept, removed_meta)."""
    kept: list[Any] = []
    removed: list[dict[str, Any]] = []
    for row in rows:
        phone = ""
        if isinstance(row, DealerSearchPlace):
            phone = (row.phone or "").strip()
        else:
            phone = str(row.get("whatsapp") or row.get("phone") or "").strip()
        kind = classify_indian_phone(phone)
        if is_indian_mobile(phone):
            kept.append(row)
            continue
        reason = phone_removal_reason(kind) or "not_mobile"
        name = ""
        dist = ""
        city = ""
        if isinstance(row, DealerSearchPlace):
            name = (row.name or "").strip()
            dist = (row.district or "").strip()
            city = (row.city or "").strip()
        else:
            name = str(row.get("name") or "").strip()
            dist = str(row.get("district") or "").strip()
            city = str(row.get("city") or "").strip()
        removed.append(
            {
                "name": name,
                "district": dist,
                "city": city,
                "phone": phone,
                "phone_kind": kind,
                "reason": reason,
            }
        )
    return kept, removed


def count_phone_kinds_for_places(
    places: list[DealerSearchPlace],
) -> dict[str, int]:
    """Count phone classification buckets on a place list (for ops / before-after)."""
    out = {"empty": 0, "mobile": 0, "landline": 0, "invalid": 0}
    for p in places:
        kind = classify_indian_phone(p.phone or "")
        out[kind] = out.get(kind, 0) + 1
    return out