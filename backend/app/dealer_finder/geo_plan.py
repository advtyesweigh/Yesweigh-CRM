"""Intelligent location discovery: ALL districts → cities per district (Gemini + verified geo).

Primary expansion path for Tamil Nadu (and any state):
1. Gemini lists ALL districts (merged with verified geo directory so none are missed).
2. For each district, Gemini lists important cities/towns/commercial centres.
3. Plan is cached in PostgreSQL — avoid repeat Gemini for the same state+plan_version.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dealer_finder.constants import ORG_DEFAULT, PLAN_VERSION
from app.dealer_finder.india_geo import districts_for_state, merge_district_lists, normalize_district_name
from app.dealer_finder.normalize import canon_location_state
from app.models import DealerLocationPlan, User

logger = logging.getLogger("crm.dealer_finder.geo")

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


def _parse_json_blob(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    m = _JSON_FENCE.search(raw)
    if m:
        raw = m.group(1).strip()
    # Find outermost object
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _normalize_plan_districts(raw_districts: list[Any], verified: list[str]) -> list[dict[str, Any]]:
    """Merge Gemini districts+cities with verified district list (verified wins for completeness)."""
    by_key: dict[str, dict[str, Any]] = {}

    def _ensure(name: str) -> dict[str, Any]:
        n = normalize_district_name(name)
        key = n.lower()
        if key not in by_key:
            by_key[key] = {"name": n, "cities": []}
        return by_key[key]

    # Seed verified districts first so every official district is present
    for d in verified:
        _ensure(d)

    for item in raw_districts or []:
        if isinstance(item, str):
            _ensure(item)
            continue
        if not isinstance(item, dict):
            continue
        name = normalize_district_name(str(item.get("name") or item.get("district") or ""))
        if not name:
            continue
        entry = _ensure(name)
        cities_raw = item.get("cities") or item.get("towns") or item.get("commercial_centres") or []
        seen = {c.lower() for c in entry["cities"]}
        for c in cities_raw:
            city = normalize_district_name(str(c or ""))
            if not city:
                continue
            # Skip city that is just the district name
            if city.lower() == name.lower():
                continue
            if city.lower() in seen:
                continue
            seen.add(city.lower())
            entry["cities"].append(city)

    # Preserve verified order, then any Gemini-only extras
    ordered: list[dict[str, Any]] = []
    used: set[str] = set()
    for d in merge_district_lists(verified, [e["name"] for e in by_key.values()]):
        key = d.lower()
        entry = by_key.get(key) or {"name": d, "cities": []}
        ordered.append(entry)
        used.add(key)
    for key, entry in by_key.items():
        if key not in used:
            ordered.append(entry)
    return ordered


async def _gemini_location_plan(state: str, verified_districts: list[str]) -> tuple[dict[str, Any], str]:
    """Ask Gemini for ALL districts + cities/towns/commercial centres per district."""
    settings = get_settings()
    api_key = (settings.gemini_api_key or "").strip()
    model = (settings.ai_model or "gemini-2.5-flash-lite").strip()
    if not api_key:
        # Offline fallback: verified districts only, district name as sole "city" seed empty
        return {
            "districts": [{"name": d, "cities": []} for d in verified_districts],
            "source": "verified_only",
            "note": "GEMINI_API_KEY missing — districts from verified geo only",
        }, ""

    verified_blob = ", ".join(verified_districts) if verified_districts else "(none)"
    prompt = f"""You are building a commercial location directory for India dealer discovery.

State: {state}

Task:
1. List ALL administrative districts in {state}. Include every district — none missing.
2. For EACH district, list important cities, towns, and commercial centres where weighing-scale /
   retail / industrial businesses are likely (district HQ, major towns, industrial hubs, market towns).
   Prefer 4–12 places per district when the district is large; fewer for small ones.
   Do NOT repeat the district name as a city. Do NOT invent obscure villages.

Verified district reference (must all appear; add any official district missing from this list):
{verified_blob}

Return ONLY valid JSON (no markdown) with this shape:
{{
  "state": "{state}",
  "districts": [
    {{"name": "District Name", "cities": ["City1", "Town2"]}}
  ]
}}
"""

    def _call() -> str:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=8192,
                response_mime_type="application/json",
            ),
        )
        return (getattr(resp, "text", None) or "") or ""

    try:
        text = await asyncio.to_thread(_call)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gemini location plan failed: %s", exc)
        return {
            "districts": [{"name": d, "cities": []} for d in verified_districts],
            "source": "verified_fallback",
            "note": f"Gemini error: {type(exc).__name__}",
        }, model

    parsed = _parse_json_blob(text)
    districts = parsed.get("districts") if isinstance(parsed.get("districts"), list) else []
    merged = _normalize_plan_districts(districts, verified_districts)
    return {
        "state": state,
        "districts": merged,
        "source": "gemini+verified",
    }, model


def expand_search_units(
    plan: dict[str, Any],
    *,
    categories: list[str],
    state: str,
    search_depth: str = "full",
    district_filter: str = "",
    city_filter: str = "",
) -> list[dict[str, Any]]:
    """Expand plan into district-wise AND city-wise × category search units.

    One statewide query is NOT enough — each district and each city gets its own Places query
    per category for maximum coverage.
    """
    depth = (search_depth or "full").strip().lower()
    dist_f = (district_filter or "").strip().lower()
    city_f = (city_filter or "").strip().lower()
    cats = [c.strip() for c in (categories or []) if (c or "").strip()]
    if not cats:
        from app.dealer_finder.constants import DEFAULT_CATEGORIES

        cats = list(DEFAULT_CATEGORIES)

    units: list[dict[str, Any]] = []
    for d in plan.get("districts") or []:
        if not isinstance(d, dict):
            continue
        district = normalize_district_name(str(d.get("name") or ""))
        if not district:
            continue
        if dist_f and dist_f not in district.lower():
            continue
        cities = [normalize_district_name(str(c)) for c in (d.get("cities") or [])]
        cities = [c for c in cities if c]
        if city_f:
            cities = [c for c in cities if city_f in c.lower()]
            if not cities and city_f not in district.lower():
                continue

        for cat in cats:
            if depth in {"full", "district_only"} and not city_f:
                units.append(
                    {
                        "kind": "district",
                        "state": state,
                        "district": district,
                        "city": "",
                        "category": cat,
                        "text_query": f"{cat} in {district} {state} India",
                    }
                )
            if depth in {"full", "city_only"}:
                for city in cities:
                    units.append(
                        {
                            "kind": "city",
                            "state": state,
                            "district": district,
                            "city": city,
                            "category": cat,
                            "text_query": f"{cat} in {city} {district} {state} India",
                        }
                    )
    return units


async def get_or_create_location_plan(
    db: AsyncSession,
    *,
    state: str,
    user: User | None = None,
    organization_id: str = ORG_DEFAULT,
    force_refresh: bool = False,
    plan_version: str = PLAN_VERSION,
) -> DealerLocationPlan:
    """Return cached plan or generate via Gemini (districts → cities) and persist."""
    state_label = canon_location_state(state)
    if not state_label:
        raise ValueError("state is required")

    existing = (
        await db.execute(
            select(DealerLocationPlan).where(
                DealerLocationPlan.organization_id == organization_id,
                DealerLocationPlan.state == state_label,
                DealerLocationPlan.plan_version == plan_version,
            )
        )
    ).scalar_one_or_none()

    if existing and not force_refresh:
        return existing

    verified = districts_for_state(state_label)
    plan_body, model = await _gemini_location_plan(state_label, verified)
    districts = plan_body.get("districts") or []
    city_count = sum(len(d.get("cities") or []) for d in districts if isinstance(d, dict))

    if existing:
        existing.plan_json = json.dumps(plan_body, ensure_ascii=False)
        existing.district_count = len(districts)
        existing.city_count = city_count
        existing.gemini_model = model or existing.gemini_model
        if user:
            existing.created_by = user.id
        await db.flush()
        return existing

    row = DealerLocationPlan(
        organization_id=organization_id,
        country="IN",
        state=state_label,
        plan_version=plan_version,
        plan_json=json.dumps(plan_body, ensure_ascii=False),
        district_count=len(districts),
        city_count=city_count,
        gemini_model=model,
        created_by=user.id if user else None,
    )
    db.add(row)
    await db.flush()
    return row


def plan_as_dict(row: DealerLocationPlan) -> dict[str, Any]:
    try:
        body = json.loads(row.plan_json or "{}")
    except json.JSONDecodeError:
        body = {}
    return {
        "id": row.id,
        "state": row.state,
        "plan_version": row.plan_version,
        "district_count": row.district_count,
        "city_count": row.city_count,
        "gemini_model": row.gemini_model,
        "source": (body.get("source") if isinstance(body, dict) else None) or "",
        "districts": body.get("districts") if isinstance(body, dict) else [],
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
