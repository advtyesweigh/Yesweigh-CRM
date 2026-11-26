"""Client-side exclusions for Dealer Finder phone-eligible NEW lists (chat + API)."""
from __future__ import annotations

import re
from typing import Any

from app.models import DealerSearchPlace


def _norm_list(values: Any, *, split_commas: bool = True) -> list[str]:
    out: list[str] = []
    if values is None:
        return out
    if isinstance(values, str):
        raw = [values]
    elif isinstance(values, (list, tuple, set)):
        raw = list(values)
    else:
        raw = [str(values)]
    for v in raw:
        s = str(v or "").strip()
        if not s:
            continue
        if split_commas and "," in s:
            for part in s.split(","):
                p = part.strip()
                if p:
                    out.append(p)
        else:
            out.append(s)
    # Dedupe case-insensitively, preserve first spelling
    seen: set[str] = set()
    deduped: list[str] = []
    for x in out:
        k = x.casefold()
        if k in seen:
            continue
        seen.add(k)
        deduped.append(x)
    return deduped


def parse_mobile_only_arg(args: dict[str, Any]) -> bool:
    """True when caller requests mobile-only list (default True for Dealer Finder phone-NEW)."""
    for key in ("mobile_only", "mobileOnly", "phones_mobile_only"):
        if key not in args:
            continue
        v = args.get(key)
        if isinstance(v, bool):
            return v
        s = str(v or "").strip().casefold()
        if s in {"0", "false", "no", "off"}:
            return False
        if s in {"1", "true", "yes", "on"}:
            return True
    return True


def parse_exclusion_args(args: dict[str, Any]) -> dict[str, list[str]]:
    """Normalize exclude_* tool/API args into district/city/keyword lists."""
    districts: list[str] = []
    cities: list[str] = []
    keywords: list[str] = []
    for key in (
        "exclude_districts",
        "exclude_district",
        "excludeDistricts",
        "excludeDistrict",
    ):
        districts.extend(_norm_list(args.get(key)))
    for key in ("exclude_cities", "exclude_city", "excludeCities", "excludeCity"):
        cities.extend(_norm_list(args.get(key)))
    for key in (
        "exclude_keywords",
        "exclude_keyword",
        "exclude_keywords_contains",
        "exclude_name_keywords",
        "excludeKeywords",
        "excludeKeyword",
    ):
        keywords.extend(_norm_list(args.get(key), split_commas=False))

    def _dedupe(vals: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for v in vals:
            k = v.casefold()
            if k in seen:
                continue
            seen.add(k)
            out.append(v)
        return out

    return {
        "exclude_districts": _dedupe(districts),
        "exclude_cities": _dedupe(cities),
        "exclude_keywords": _dedupe(keywords),
    }


def exclusions_active(exclusions: dict[str, list[str]]) -> bool:
    return bool(
        exclusions.get("exclude_districts")
        or exclusions.get("exclude_cities")
        or exclusions.get("exclude_keywords")
    )


def _row_fields(row: DealerSearchPlace | dict[str, Any]) -> tuple[str, str, str]:
    if isinstance(row, DealerSearchPlace):
        return (
            (row.district or "").strip(),
            (row.city or "").strip(),
            (row.name or "").strip(),
        )
    dist = str(row.get("district") or "").strip()
    city = str(row.get("city") or "").strip()
    name = str(row.get("name") or "").strip()
    return dist, city, name


def row_excluded(
    row: DealerSearchPlace | dict[str, Any],
    exclusions: dict[str, list[str]],
) -> str | None:
    """Return exclusion reason if row should be dropped, else None."""
    district, city, name = _row_fields(row)
    d_cf = district.casefold()
    c_cf = city.casefold()
    n_cf = name.casefold()
    for d in exclusions.get("exclude_districts") or []:
        if d_cf == d.casefold():
            return f"district={district or d}"
    for c in exclusions.get("exclude_cities") or []:
        if c_cf == c.casefold():
            return f"city={city or c}"
    for kw in exclusions.get("exclude_keywords") or []:
        needle = kw.casefold()
        if not needle:
            continue
        if needle in n_cf or needle in c_cf or needle in d_cf:
            return f"keyword={kw}"
    return None


def filter_rows(
    rows: list[Any],
    exclusions: dict[str, list[str]],
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Return (kept_rows, removed_meta) after applying exclusions."""
    if not exclusions_active(exclusions):
        return list(rows), []
    kept: list[Any] = []
    removed: list[dict[str, Any]] = []
    for row in rows:
        reason = row_excluded(row, exclusions)
        if reason:
            dist, city, name = _row_fields(row)
            phone = ""
            if isinstance(row, DealerSearchPlace):
                phone = (row.phone or "").strip()
            else:
                phone = str(row.get("whatsapp") or row.get("phone") or "").strip()
            removed.append(
                {
                    "name": name,
                    "district": dist,
                    "city": city,
                    "phone": phone,
                    "reason": reason,
                }
            )
        else:
            kept.append(row)
    return kept, removed


# User chat → exclusion args (backend force-run when model skips tool).
_FILTER_INTENT_RE = re.compile(
    r"\b(?:remove|exclude|drop|filter\s+out|omit|without)\b",
    re.I,
)
_DISTRICT_VALUE_RE = re.compile(
    r"(?:district\s*[=:]\s*|district\s+|remove\s+)([A-Za-z][A-Za-z .-]{1,40})",
    re.I,
)
_CITY_VALUE_RE = re.compile(
    r"(?:city\s*[=:]\s*|city\s+)([A-Za-z][A-Za-z .-]{1,40})",
    re.I,
)
_KEYWORD_VALUE_RE = re.compile(
    r"(?:keyword\s*[=:]\s*|keyword\s+contains\s+|name\s+contains\s+|containing\s+)"
    r"([A-Za-z0-9][A-Za-z0-9 ._-]{0,60})",
    re.I,
)
_LIST_CTX_RE = re.compile(
    r"\b(?:dealer\s+finder|final\s+list|results?\s+list|places?\s+list|from\s+list)\b",
    re.I,
)
_MOBILE_ONLY_RE = re.compile(
    r"\b(?:remove|drop|exclude|without)\s+"
    r"(?:landlines?|landline\s+numbers?|non[- ]?mobile|non[- ]?whatsapp)\b",
    re.I,
)
_MOBILE_KEEP_RE = re.compile(
    r"\b(?:mobile\s+only|mobile-only|only\s+mobiles?|whatsapp\s+numbers?\s+only)\b",
    re.I,
)


def parse_filter_command(command: str | None) -> dict[str, Any] | None:
    """Detect remove/exclude … from dealer finder list in user text."""
    text = (command or "").strip()
    if not text or not _FILTER_INTENT_RE.search(text):
        return None
    if not _LIST_CTX_RE.search(text) and "district" not in text.lower() and "keyword" not in text.lower():
        # Still allow bare "remove district Mahe" when district/city/keyword named
        if not _DISTRICT_VALUE_RE.search(text) and not _CITY_VALUE_RE.search(text):
            if not _KEYWORD_VALUE_RE.search(text):
                return None
    out: dict[str, Any] = {}
    if _MOBILE_ONLY_RE.search(text) or _MOBILE_KEEP_RE.search(text):
        out["mobile_only"] = True
    dm = _DISTRICT_VALUE_RE.search(text)
    if dm:
        dist = dm.group(1).strip(" .")
        if dist.lower() not in {"from", "the", "list", "results", "final", "dealer", "finder"}:
            out["exclude_districts"] = [dist]
    cm = _CITY_VALUE_RE.search(text)
    if cm:
        city = cm.group(1).strip(" .")
        if city.lower() not in {"from", "the", "list", "results", "final"}:
            out["exclude_cities"] = [city]
    km = _KEYWORD_VALUE_RE.search(text)
    if km:
        kw = km.group(1).strip(" .")
        if kw:
            out["exclude_keywords"] = [kw]
    # "remove Mahe" / "exclude Mahe from list" when Mahe is not a stop-word
    if "exclude_districts" not in out and re.search(r"\b(?:remove|exclude)\s+([A-Za-z]{3,30})\b", text, re.I):
        m = re.search(
            r"\b(?:remove|exclude)\s+(?:district\s+)?([A-Za-z][A-Za-z .-]{1,30})(?:\s+from|\s*$)",
            text,
            re.I,
        )
        if m:
            val = m.group(1).strip(" .")
            if val.lower() not in {
                "district",
                "city",
                "keyword",
                "the",
                "from",
                "list",
                "results",
                "dealer",
                "finder",
                "final",
                "phone",
                "places",
            }:
                out["exclude_districts"] = [val]
    return out if out else None