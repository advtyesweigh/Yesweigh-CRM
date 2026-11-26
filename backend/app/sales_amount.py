"""Canonical tax-exclusive sales amount for zoho_sales aggregations.

Prefer ``sub_total`` (Zoho / CSV SubTotal, excl. GST). Fall back to ``total``
only when sub_total is missing (legacy rows before backfill).
"""
from __future__ import annotations

from sqlalchemy import func, or_

from app.models import ZohoSale

# Statuses excluded from lifetime / dealer-list sales (matches dashboard Top Dealers).
_NON_COUNTABLE_STATUSES = ("void", "cancelled", "canceled", "draft", "deleted")


def sales_amount_expr():
    """SQL expression: COALESCE(sub_total, total)."""
    return func.coalesce(ZohoSale.sub_total, ZohoSale.total)


def zoho_sale_countable_clause():
    """Only invoice rows that represent real sales (not void/draft/cancelled)."""
    status = func.lower(func.coalesce(ZohoSale.status, ""))
    return (
        ZohoSale.document_type == "invoice",
        or_(status == "", ~status.in_(list(_NON_COUNTABLE_STATUSES))),
    )


def sale_status_excluded(status: str | None) -> bool:
    """True for void / cancelled / draft / deleted (and US spelling canceled)."""
    return (status or "").strip().lower() in _NON_COUNTABLE_STATUSES


def is_countable_invoice(document_type: str | None, status: str | None) -> bool:
    """Python twin of ``zoho_sale_countable_clause`` for tests and funnel rows."""
    if (document_type or "").strip().lower() != "invoice":
        return False
    return not sale_status_excluded(status)
