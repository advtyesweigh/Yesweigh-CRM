"""Verified India state → districts directory (seed for Gemini expansion plans)."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.google_places_search import resolve_state
from app.state_normalize import canon_state

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "indiaStatesDistricts.json"
_COMPACT = re.compile(r"[^a-z0-9]+")


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    with _DATA_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def list_states() -> list[str]:
    return list(_load().get("states") or [])


def _state_key(raw: str) -> str:
    label, _ = resolve_state(raw)
    canon = canon_state(label or raw) or (label or raw).strip()
    if canon in {"Kerala", "Keralam"}:
        return "Keralam"
    return canon


def districts_for_state(state: str) -> list[str]:
    """Return verified district list for a state (empty if unknown)."""
    data = _load()
    by_state: dict[str, list[str]] = data.get("districtsByState") or {}
    key = _state_key(state)
    if key in by_state:
        return list(by_state[key])
    # Compact fallback
    compact = _COMPACT.sub("", key.lower())
    for k, vals in by_state.items():
        if _COMPACT.sub("", k.lower()) == compact:
            return list(vals)
    return []


def normalize_district_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


def merge_district_lists(*lists: list[str]) -> list[str]:
    """Union districts preserving first-seen order (verified list first preferred)."""
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for raw in lst or []:
            d = normalize_district_name(raw)
            if not d:
                continue
            key = d.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
    return out
