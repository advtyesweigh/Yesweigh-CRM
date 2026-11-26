"""Normalized search identity + URL helpers for Dealer Finder history."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from app.dealer_finder.constants import (
    COUNTRY_DEFAULT,
    LANGUAGE_DEFAULT,
    ORG_DEFAULT,
    PLAN_VERSION,
)
from app.google_places_search import resolve_state
from app.phone_util import normalize_in_mobile
from app.state_normalize import canon_state

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def norm_text(value: str | None) -> str:
    s = _WS.sub(" ", (value or "").strip().lower())
    return s


def compact_key(value: str | None) -> str:
    return _NON_ALNUM.sub("", (value or "").lower())


def canon_location_state(raw: str | None) -> str:
    label, _ = resolve_state(raw or "")
    return canon_state(label or raw or "") or (label or raw or "").strip()


def search_identity(
    *,
    organization_id: str = ORG_DEFAULT,
    country: str = COUNTRY_DEFAULT,
    state: str,
    district: str = "",
    city: str = "",
    category: str,
    language: str = LANGUAGE_DEFAULT,
    plan_version: str = PLAN_VERSION,
    max_results: int = 60,
    page_size: int = 20,
) -> dict[str, str | int]:
    """Stable identity fields used for history uniqueness + cooldown."""
    return {
        "organization_id": (organization_id or ORG_DEFAULT).strip().lower(),
        "country": (country or COUNTRY_DEFAULT).strip().upper(),
        "state": canon_location_state(state),
        "district": norm_text(district),
        "city": norm_text(city),
        "category": norm_text(category),
        "language": (language or LANGUAGE_DEFAULT).strip().lower(),
        "plan_version": (plan_version or PLAN_VERSION).strip(),
        "max_results": int(max_results),
        "page_size": int(page_size),
    }


def identity_hash(identity: dict[str, str | int]) -> str:
    parts = [
        str(identity.get("organization_id") or ""),
        str(identity.get("country") or ""),
        str(identity.get("state") or ""),
        str(identity.get("district") or ""),
        str(identity.get("city") or ""),
        str(identity.get("category") or ""),
        str(identity.get("language") or ""),
        str(identity.get("plan_version") or ""),
        str(identity.get("max_results") or ""),
        str(identity.get("page_size") or ""),
    ]
    blob = "|".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def validate_website_url(raw: str | None) -> str:
    """Return a safe http(s) URL or empty. Never invent."""
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith("//"):
        s = "https:" + s
    if not re.match(r"^https?://", s, re.I):
        if "." in s and " " not in s:
            s = "https://" + s
        else:
            return ""
    try:
        parsed = urlparse(s)
    except Exception:  # noqa: BLE001
        return ""
    if parsed.scheme.lower() not in {"http", "https"}:
        return ""
    if not parsed.netloc or "." not in parsed.netloc:
        return ""
    # Cap length for CRM storage
    return s[:500]


def maps_url_for_place(place_id: str) -> str:
    pid = (place_id or "").strip()
    if not pid:
        return ""
    if pid.startswith("places/"):
        pid = pid[len("places/") :]
    return f"https://www.google.com/maps/place/?q=place_id:{pid}"


def whatsapp_display(phone: str | None) -> str:
    """10-digit WhatsApp mobile when available."""
    return normalize_in_mobile(phone or "")
