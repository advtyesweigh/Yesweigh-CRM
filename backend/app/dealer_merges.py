"""Fold duplicate Zoho dealer contacts into one CRM survivor for sales totals.

Sales stay on their original zoho_sales.customer_id rows; aggregations remap
`from` → `to` so list totals / Top Dealers / sales-by-state show one bucket.
Hidden `from` dealers stay in DB but are excluded from lists via exclusions.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import case, literal

# from Zoho contact id → survivor Zoho contact id
DEALER_SALES_MERGES: dict[str, str] = {
    # ELMASS SYSTEMS & SERVICES → ELMASS' Weighing & Automation Pvt. Ltd
    "99381000000084733": "99381000005074017",
    # Venkatachalam → VENKATACHALAM & CO (same phone 9655569000, Coimbatore/TN)
    "99381000000747828": "99381000014693003",
}

# Survivor display name (optional; list still uses dealers table row)
DEALER_MERGE_SURVIVOR_NAMES: dict[str, str] = {
    "99381000005074017": "ELMASS' Weighing & Automation Pvt. Ltd",
    "99381000014693003": "VENKATACHALAM & CO",
}


def merged_from_ids(survivor_id: str) -> list[str]:
    """Zoho ids whose sales fold into this survivor (excludes the survivor itself)."""
    sid = (survivor_id or "").strip()
    return [src for src, dst in DEALER_SALES_MERGES.items() if dst == sid]


def sales_customer_ids_for_dealer(zoho_contact_id: str) -> list[str]:
    """Ids to sum when reading lifetime sales for a visible dealer row."""
    zid = (zoho_contact_id or "").strip()
    if not zid:
        return []
    ids = [zid, *merged_from_ids(zid)]
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def remapped_customer_id_expr(customer_id_col: Any) -> Any:
    """SQL CASE: map merge sources onto their survivor contact id."""
    if not DEALER_SALES_MERGES:
        return customer_id_col
    whens = [
        (customer_id_col == src, literal(dst))
        for src, dst in DEALER_SALES_MERGES.items()
    ]
    return case(*whens, else_=customer_id_col)
