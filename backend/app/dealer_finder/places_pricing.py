"""India Places API (New) billable estimates for Dealer Finder.

YesWeigh runs with ``regionCode=IN`` and India-eligible Cloud billing. Estimates
follow per-SKU monthly free caps and India list USD (invoiced in INR on Console).

References:
- India SKU sheet: https://developers.google.com/maps/billing-and-pricing/pricing-india-ondc
- March 2025 free caps per category: https://developers.google.com/maps/billing-and-pricing/march-2025

Stage mapping (see ``constants.FIELD_MASK_STAGE*``):
- Stage 1 → Places API Text Search Pro (India) — Pro, 35,000 free/mo
- Stage 2 → Places API Place Details Enterprise (India) — Enterprise, 7,000 free/mo
  (phone / website field mask)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

# Per-SKU free monthly billable events (India, March 2025+)
FREE_TEXT_SEARCH_PRO_INDIA_MONTH = 35_000
FREE_PLACE_DETAILS_ENTERPRISE_INDIA_MONTH = 7_000

# First paid tier — USD per 1,000 billable events (India sheet)
USD_PER_1K_TEXT_SEARCH_PRO_INDIA = Decimal("9.60")
USD_PER_1K_PLACE_DETAILS_ENTERPRISE_INDIA = Decimal("6.00")

# Display-only; Google Console INR is authoritative for actual charges.
USD_TO_INR_INDICATIVE = Decimal("84.0")

# Console reverse-calibration — Places API (New) SKU Place Details Enterprise (India).
# Authoritative Cloud Console row (Nov 2026 billing): real usage count, gross, credits, net.
CONSOLE_SKU_PLACE_DETAILS_ENTERPRISE_IN = "448A-2967-E6BF"
CONSOLE_SKU_PLACE_DETAILS_ENTERPRISE_NAME = "Places API Place Details Enterprise (India)"
CONSOLE_REF_USAGE_COUNT = 72_670
CONSOLE_REF_GROSS_INR = Decimal("37522.52")
CONSOLE_REF_OTHER_SAVINGS_INR = Decimal("28569.00")
CONSOLE_REF_NET_SUBTOTAL_INR = Decimal("8953.52")
CONSOLE_OTHER_SAVINGS_FRACTION = (
    CONSOLE_REF_OTHER_SAVINGS_INR / CONSOLE_REF_GROSS_INR
).quantize(Decimal("0.000001"))
# Effective ₹/request (display); gross/net use ratio math so ref month hits Console exactly.
INR_PER_REQUEST_GROSS_CONSOLE = (CONSOLE_REF_GROSS_INR / Decimal(CONSOLE_REF_USAGE_COUNT)).quantize(
    Decimal("0.0001")
)
INR_PER_REQUEST_NET_CONSOLE = (CONSOLE_REF_NET_SUBTOTAL_INR / Decimal(CONSOLE_REF_USAGE_COUNT)).quantize(
    Decimal("0.0001")
)
# Legacy aliases (stage-2 == this SKU in Dealer Finder).
CONSOLE_REF_STAGE2_TOTAL = CONSOLE_REF_USAGE_COUNT
CONSOLE_REF_STAGE2_GROSS_INR = CONSOLE_REF_GROSS_INR
CONSOLE_REF_STAGE2_BILLABLE = CONSOLE_REF_USAGE_COUNT - FREE_PLACE_DETAILS_ENTERPRISE_INDIA_MONTH
INR_PER_STAGE2_GROSS_CONSOLE = INR_PER_REQUEST_GROSS_CONSOLE
INR_PER_1K_STAGE2_BILLABLE_CONSOLE = (
    CONSOLE_REF_GROSS_INR / Decimal(CONSOLE_REF_STAGE2_BILLABLE) * Decimal(1000)
).quantize(Decimal("0.01"))

# Legacy US list (non-India) — reference only, not used for caps or throttle.
US_LIST_USD_PER_1K_TEXT_SEARCH_PRO = Decimal("32.00")
US_LIST_USD_PER_1K_PLACE_DETAILS_ENTERPRISE = Decimal("40.00")


def billable_request_counts(*, stage1: int, stage2: int) -> tuple[int, int]:
    """Billable events this month after each SKU's own free cap."""
    s1 = max(0, int(stage1 or 0))
    s2 = max(0, int(stage2 or 0))
    return (
        max(0, s1 - FREE_TEXT_SEARCH_PRO_INDIA_MONTH),
        max(0, s2 - FREE_PLACE_DETAILS_ENTERPRISE_INDIA_MONTH),
    )


def estimated_billable_usd(*, stage1: int, stage2: int) -> Decimal:
    """Estimated USD for billable events beyond India per-SKU free caps."""
    b1, b2 = billable_request_counts(stage1=stage1, stage2=stage2)
    return (
        (USD_PER_1K_TEXT_SEARCH_PRO_INDIA * Decimal(b1) / Decimal(1000))
        + (USD_PER_1K_PLACE_DETAILS_ENTERPRISE_INDIA * Decimal(b2) / Decimal(1000))
    ).quantize(Decimal("0.0001"))


def us_list_gross_usd(*, stage1: int, stage2: int) -> Decimal:
    """Pessimistic US global list price — visibility only, not India billing."""
    s1 = max(0, int(stage1 or 0))
    s2 = max(0, int(stage2 or 0))
    return (
        (US_LIST_USD_PER_1K_TEXT_SEARCH_PRO * Decimal(s1) / Decimal(1000))
        + (US_LIST_USD_PER_1K_PLACE_DETAILS_ENTERPRISE * Decimal(s2) / Decimal(1000))
    ).quantize(Decimal("0.0001"))


def estimated_billable_inr(*, stage1: int, stage2: int) -> Decimal:
    """India published list on billable events (after per-SKU free caps), in indicative INR."""
    return (estimated_billable_usd(stage1=stage1, stage2=stage2) * USD_TO_INR_INDICATIVE).quantize(
        Decimal("0.01")
    )


def console_gross_estimated_inr_stage2(*, stage2: int) -> Decimal:
    """Console-style gross for Place Details Enterprise — reverse-calibrated from billing."""
    s2 = max(0, int(stage2 or 0))
    if s2 <= 0:
        return Decimal("0.00")
    return (CONSOLE_REF_GROSS_INR * Decimal(s2) / Decimal(CONSOLE_REF_USAGE_COUNT)).quantize(
        Decimal("0.01")
    )


def console_gross_estimated_inr_details(*, stage1: int, stage2: int) -> Decimal:
    """Gross Details SKU estimate only (Console line-item model; stage1 Text Search excluded)."""
    del stage1  # Details SKU only
    return console_gross_estimated_inr_stage2(stage2=stage2)


def console_implied_billable_inr_stage2(*, stage2: int) -> Decimal:
    """Gross usage cost on billable stage-2 only (after Enterprise free cap), Console-calibrated."""
    _b1, b2 = billable_request_counts(stage1=0, stage2=stage2)
    if b2 <= 0:
        return Decimal("0.00")
    return (
        CONSOLE_REF_GROSS_INR * Decimal(b2) / Decimal(CONSOLE_REF_USAGE_COUNT)
    ).quantize(Decimal("0.01"))


def console_other_savings_inr(*, gross_inr: Decimal) -> Decimal:
    """Other savings (credits / promos) — negative on Console; returned as positive magnitude."""
    g = max(Decimal("0"), gross_inr)
    if g <= 0:
        return Decimal("0.00")
    return (g * CONSOLE_OTHER_SAVINGS_FRACTION).quantize(Decimal("0.01"))


def console_net_subtotal_inr(*, gross_inr: Decimal) -> Decimal:
    """Net subtotal after Other savings — primary invoice-aligned figure."""
    g = max(Decimal("0"), gross_inr)
    return (g - console_other_savings_inr(gross_inr=g)).quantize(Decimal("0.01"))


def console_net_estimated_inr_stage2(*, stage2: int) -> Decimal:
    """Net subtotal for Place Details Enterprise (India) metered events."""
    s2 = max(0, int(stage2 or 0))
    if s2 <= 0:
        return Decimal("0.00")
    return (
        CONSOLE_REF_NET_SUBTOTAL_INR * Decimal(s2) / Decimal(CONSOLE_REF_USAGE_COUNT)
    ).quantize(Decimal("0.01"))


def console_billing_estimate_stage2(*, stage2: int) -> dict[str, Any]:
    """Console-aligned SKU row for Place Details Enterprise (India) — gross, savings, net."""
    s2 = max(0, int(stage2 or 0))
    gross = console_gross_estimated_inr_stage2(stage2=s2)
    net = console_net_estimated_inr_stage2(stage2=s2)
    savings = (gross - net).quantize(Decimal("0.01"))
    return {
        "sku_id": CONSOLE_SKU_PLACE_DETAILS_ENTERPRISE_IN,
        "sku_name": CONSOLE_SKU_PLACE_DETAILS_ENTERPRISE_NAME,
        "usage_count": s2,
        "usage_cost_inr": float(gross),
        "other_savings_inr": float(-savings),
        "subtotal_inr": float(net),
        "inr_per_request_gross": float(INR_PER_REQUEST_GROSS_CONSOLE),
        "inr_per_request_net": float(INR_PER_REQUEST_NET_CONSOLE),
    }


def reverse_calibrate_meta() -> dict[str, Any]:
    india_inr_per_1k = (USD_PER_1K_PLACE_DETAILS_ENTERPRISE_INDIA * USD_TO_INR_INDICATIVE).quantize(
        Decimal("0.01")
    )
    return {
        "console_sku_id": CONSOLE_SKU_PLACE_DETAILS_ENTERPRISE_IN,
        "console_ref_usage_count": CONSOLE_REF_USAGE_COUNT,
        "console_ref_gross_inr": float(CONSOLE_REF_GROSS_INR),
        "console_ref_other_savings_inr": float(CONSOLE_REF_OTHER_SAVINGS_INR),
        "console_ref_net_subtotal_inr": float(CONSOLE_REF_NET_SUBTOTAL_INR),
        "other_savings_fraction": float(CONSOLE_OTHER_SAVINGS_FRACTION),
        "inr_per_request_gross": float(INR_PER_REQUEST_GROSS_CONSOLE),
        "inr_per_request_net": float(INR_PER_REQUEST_NET_CONSOLE),
        "console_ref_stage2_total": CONSOLE_REF_STAGE2_TOTAL,
        "console_ref_stage2_gross_inr": float(CONSOLE_REF_STAGE2_GROSS_INR),
        "console_ref_stage2_billable": CONSOLE_REF_STAGE2_BILLABLE,
        "inr_per_stage2_gross_console": float(INR_PER_STAGE2_GROSS_CONSOLE),
        "inr_per_1k_stage2_billable_console": float(INR_PER_1K_STAGE2_BILLABLE_CONSOLE),
        "india_list_inr_per_1k_stage2_billable": float(india_inr_per_1k),
        "formula_gross": "month_stage2 × (₹37,522.52 / 72,670)",
        "formula_net": "usage_cost × (1 − 28,569/37,522.52) ≈ month_stage2 × ₹0.123/request",
        "console_net_note": (
            "Console *usage cost* is gross INR per SKU; *Other savings* reduces *subtotal*. "
            "Ops throttle (USD billable-after-caps) is separate from this invoice."
        ),
    }


def estimated_india_gross_list_usd(*, stage1: int, stage2: int) -> Decimal:
    """All metered events at India list price — approximates Console *usage cost* before savings/credits."""
    s1 = max(0, int(stage1 or 0))
    s2 = max(0, int(stage2 or 0))
    return (
        (USD_PER_1K_TEXT_SEARCH_PRO_INDIA * Decimal(s1) / Decimal(1000))
        + (USD_PER_1K_PLACE_DETAILS_ENTERPRISE_INDIA * Decimal(s2) / Decimal(1000))
    ).quantize(Decimal("0.01"))


def estimated_india_gross_list_inr(*, stage1: int, stage2: int) -> Decimal:
    return (estimated_india_gross_list_usd(stage1=stage1, stage2=stage2) * USD_TO_INR_INDICATIVE).quantize(
        Decimal("0.01")
    )


def marginal_billable_usd_per_request(
    *, stage1: int, stage2: int, extra_requests: int
) -> float:
    """Average marginal USD for ``extra_requests`` (Dealer Finder is stage-2 heavy)."""
    extra = max(0, int(extra_requests))
    if extra <= 0:
        return 0.0
    before = float(estimated_billable_usd(stage1=stage1, stage2=stage2))
    after = float(
        estimated_billable_usd(
            stage1=stage1,
            stage2=stage2 + extra,
        )
    )
    return (after - before) / extra


def estimate_billable_usd_from_total_requests(total: int) -> Decimal:
    """Approximate monthly billable when only total API count is known (~12% stage-1)."""
    n = max(0, int(total))
    if n <= 0:
        return Decimal("0")
    s1 = max(0, int(round(n * 0.12)))
    if s1 > n:
        s1 = n
    return estimated_billable_usd(stage1=s1, stage2=n - s1)


def india_pricing_meta() -> dict[str, Any]:
    return {
        "region": "IN",
        "model": "console_sku_reverse_calibrated",
        "source_of_truth": "Google Cloud Console billing (INR)",
        "stage1_sku": "Places API Text Search Pro (India)",
        "stage1_free_monthly": FREE_TEXT_SEARCH_PRO_INDIA_MONTH,
        "stage1_usd_per_1k": float(USD_PER_1K_TEXT_SEARCH_PRO_INDIA),
        "stage2_sku": CONSOLE_SKU_PLACE_DETAILS_ENTERPRISE_NAME,
        "stage2_sku_id": CONSOLE_SKU_PLACE_DETAILS_ENTERPRISE_IN,
        "stage2_free_monthly": FREE_PLACE_DETAILS_ENTERPRISE_INDIA_MONTH,
        "stage2_usd_per_1k": float(USD_PER_1K_PLACE_DETAILS_ENTERPRISE_INDIA),
        "usd_to_inr_indicative": float(USD_TO_INR_INDICATIVE),
        "console_calibration": reverse_calibrate_meta(),
    }