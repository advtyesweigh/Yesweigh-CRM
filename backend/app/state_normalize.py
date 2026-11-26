"""Canonical Indian state labels for CRM dealers / leads / sales aggregations.

Merges English variants (Kerala/Keralam, Orissa/Odisha) and local-script labels
(बिहार, தமிழ் நாடு, …) into one canonical bucket per state for filters and map counts.
"""
from __future__ import annotations

import re
import unicodedata

# Canonical CRM state → all stored spellings that should match in SQL (incl. Indic).
_STATE_VARIANTS: dict[str, list[str]] = {
    "Andaman and Nicobar": [
        "Andaman and Nicobar",
        "Andaman and Nicobar Islands",
        "Andaman & Nicobar Islands",
    ],
    "Andhra Pradesh": ["Andhra Pradesh", "आंध्र प्रदेश"],
    "Arunachal Pradesh": ["Arunachal Pradesh", "अरुणाचल प्रदेश"],
    "Assam": ["Assam", "असम"],
    "Bihar": ["Bihar", "बिहार"],
    "Chandigarh": ["Chandigarh", "चंडीगढ़"],
    "Chhattisgarh": ["Chhattisgarh", "Chhatisgarh", "छत्तीसगढ़"],
    "Dadra and Nagar Haveli and Daman and Diu": [
        "Dadra and Nagar Haveli and Daman and Diu",
        "Dadra & Nagar Haveli",
        "Dadra and Nagar Haveli",
        "Daman and Diu",
    ],
    "Delhi": ["Delhi", "दिल्ली", "NCT of Delhi", "National Capital Territory of Delhi"],
    "Goa": ["Goa", "गोआ"],
    "Gujarat": ["Gujarat", "गुजरात"],
    "Haryana": ["Haryana", "हरियाणा"],
    "Himachal Pradesh": ["Himachal Pradesh", "हिमाचल प्रदेश"],
    "Jammu and Kashmir": [
        "Jammu and Kashmir",
        "Jammu & Kashmir",
        "J&K",
        "JK",
        "Kashmir",
        "जम्मू और कश्मीर",
        "जम्मू एवं कश्मीर",
    ],
    "Jharkhand": ["Jharkhand", "झारखंड"],
    "Karnataka": ["Karnataka", "कर्नाटक"],
    "Keralam": ["Keralam", "Kerala", "കേരളം"],
    "Ladakh": ["Ladakh", "लद्दाख", "Ladakh UT", "UT of Ladakh"],
    "Lakshadweep": ["Lakshadweep", "लक्षद्वीप"],
    "Madhya Pradesh": ["Madhya Pradesh", "मध्य प्रदेश"],
    "Maharashtra": ["Maharashtra", "महाराष्ट्र"],
    "Manipur": ["Manipur", "मणिपुर"],
    "Meghalaya": ["Meghalaya", "मेघालय"],
    "Mizoram": ["Mizoram", "मिज़ोरम", "मिजोरम"],
    "Nagaland": ["Nagaland", "नागालैंड"],
    "Odisha": ["Odisha", "Orissa", "ओडिशा", "उड़ीसा"],
    "Puducherry": ["Puducherry", "Pondicherry", "पुडुचेरी"],
    "Punjab": ["Punjab", "पंजाब"],
    "Rajasthan": ["Rajasthan", "राजस्थान"],
    "Sikkim": ["Sikkim", "सिक्किम"],
    "Tamil Nadu": ["Tamil Nadu", "தமிழ் நாடு", "தமிழ்நாடு"],
    "Telangana": ["Telangana", "तेलंगाना", "తెలంగాణ"],
    "Tripura": ["Tripura", "त्रिपुरा"],
    "Uttar Pradesh": ["Uttar Pradesh", "उत्तर प्रदेश"],
    "Uttarakhand": ["Uttarakhand", "Uttaranchal", "उत्तराखंड"],
    "West Bengal": ["West Bengal", "पश्चिम बंगाल", "পশ্চিমবঙ্গ"],
}

# Trimmed alias (exact) → canonical.
_ALIAS_TO_CANON: dict[str, str] = {}
for _canon, _variants in _STATE_VARIANTS.items():
    for _v in _variants:
        key = _v.strip()
        if key:
            _ALIAS_TO_CANON[key] = _canon

# Lowercased spaced English keys → canonical (legacy imports).
_STATE_CANON: dict[str, str] = {
    "kerala": "Keralam",
    "keralam": "Keralam",
    "orissa": "Odisha",
    "odisha": "Odisha",
    "uttaranchal": "Uttarakhand",
    "uttarakhand": "Uttarakhand",
    "pondicherry": "Puducherry",
    "puducherry": "Puducherry",
    "nct of delhi": "Delhi",
    "delhi": "Delhi",
    "jammu & kashmir": "Jammu and Kashmir",
    "jammu and kashmir": "Jammu and Kashmir",
    "andaman & nicobar islands": "Andaman and Nicobar",
    "andaman and nicobar islands": "Andaman and Nicobar",
}

# Compact alphanumeric English key → (canonical, match list) — Kerala kept for compat.
_STATE_MATCH: dict[str, tuple[str, list[str]]] = {
    "kerala": ("Keralam", _STATE_VARIANTS["Keralam"]),
    "keralam": ("Keralam", _STATE_VARIANTS["Keralam"]),
}

for _canon, _variants in _STATE_VARIANTS.items():
    compact = re.sub(r"[^a-z0-9]", "", _canon.lower())
    if compact and compact not in _STATE_MATCH:
        _STATE_MATCH[compact] = (_canon, list(_variants))

# All non-canonical spellings → canonical (for migrate write-back).
STATE_WRITE_BACK: list[tuple[str, str]] = []
for _canon, _variants in _STATE_VARIANTS.items():
    for _v in _variants:
        v = _v.strip()
        if v and v != _canon:
            STATE_WRITE_BACK.append((v, _canon))


def _spaced_key(raw: str) -> str:
    s = (raw or "").strip()
    return " ".join(s.lower().replace("&", " and ").split())


def _compact_key(raw: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (raw or "").lower())


def _nfc(raw: str) -> str:
    return unicodedata.normalize("NFC", (raw or "").strip())


def canon_state(raw: str | None) -> str:
    """Normalize a stored/display state label (empty → empty)."""
    s = _nfc(raw or "")
    if not s:
        return ""
    hit = _ALIAS_TO_CANON.get(s)
    if hit:
        return hit
    key = _spaced_key(s)
    if key in _STATE_CANON:
        return _STATE_CANON[key]
    compact = _compact_key(s)
    hit = _STATE_MATCH.get(compact)
    if hit:
        return hit[0]
    return s


def _variants_for_canon(canon: str) -> list[str]:
    return list(_STATE_VARIANTS.get(canon, [canon]))


def split_state_filter_tokens(raw: str | None) -> list[str]:
    """Split multi-state filter values on ``|`` or ``,`` (e.g. Tamil Nadu|Puducherry)."""
    s = _nfc(raw or "").strip()
    if not s:
        return []
    return [p.strip() for p in re.split(r"\s*[|,]\s*", s) if p.strip()]


def state_match_values(raw: str | None) -> list[str]:
    """Values to OR-match in SQL for a user/filter state string."""
    s = _nfc(raw or "")
    if not s:
        return []
    direct = _ALIAS_TO_CANON.get(s)
    if direct:
        return _variants_for_canon(direct)
    compact = _compact_key(s)
    hit = _STATE_MATCH.get(compact)
    if hit:
        return list(hit[1])
    canon = canon_state(s)
    if canon != s and canon in _STATE_VARIANTS:
        return _variants_for_canon(canon)
    out: list[str] = []
    for v in (s, canon):
        if v and v not in out:
            out.append(v)
    return out


def state_match_values_multi(raw: str | None) -> list[str]:
    """OR-match values for one or more pipe/comma-separated state tokens."""
    tokens = split_state_filter_tokens(raw)
    if len(tokens) <= 1 and "|" not in (raw or "") and "," not in (raw or ""):
        return state_match_values(raw)
    out: list[str] = []
    for token in tokens:
        for v in state_match_values(token):
            if v not in out:
                out.append(v)
    return out


def resolve_state_match(raw: str | None) -> tuple[str, list[str]]:
    """Return (canonical_label, match_values) for filters / AI tools."""
    s = _nfc(raw or "")
    if not s:
        return "", []
    direct = _ALIAS_TO_CANON.get(s)
    if direct:
        return direct, _variants_for_canon(direct)
    compact = _compact_key(s)
    hit = _STATE_MATCH.get(compact)
    if hit:
        return hit[0], list(hit[1])
    canon = canon_state(s)
    vals = state_match_values(s)
    return canon or s, vals or ([s] if s else [])


# Back-compat alias for leads map aggregation.
lead_state_bucket = canon_state