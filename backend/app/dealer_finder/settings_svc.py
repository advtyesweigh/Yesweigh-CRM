"""Dealer Finder admin settings + budget checks."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_finder.constants import COOLDOWN_PRESETS_DAYS, ORG_DEFAULT
from app.dealer_finder.places_pricing import (
    CONSOLE_SKU_PLACE_DETAILS_ENTERPRISE_IN,
    FREE_PLACE_DETAILS_ENTERPRISE_INDIA_MONTH,
    FREE_TEXT_SEARCH_PRO_INDIA_MONTH,
    INR_PER_REQUEST_GROSS_CONSOLE,
    INR_PER_REQUEST_NET_CONSOLE,
    console_billing_estimate_stage2,
    console_gross_estimated_inr_details,
    console_implied_billable_inr_stage2,
    console_net_estimated_inr_stage2,
    estimated_billable_inr,
    estimated_billable_usd,
    estimated_india_gross_list_inr,
    estimated_india_gross_list_usd,
    india_pricing_meta,
    us_list_gross_usd,
)
from app.models import DealerFinderSettings, DealerPlacesUsage, DealerSearchJob, User

# Paid throttle default (USD) — India-estimated billable only; not Console gross / not US list.
_DEFAULT_SPEND_USD = 100.0
# All-India full-country runs can exceed legacy $400 before Console net INR is still ₹0.
RECOMMENDED_OPS_CAP_ALL_INDIA_USD = 2000.0
_DEFAULT_JOB_REQ = 100_000
_DEFAULT_DAY_REQ = 100_000


def free_requests_quota(settings: DealerFinderSettings) -> int:
    """Legacy combined free hint (Text Search Pro India cap). Prefer per-SKU fields in usage."""
    free = getattr(settings, "free_requests_per_month", None)
    if free is not None and int(free) > 0:
        return int(free)
    return int(settings.max_requests_per_month or FREE_TEXT_SEARCH_PRO_INDIA_MONTH)


def enrich_usage_with_free_tier(
    usage: dict[str, Any], settings: DealerFinderSettings
) -> dict[str, Any]:
    """Attach India per-SKU free caps and billable estimates for UI + throttle."""
    s1 = int(usage.get("month_stage1") or 0)
    s2 = int(usage.get("month_stage2") or 0)
    billable_usd = float(estimated_billable_usd(stage1=s1, stage2=s2))
    billable_inr = float(estimated_billable_inr(stage1=s1, stage2=s2))
    out = dict(usage)
    out["cost_model"] = india_pricing_meta()
    out["month_stage1_free_cap"] = FREE_TEXT_SEARCH_PRO_INDIA_MONTH
    out["month_stage2_free_cap"] = FREE_PLACE_DETAILS_ENTERPRISE_INDIA_MONTH
    out["month_stage1_free_remaining"] = max(0, FREE_TEXT_SEARCH_PRO_INDIA_MONTH - s1)
    out["month_stage2_free_remaining"] = max(0, FREE_PLACE_DETAILS_ENTERPRISE_INDIA_MONTH - s2)
    out["month_stage1_billable"] = max(0, s1 - FREE_TEXT_SEARCH_PRO_INDIA_MONTH)
    out["month_stage2_billable"] = max(0, s2 - FREE_PLACE_DETAILS_ENTERPRISE_INDIA_MONTH)
    # Legacy aliases — now India billable, not US list × blended free tier.
    legacy_free = free_requests_quota(settings)
    out["free_requests_per_month"] = legacy_free
    month_req = int(usage.get("month_requests") or 0)
    out["month_free_remaining"] = max(0, legacy_free - month_req)
    out["month_paid_requests"] = out["month_stage1_billable"] + out["month_stage2_billable"]
    out["month_billable_estimated_usd"] = billable_usd
    out["month_billable_estimated_inr"] = billable_inr
    console_gross = console_gross_estimated_inr_details(stage1=s1, stage2=s2)
    console_net = console_net_estimated_inr_stage2(stage2=s2)
    console_savings = (console_gross - console_net).quantize(Decimal("0.01"))
    out["month_console_sku_id"] = CONSOLE_SKU_PLACE_DETAILS_ENTERPRISE_IN
    out["month_console_sku_usage_count"] = s2
    out["month_console_gross_estimated_inr"] = float(console_gross)
    out["month_console_other_savings_inr"] = float(-console_savings)
    out["month_console_net_subtotal_inr"] = float(console_net)
    out["month_console_inr_per_request_gross"] = float(INR_PER_REQUEST_GROSS_CONSOLE)
    out["month_console_inr_per_request_net"] = float(INR_PER_REQUEST_NET_CONSOLE)
    out["month_console_billing"] = console_billing_estimate_stage2(stage2=s2)
    out["month_console_billable_gross_inr"] = float(
        console_implied_billable_inr_stage2(stage2=s2)
    )
    out["month_console_net_estimated_inr"] = float(console_net_estimated_inr_stage2(stage2=s2))
    out["month_paid_estimated_usd"] = round(billable_usd, 4)
    out["month_estimated_usd"] = billable_usd
    out["month_gross_estimated_usd"] = float(us_list_gross_usd(stage1=s1, stage2=s2))
    out["recommended_ops_cap_all_india_usd"] = RECOMMENDED_OPS_CAP_ALL_INDIA_USD
    out["month_gross_india_list_usd"] = float(estimated_india_gross_list_usd(stage1=s1, stage2=s2))
    out["month_gross_india_list_inr"] = float(estimated_india_gross_list_inr(stage1=s1, stage2=s2))
    out["max_monthly_spend_usd"] = float(
        settings.max_monthly_spend_usd or _DEFAULT_SPEND_USD
    )
    return out



def settings_public(row: DealerFinderSettings) -> dict[str, Any]:
    free = free_requests_quota(row)
    return {
        "organization_id": row.organization_id,
        "cooldown_days": int(row.cooldown_days or 30),
        "cooldown_presets": list(COOLDOWN_PRESETS_DAYS),
        "max_requests_per_job": int(row.max_requests_per_job or _DEFAULT_JOB_REQ),
        "max_requests_per_day": int(row.max_requests_per_day or _DEFAULT_DAY_REQ),
        "free_requests_per_month": free,
        "max_requests_per_month": free,
        "max_monthly_spend_usd": float(row.max_monthly_spend_usd or _DEFAULT_SPEND_USD),
        "cost_model": {
            **india_pricing_meta(),
            "throttle": (
                f"Ops pause when CRM India billable estimate reaches ~$"
                f"{float(row.max_monthly_spend_usd or _DEFAULT_SPEND_USD):.0f} "
                "(internal throttle — not your Console invoice). "
                "Console *usage cost* may show INR gross SKU lines; *subtotal* can be ₹0 after free tier / credits."
            ),
        },
        "max_concurrent_jobs": int(row.max_concurrent_jobs or 1),
        "expensive_field_policy": row.expensive_field_policy or "when_needed",
        "persist_website_on_import": bool(row.persist_website_on_import),
        "all_india_enabled": bool(getattr(row, "all_india_enabled", False)),
        "pilot_district_completed": bool(getattr(row, "pilot_district_completed", False)),
        "pilot_state": getattr(row, "pilot_state", None) or "Rajasthan",
        "pilot_district": getattr(row, "pilot_district", None) or "Jaipur",
    }


async def get_or_create_settings(
    db: AsyncSession, *, organization_id: str = ORG_DEFAULT
) -> DealerFinderSettings:
    row = (
        await db.execute(
            select(DealerFinderSettings).where(
                DealerFinderSettings.organization_id == organization_id
            )
        )
    ).scalar_one_or_none()
    if row:
        return row
    row = DealerFinderSettings(organization_id=organization_id)
    db.add(row)
    await db.flush()
    return row


async def patch_settings(
    db: AsyncSession,
    user: User,
    patch: dict[str, Any],
    *,
    organization_id: str = ORG_DEFAULT,
) -> DealerFinderSettings:
    row = await get_or_create_settings(db, organization_id=organization_id)
    if "cooldown_days" in patch and patch["cooldown_days"] is not None:
        days = int(patch["cooldown_days"])
        row.cooldown_days = max(1, min(365, days))
    for key in (
        "max_requests_per_job",
        "max_requests_per_day",
        "max_concurrent_jobs",
    ):
        if key in patch and patch[key] is not None:
            setattr(row, key, max(1, int(patch[key])))
    free_patch = None
    if "free_requests_per_month" in patch and patch["free_requests_per_month"] is not None:
        free_patch = max(0, int(patch["free_requests_per_month"]))
    elif "max_requests_per_month" in patch and patch["max_requests_per_month"] is not None:
        free_patch = max(0, int(patch["max_requests_per_month"]))
    if free_patch is not None:
        row.free_requests_per_month = free_patch
        row.max_requests_per_month = free_patch
    if "max_monthly_spend_usd" in patch and patch["max_monthly_spend_usd"] is not None:
        new_cap = float(patch["max_monthly_spend_usd"])
        old_cap = float(row.max_monthly_spend_usd or _DEFAULT_SPEND_USD)
        if new_cap > old_cap + 0.01 and user.role.lower() not in {
            "admin",
            "super_admin",
            "super admin",
            "management",
        }:
            raise ValueError("Admin approval required to raise monthly Places spend cap")
        row.max_monthly_spend_usd = Decimal(str(patch["max_monthly_spend_usd"]))
    if "expensive_field_policy" in patch and patch["expensive_field_policy"]:
        pol = str(patch["expensive_field_policy"]).strip().lower()
        if pol in {"always", "when_needed", "never"}:
            row.expensive_field_policy = pol
    if "persist_website_on_import" in patch and patch["persist_website_on_import"] is not None:
        row.persist_website_on_import = bool(patch["persist_website_on_import"])
    if "all_india_enabled" in patch and patch["all_india_enabled"] is not None:
        if not getattr(row, "pilot_district_completed", False) and patch["all_india_enabled"]:
            raise ValueError("Complete pilot district before enabling all-India")
        row.all_india_enabled = bool(patch["all_india_enabled"])
    if "pilot_state" in patch and patch["pilot_state"]:
        row.pilot_state = str(patch["pilot_state"]).strip()[:80]
    if "pilot_district" in patch and patch["pilot_district"]:
        row.pilot_district = str(patch["pilot_district"]).strip()[:80]
    row.updated_by_user_id = user.id
    row.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return row


async def usage_totals(
    db: AsyncSession, *, organization_id: str = ORG_DEFAULT
) -> dict[str, Any]:
    today = date.today()
    month_start = today.replace(day=1)
    day_row = (
        await db.execute(
            select(DealerPlacesUsage).where(
                DealerPlacesUsage.organization_id == organization_id,
                DealerPlacesUsage.usage_date == today,
            )
        )
    ).scalar_one_or_none()
    month_q = await db.execute(
        select(
            func.coalesce(func.sum(DealerPlacesUsage.stage1_requests), 0),
            func.coalesce(func.sum(DealerPlacesUsage.stage2_requests), 0),
        ).where(
            DealerPlacesUsage.usage_date >= month_start,
            DealerPlacesUsage.organization_id == organization_id,
        )
    )
    m1, m2 = month_q.one()
    month_s1 = int(m1 or 0)
    month_s2 = int(m2 or 0)
    billable_usd = float(estimated_billable_usd(stage1=month_s1, stage2=month_s2))
    return {
        "today_stage1": int(day_row.stage1_requests) if day_row else 0,
        "today_stage2": int(day_row.stage2_requests) if day_row else 0,
        "today_requests": (int(day_row.stage1_requests) + int(day_row.stage2_requests))
        if day_row
        else 0,
        "month_stage1": month_s1,
        "month_stage2": month_s2,
        "month_requests": month_s1 + month_s2,
        "month_billable_estimated_usd": billable_usd,
        "month_estimated_usd": billable_usd,
    }


async def budget_allows(
    db: AsyncSession,
    settings: DealerFinderSettings,
    *,
    organization_id: str = ORG_DEFAULT,
    extra_requests: int = 1,
    job_requests_so_far: int = 0,
) -> tuple[bool, str]:
    """Enforce job/day caps + monthly paid throttle on India-estimated billable USD."""
    usage = await usage_totals(db, organization_id=organization_id)
    if job_requests_so_far + extra_requests > int(
        settings.max_requests_per_job or _DEFAULT_JOB_REQ
    ):
        return False, "Job request budget reached"
    if usage["today_requests"] + extra_requests > int(
        settings.max_requests_per_day or _DEFAULT_DAY_REQ
    ):
        return False, "Daily Places request budget reached"

    spend_cap = float(settings.max_monthly_spend_usd or _DEFAULT_SPEND_USD)
    s1 = int(usage.get("month_stage1") or 0)
    s2 = int(usage.get("month_stage2") or 0)
    extra = max(0, int(extra_requests))
    # Project extra calls as stage-2 (contact enrichment dominates volume).
    projected_billable = float(
        estimated_billable_usd(stage1=s1, stage2=s2 + extra)
    )
    if projected_billable >= spend_cap:
        return (
            False,
            f"Monthly Places billable spend estimate reached "
            f"(${spend_cap:.0f} ops cap — CRM billable estimate, not Console invoice)",
        )

    running = (
        await db.execute(
            select(func.count())
            .select_from(DealerSearchJob)
            .where(
                DealerSearchJob.organization_id == organization_id,
                DealerSearchJob.status == "Running",
            )
        )
    ).scalar_one()
    if int(running or 0) > int(settings.max_concurrent_jobs or 1):
        return False, "Too many concurrent Dealer Finder jobs"
    return True, ""