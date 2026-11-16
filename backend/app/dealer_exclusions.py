"""Durable dealer hide/skip rules — not real CRM dealers.

Used by list API (always hidden) and Zoho import (never recreated as dealers).
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import or_

from app.dealer_merges import DEALER_SALES_MERGES

# Exact names (case-insensitive)
EXCLUDED_EXACT_NAMES: frozenset[str] = frozenset(
    {
        "shanghai yousheng weighing apparatus co.ltd.",
        "shanghai yousheng weighing apparatus co. ltd.",
        # Merged into ELMASS' Weighing & Automation Pvt. Ltd (sales fold via dealer_merges)
        "elmass systems & services",
        # Merged into VENKATACHALAM & CO (sales fold via dealer_merges)
        "venkatachalam",
    }
)

# Prefixes (case-insensitive): name or company starts with these
EXCLUDED_NAME_PREFIXES: tuple[str, ...] = (
    "retail ",
    "yescloud",
)

# Known junk Zoho contact ids (Retail* + Shanghai) + merge sources (hidden survivors keep sales)
EXCLUDED_ZOHO_CONTACT_IDS: frozenset[str] = frozenset(
    {
        "99381000030380018",
        "99381000030380004",
        "99381000019875126",
        "99381000030360001",
        "99381000030584003",
        "99381000030380032",
        "99381000030380025",
        "99381000030380011",
        "99381000005368943",
        "99381000001897085",
        # ELMASS SYSTEMS & SERVICES (merged → 99381000005074017)
        *DEALER_SALES_MERGES.keys(),
    }
)

_PREFIX_RE = re.compile(
    r"^(?:" + "|".join(re.escape(p) for p in EXCLUDED_NAME_PREFIXES) + r")",
    re.IGNORECASE,
)


def is_excluded_dealer_name(name: str = "", company: str = "") -> bool:
    for raw in (name or "", company or ""):
        s = raw.strip()
        if not s:
            continue
        if s.casefold() in EXCLUDED_EXACT_NAMES:
            return True
        if _PREFIX_RE.match(s):
            return True
    return False


def is_excluded_zoho_id(zoho_contact_id: str = "") -> bool:
    return (zoho_contact_id or "").strip() in EXCLUDED_ZOHO_CONTACT_IDS


def is_excluded_contact(
    contact: dict[str, Any] | None = None,
    *,
    name: str = "",
    company: str = "",
    zoho_id: str = "",
) -> bool:
    if contact:
        name = name or str(contact.get("contact_name") or contact.get("customer_name") or "")
        company = company or str(contact.get("company_name") or "")
        zoho_id = zoho_id or str(contact.get("contact_id") or contact.get("customer_id") or "")
    if is_excluded_zoho_id(zoho_id):
        return True
    return is_excluded_dealer_name(name, company)


def dealer_not_excluded_clause(
    name_col: Any,
    company_col: Any,
    zoho_id_col: Any | None = None,
) -> Any:
    """SQLAlchemy WHERE: dealer is NOT in the exclusion set."""
    clauses: list[Any] = []
    for exact in EXCLUDED_EXACT_NAMES:
        clauses.append(name_col.ilike(exact))
        clauses.append(company_col.ilike(exact))
    for prefix in EXCLUDED_NAME_PREFIXES:
        like = f"{prefix}%"
        clauses.append(name_col.ilike(like))
        clauses.append(company_col.ilike(like))
    if zoho_id_col is not None:
        for zid in EXCLUDED_ZOHO_CONTACT_IDS:
            clauses.append(zoho_id_col == zid)
    return ~or_(*clauses)
