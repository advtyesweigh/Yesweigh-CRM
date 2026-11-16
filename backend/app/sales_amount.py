"""Canonical tax-exclusive sales amount for zoho_sales aggregations.

Prefer ``sub_total`` (Zoho / CSV SubTotal, excl. GST). Fall back to ``total``
only when sub_total is missing (legacy rows before backfill).
"""
from __future__ import annotations

from sqlalchemy import func

from app.models import ZohoSale


def sales_amount_expr():
    """SQL expression: COALESCE(sub_total, total)."""
    return func.coalesce(ZohoSale.sub_total, ZohoSale.total)
