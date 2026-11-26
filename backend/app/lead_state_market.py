"""Map canonical Indian states / UTs to lead business-language markets (state-inferred)."""
from __future__ import annotations

from app.state_normalize import state_match_values

# Canonical state labels (same buckets as lead_state_bucket / map paths).
_NORTHEAST = (
    "Tripura",
    "Meghalaya",
    "Manipur",
    "Mizoram",
    "Nagaland",
    "Sikkim",
    "Assam",
    "Arunachal Pradesh",
)

CANON_STATE_MARKET_LANGUAGE: dict[str, str] = {
    "Keralam": "ml",
    "Lakshadweep": "ml",
    "Tamil Nadu": "ta",
    "Puducherry": "ta",
    "Andaman and Nicobar": "ta",
    "Karnataka": "kn",
    "Andhra Pradesh": "te",
    "Telangana": "te",
    "Delhi": "hi",
    "Uttar Pradesh": "hi",
    "Bihar": "hi",
    "Madhya Pradesh": "hi",
    "Rajasthan": "hi",
    "Haryana": "hi",
    "Himachal Pradesh": "hi",
    "Uttarakhand": "hi",
    "Chhattisgarh": "hi",
    "Jharkhand": "hi",
    "Jammu and Kashmir": "hi",
    "Ladakh": "hi",
    "Maharashtra": "mr",
    "Dadra and Nagar Haveli and Daman and Diu": "mr",
    "Goa": "mr",
    "Gujarat": "gu",
    "West Bengal": "bn",
    "Punjab": "pa",
    "Chandigarh": "pa",
    "Odisha": "od",
}

for _st in _NORTHEAST:
    CANON_STATE_MARKET_LANGUAGE[_st] = "as"


def market_language_for_canon_state(canon: str) -> str:
    """Return market code (ml, ta, …) or '' when unassigned."""
    key = (canon or "").strip()
    if not key:
        return ""
    return CANON_STATE_MARKET_LANGUAGE.get(key, "")


_CANON_STATES_BY_MARKET: dict[str, list[str]] = {}


def canon_states_for_market(market: str) -> list[str]:
    """Canonical states that belong to a language market."""
    code = (market or "").strip().lower()
    if not code or code in ("other", "ne"):
        return []
    if not _CANON_STATES_BY_MARKET:
        by_m: dict[str, list[str]] = {}
        for state, mkt in CANON_STATE_MARKET_LANGUAGE.items():
            if not mkt:
                continue
            by_m.setdefault(mkt, []).append(state)
        for mkt in by_m:
            by_m[mkt].sort()
        _CANON_STATES_BY_MARKET.update(by_m)
    return list(_CANON_STATES_BY_MARKET.get(code, []))


def state_match_values_for_market(market: str) -> list[str]:
    """All CRM Lead.state spellings for states in the given market."""
    seen: set[str] = set()
    out: list[str] = []
    for canon in canon_states_for_market(market):
        for v in state_match_values(canon):
            if v not in seen:
                seen.add(v)
                out.append(v)
    return out