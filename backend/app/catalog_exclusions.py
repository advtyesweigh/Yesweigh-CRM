"""Product categories excluded from CRM catalog sync + Products UI.

These were removed from the live YesOne catalog / CRM Products grid and must
never be re-inserted by `sync_firebase_catalog`. Matching is case-insensitive.

Sales-by-Items-Qty uses a stricter allowlist (`is_sales_qty_category`): hardware
Products-page categories only. SOFTWARE KEYS stay visible on Products but are
excluded from unit sales totals / qty rankings.
"""
from __future__ import annotations

# Keep in sync with excluded_category_display_names() below.
EXCLUDED_CATEGORY_NAMES: frozenset[str] = frozenset(
    {
        "generic spare parts",
        "stamping gj",
        "stamping kl",
        "inactive",
    }
)

# Hardware categories on the Products page — used for Sales by Items Qty /
# Analytics Total Units / top-product-by-qty. Case-insensitive exact match.
SALES_QTY_CATEGORY_NAMES: frozenset[str] = frozenset(
    {
        "weighing scale import",
        "bill printing scales",
        "weighing scales india",
        "analytical scales",
        "industrial weighing scale",
        "indicators & ccm",
        "touch pos systems",
        "printers & scanners",
    }
)


def is_excluded_category(name: str) -> bool:
    return name.strip().lower() in EXCLUDED_CATEGORY_NAMES


def is_sales_qty_category(name: str) -> bool:
    """True when category is a hardware Products-page bucket for unit sales."""
    return name.strip().lower() in SALES_QTY_CATEGORY_NAMES


def excluded_category_display_names() -> tuple[str, ...]:
    """Title-case-ish labels for SQL wipe / logging (match known DB names)."""
    return (
        "GENERIC SPARE PARTS",
        "STAMPING GJ",
        "STAMPING KL",
        "INACTIVE",
    )
