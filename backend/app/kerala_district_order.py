"""Kerala district display order: north → south (Kasaragod … Thiruvananthapuram).

Used as the default Leads list sort. Unknown / empty districts sort last.
Normalize common LMOMS / colloquial spellings before ranking.
"""
from __future__ import annotations

import re
from typing import Any

# Canonical north→south order (1 = top of list).
KERALA_DISTRICT_ORDER: list[str] = [
    "Kasaragod",
    "Kannur",
    "Wayanad",
    "Kozhikode",
    "Malappuram",
    "Palakkad",
    "Thrissur",
    "Ernakulam",
    "Idukki",
    "Kottayam",
    "Alappuzha",
    "Pathanamthitta",
    "Kollam",
    "Thiruvananthapuram",
]

# Compact lowercase keys → canonical name (covers DB / UI spelling variants).
_DISTRICT_CANONICAL: dict[str, str] = {
    "kasaragod": "Kasaragod",
    "kasargod": "Kasaragod",
    "kasargode": "Kasaragod",
    "kannur": "Kannur",
    "cannanore": "Kannur",
    "wayanad": "Wayanad",
    "wynad": "Wayanad",
    "kozhikode": "Kozhikode",
    "kozhikkode": "Kozhikode",
    "calicut": "Kozhikode",
    "malappuram": "Malappuram",
    "malapuram": "Malappuram",
    "mallapuram": "Malappuram",
    "palakkad": "Palakkad",
    "palghat": "Palakkad",
    "thrissur": "Thrissur",
    "trichur": "Thrissur",
    "ernakulam": "Ernakulam",
    "cochin": "Ernakulam",
    "kochi": "Ernakulam",
    "idukki": "Idukki",
    "kottayam": "Kottayam",
    "alappuzha": "Alappuzha",
    "alleppey": "Alappuzha",
    "pathanamthitta": "Pathanamthitta",
    "pattanamtitta": "Pathanamthitta",
    "kollam": "Kollam",
    "quilon": "Kollam",
    "thiruvananthapuram": "Thiruvananthapuram",
    "thiruvanathapuram": "Thiruvananthapuram",
    "trivandrum": "Thiruvananthapuram",
}

UNKNOWN_RANK = 9_000

_RANK_BY_CANONICAL: dict[str, int] = {
    name: i + 1 for i, name in enumerate(KERALA_DISTRICT_ORDER)
}


def _compact_key(raw: str | None) -> str:
    return "".join(ch for ch in (raw or "").lower() if ch.isalnum())


def normalize_kerala_district(district: str | None) -> str:
    """Map spelling variants to canonical Kerala district name, else stripped original."""
    raw = (district or "").strip()
    if not raw:
        return ""
    return _DISTRICT_CANONICAL.get(_compact_key(raw), raw)


# Town / locality spellings seen on dealers.city → canonical Kerala district.
# Keeps district sales aggregates inclusive of town rows (e.g. Manjeri → Malappuram).
_TOWN_TO_DISTRICT: dict[str, str] = {
    "manjeri": "Malappuram",
    "perinthalmanna": "Malappuram",
    "tirur": "Malappuram",
    "kondotty": "Malappuram",
    "kuttippuram": "Malappuram",
    "edapal": "Malappuram",
    "edappal": "Malappuram",
    "ponnani": "Malappuram",
    "nilambur": "Malappuram",
    "calicut": "Kozhikode",
    "feroke": "Kozhikode",
    "koyilandy": "Kozhikode",
    "perambra": "Kozhikode",
    "cochin": "Ernakulam",
    "kochi": "Ernakulam",
    "kochin": "Ernakulam",
    "aluva": "Ernakulam",
    "muvattupuzha": "Ernakulam",
    "vypin": "Ernakulam",
    "chalakudy": "Thrissur",
    "kodungallur": "Thrissur",
    "kodugallur": "Thrissur",
    "chavakkad": "Thrissur",
    "cherpulassery": "Palakkad",
    "kalpetta": "Wayanad",
    "kalpatta": "Wayanad",
    "waynad": "Wayanad",
    "thodupuzha": "Idukki",
    "adimaly": "Idukki",
    "nedumkandam": "Idukki",
    "cherthala": "Alappuzha",
    "haripad": "Alappuzha",
    "kayamkulam": "Alappuzha",
    "mavelikkara": "Alappuzha",
    "changanacherry": "Kottayam",
    "changanassery": "Kottayam",
    "vaikkom": "Kottayam",
    "thiruvalla": "Pathanamthitta",
    "kottarakkara": "Kollam",
    "karungappally": "Kollam",
    "attingal": "Thiruvananthapuram",
    "trivandrum": "Thiruvananthapuram",
}


def resolve_kerala_district_query(raw: str | None) -> str:
    """Resolve user text like 'malappuram dt' / 'Manjeri' to a canonical Kerala district.

    Strips common suffixes (dt / dist / district / area). Returns '' if unknown.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    # Drop trailing region suffixes: "Malappuram DT", "malappuram dist."
    cleaned = re.sub(
        r"(?i)[,\s]*(?:dt|dist\.?|district|area)\s*$",
        "",
        text,
    ).strip(" ,.-")
    cleaned = re.sub(r"(?i)\b(?:dt|dist\.?|district|area)\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    if not cleaned:
        return ""
    key = _compact_key(cleaned)
    if key in _DISTRICT_CANONICAL:
        return _DISTRICT_CANONICAL[key]
    if key in _TOWN_TO_DISTRICT:
        return _TOWN_TO_DISTRICT[key]
    # Compound city strings: "perinthalmanna, Malappuram Dt." / "malappuram dist"
    for token in re.split(r"[\s,/|-]+", cleaned):
        tkey = _compact_key(token)
        if tkey in _DISTRICT_CANONICAL:
            return _DISTRICT_CANONICAL[tkey]
        if tkey in _TOWN_TO_DISTRICT:
            return _TOWN_TO_DISTRICT[tkey]
    canon = normalize_kerala_district(cleaned)
    if canon in _RANK_BY_CANONICAL:
        return canon
    return ""


def kerala_district_city_match_terms(canonical: str) -> list[str]:
    """Spellings / substrings to match dealers.city for a canonical Kerala district."""
    canon = (canonical or "").strip()
    if not canon:
        return []
    terms: list[str] = [canon]
    for key, name in _DISTRICT_CANONICAL.items():
        if name == canon:
            terms.append(name)
            terms.append(key)
    for town, name in _TOWN_TO_DISTRICT.items():
        if name == canon:
            terms.append(town)
    # Preserve order, casefold-dedupe
    out: list[str] = []
    seen: set[str] = set()
    for t in terms:
        s = (t or "").strip()
        if not s:
            continue
        k = s.casefold()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def kerala_district_rank(district: str | None) -> int:
    """Sort key: known Kerala districts by N→S rank; unknown/empty last."""
    canon = normalize_kerala_district(district)
    if not canon:
        return UNKNOWN_RANK + 1
    return _RANK_BY_CANONICAL.get(canon, UNKNOWN_RANK)


def district_rank_whens(column: Any) -> list[tuple]:
    """Build (predicate, rank) pairs for sqlalchemy.case on lower(trim(district))."""
    from sqlalchemy import func, or_

    lowered = func.lower(func.trim(column))
    by_rank: dict[int, set[str]] = {}
    for key, canon in _DISTRICT_CANONICAL.items():
        rank = _RANK_BY_CANONICAL[canon]
        by_rank.setdefault(rank, set()).add(key)
        by_rank[rank].add(canon.lower())

    whens: list[tuple] = []
    for rank, spellings in sorted(by_rank.items()):
        ordered = sorted(spellings)
        if len(ordered) == 1:
            whens.append((lowered == ordered[0], rank))
        else:
            whens.append((or_(*[lowered == s for s in ordered]), rank))
    return whens
