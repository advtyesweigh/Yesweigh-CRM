"""All-India dealer discovery — excluded states, pilot defaults, cost preflight."""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_finder.constants import DEFAULT_CATEGORIES, ORG_DEFAULT
from app.dealer_finder.geo_plan import expand_search_units
from app.dealer_finder.india_geo import districts_for_state, list_states
from app.dealer_finder.normalize import canon_location_state
from app.dealer_finder.places_pricing import (
    marginal_billable_usd_per_request,
    estimate_billable_usd_from_total_requests,
)
from app.dealer_finder.settings_svc import usage_totals
from app.models import DealerFinderSettings, DealerLocationPlan

_COMPACT = re.compile(r"[^a-z0-9]+")

# Never auto-scan these (South already covered in CRM / separate jobs).
EXCLUDED_STATE_LABELS: frozenset[str] = frozenset(
    {
        "Tamil Nadu",
        "Kerala",
        "Keralam",
        "Puducherry",
    }
)

DEFAULT_PILOT_STATE = "Rajasthan"
DEFAULT_PILOT_DISTRICT = "Jaipur"

ALL_INDIA_MODE_PILOT = "pilot"
ALL_INDIA_MODE_FULL = "full"


def is_excluded_state(state: str) -> bool:
    """True when state is Tamil Nadu, Kerala/Keralam, or Puducherry."""
    label = (canon_location_state(state) or (state or "").strip()) or ""
    if not label:
        return False
    if label in EXCLUDED_STATE_LABELS:
        return True
    key = _COMPACT.sub("", label.lower())
    for ex in EXCLUDED_STATE_LABELS:
        if _COMPACT.sub("", ex.lower()) == key:
            return True
    return False


def assert_state_allowed(state: str) -> str:
    """Return canonical state label or raise ValueError.

    For all-India pilot/full queues only — not single-state Dealer Finder / AI jobs."""
    label = canon_location_state(state)
    if not label:
        raise ValueError("state is required")
    if is_excluded_state(label):
        raise ValueError(
            f"State {label} is excluded from all-India discovery "
            "(Tamil Nadu, Kerala, Puducherry are manual-only)."
        )
    return label


def eligible_states() -> list[str]:
    """Indian states/UTs in geo directory minus excluded."""
    out: list[str] = []
    for s in list_states():
        if is_excluded_state(s):
            continue
        out.append(s)
    return out


def full_india_state_queue() -> list[str]:
    """Alphabetical eligible states for sequential full-country runs."""
    return sorted(eligible_states(), key=lambda x: x.lower())


async def cached_plan_body(
    db: AsyncSession,
    *,
    state: str,
    organization_id: str = ORG_DEFAULT,
    plan_version: str = "v1-district-city-expand",
) -> dict[str, Any] | None:
    """Return cached Gemini plan JSON if present (no Gemini call)."""
    label = assert_state_allowed(state)
    row = (
        await db.execute(
            select(DealerLocationPlan).where(
                DealerLocationPlan.organization_id == organization_id,
                DealerLocationPlan.state == label,
                DealerLocationPlan.plan_version == plan_version,
            )
        )
    ).scalar_one_or_none()
    if not row:
        return None
    try:
        body = json.loads(row.plan_json or "{}")
    except json.JSONDecodeError:
        return None
    return body if isinstance(body, dict) else None


def _verified_only_plan(state: str) -> dict[str, Any]:
    districts = districts_for_state(state)
    return {
        "districts": [{"name": d, "cities": []} for d in districts],
        "source": "verified_only_preflight",
    }


def count_units_for_state(
    plan: dict[str, Any],
    *,
    state: str,
    categories: list[str] | None = None,
    district_filter: str = "",
    search_depth: str = "full",
) -> int:
    cats = [c.strip() for c in (categories or DEFAULT_CATEGORIES) if (c or "").strip()]
    if not cats:
        cats = list(DEFAULT_CATEGORIES)
    units = expand_search_units(
        plan,
        categories=cats,
        state=state,
        search_depth=search_depth,
        district_filter=district_filter,
    )
    return len(units)


async def preflight_estimate(
    db: AsyncSession,
    *,
    states: list[str] | None = None,
    categories: list[str] | None = None,
    district_filter: str = "",
    search_depth: str = "full",
    organization_id: str = ORG_DEFAULT,
) -> dict[str, Any]:
    """Cost / unit estimate before starting large jobs (uses cached plans when possible)."""
    queue = states or full_india_state_queue()
    queue = [assert_state_allowed(s) for s in queue]
    usage = await usage_totals(db, organization_id=organization_id)
    s1 = int(usage.get("month_stage1") or 0)
    s2 = int(usage.get("month_stage2") or 0)

    total_units = 0
    per_state: list[dict[str, Any]] = []
    plans_missing_gemini = 0
    for st in queue:
        body = await cached_plan_body(db, state=st, organization_id=organization_id)
        if body and body.get("districts"):
            plan = body
            plan_src = body.get("source") or "cached"
        else:
            plan = _verified_only_plan(st)
            plan_src = "verified_only_preflight"
            plans_missing_gemini += 1
        n = count_units_for_state(
            plan,
            state=st,
            categories=categories,
            district_filter=district_filter,
            search_depth=search_depth,
        )
        total_units += n
        per_state.append({"state": st, "units": n, "plan_source": plan_src})

    # Heuristic: ~1.8 API requests per unit (pagination + stage-2 phone enrichment).
    est_requests = int(round(total_units * 1.8))
    marginal = marginal_billable_usd_per_request(
        stage1=s1, stage2=s2, extra_requests=est_requests
    )
    est_add_usd = float(marginal * est_requests)
    return {
        "states": len(queue),
        "excluded_states": sorted(EXCLUDED_STATE_LABELS),
        "total_units": total_units,
        "estimated_api_requests": est_requests,
        "estimated_marginal_usd": round(est_add_usd, 4),
        "estimated_total_job_usd": float(
            estimate_billable_usd_from_total_requests(est_requests)
        ),
        "plans_missing_gemini": plans_missing_gemini,
        "per_state": per_state[:100],
        "per_state_truncated": len(per_state) > 100,
        "note": (
            "Preflight uses cached Gemini plans when available; otherwise district-only "
            "verified geo (underestimates city units). Actual spend follows India SKU caps "
            "in places_pricing.py and monthly spend gate in settings."
        ),
    }


def gates_public(settings: DealerFinderSettings) -> dict[str, Any]:
    return {
        "excluded_states": sorted(EXCLUDED_STATE_LABELS),
        "eligible_state_count": len(eligible_states()),
        "pilot_defaults": {
            "state": settings.pilot_state or DEFAULT_PILOT_STATE,
            "district": settings.pilot_district or DEFAULT_PILOT_DISTRICT,
        },
        "pilot_district_completed": bool(settings.pilot_district_completed),
        "all_india_enabled": bool(settings.all_india_enabled),
        "full_country_requires": [
            "pilot_district_completed",
            "all_india_enabled (admin)",
            "confirm=true on start",
            "preflight cost acknowledged",
        ],
    }