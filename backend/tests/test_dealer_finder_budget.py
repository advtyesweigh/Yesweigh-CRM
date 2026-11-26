"""Unit tests for India per-SKU Places cost controls."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.dealer_finder.places_pricing import (
    CONSOLE_REF_GROSS_INR,
    CONSOLE_REF_NET_SUBTOTAL_INR,
    CONSOLE_REF_OTHER_SAVINGS_INR,
    CONSOLE_REF_USAGE_COUNT,
    FREE_PLACE_DETAILS_ENTERPRISE_INDIA_MONTH,
    FREE_TEXT_SEARCH_PRO_INDIA_MONTH,
    console_billing_estimate_stage2,
    console_gross_estimated_inr_details,
    estimated_billable_usd,
    estimated_india_gross_list_usd,
)
from app.dealer_finder.settings_svc import enrich_usage_with_free_tier, free_requests_quota


def test_free_quota_legacy_column() -> None:
    row = SimpleNamespace(free_requests_per_month=7000, max_requests_per_month=999)
    assert free_requests_quota(row) == 7000  # type: ignore[arg-type]


def test_india_gross_list_all_events() -> None:
    s1, s2 = 1813, 13374
    assert estimated_india_gross_list_usd(stage1=s1, stage2=s2) == Decimal("97.4676")


def test_console_gross_reverse_calibration() -> None:
    gross = console_gross_estimated_inr_details(
        stage1=1813, stage2=CONSOLE_REF_USAGE_COUNT
    )
    assert gross == CONSOLE_REF_GROSS_INR


def test_console_billing_matches_sku_row() -> None:
    row = console_billing_estimate_stage2(stage2=CONSOLE_REF_USAGE_COUNT)
    assert row["usage_count"] == CONSOLE_REF_USAGE_COUNT
    assert row["usage_cost_inr"] == float(CONSOLE_REF_GROSS_INR)
    assert row["other_savings_inr"] == float(-CONSOLE_REF_OTHER_SAVINGS_INR)
    assert row["subtotal_inr"] == float(CONSOLE_REF_NET_SUBTOTAL_INR)


def test_india_billable_uses_per_sku_free_caps() -> None:
    # TN-scale month from screenshot: S1 under Pro cap, S2 above Enterprise cap
    s1, s2 = 1813, 13374
    assert estimated_billable_usd(stage1=s1, stage2=s2) == Decimal("38.2440")
    assert estimated_billable_usd(stage1=1000, stage2=5000) == Decimal("0")


def test_enrich_usage_billable_not_us_list() -> None:
    settings = SimpleNamespace(
        free_requests_per_month=7000,
        max_requests_per_month=7000,
        max_monthly_spend_usd=Decimal("400.00"),
    )
    usage = {
        "month_stage1": 1813,
        "month_stage2": 13374,
        "month_requests": 15187,
        "today_requests": 0,
    }
    enriched = enrich_usage_with_free_tier(usage, settings)  # type: ignore[arg-type]
    assert enriched["month_paid_estimated_usd"] == 38.244
    assert enriched["month_stage1_billable"] == 0
    assert enriched["month_stage2_billable"] == 13374 - FREE_PLACE_DETAILS_ENTERPRISE_INDIA_MONTH
    assert enriched["month_stage1_free_remaining"] == FREE_TEXT_SEARCH_PRO_INDIA_MONTH - 1813
    assert enriched["month_gross_estimated_usd"] > 500  # US list reference only
    expected_gross = float(
        console_gross_estimated_inr_details(stage1=1813, stage2=13374)
    )
    assert enriched["month_console_gross_estimated_inr"] == expected_gross
    assert enriched["month_console_sku_id"] == "448A-2967-E6BF"
    assert enriched["month_gross_india_list_usd"] == float(
        estimated_india_gross_list_usd(stage1=1813, stage2=13374)
    )