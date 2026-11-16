"""Map Zoho invoice item names / categories → CRM dashboard product buckets."""
from __future__ import annotations

import re

# Canonical buckets used by Sales by Value / Sales by Items Qty widgets.
PRODUCT_CATEGORIES = (
    "Bench Scale",
    "Label Scale",
    "Platform Scale",
    "Price Computing",
    "POS / Billing",
    "Others",
)

# Preferred catalog product names (substring, case-insensitive) for dashboard thumbnails.
# First match with an image wins per sales bucket.
CATEGORY_IMAGE_PREFERENCES: dict[str, tuple[str, ...]] = {
    "Price Computing": (
        "electronic  cash scale q8",
        "bill  printing scale q7",
        "ai pos scale",
        "bill printing scale",
        "cash scale",
    ),
    "Bench Scale": (
        "bench scale ad f",
        "bench scale pro",
        "bench scale",
    ),
    "Label Scale": (
        "label printing scale q9",
        "label printing scale q10",
        "label printing scale",
        "ai barcode label scale",
    ),
    "Platform Scale": (
        "platform scale 40x40",
        "electronic platform scale",
        "platform scale",
        "pallet scale",
    ),
    "POS / Billing": (
        "all in one pos 14",
        "sunmi d2",
        "pos terminal 15.6",
        "pos terminal",
        "touch pos",
    ),
    "Others": (
        "electronic weighing scale dj600",
        "jwellery scale",
        "analytical",
        "hanging weighing scale",
    ),
}

_SKIP_STATUSES = frozenset({"void", "cancelled", "canceled", "draft"})


def is_skipped_invoice_status(status: str) -> bool:
    return (status or "").strip().lower() in _SKIP_STATUSES


def map_product_category(item_name: str, zoho_category: str = "") -> str:
    """Heuristic map from Zoho Item Name + Category Name → dashboard bucket."""
    n = (item_name or "").lower()
    z = (zoho_category or "").lower()

    if any(
        k in n
        for k in (
            "label printing",
            "label scale",
            "q9lbl",
            "lbl printing",
            "label indicator",
        )
    ):
        return "Label Scale"
    if "label" in n and "scale" in n:
        return "Label Scale"

    if re.search(
        r"bench\s*scale|platform\s*scale\s*-?\s*bench|bench\s*pro|economic\s*bench|\bbench\b",
        n,
    ):
        return "Bench Scale"
    if re.search(r"\bqz[12]\b", n) and "platform" not in n:
        return "Bench Scale"

    if any(
        k in n
        for k in ("platform scale", "floor scale", "pallet scale", "portable scale")
    ) or re.search(r"platform\s*\d", n):
        return "Platform Scale"
    if "platform" in n and "scale" in n:
        return "Platform Scale"

    if any(
        k in n
        for k in (
            "pos terminal",
            "pos system",
            "touch pos",
            "cash register",
            "sunmi",
            "spos",
            "scangle",
            "ecs cloud",
            "yes cloud",
            "cloud recharge",
            "software key",
            "pos key",
            "billing software",
        )
    ):
        return "POS / Billing"
    if z in ("touch pos systems", "software keys", "printers & scanners"):
        return "POS / Billing"

    if any(
        k in n
        for k in (
            "bill printing",
            "cash scale",
            "table top",
            "price comput",
            "atm pc",
            "electronic cash",
            "pc (",
            "chottu",
            "vibrant",
        )
    ):
        return "Price Computing"
    if z in ("bill printing scales", "weighing scales india") and "platform" not in n and "bench" not in n:
        return "Price Computing"
    if z == "weighing scale import" and any(k in n for k in ("table top", "pc ", "atm")):
        return "Price Computing"

    return "Others"
