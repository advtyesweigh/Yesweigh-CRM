"""Google Places Text Search for external dealer / business discovery.

Used by AI tools ``search_google_businesses`` and ``find_new_dealers``.
Env: ``GOOGLE_MAPS_API_KEY`` or ``GOOGLE_PLACES_API_KEY`` (never commit).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings
from app.enrich_leads_places import (
    FIELD_MASK,
    PLACES_SEARCH_URL,
    extract_city,
    format_phone,
    name_match_score,
)
from app.phone_util import normalize_in_mobile

# Compact aliases → (canonical label, match spellings for CRM filters)
STATE_ALIASES: dict[str, tuple[str, list[str]]] = {
    "up": ("Uttar Pradesh", ["Uttar Pradesh", "UP", "UttarPradesh", "Uthar Pradesh"]),
    "uttarpradesh": ("Uttar Pradesh", ["Uttar Pradesh", "UP", "UttarPradesh", "Uthar Pradesh"]),
    "utharpradesh": ("Uttar Pradesh", ["Uttar Pradesh", "UP", "UttarPradesh", "Uthar Pradesh"]),
    "uttarpadesh": ("Uttar Pradesh", ["Uttar Pradesh", "UP", "UttarPradesh"]),
    "kerala": ("Keralam", ["Keralam", "Kerala"]),
    "keralam": ("Keralam", ["Keralam", "Kerala"]),
    "tamilnadu": ("Tamil Nadu", ["Tamil Nadu", "Tamilnadu"]),
    "karnataka": ("Karnataka", ["Karnataka"]),
    "andhrapradesh": ("Andhra Pradesh", ["Andhra Pradesh"]),
    "telangana": ("Telangana", ["Telangana"]),
    "odisha": ("Odisha", ["Odisha", "Orissa"]),
    "orissa": ("Odisha", ["Odisha", "Orissa"]),
    "maharashtra": ("Maharashtra", ["Maharashtra"]),
    "goa": ("Goa", ["Goa"]),
    "gujarat": ("Gujarat", ["Gujarat"]),
    "rajasthan": ("Rajasthan", ["Rajasthan"]),
    "delhi": ("Delhi", ["Delhi", "NCT of Delhi"]),
    "nctofdelhi": ("Delhi", ["Delhi", "NCT of Delhi"]),
    "westbengal": ("West Bengal", ["West Bengal"]),
    "madhyapradesh": ("Madhya Pradesh", ["Madhya Pradesh", "MP"]),
    "mp": ("Madhya Pradesh", ["Madhya Pradesh", "MP"]),
    "bihar": ("Bihar", ["Bihar"]),
    "punjab": ("Punjab", ["Punjab"]),
    "haryana": ("Haryana", ["Haryana"]),
    "assam": ("Assam", ["Assam"]),
    "jharkhand": ("Jharkhand", ["Jharkhand"]),
    "chhattisgarh": ("Chhattisgarh", ["Chhattisgarh", "Chattisgarh"]),
    "uttarakhand": ("Uttarakhand", ["Uttarakhand", "Uttaranchal"]),
    "uttaranchal": ("Uttarakhand", ["Uttarakhand", "Uttaranchal"]),
    "himachalpradesh": ("Himachal Pradesh", ["Himachal Pradesh", "HP"]),
    "jammuandkashmir": ("Jammu and Kashmir", ["Jammu and Kashmir", "J&K"]),
    "pondicherry": ("Puducherry", ["Puducherry", "Pondicherry"]),
    "pondichery": ("Puducherry", ["Puducherry", "Pondicherry", "Pondichery"]),
    "puducherry": ("Puducherry", ["Puducherry", "Pondicherry"]),
}

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
# Places API (New) Text Search: ≤20 per page; ≤60 total across pages (Google hard cap).
_DEFAULT_MAX_PAGES = 3
_DEFAULT_PAGE_SIZE = 20
_GOOGLE_HARD_MAX = 60
_DEFAULT_MAX_RESULTS = 60
_NAME_CITY_MIN_SCORE = 0.55


def places_api_key() -> str:
    settings = get_settings()
    return (settings.google_maps_api_key or settings.google_places_api_key or "").strip()


def places_configured() -> bool:
    return bool(places_api_key())


def resolve_state(raw: str) -> tuple[str, list[str]]:
    """Return (canonical_label, CRM match spellings)."""
    text = (raw or "").strip()
    if not text:
        return "", []
    compact = "".join(ch for ch in text.lower() if ch.isalnum())
    hit = STATE_ALIASES.get(compact)
    if hit:
        return hit[0], list(hit[1])
    # Title-case fallback for unknown states
    label = _WS.sub(" ", text).strip().title()
    return label, [label, text]


_GENERIC_DEALER_KW = re.compile(
    r"^(?:dealers?|distributors?|suppliers?)(?:\s+only)?$", re.I
)
_BARE_DEALER_WORD = re.compile(r"\b(?:dealers?|distributors?|suppliers?)\b", re.I)
_WEIGHING_MARKERS = re.compile(
    r"weigh|scale|weighbridge|billing\s*scale|label\s*print",
    re.I,
)
_DEFAULT_WEIGHING_KW = "weighing scale dealers"


def default_weighing_places_keyword(keyword: str) -> str:
    """Coerce dealer/distributor/supplier phrasing → weighing-scale scope (YesWeigh hard rule)."""
    kw = (keyword or "").strip()
    if not kw or _GENERIC_DEALER_KW.match(kw):
        return _DEFAULT_WEIGHING_KW
    low = kw.lower()
    if _WEIGHING_MARKERS.search(low):
        return kw
    # Any dealer/distributor/supplier wording without weighing markers → weighing-scale only.
    if _BARE_DEALER_WORD.search(low):
        return _DEFAULT_WEIGHING_KW
    if _GENERIC_DEALER_KW.search(low):
        return _DEFAULT_WEIGHING_KW
    return kw


def places_keyword_was_coerced(keyword: str, resolved: str) -> bool:
    """True when default_weighing_places_keyword changed the caller's keyword."""
    raw = (keyword or "").strip().lower()
    if not raw:
        return True
    return raw != (resolved or "").strip().lower()


def weighing_places_scope_note(raw_keyword: str, resolved_keyword: str) -> str | None:
    """Human note when keyword was coerced to weighing-scale scope."""
    if not places_keyword_was_coerced(raw_keyword, resolved_keyword):
        return None
    raw = (raw_keyword or "").strip() or "(empty)"
    return (
        f"YesWeigh rule: coerced keyword {raw!r} → {resolved_keyword!r} "
        "(dealer = weighing scale dealer only)."
    )


def build_text_query(keyword: str, state: str, city: str | None = None) -> str:
    kw = default_weighing_places_keyword(keyword)
    st_label, _ = resolve_state(state)
    st = st_label or (state or "").strip()
    city_label = _WS.sub(" ", (city or "").strip())
    if not st:
        if city_label:
            return f"{kw} in {city_label} India"
        return f"{kw} India"
    # Avoid doubling place names when keyword already includes them.
    low = kw.lower()
    place_bits = [p for p in (city_label, st) if p]
    if any(p.lower() in low for p in place_bits) or "india" in low:
        return kw
    if city_label:
        return f"{kw} in {city_label}, {st} India"
    return f"{kw} in {st} India"


def _norm_city(value: str) -> str:
    s = _NON_ALNUM.sub(" ", (value or "").lower())
    return _WS.sub(" ", s).strip()


def _city_overlap(a: str, b: str) -> bool:
    ca, cb = _norm_city(a), _norm_city(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    if ca in cb or cb in ca:
        return True
    ta = {t for t in ca.split() if len(t) >= 3}
    tb = {t for t in cb.split() if len(t) >= 3}
    return bool(ta & tb)


def _extract_state_from_components(components: list[dict[str, Any]], fallback: str) -> str:
    for c in components or []:
        types = c.get("types") or []
        if "administrative_area_level_1" in types:
            text = str(c.get("longText") or c.get("shortText") or "").strip()
            if text:
                return text[:80]
    return (fallback or "").strip()


def place_to_row(place: dict[str, Any], *, state_fallback: str = "") -> dict[str, Any]:
    display = ((place.get("displayName") or {}).get("text")) or ""
    addr = (place.get("formattedAddress") or "").strip()
    comps = place.get("addressComponents") or []
    city = extract_city(comps, state_fallback)
    state = _extract_state_from_components(comps, state_fallback)
    phone = format_phone(
        place.get("nationalPhoneNumber") or "",
        place.get("internationalPhoneNumber") or "",
    )
    return {
        "name": display.strip(),
        "company": display.strip(),
        "address": addr,
        "city": city,
        "state": state or state_fallback,
        "phone": phone,
        "place_id": place.get("id") or "",
        "source": "google_places",
    }


@dataclass
class CrmContact:
    name: str
    city: str
    phone10: str
    kind: str  # dealer | lead
    record_id: int


def crm_match_reason(
    google_row: dict[str, Any],
    crm: list[CrmContact],
    *,
    min_name_score: float = _NAME_CITY_MIN_SCORE,
) -> tuple[bool, str]:
    """Return (is_existing, reason). Match by phone OR fuzzy name+city."""
    g_phone = normalize_in_mobile(str(google_row.get("phone") or ""))
    g_name = str(google_row.get("name") or google_row.get("company") or "")
    g_city = str(google_row.get("city") or "")

    if g_phone:
        for c in crm:
            if c.phone10 and c.phone10 == g_phone:
                return True, f"phone:{c.kind}:{c.record_id}"

    for c in crm:
        score = name_match_score(g_name, c.name)
        if score < min_name_score:
            continue
        # Prefer city overlap; allow strong name-only match (≥0.85) when CRM city blank
        if _city_overlap(g_city, c.city):
            return True, f"name_city:{c.kind}:{c.record_id}:score={score:.2f}"
        if score >= 0.85 and (not c.city or not g_city):
            return True, f"name:{c.kind}:{c.record_id}:score={score:.2f}"
    return False, ""


async def search_places_text(
    *,
    text_query: str,
    max_results: int = _DEFAULT_MAX_RESULTS,
    page_size: int = _DEFAULT_PAGE_SIZE,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Call Places API (New) Text Search with pagination.

    Google limits: ``pageSize`` 1–20 per request; ≤60 results total across pages.
    We default to 60 and paginate up to 3 pages. Returns
    ``{ok, places, query, pages, error?}``. Never includes the API key.
    """
    key = (api_key or places_api_key()).strip()
    if not key:
        return {
            "ok": False,
            "error": (
                "Google Places API key not configured. "
                "Set GOOGLE_MAPS_API_KEY or GOOGLE_PLACES_API_KEY on the server."
            ),
            "places": [],
            "query": text_query,
            "pages": 0,
        }

    max_results = max(1, min(_GOOGLE_HARD_MAX, int(max_results or _DEFAULT_MAX_RESULTS)))
    page_size = max(1, min(_DEFAULT_PAGE_SIZE, int(page_size or _DEFAULT_PAGE_SIZE)))
    pages_needed = min(_DEFAULT_MAX_PAGES, (max_results + page_size - 1) // page_size)

    collected: list[dict[str, Any]] = []
    page_token = ""
    pages = 0
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            for _ in range(pages_needed):
                body: dict[str, Any] = {
                    "textQuery": text_query,
                    "regionCode": "IN",
                    # pageSize replaces deprecated maxResultCount (Places API New).
                    "pageSize": page_size,
                    "languageCode": "en",
                }
                if page_token:
                    body["pageToken"] = page_token
                resp = await client.post(
                    PLACES_SEARCH_URL,
                    headers={
                        "Content-Type": "application/json",
                        "X-Goog-Api-Key": key,
                        "X-Goog-FieldMask": FIELD_MASK + ",nextPageToken",
                    },
                    json=body,
                )
                if resp.status_code == 403:
                    return {
                        "ok": False,
                        "error": (
                            "Google Places API denied (403). Enable Places API (New) "
                            "Text Search for this key, or check billing/quota."
                        ),
                        "places": collected,
                        "query": text_query,
                        "pages": pages,
                        "http_status": 403,
                    }
                if resp.status_code == 429:
                    return {
                        "ok": False,
                        "error": "Google Places quota exceeded (429). Try again later.",
                        "places": collected,
                        "query": text_query,
                        "pages": pages,
                        "http_status": 429,
                    }
                if resp.status_code >= 400:
                    # Avoid leaking response bodies that might echo key fragments
                    return {
                        "ok": False,
                        "error": f"Google Places HTTP {resp.status_code}",
                        "places": collected,
                        "query": text_query,
                        "pages": pages,
                        "http_status": resp.status_code,
                    }
                data = resp.json()
                pages += 1
                batch = list(data.get("places") or [])
                collected.extend(batch)
                page_token = str(data.get("nextPageToken") or "").strip()
                if not page_token or len(collected) >= max_results:
                    break
    except httpx.TimeoutException:
        return {
            "ok": False,
            "error": "Google Places request timed out",
            "places": collected,
            "query": text_query,
            "pages": pages,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"Google Places error: {type(exc).__name__}",
            "places": collected,
            "query": text_query,
            "pages": pages,
        }

    return {
        "ok": True,
        "places": collected[:max_results],
        "query": text_query,
        "pages": pages,
        "total_raw": len(collected[:max_results]),
    }
