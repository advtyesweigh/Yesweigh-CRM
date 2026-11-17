"""Secure read-only CRM tools for the AI agent (Phase 1 + dealer sales)."""
from __future__ import annotations

import json
import re
import time
from calendar import monthrange
from datetime import date
from typing import Any, Callable, Awaitable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.provider import ToolSpec
from app.dealer_exclusions import dealer_not_excluded_clause
from app.dealer_merges import remapped_customer_id_expr, sales_customer_ids_for_dealer
from app.kerala_district_order import (
    kerala_district_city_match_terms,
    resolve_kerala_district_query,
)
from app.models import (
    AiToolCall,
    CatalogProduct,
    Dealer,
    Employee,
    User,
    WhatsAppConversation,
    WhatsAppInstance,
    WhatsAppInstanceEmployee,
    ZohoCreditNote,
    ZohoDealer,
    ZohoInvoiceLineItem,
    ZohoSale,
)
from app import zoho_service as zs
from app.sales_amount import sales_amount_expr

ORG = "yesweigh"
_DEALER_SEARCH_LIMIT = 25
_DEALER_LIST_LIMIT = 1500
_QUERY_ROW_LIMIT = 100


def _is_admin(user: User) -> bool:
    return user.role.lower() in {"admin", "super_admin", "super admin", "management"}


async def employee_for_user(db: AsyncSession, user: User) -> Employee | None:
    return (
        await db.execute(select(Employee).where(or_(Employee.user_id == user.id, Employee.email == user.email)))
    ).scalar_one_or_none()


async def authorized_instance_ids(db: AsyncSession, user: User) -> list[int] | None:
    """None = all YesWeigh instances (admin). Else allowed instance PKs."""
    if _is_admin(user):
        return None
    emp = await employee_for_user(db, user)
    if not emp:
        return []
    rows = (
        await db.execute(
            select(WhatsAppInstanceEmployee.whatsapp_instance_id).where(
                WhatsAppInstanceEmployee.employee_id == emp.id,
                WhatsAppInstanceEmployee.is_active.is_(True),
                WhatsAppInstanceEmployee.organization_id == ORG,
            )
        )
    ).scalars().all()
    return list(rows)


def resolve_sales_period(args: dict[str, Any]) -> dict[str, str]:
    """Public alias for generated tools — Indian FY / MTD / custom bounds."""
    return _resolve_sales_period(args)


def _builtin_tool_specs() -> list[ToolSpec]:
    specs = [
        ToolSpec(
            name="get_sales_summary",
            description=(
                "Return verified YesWeigh company-wide sales summary from Zoho-synced invoices: "
                "this month MTD, last month same-day window, Indian FY YTD, and prior FY YTD. "
                "Amounts are tax-exclusive (sub_total). Never invent figures. "
                "For a single dealer/customer use get_dealer_sales. "
                "For district/area/DT or state totals use get_region_sales."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {},
            },
        ),
        ToolSpec(
            name="search_dealers",
            description=(
                "Fuzzy/partial search of CRM dealers by name or company "
                "(e.g. 'meezan', 'Meezan Electronics'). Returns top candidates with city/state/phone. "
                "Use before or instead of get_dealer_sales when the dealer name is unclear. "
                "Do NOT use for full state/district contact or WhatsApp lists — use "
                "query_business_data dataset=dealers with state=…"
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "query": {
                        "type": "STRING",
                        "description": "Partial or full dealer/company name to search.",
                    },
                    "limit": {
                        "type": "INTEGER",
                        "description": "Max candidates to return (1–50, default 25).",
                    },
                },
                "required": ["query"],
            },
        ),
        ToolSpec(
            name="get_dealer_sales",
            description=(
                "Return verified tax-exclusive sales totals for one dealer/customer from zoho_sales. "
                "Resolve by partial name (query) or exact customer_id / dealer_id. "
                "Periods: fy_ytd (Indian FY Apr–Mar / 'this year', default), mtd, or custom (date_from/date_to). "
                "If multiple dealers match the name, returns candidates only — do not invent a single total. "
                "Do NOT use this for district/area/DT sales — use get_region_sales instead."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "query": {
                        "type": "STRING",
                        "description": "Partial or full dealer/customer name (e.g. 'Meezan Electronics').",
                    },
                    "customer_id": {
                        "type": "STRING",
                        "description": "Zoho contact/customer id when known from a prior search.",
                    },
                    "dealer_id": {
                        "type": "INTEGER",
                        "description": "CRM dealers.id when known from a prior search.",
                    },
                    "period": {
                        "type": "STRING",
                        "description": "fy_ytd | mtd | custom. Default fy_ytd (Indian financial year to date).",
                    },
                    "date_from": {
                        "type": "STRING",
                        "description": "YYYY-MM-DD inclusive start when period=custom.",
                    },
                    "date_to": {
                        "type": "STRING",
                        "description": "YYYY-MM-DD inclusive end when period=custom.",
                    },
                },
            },
        ),
        ToolSpec(
            name="get_region_sales",
            description=(
                "Return verified tax-exclusive sales totals for a district/area/DT or Indian state "
                "(e.g. 'malappuram dt', 'Malappuram', 'Ernakulam district', 'Kerala'). "
                "Aggregates zoho_sales for all matching dealers in one call — never loop search_dealers "
                "or get_dealer_sales per dealer for region queries. "
                "Periods: fy_ytd (default), mtd, or custom. Amounts are tax-exclusive (sub_total)."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "query": {
                        "type": "STRING",
                        "description": (
                            "District, area, DT, or state name "
                            "(e.g. 'malappuram dt', 'Malappuram', 'Kerala')."
                        ),
                    },
                    "period": {
                        "type": "STRING",
                        "description": "fy_ytd | mtd | custom. Default fy_ytd (Indian financial year to date).",
                    },
                    "date_from": {
                        "type": "STRING",
                        "description": "YYYY-MM-DD inclusive start when period=custom.",
                    },
                    "date_to": {
                        "type": "STRING",
                        "description": "YYYY-MM-DD inclusive end when period=custom.",
                    },
                },
                "required": ["query"],
            },
        ),
        ToolSpec(
            name="get_product_sales_by_dealer",
            description=(
                "Product / SKU / category sales broken down by dealer from zoho_invoice_line_items. "
                "Use for questions like 'label print scale sales by dealer this year', "
                "'Q9LBL sales by dealer', 'bench scale FY by customer'. "
                "product_query matches item_name and product_category (e.g. 'label print scale', "
                "'Label Scale', 'platform scale'). "
                "Optional dealer_query narrows to one dealer; otherwise returns top dealers by "
                "quantity (pcs) descending (default). Use sort_by=amount only when the user "
                "explicitly asks for top by ₹ value. "
                "Periods: fy_ytd (default / this year), mtd, custom. Amounts = line item_total (tax-excl)."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "product_query": {
                        "type": "STRING",
                        "description": (
                            "Product, SKU, or category text "
                            "(e.g. 'label print scale', 'Label Scale', 'Q9LBL')."
                        ),
                    },
                    "dealer_query": {
                        "type": "STRING",
                        "description": "Optional dealer/customer name to filter to one dealer.",
                    },
                    "period": {
                        "type": "STRING",
                        "description": "fy_ytd | mtd | custom. Default fy_ytd (Indian FY to date).",
                    },
                    "date_from": {
                        "type": "STRING",
                        "description": "YYYY-MM-DD inclusive start when period=custom.",
                    },
                    "date_to": {
                        "type": "STRING",
                        "description": "YYYY-MM-DD inclusive end when period=custom.",
                    },
                    "limit": {
                        "type": "INTEGER",
                        "description": "Max dealers to return (1–100, default 50).",
                    },
                    "sort_by": {
                        "type": "STRING",
                        "description": (
                            "quantity (default) | amount. Default ranks dealers by pcs sold "
                            "desc (₹ as tiebreak). Use amount only when user asks for top by value."
                        ),
                    },
                },
                "required": ["product_query"],
            },
        ),
        ToolSpec(
            name="query_business_data",
            description=(
                "Flexible read-only CRM analytics for business questions not covered by a "
                "specialized tool. Pick a dataset and optional filters/group_by. "
                "Datasets: invoice_lines (product/SKU line sales), zoho_sales (invoice totals), "
                "dealers (full contact/WhatsApp lists by state/city — returns Dealer, City, State, Phone), "
                "credit_notes, whatsapp, catalog, employees (admin). "
                "For 'all Kerala dealers WhatsApp/phone list' → dataset=dealers, state=Kerala "
                "(omit limit or use a high limit; server returns the complete filtered set up to 1500). "
                "CRM UI renders a dealer contact report card — reply with one short sentence only. "
                "Never invent figures. No writes. Prefer get_product_sales_by_dealer for "
                "product×dealer breakdowns, get_dealer_sales / get_region_sales / get_sales_summary "
                "when those fit better."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "dataset": {
                        "type": "STRING",
                        "description": (
                            "invoice_lines | zoho_sales | dealers | credit_notes | "
                            "whatsapp | catalog | employees"
                        ),
                    },
                    "product_query": {
                        "type": "STRING",
                        "description": "Filter products/SKUs/categories (invoice_lines / catalog).",
                    },
                    "dealer_query": {
                        "type": "STRING",
                        "description": "Filter by dealer/customer name.",
                    },
                    "city": {"type": "STRING", "description": "Filter dealers.city / region."},
                    "state": {
                        "type": "STRING",
                        "description": (
                            "Filter dealers.state (e.g. Kerala, Tamil Nadu). "
                            "Required for full-state contact / WhatsApp lists."
                        ),
                    },
                    "period": {
                        "type": "STRING",
                        "description": (
                            "fy_ytd | mtd | last_6_months | last_3_months | last_12_months | custom. "
                            "For 'last N months' prefer last_N_months (server computes dates from today) "
                            "instead of inventing custom date_from/date_to."
                        ),
                    },
                    "date_from": {"type": "STRING", "description": "YYYY-MM-DD when period=custom."},
                    "date_to": {"type": "STRING", "description": "YYYY-MM-DD when period=custom."},
                    "group_by": {
                        "type": "STRING",
                        "description": (
                            "dealer | product | category | city | state | month | none. "
                            "Default depends on dataset."
                        ),
                    },
                    "limit": {
                        "type": "INTEGER",
                        "description": (
                            "Max rows. Rankings default 50 (cap 100). "
                            "Dealer contact lists (dataset=dealers) default 1500 (cap 1500) "
                            "so full-state WhatsApp/phone lists are complete."
                        ),
                    },
                    "sort_by": {
                        "type": "STRING",
                        "description": (
                            "For invoice_lines×dealer: quantity (default) | amount. "
                            "Passed through to get_product_sales_by_dealer."
                        ),
                    },
                },
                "required": ["dataset"],
            },
        ),
        ToolSpec(
            name="get_unanswered_messages",
            description=(
                "List open WhatsApp conversations with unread messages (awaiting reply), "
                "scoped to the employee's authorized instances. Returns counts and top threads."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "limit": {
                        "type": "INTEGER",
                        "description": "Max conversations to return (1–50).",
                    }
                },
            },
        ),
        ToolSpec(
            name="get_whatsapp_connection_status",
            description=(
                "Summarize WhatsApp instance connection statuses for authorized YesWeigh CRM instances "
                "(Evolution GO). Does not expose API keys or tokens."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {},
            },
        ),
        ToolSpec(
            name="get_employee_whatsapp_instances",
            description=(
                "List employee WhatsApp instances registered in YesWeigh CRM (display name, employee, "
                "connection status, phone). Never returns Evolution credentials."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {},
            },
        ),
        ToolSpec(
            name="remember_fact",
            description=(
                "Persist a company fact, abbreviation, machine short name, alias, or report-type "
                "definition learned from the user into CRM knowledge (Postgres). "
                "Call when the user teaches something like 'MSB means …', 'short name for X is Y', "
                "or 'remember that …'. Upserts by kind+key. Confirm briefly after saving."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "kind": {
                        "type": "STRING",
                        "description": (
                            "alias | abbreviation | report_type | machine_short_name | fact. "
                            "Default fact."
                        ),
                    },
                    "key": {
                        "type": "STRING",
                        "description": "Short term / code to remember (e.g. MSB, Q9LBL).",
                    },
                    "value": {
                        "type": "STRING",
                        "description": "Full meaning or expansion.",
                    },
                    "notes": {
                        "type": "STRING",
                        "description": "Optional context.",
                    },
                },
                "required": ["key", "value"],
            },
        ),
        ToolSpec(
            name="search_knowledge",
            description=(
                "Search previously learned AI knowledge (abbreviations, machine short names, "
                "aliases, report types, facts) stored in CRM Postgres. Use when a short code "
                "or term is unclear and may have been taught in a prior chat."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "query": {
                        "type": "STRING",
                        "description": "Substring to match against key, value, or notes.",
                    },
                    "kind": {
                        "type": "STRING",
                        "description": "Optional filter: alias | abbreviation | report_type | machine_short_name | fact.",
                    },
                    "limit": {
                        "type": "INTEGER",
                        "description": "Max rows (1–50, default 20).",
                    },
                },
                "required": ["query"],
            },
        ),
        ToolSpec(
            name="search_vendors",
            description=(
                "Search vendors/suppliers. Tries CRM Postgres first; when SQL has no vendor data "
                "(usual — CRM syncs customers/dealers only), falls back to live Zoho Books API "
                "(contact_type=vendor). Use for supplier lookup by name/company/GST. Read-only."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "query": {
                        "type": "STRING",
                        "description": "Partial or full vendor/supplier/company name.",
                    },
                    "limit": {
                        "type": "INTEGER",
                        "description": "Max candidates (1–25, default 10).",
                    },
                },
                "required": ["query"],
            },
        ),
        ToolSpec(
            name="get_vendor",
            description=(
                "Get one vendor/supplier profile. SQL first, then live Zoho Books contact detail "
                "when not in Postgres. Pass vendor_id from search_vendors, or query=name when unique. "
                "Returns address, GST, phones, contact persons, payables. Read-only."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "vendor_id": {
                        "type": "STRING",
                        "description": "Zoho contact/vendor id from search_vendors.",
                    },
                    "query": {
                        "type": "STRING",
                        "description": "Vendor name when vendor_id unknown (must resolve uniquely).",
                    },
                },
            },
        ),
        ToolSpec(
            name="get_vendor_related",
            description=(
                "Related vendor/supplier documents from live Zoho Books when SQL lacks them: "
                "bills, purchase orders, and vendor payments. Pass vendor_id (preferred) or query. "
                "Use after search_vendors / get_vendor. Read-only — never invent amounts."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "vendor_id": {
                        "type": "STRING",
                        "description": "Zoho vendor/contact id.",
                    },
                    "query": {
                        "type": "STRING",
                        "description": "Vendor name if vendor_id unknown.",
                    },
                    "include": {
                        "type": "STRING",
                        "description": (
                            "Comma list: bills,purchaseorders,payments. "
                            "Default all three."
                        ),
                    },
                    "limit": {
                        "type": "INTEGER",
                        "description": "Max rows per document type (1–50, default 15).",
                    },
                },
            },
        ),
        ToolSpec(
            name="request_new_tool",
            description=(
                "Last resort ONLY for truly novel CRM capabilities that builtins cannot answer. "
                "NEVER use for product/SKU/category sales by dealer — call get_product_sales_by_dealer "
                "(or query_business_data dataset=invoice_lines) instead. "
                "NEVER use for vendors/suppliers — use search_vendors / get_vendor / get_vendor_related. "
                "Factory can take minutes and often times out. Admin/management only."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "capability": {
                        "type": "STRING",
                        "description": "What the new tool should do (one sentence).",
                    },
                    "user_question": {
                        "type": "STRING",
                        "description": "The user's original question to answer once the tool exists.",
                    },
                },
                "required": ["capability", "user_question"],
            },
        ),
    ]
    return specs


def tool_specs(*, include_factory_meta: bool = True) -> list[ToolSpec]:
    """Builtin + hot-loaded generated tools (+ optional request_new_tool meta)."""
    from app.ai.generated_tools.loader import generated_tool_specs, load_all_generated_tools
    from app.ai.tool_factory import tool_factory_enabled

    load_all_generated_tools()
    specs = _builtin_tool_specs()
    if not include_factory_meta or not tool_factory_enabled():
        specs = [s for s in specs if s.name != "request_new_tool"]
    # Generated tools override same-name builtins only if present
    generated = generated_tool_specs()
    names = {s.name for s in specs}
    for g in generated:
        if g.name in names:
            continue
        specs.append(g)
    return specs


def refresh_generated_handlers() -> None:
    """Merge generated tool handlers into TOOL_HANDLERS (idempotent)."""
    from app.ai.generated_tools.loader import list_generated_tools, load_all_generated_tools

    load_all_generated_tools(force=True)
    for tool in list_generated_tools():
        TOOL_HANDLERS[tool.name] = tool.handler


async def tool_remember_fact(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    """Persist learned glossary / fact into ai_knowledge (upsert by kind+key)."""
    from app.ai.knowledge import row_to_dict, upsert_knowledge

    key = str(args.get("key") or "").strip()
    value = str(args.get("value") or "").strip()
    if not key or not value:
        return {"ok": False, "error": "key and value are required"}
    kind = str(args.get("kind") or "fact")
    notes = str(args.get("notes") or "")
    session_id = args.get("_session_id")
    run_id = args.get("_run_id")
    try:
        sid = int(session_id) if session_id is not None else None
    except (TypeError, ValueError):
        sid = None
    try:
        rid = int(run_id) if run_id is not None else None
    except (TypeError, ValueError):
        rid = None
    row = await upsert_knowledge(
        db,
        kind=kind,
        key=key,
        value=value,
        notes=notes,
        source="tool",
        session_id=sid,
        run_id=rid,
    )
    return {"ok": True, "saved": row_to_dict(row), "message": f"Remembered [{row.kind}] {row.key} → {row.value}"}


async def tool_search_knowledge(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    """Search ai_knowledge glossary."""
    from app.ai.knowledge import row_to_dict, search_knowledge

    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    kind = str(args.get("kind") or "").strip() or None
    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    rows = await search_knowledge(db, query=query, kind=kind, limit=limit)
    return {
        "ok": True,
        "count": len(rows),
        "items": [row_to_dict(r) for r in rows],
    }


async def tool_request_new_tool(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    """Meta-tool: signals the agent loop to run the Cursor tool factory.

    Fast-path: product×dealer sales is already covered by get_product_sales_by_dealer —
    run that builtin instead of blocking on Cursor.
    """
    from app.ai.tool_factory import (
        capability_covered_by_builtin,
        extract_product_query_hint,
        is_factory_admin,
        tool_factory_enabled,
    )

    capability = str(args.get("capability") or "").strip()
    user_question = str(args.get("user_question") or "").strip()
    if not capability or not user_question:
        return {"ok": False, "factory": False, "error": "capability and user_question are required"}

    covered = capability_covered_by_builtin(capability=capability, user_question=user_question)
    if covered == "get_product_sales_by_dealer":
        product_query = extract_product_query_hint(capability, user_question)
        sales = await tool_get_product_sales_by_dealer(
            db,
            user,
            {
                "product_query": product_query,
                "period": "fy_ytd",
                "limit": 25,
            },
        )
        return {
            "ok": True,
            "factory": False,
            "redirected": "get_product_sales_by_dealer",
            "product_query": product_query,
            "sales": sales,
            "message": (
                "Used existing builtin get_product_sales_by_dealer — do not call request_new_tool. "
                "Answer the user from the sales payload."
            ),
        }

    if not tool_factory_enabled():
        return {
            "ok": False,
            "factory": False,
            "error": "Tool factory is disabled or CURSOR_FAISAL_API_KEY is missing",
        }
    if not is_factory_admin(user):
        return {
            "ok": False,
            "factory": False,
            "error": "Only admin/management can request new tools",
        }
    return {
        "ok": True,
        "factory": True,
        "pending": True,
        "capability": capability[:500],
        "user_question": user_question[:2000],
        "message": (
            "Tool factory will build a read-only CRM tool for this capability. "
            "Wait for registration, then retry the user question."
        ),
    }


def _month_bounds(y: int, m: int) -> tuple[str, str]:
    last = monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def _shift_month(y: int, m: int, delta: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) + delta
    return idx // 12, idx % 12 + 1


def _indian_fy_start_year(d: date) -> int:
    return d.year if d.month >= 4 else d.year - 1


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().casefold())


def _name_tokens(s: str, *, min_len: int = 2) -> list[str]:
    return [t for t in _norm_name(s).split(" ") if len(t) >= min_len]


def _token_close(a: str, b: str) -> bool:
    """Loose token equality: substring, shared prefix, or crude plural stem."""
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    if len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    stem_a, stem_b = a.rstrip("s"), b.rstrip("s")
    if len(stem_a) >= 4 and stem_a == stem_b:
        return True
    return False


def _dealer_match_score(query: str, name: str, company: str) -> int:
    """Higher is better: exact=100, starts-with=80, contains=50, all-tokens=45, partial-tokens=20."""
    q = _norm_name(query)
    if not q:
        return 0
    best = 0
    q_tokens = _name_tokens(q)
    for raw in (name, company):
        n = _norm_name(raw)
        if not n:
            continue
        if n == q:
            best = max(best, 100)
            continue
        if n.startswith(q) or q.startswith(n):
            best = max(best, 80)
            continue
        if q in n:
            best = max(best, 50)
            continue
        # Token overlap: "meezan electronics" ↔ "MEEZAN ELECTRONIC SCALES…"
        n_tokens = _name_tokens(n)
        if q_tokens and n_tokens:
            hits = sum(1 for qt in q_tokens if any(_token_close(qt, nt) for nt in n_tokens))
            if hits == len(q_tokens):
                best = max(best, 45)
            elif hits > 0 and hits >= max(1, len(q_tokens) - 1):
                best = max(best, 20)
    return best


def _dealer_candidate(d: Dealer, *, score: int | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "dealer_id": d.id,
        "name": d.name or "",
        "company": d.company or "",
        "customer_id": d.zoho_contact_id or "",
        "city": d.city or "",
        "state": d.state or "",
        "phone": d.phone or "",
    }
    if score is not None:
        item["match_score"] = score
    return item


async def _search_dealer_rows(
    db: AsyncSession,
    query: str,
    *,
    limit: int = _DEALER_SEARCH_LIMIT,
) -> list[tuple[Dealer, int]]:
    term = (query or "").strip()
    if not term:
        return []
    visible = dealer_not_excluded_clause(Dealer.name, Dealer.company, Dealer.zoho_contact_id)
    # Prefer token AND so "Meezan Electronics" hits "MEEZAN ELECTRONIC SCALES…"
    tokens = [t for t in re.split(r"\s+", term) if len(t) >= 3]
    if tokens:
        token_clauses = []
        for t in tokens:
            variants = _sql_token_variants(t)
            variant_ors = [
                or_(Dealer.name.ilike(f"%{v}%"), Dealer.company.ilike(f"%{v}%")) for v in variants
            ]
            token_clauses.append(or_(*variant_ors) if len(variant_ors) > 1 else variant_ors[0])
        name_filter = and_(*token_clauses)
    else:
        like = f"%{term}%"
        name_filter = or_(Dealer.name.ilike(like), Dealer.company.ilike(like))
    rows = (
        await db.execute(
            select(Dealer)
            .where(visible)
            .where(name_filter)
            .order_by(Dealer.name.asc())
            .limit(80)
        )
    ).scalars().all()
    scored = [(d, _dealer_match_score(term, d.name or "", d.company or "")) for d in rows]
    scored = [(d, s) for d, s in scored if s > 0]
    scored.sort(key=lambda x: (-x[1], (x[0].name or "").casefold()))
    return scored[: max(1, min(50, limit))]


def _resolve_sales_period(args: dict[str, Any]) -> dict[str, str]:
    """Resolve fy_ytd / mtd / last_N_months / custom into inclusive ISO date bounds + label."""
    today = date.today()
    period = str(args.get("period") or "fy_ytd").strip().lower().replace(" ", "_").replace("-", "_")
    if period in {"this_year", "fy", "financial_year", "ytd"}:
        period = "fy_ytd"
    if period in {"this_month", "month"}:
        period = "mtd"
    if period in {"last_6_months", "last6months", "past_6_months", "previous_6_months", "6_months"}:
        period = "last_6_months"

    # last_N_months (3/6/12) — prefer over model-invented custom years
    m = re.fullmatch(r"last_(\d{1,2})_months?", period)
    if m or period == "last_6_months":
        n = int(m.group(1)) if m else 6
        n = max(1, min(24, n))
        # Inclusive window ending today: go back (n-1) calendar months to the 1st
        y, mo = today.year, today.month
        mo -= n - 1
        while mo <= 0:
            mo += 12
            y -= 1
        start = date(y, mo, 1)
        return {
            "period": f"last_{n}_months",
            "label": f"Last {n} months ({start.isoformat()} → {today.isoformat()})",
            "from": start.isoformat(),
            "to": today.isoformat(),
        }

    if period == "custom":
        date_from = str(args.get("date_from") or "").strip()
        date_to = str(args.get("date_to") or "").strip() or today.isoformat()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_from or ""):
            raise ValueError("date_from must be YYYY-MM-DD when period=custom")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_to):
            raise ValueError("date_to must be YYYY-MM-DD when period=custom")
        if date_from > date_to:
            raise ValueError("date_from must be on or before date_to")
        return {
            "period": "custom",
            "label": f"{date_from} → {date_to}",
            "from": date_from,
            "to": date_to,
        }

    if period == "mtd":
        y, mth = today.year, today.month
        this_from, _ = _month_bounds(y, mth)
        return {
            "period": "mtd",
            "label": f"{today.strftime('%b %Y')} MTD",
            "from": this_from,
            "to": today.isoformat(),
        }

    # Default: Indian FY YTD
    fy_start_y = _indian_fy_start_year(today)
    return {
        "period": "fy_ytd",
        "label": f"FY {fy_start_y}-{str(fy_start_y + 1)[-2:]} YTD",
        "from": date(fy_start_y, 4, 1).isoformat(),
        "to": today.isoformat(),
    }


async def _sum_sales_for_customer_ids(
    db: AsyncSession,
    customer_ids: list[str],
    *,
    start: str,
    end: str,
) -> tuple[float, int]:
    ids = [c for c in customer_ids if (c or "").strip()]
    if not ids:
        return 0.0, 0
    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(sales_amount_expr()), 0),
                func.count(),
            ).where(
                ZohoSale.customer_id.in_(ids),
                ZohoSale.date != "",
                ZohoSale.date >= start,
                ZohoSale.date <= end,
            )
        )
    ).one()
    return round(float(row[0] or 0), 2), int(row[1] or 0)


async def _sum_sales_by_customer_name(
    db: AsyncSession,
    customer_name: str,
    *,
    start: str,
    end: str,
) -> tuple[float, int, list[dict[str, Any]]]:
    """Fallback when no dealer master row: match zoho_sales.customer_name."""
    _ = start, end
    term = (customer_name or "").strip()
    if not term:
        return 0.0, 0, []
    tokens = [t for t in re.split(r"\s+", term) if len(t) >= 3]
    if tokens:
        token_clauses = []
        for t in tokens:
            variants = _sql_token_variants(t)
            token_clauses.append(or_(*[ZohoSale.customer_name.ilike(f"%{v}%") for v in variants]))
        name_filter = and_(*token_clauses)
    else:
        name_filter = ZohoSale.customer_name.ilike(f"%{term}%")
    # Distinct name/id buckets that match
    name_rows = (
        await db.execute(
            select(
                ZohoSale.customer_id,
                ZohoSale.customer_name,
                func.count().label("n"),
            )
            .where(
                name_filter,
                ZohoSale.customer_id != "",
            )
            .group_by(ZohoSale.customer_id, ZohoSale.customer_name)
            .order_by(func.count().desc())
            .limit(20)
        )
    ).all()
    candidates: list[dict[str, Any]] = []
    for cid, cname, n in name_rows:
        score = _dealer_match_score(term, cname or "", "")
        if score <= 0:
            continue
        candidates.append(
            {
                "dealer_id": None,
                "name": cname or "",
                "company": "",
                "customer_id": cid or "",
                "city": "",
                "state": "",
                "phone": "",
                "match_score": score,
                "invoice_count_all_time": int(n or 0),
                "source": "zoho_sales.customer_name",
            }
        )
    candidates.sort(key=lambda c: (-int(c["match_score"]), -int(c["invoice_count_all_time"])))
    return 0.0, 0, candidates


def _sql_token_variants(token: str) -> list[str]:
    """ILIKE variants so 'Electronics' also hits 'ELECTRONIC'."""
    t = (token or "").strip()
    if not t:
        return []
    out = [t]
    stem = t.rstrip("sS")
    if stem != t and len(stem) >= 4:
        out.append(stem)
    return out


def _pick_unique_match(scored: list[tuple[Dealer, int]], query: str = "") -> Dealer | None:
    if not scored:
        return None
    if len(scored) == 1:
        return scored[0][0]
    # Unique exact match among results
    exact = [d for d, s in scored if s >= 100]
    if len(exact) == 1:
        return exact[0]
    # Multi-token queries only: unique top token/phrase match (avoid "meezan" auto-pick)
    q_tokens = _name_tokens(query, min_len=3)
    if len(q_tokens) >= 2 and scored[0][1] >= 45 and scored[0][1] > scored[1][1]:
        return scored[0][0]
    return None


async def tool_get_sales_summary(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    _ = user, args
    today = date.today()
    y, m, d = today.year, today.month, today.day
    this_from, _ = _month_bounds(y, m)
    this_to = today.isoformat()
    ly, lm = _shift_month(y, m, -1)
    last_from, _ = _month_bounds(ly, lm)
    last_dom = min(d, monthrange(ly, lm)[1])
    last_to = f"{ly:04d}-{lm:02d}-{last_dom:02d}"

    fy_start_y = _indian_fy_start_year(today)
    fy_from = date(fy_start_y, 4, 1).isoformat()
    fy_to = this_to
    fy_label = f"{fy_start_y}-{str(fy_start_y + 1)[-2:]}"
    prior_start_y = fy_start_y - 1
    prior_from = date(prior_start_y, 4, 1).isoformat()
    try:
        prior_to_d = date(today.year - 1, today.month, today.day)
    except ValueError:
        prior_to_d = date(today.year - 1, today.month, 28)
    prior_fy_end = date(prior_start_y + 1, 3, 31)
    if prior_to_d > prior_fy_end:
        prior_to_d = prior_fy_end
    prior_to = prior_to_d.isoformat()
    prior_label = f"{prior_start_y}-{str(prior_start_y + 1)[-2:]}"

    async def sum_range(start: str, end: str) -> tuple[float, int]:
        row = (
            await db.execute(
                select(
                    func.coalesce(func.sum(sales_amount_expr()), 0),
                    func.count(),
                ).where(
                    ZohoSale.date != "",
                    ZohoSale.date >= start,
                    ZohoSale.date <= end,
                )
            )
        ).one()
        return round(float(row[0] or 0), 2), int(row[1] or 0)

    this_total, this_n = await sum_range(this_from, this_to)
    last_total, last_n = await sum_range(last_from, last_to)
    fy_total, fy_n = await sum_range(fy_from, fy_to)
    prior_total, prior_n = await sum_range(prior_from, prior_to)
    yoy_pct = None
    if prior_total > 0:
        yoy_pct = round(100.0 * (fy_total - prior_total) / prior_total, 1)

    return {
        "as_of": this_to,
        "currency_note": "INR, tax-exclusive (Zoho sub_total)",
        "this_month_mtd": {"total": this_total, "invoice_count": this_n, "from": this_from, "to": this_to},
        "last_month_same_day": {
            "total": last_total,
            "invoice_count": last_n,
            "from": last_from,
            "to": last_to,
        },
        "fy_ytd": {"label": fy_label, "total": fy_total, "invoice_count": fy_n, "from": fy_from, "to": fy_to},
        "prior_fy_ytd": {
            "label": prior_label,
            "total": prior_total,
            "invoice_count": prior_n,
            "from": prior_from,
            "to": prior_to,
        },
        "yoy_pct": yoy_pct,
        "source": "zoho_sales",
    }


async def tool_search_dealers(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    _ = user
    query = str(args.get("query") or "").strip()
    limit = int(args.get("limit") or _DEALER_SEARCH_LIMIT)
    limit = max(1, min(50, limit))
    if not query:
        return {"query": "", "count": 0, "items": [], "note": "query is required"}
    scored = await _search_dealer_rows(db, query, limit=limit)
    items = [_dealer_candidate(d, score=s) for d, s in scored]
    return {
        "query": query,
        "count": len(items),
        "items": items,
        "note": "Pick customer_id or dealer_id, then call get_dealer_sales" if items else "No dealers matched",
    }


async def tool_get_dealer_sales(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    _ = user
    period_info = _resolve_sales_period(args)
    start, end = period_info["from"], period_info["to"]
    query = str(args.get("query") or "").strip()
    customer_id = str(args.get("customer_id") or "").strip()
    dealer_id_raw = args.get("dealer_id")
    dealer_id: int | None = None
    if dealer_id_raw is not None and str(dealer_id_raw).strip() != "":
        try:
            dealer_id = int(dealer_id_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("dealer_id must be an integer") from exc

    dealer: Dealer | None = None
    ambiguous: list[dict[str, Any]] = []

    if dealer_id is not None:
        dealer = await db.get(Dealer, dealer_id)
        if not dealer:
            return {
                "ok": False,
                "error": f"No dealer with id={dealer_id}",
                "period": period_info,
            }
    elif customer_id:
        dealer = (
            await db.execute(
                select(Dealer).where(Dealer.zoho_contact_id == customer_id).limit(1)
            )
        ).scalar_one_or_none()
        if not dealer:
            # Direct Zoho customer_id sales (no dealers row)
            total, n = await _sum_sales_for_customer_ids(
                db, sales_customer_ids_for_dealer(customer_id) or [customer_id], start=start, end=end
            )
            return {
                "ok": True,
                "resolved": True,
                "dealer": {
                    "dealer_id": None,
                    "name": "",
                    "company": "",
                    "customer_id": customer_id,
                    "city": "",
                    "state": "",
                    "phone": "",
                },
                "period": period_info,
                "total": total,
                "invoice_count": n,
                "currency_note": "INR, tax-exclusive (Zoho sub_total)",
                "source": "zoho_sales",
                "matched_customer_ids": sales_customer_ids_for_dealer(customer_id) or [customer_id],
            }
    elif query:
        scored = await _search_dealer_rows(db, query, limit=_DEALER_SEARCH_LIMIT)
        dealer = _pick_unique_match(scored, query)
        if dealer is None and scored:
            ambiguous = [_dealer_candidate(d, score=s) for d, s in scored]
        elif dealer is None:
            # Fallback: invoice customer_name match (not in dealers master)
            _, _, sales_cands = await _sum_sales_by_customer_name(db, query, start=start, end=end)
            if not sales_cands:
                return {
                    "ok": False,
                    "resolved": False,
                    "error": f"No dealer or customer matched '{query}'",
                    "period": period_info,
                    "candidates": [],
                }
            exact = [c for c in sales_cands if int(c.get("match_score") or 0) >= 100]
            unique = None
            q_tokens = _name_tokens(query, min_len=3)
            if len(sales_cands) == 1:
                unique = sales_cands[0]
            elif len(exact) == 1:
                unique = exact[0]
            elif (
                len(q_tokens) >= 2
                and int(sales_cands[0].get("match_score") or 0) >= 45
                and int(sales_cands[0].get("match_score") or 0)
                > int(sales_cands[1].get("match_score") or 0)
            ):
                unique = sales_cands[0]
            if unique is None:
                return {
                    "ok": True,
                    "resolved": False,
                    "ambiguous": True,
                    "message": (
                        f"Multiple customers matched '{query}'. "
                        "Ask the user which one, then call again with customer_id."
                    ),
                    "period": period_info,
                    "candidates": sales_cands[:_DEALER_SEARCH_LIMIT],
                    "total": None,
                }
            cid = str(unique.get("customer_id") or "")
            total, n = await _sum_sales_for_customer_ids(db, [cid], start=start, end=end)
            return {
                "ok": True,
                "resolved": True,
                "dealer": unique,
                "period": period_info,
                "total": total,
                "invoice_count": n,
                "currency_note": "INR, tax-exclusive (Zoho sub_total)",
                "source": "zoho_sales",
                "matched_customer_ids": [cid],
            }
    else:
        return {
            "ok": False,
            "error": "Provide query, customer_id, or dealer_id",
            "period": period_info,
        }

    if ambiguous:
        return {
            "ok": True,
            "resolved": False,
            "ambiguous": True,
            "message": (
                f"Multiple dealers matched '{query}'. "
                "Ask the user which one, then call again with customer_id or dealer_id."
            ),
            "period": period_info,
            "candidates": ambiguous,
            "total": None,
        }

    assert dealer is not None
    zid = (dealer.zoho_contact_id or "").strip()
    ids = sales_customer_ids_for_dealer(zid) if zid else []
    total, n = await _sum_sales_for_customer_ids(db, ids, start=start, end=end)
    return {
        "ok": True,
        "resolved": True,
        "dealer": _dealer_candidate(dealer),
        "period": period_info,
        "total": total,
        "invoice_count": n,
        "currency_note": "INR, tax-exclusive (Zoho sub_total)",
        "source": "zoho_sales",
        "matched_customer_ids": ids,
    }


_STATE_MATCH_ALIASES: dict[str, list[str]] = {
    "kerala": ["Kerala"],
    "tamilnadu": ["Tamil Nadu", "Tamilnadu"],
    "tamilnaduindia": ["Tamil Nadu", "Tamilnadu"],
    "karnataka": ["Karnataka"],
    "andhrapradesh": ["Andhra Pradesh"],
    "telangana": ["Telangana"],
    "odisha": ["Odisha", "Orissa"],
    "orissa": ["Odisha", "Orissa"],
    "maharashtra": ["Maharashtra"],
    "goa": ["Goa"],
    "gujarat": ["Gujarat"],
    "rajasthan": ["Rajasthan"],
    "delhi": ["Delhi", "NCT of Delhi"],
    "pondicherry": ["Puducherry", "Pondicherry"],
    "puducherry": ["Puducherry", "Pondicherry"],
}


def _strip_region_suffixes(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    cleaned = re.sub(r"(?i)[,\s]*(?:dt|dist\.?|district|area|state)\s*$", "", text)
    cleaned = re.sub(r"(?i)\b(?:dt|dist\.?|district|area)\b", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" ,.-")


def _resolve_state_query(raw: str) -> tuple[str, list[str]]:
    """Return (canonical_label, match_values) for Indian state queries, else ('', [])."""
    cleaned = _strip_region_suffixes(raw)
    if not cleaned:
        return "", []
    compact = "".join(ch for ch in cleaned.lower() if ch.isalnum())
    aliases = _STATE_MATCH_ALIASES.get(compact)
    if aliases:
        return aliases[0], list(aliases)
    # Exact-ish match against known alias labels
    for vals in _STATE_MATCH_ALIASES.values():
        for v in vals:
            if _norm_name(v) == _norm_name(cleaned):
                return vals[0], list(vals)
    return "", []


def _dealer_city_region_filter(terms: list[str]):
    """SQL filter: dealers.city matches any spelling or contains the district token."""
    clauses = []
    for term in terms:
        t = (term or "").strip()
        if len(t) < 3:
            continue
        like = f"%{t}%"
        clauses.append(Dealer.city.ilike(like))
    if not clauses:
        return None
    return or_(*clauses)


async def tool_get_region_sales(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    """Aggregate tax-exclusive sales for a district/area (dealers.city) or state."""
    _ = user
    period_info = _resolve_sales_period(args)
    start, end = period_info["from"], period_info["to"]
    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required", "period": period_info}

    scope = "district"
    region_label = ""
    city_filter = None
    state_filter = None
    match_terms: list[str] = []

    district = resolve_kerala_district_query(query)
    if district:
        region_label = district
        match_terms = kerala_district_city_match_terms(district)
        city_filter = _dealer_city_region_filter(match_terms)
        scope = "district"
    else:
        state_label, state_vals = _resolve_state_query(query)
        if state_label:
            region_label = state_label
            state_filter = or_(*[Dealer.state.ilike(v) for v in state_vals])
            scope = "state"
            match_terms = state_vals
        else:
            # Fallback: treat cleaned query as a city/district substring on dealers.city
            cleaned = _strip_region_suffixes(query) or query
            region_label = cleaned
            match_terms = [cleaned]
            city_filter = _dealer_city_region_filter(match_terms)
            scope = "district"

    if city_filter is None and state_filter is None:
        return {
            "ok": False,
            "resolved": False,
            "error": f"Could not resolve region from '{query}'",
            "period": period_info,
            "query": query,
        }

    visible = dealer_not_excluded_clause(Dealer.name, Dealer.company, Dealer.zoho_contact_id)
    remapped = remapped_customer_id_expr(ZohoSale.customer_id)

    dealer_q = (
        select(func.count(func.distinct(Dealer.id)))
        .where(visible)
        .where(Dealer.zoho_contact_id != "")
    )
    if city_filter is not None:
        dealer_q = dealer_q.where(city_filter)
    if state_filter is not None:
        dealer_q = dealer_q.where(state_filter)
    dealer_count = int((await db.execute(dealer_q)).scalar() or 0)

    city_q = (
        select(Dealer.city, func.count())
        .where(visible)
        .where(Dealer.city != "")
    )
    if city_filter is not None:
        city_q = city_q.where(city_filter)
    if state_filter is not None:
        city_q = city_q.where(state_filter)
    city_rows = (
        await db.execute(
            city_q.group_by(Dealer.city).order_by(func.count().desc()).limit(15)
        )
    ).all()
    matched_cities = [
        {"city": str(c or ""), "dealer_count": int(n or 0)} for c, n in city_rows if str(c or "").strip()
    ]

    sales_q = (
        select(
            func.coalesce(func.sum(sales_amount_expr()), 0),
            func.count(),
        )
        .select_from(ZohoSale)
        .join(Dealer, Dealer.zoho_contact_id == remapped)
        .where(visible)
        .where(ZohoSale.customer_id != "")
        .where(ZohoSale.date != "")
        .where(ZohoSale.date >= start)
        .where(ZohoSale.date <= end)
    )
    if city_filter is not None:
        sales_q = sales_q.where(city_filter)
    if state_filter is not None:
        sales_q = sales_q.where(state_filter)

    total_raw, invoice_n = (await db.execute(sales_q)).one()
    total = round(float(total_raw or 0), 2)

    return {
        "ok": True,
        "resolved": True,
        "query": query,
        "scope": scope,
        "region": region_label,
        "match_terms": match_terms[:20],
        "matched_cities": matched_cities,
        "dealer_count": dealer_count,
        "period": period_info,
        "total": total,
        "invoice_count": int(invoice_n or 0),
        "currency_note": "INR, tax-exclusive (Zoho sub_total)",
        "source": "zoho_sales via dealers.city/state",
        "note": (
            "CRM stores district/area on dealers.city (UI District filter). "
            "Town rows (e.g. Manjeri) fold into Kerala districts when known."
        ),
    }


_PRODUCT_STOPWORDS = frozenset(
    {
        "this",
        "year",
        "sales",
        "sale",
        "slaes",
        "sum",
        "total",
        "by",
        "dealer",
        "dealers",
        "customer",
        "customers",
        "the",
        "for",
        "with",
        "and",
        "from",
        "show",
        "give",
        "me",
        "please",
        "fy",
        "ytd",
        "mtd",
        "month",
        "of",
        "all",
        "any",
    }
)


def _line_item_active_clause():
    """Exclude void/draft/cancelled invoice lines when status is set."""
    status = func.lower(func.coalesce(ZohoInvoiceLineItem.invoice_status, ""))
    return or_(
        status == "",
        ~status.in_(list({"void", "cancelled", "canceled", "draft", "deleted"})),
    )


def _product_filter_clause(product_query: str):
    """Match invoice line items by category shortcuts or token AND on name/category."""
    raw = (product_query or "").strip()
    if not raw:
        return None
    q = raw.casefold()
    # Label printing scales (common CRM ask)
    if "label" in q and any(k in q for k in ("print", "printing", "lbl", "scale", "q9")):
        return or_(
            ZohoInvoiceLineItem.product_category.ilike("Label Scale"),
            ZohoInvoiceLineItem.item_name.ilike("%label%print%"),
            ZohoInvoiceLineItem.item_name.ilike("%label printing%"),
            ZohoInvoiceLineItem.item_name.ilike("%q9lbl%"),
            ZohoInvoiceLineItem.item_name.ilike("%lbl%"),
        )
    # Exact-ish known categories
    for cat in (
        "Label Scale",
        "Bench Scale",
        "Platform Scale",
        "Price Computing",
        "POS / Billing",
    ):
        if q == cat.casefold() or cat.casefold() in q:
            return ZohoInvoiceLineItem.product_category.ilike(cat)

    tokens = [
        t
        for t in re.split(r"[\s,/|+_-]+", raw)
        if len(t) >= 2 and t.casefold() not in _PRODUCT_STOPWORDS
    ]
    if not tokens:
        like = f"%{raw}%"
        return or_(
            ZohoInvoiceLineItem.item_name.ilike(like),
            ZohoInvoiceLineItem.product_category.ilike(like),
            ZohoInvoiceLineItem.product_id.ilike(like),
        )
    clauses = []
    for t in tokens[:6]:
        variants = _sql_token_variants(t)
        ors = []
        for v in variants:
            like = f"%{v}%"
            ors.extend(
                [
                    ZohoInvoiceLineItem.item_name.ilike(like),
                    ZohoInvoiceLineItem.product_category.ilike(like),
                    ZohoInvoiceLineItem.product_id.ilike(like),
                ]
            )
        clauses.append(or_(*ors))
    return and_(*clauses) if clauses else None


async def tool_get_product_sales_by_dealer(
    db: AsyncSession, user: User, args: dict[str, Any]
) -> dict[str, Any]:
    """Sum invoice line items for a product/SKU/category, grouped by dealer."""
    _ = user
    product_query = str(args.get("product_query") or "").strip()
    if not product_query:
        return {"ok": False, "error": "product_query is required"}

    period_info = _resolve_sales_period(args)
    start, end = period_info["from"], period_info["to"]
    limit = max(1, min(_QUERY_ROW_LIMIT, int(args.get("limit") or 50)))
    dealer_query = str(args.get("dealer_query") or "").strip()
    sort_raw = str(args.get("sort_by") or "quantity").strip().lower()
    sort_by = "amount" if sort_raw in {"amount", "value", "total", "rupees", "₹", "inr"} else "quantity"

    product_filter = _product_filter_clause(product_query)
    if product_filter is None:
        return {"ok": False, "error": "Could not build product filter", "product_query": product_query}

    base = [
        ZohoInvoiceLineItem.invoice_date != "",
        ZohoInvoiceLineItem.invoice_date >= start,
        ZohoInvoiceLineItem.invoice_date <= end,
        _line_item_active_clause(),
        product_filter,
    ]

    if dealer_query:
        scored = await _search_dealer_rows(db, dealer_query, limit=8)
        pick = _pick_unique_match(scored, dealer_query)
        if pick is None and scored:
            return {
                "ok": True,
                "resolved": False,
                "ambiguous": True,
                "product_query": product_query,
                "period": period_info,
                "candidates": [_dealer_candidate(d, score=s) for d, s in scored[:8]],
                "note": "Multiple dealers matched — pick customer_id/dealer and retry with dealer_query or ask user.",
            }
        if pick is not None:
            ids = sales_customer_ids_for_dealer(pick.zoho_contact_id or "") or (
                [pick.zoho_contact_id] if pick.zoho_contact_id else []
            )
            if ids:
                base.append(ZohoInvoiceLineItem.customer_id.in_(ids))
            else:
                like = f"%{dealer_query}%"
                base.append(ZohoInvoiceLineItem.customer_name.ilike(like))
        else:
            like = f"%{dealer_query}%"
            base.append(ZohoInvoiceLineItem.customer_name.ilike(like))

    sum_value = func.coalesce(func.sum(ZohoInvoiceLineItem.item_total), 0)
    sum_qty = func.coalesce(func.sum(ZohoInvoiceLineItem.quantity), 0)
    # Default: highest pcs first; ₹ as tiebreak. sort_by=amount flips primary key.
    if sort_by == "amount":
        order_cols = (sum_value.desc(), sum_qty.desc())
    else:
        order_cols = (sum_qty.desc(), sum_value.desc())

    rows = (
        await db.execute(
            select(
                ZohoInvoiceLineItem.customer_id,
                ZohoInvoiceLineItem.customer_name,
                sum_value,
                sum_qty,
                func.count(),
            )
            .where(*base)
            .group_by(ZohoInvoiceLineItem.customer_id, ZohoInvoiceLineItem.customer_name)
            .order_by(*order_cols)
            .limit(limit)
        )
    ).all()

    # Sample matched product names for transparency
    sample_products = (
        await db.execute(
            select(
                ZohoInvoiceLineItem.item_name,
                ZohoInvoiceLineItem.product_category,
                func.coalesce(func.sum(ZohoInvoiceLineItem.item_total), 0),
            )
            .where(*base)
            .group_by(ZohoInvoiceLineItem.item_name, ZohoInvoiceLineItem.product_category)
            .order_by(func.coalesce(func.sum(ZohoInvoiceLineItem.item_total), 0).desc())
            .limit(8)
        )
    ).all()

    dealers_out: list[dict[str, Any]] = []
    grand_value = 0.0
    grand_qty = 0.0
    grand_lines = 0
    for cid, cname, value, qty, n in rows:
        v = round(float(value or 0), 2)
        q = round(float(qty or 0), 3)
        ln = int(n or 0)
        grand_value += v
        grand_qty += q
        grand_lines += ln
        dealers_out.append(
            {
                "customer_id": cid or "",
                "customer_name": cname or "",
                "total": v,
                "quantity": q,
                "line_count": ln,
            }
        )

    return {
        "ok": True,
        "resolved": True,
        "product_query": product_query,
        "dealer_query": dealer_query or None,
        "period": period_info,
        "sort_by": sort_by,
        "dealer_count": len(dealers_out),
        "total": round(grand_value, 2),
        "quantity": round(grand_qty, 3),
        "line_count": grand_lines,
        "dealers": dealers_out,
        "matched_products": [
            {
                "item_name": name or "",
                "product_category": cat or "",
                "total": round(float(val or 0), 2),
            }
            for name, cat, val in sample_products
        ],
        "currency_note": "INR, tax-exclusive line item_total",
        "ranking_note": (
            "Dealers ordered by quantity (pcs) descending, then ₹ as tiebreak."
            if sort_by == "quantity"
            else "Dealers ordered by ₹ value descending, then quantity as tiebreak."
        ),
        "source": "zoho_invoice_line_items",
    }


async def tool_query_business_data(
    db: AsyncSession, user: User, args: dict[str, Any]
) -> dict[str, Any]:
    """Allowlisted read-only analytics across CRM business tables."""
    dataset = str(args.get("dataset") or "").strip().lower().replace("-", "_")
    aliases = {
        "invoice_line_items": "invoice_lines",
        "line_items": "invoice_lines",
        "products": "invoice_lines",
        "sales": "zoho_sales",
        "invoices": "zoho_sales",
        "wa": "whatsapp",
        "whatsapp_conversations": "whatsapp",
        "catalog_products": "catalog",
        "employee": "employees",
        "creditnote": "credit_notes",
        "credit_note": "credit_notes",
    }
    dataset = aliases.get(dataset, dataset)
    group_by = str(args.get("group_by") or "").strip().lower() or "none"
    product_query = str(args.get("product_query") or "").strip()
    dealer_query = str(args.get("dealer_query") or "").strip()
    city = str(args.get("city") or "").strip()
    state = str(args.get("state") or "").strip()
    # Dealer contact / WhatsApp lists need the full filtered set (not top-25 rankings).
    if dataset == "dealers":
        limit = max(1, min(_DEALER_LIST_LIMIT, int(args.get("limit") or _DEALER_LIST_LIMIT)))
    else:
        limit = max(1, min(_QUERY_ROW_LIMIT, int(args.get("limit") or 50)))

    if dataset == "invoice_lines":
        # Delegate product×dealer (most common) to the durable tool.
        if group_by in {"", "none", "dealer"} or not group_by:
            return await tool_get_product_sales_by_dealer(
                db,
                user,
                {
                    "product_query": product_query or "scale",
                    "dealer_query": dealer_query,
                    "period": args.get("period"),
                    "date_from": args.get("date_from"),
                    "date_to": args.get("date_to"),
                    "limit": limit,
                    "sort_by": args.get("sort_by"),
                },
            )
        period_info = _resolve_sales_period(args)
        start, end = period_info["from"], period_info["to"]
        base = [
            ZohoInvoiceLineItem.invoice_date != "",
            ZohoInvoiceLineItem.invoice_date >= start,
            ZohoInvoiceLineItem.invoice_date <= end,
            _line_item_active_clause(),
        ]
        if product_query:
            pf = _product_filter_clause(product_query)
            if pf is not None:
                base.append(pf)
        if dealer_query:
            base.append(ZohoInvoiceLineItem.customer_name.ilike(f"%{dealer_query}%"))

        if group_by == "product":
            rows = (
                await db.execute(
                    select(
                        ZohoInvoiceLineItem.item_name,
                        ZohoInvoiceLineItem.product_category,
                        func.coalesce(func.sum(ZohoInvoiceLineItem.item_total), 0),
                        func.coalesce(func.sum(ZohoInvoiceLineItem.quantity), 0),
                        func.count(),
                    )
                    .where(*base)
                    .group_by(ZohoInvoiceLineItem.item_name, ZohoInvoiceLineItem.product_category)
                    .order_by(func.coalesce(func.sum(ZohoInvoiceLineItem.item_total), 0).desc())
                    .limit(limit)
                )
            ).all()
            items = [
                {
                    "item_name": n or "",
                    "product_category": c or "",
                    "total": round(float(v or 0), 2),
                    "quantity": round(float(q or 0), 3),
                    "line_count": int(cnt or 0),
                }
                for n, c, v, q, cnt in rows
            ]
        elif group_by == "category":
            rows = (
                await db.execute(
                    select(
                        ZohoInvoiceLineItem.product_category,
                        func.coalesce(func.sum(ZohoInvoiceLineItem.item_total), 0),
                        func.coalesce(func.sum(ZohoInvoiceLineItem.quantity), 0),
                        func.count(),
                    )
                    .where(*base)
                    .group_by(ZohoInvoiceLineItem.product_category)
                    .order_by(func.coalesce(func.sum(ZohoInvoiceLineItem.item_total), 0).desc())
                    .limit(limit)
                )
            ).all()
            items = [
                {
                    "product_category": c or "Others",
                    "total": round(float(v or 0), 2),
                    "quantity": round(float(q or 0), 3),
                    "line_count": int(cnt or 0),
                }
                for c, v, q, cnt in rows
            ]
        elif group_by == "month":
            month_expr = func.substr(ZohoInvoiceLineItem.invoice_date, 1, 7)
            rows = (
                await db.execute(
                    select(
                        month_expr,
                        func.coalesce(func.sum(ZohoInvoiceLineItem.item_total), 0),
                        func.coalesce(func.sum(ZohoInvoiceLineItem.quantity), 0),
                        func.count(),
                    )
                    .where(*base)
                    .group_by(month_expr)
                    .order_by(month_expr.asc())
                    .limit(limit)
                )
            ).all()
            items = [
                {
                    "month": m or "",
                    "total": round(float(v or 0), 2),
                    "quantity": round(float(q or 0), 3),
                    "line_count": int(cnt or 0),
                }
                for m, v, q, cnt in rows
            ]
        else:
            return {
                "ok": False,
                "error": f"Unsupported group_by for invoice_lines: {group_by}",
                "allowed": ["dealer", "product", "category", "month", "none"],
            }
        return {
            "ok": True,
            "dataset": "invoice_lines",
            "group_by": group_by,
            "period": period_info,
            "product_query": product_query or None,
            "items": items,
            "currency_note": "INR, tax-exclusive line item_total",
            "source": "zoho_invoice_line_items",
        }

    if dataset == "zoho_sales":
        period_info = _resolve_sales_period(args)
        start, end = period_info["from"], period_info["to"]
        if group_by in {"dealer", "none", ""}:
            if dealer_query:
                return await tool_get_dealer_sales(db, user, {**args, "query": dealer_query})
            # Top dealers by zoho_sales in period
            remapped = remapped_customer_id_expr(ZohoSale.customer_id)
            visible = dealer_not_excluded_clause(Dealer.name, Dealer.company, Dealer.zoho_contact_id)
            q = (
                select(
                    Dealer.id,
                    Dealer.name,
                    Dealer.company,
                    Dealer.zoho_contact_id,
                    Dealer.city,
                    Dealer.state,
                    func.coalesce(func.sum(sales_amount_expr()), 0),
                    func.count(),
                )
                .select_from(ZohoSale)
                .join(Dealer, Dealer.zoho_contact_id == remapped)
                .where(visible)
                .where(ZohoSale.date != "")
                .where(ZohoSale.date >= start)
                .where(ZohoSale.date <= end)
            )
            if city:
                q = q.where(Dealer.city.ilike(f"%{city}%"))
            if state:
                q = q.where(Dealer.state.ilike(f"%{state}%"))
            rows = (
                await db.execute(
                    q.group_by(
                        Dealer.id,
                        Dealer.name,
                        Dealer.company,
                        Dealer.zoho_contact_id,
                        Dealer.city,
                        Dealer.state,
                    )
                    .order_by(func.coalesce(func.sum(sales_amount_expr()), 0).desc())
                    .limit(limit)
                )
            ).all()
            items = [
                {
                    "dealer_id": did,
                    "name": name or "",
                    "company": company or "",
                    "customer_id": zid or "",
                    "city": c or "",
                    "state": st or "",
                    "total": round(float(tot or 0), 2),
                    "invoice_count": int(n or 0),
                }
                for did, name, company, zid, c, st, tot, n in rows
            ]
            return {
                "ok": True,
                "dataset": "zoho_sales",
                "group_by": "dealer",
                "period": period_info,
                "state": state or None,
                "city": city or None,
                "filter_state": state or None,
                "filter_city": city or None,
                "items": items,
                "sort_by": "amount",
                "ranking_note": "Dealers ordered by ₹ amount descending.",
                "currency_note": "INR, tax-exclusive Zoho sub_total",
                "source": "zoho_sales",
            }
        if group_by == "month":
            month_expr = func.substr(ZohoSale.date, 1, 7)
            rows = (
                await db.execute(
                    select(
                        month_expr,
                        func.coalesce(func.sum(sales_amount_expr()), 0),
                        func.count(),
                    )
                    .where(
                        ZohoSale.date != "",
                        ZohoSale.date >= start,
                        ZohoSale.date <= end,
                    )
                    .group_by(month_expr)
                    .order_by(month_expr.asc())
                    .limit(limit)
                )
            ).all()
            return {
                "ok": True,
                "dataset": "zoho_sales",
                "group_by": "month",
                "period": period_info,
                "items": [
                    {
                        "month": m or "",
                        "total": round(float(v or 0), 2),
                        "invoice_count": int(n or 0),
                    }
                    for m, v, n in rows
                ],
                "currency_note": "INR, tax-exclusive Zoho sub_total",
                "source": "zoho_sales",
            }
        if group_by in {"city", "state"}:
            # Reuse region-style aggregation via dealers
            col = Dealer.city if group_by == "city" else Dealer.state
            remapped = remapped_customer_id_expr(ZohoSale.customer_id)
            visible = dealer_not_excluded_clause(Dealer.name, Dealer.company, Dealer.zoho_contact_id)
            rows = (
                await db.execute(
                    select(
                        col,
                        func.coalesce(func.sum(sales_amount_expr()), 0),
                        func.count(),
                        func.count(func.distinct(Dealer.id)),
                    )
                    .select_from(ZohoSale)
                    .join(Dealer, Dealer.zoho_contact_id == remapped)
                    .where(visible)
                    .where(ZohoSale.date != "")
                    .where(ZohoSale.date >= start)
                    .where(ZohoSale.date <= end)
                    .where(col != "")
                    .group_by(col)
                    .order_by(func.coalesce(func.sum(sales_amount_expr()), 0).desc())
                    .limit(limit)
                )
            ).all()
            return {
                "ok": True,
                "dataset": "zoho_sales",
                "group_by": group_by,
                "period": period_info,
                "state": state or None,
                "city": city or None,
                "items": [
                    {
                        group_by: (label or ""),
                        "total": round(float(v or 0), 2),
                        "invoice_count": int(n or 0),
                        "dealer_count": int(dc or 0),
                    }
                    for label, v, n, dc in rows
                ],
                "sort_by": "amount",
                "ranking_note": f"Ordered by ₹ amount descending ({group_by}).",
                "currency_note": "INR, tax-exclusive Zoho sub_total",
                "source": "zoho_sales via dealers",
            }
        return {
            "ok": False,
            "error": f"Unsupported group_by for zoho_sales: {group_by}",
            "allowed": ["dealer", "month", "city", "state", "none"],
        }

    if dataset == "dealers":
        visible = dealer_not_excluded_clause(Dealer.name, Dealer.company, Dealer.zoho_contact_id)
        q = select(Dealer).where(visible)
        if dealer_query:
            like = f"%{dealer_query}%"
            q = q.where(or_(Dealer.name.ilike(like), Dealer.company.ilike(like)))
        if city:
            q = q.where(Dealer.city.ilike(f"%{city}%"))
        state_label = ""
        if state:
            state_label, state_vals = _resolve_state_query(state)
            if state_vals:
                q = q.where(or_(*[Dealer.state.ilike(v) for v in state_vals]))
                state_label = state_label or state_vals[0]
            else:
                q = q.where(Dealer.state.ilike(f"%{state}%"))
                state_label = state
        status_filter = str(args.get("status") or "").strip()
        if status_filter:
            q = q.where(Dealer.status.ilike(status_filter))
        # Complete filtered set (phone may be blank) — UI card shows all rows.
        total_matching = (
            await db.execute(select(func.count()).select_from(q.subquery()))
        ).scalar() or 0
        rows = (
            await db.execute(
                q.order_by(Dealer.name.asc(), Dealer.company.asc()).limit(limit)
            )
        ).scalars().all()
        items = [_dealer_candidate(d) for d in rows]
        with_phone = sum(1 for it in items if str(it.get("phone") or "").strip())
        scope = state_label or city or (dealer_query or "All")
        truncated = int(total_matching) > len(items)
        return {
            "ok": True,
            "dataset": "dealers",
            "report_kind": "dealer_contacts",
            "count": len(items),
            "total_matching": int(total_matching),
            "with_phone": with_phone,
            "truncated": truncated,
            "state": state_label or (state or None),
            "city": city or None,
            "filter_state": state_label or (state or None),
            "filter_city": city or None,
            "scope": scope,
            "items": items,
            "columns": ["dealer", "city", "state", "phone"],
            "note": (
                f"Complete dealer contact list for {scope}: {len(items)} of {int(total_matching)} "
                f"({with_phone} with phone)."
                + (" Result capped by limit — raise limit." if truncated else "")
            ),
            "source": "dealers",
        }

    if dataset == "credit_notes":
        period_info = _resolve_sales_period(args)
        start, end = period_info["from"], period_info["to"]
        q = select(
            ZohoCreditNote.customer_name,
            ZohoCreditNote.customer_id,
            func.coalesce(func.sum(ZohoCreditNote.total), 0),
            func.count(),
        ).where(
            ZohoCreditNote.date != "",
            ZohoCreditNote.date >= start,
            ZohoCreditNote.date <= end,
        )
        if dealer_query:
            q = q.where(ZohoCreditNote.customer_name.ilike(f"%{dealer_query}%"))
        rows = (
            await db.execute(
                q.group_by(ZohoCreditNote.customer_name, ZohoCreditNote.customer_id)
                .order_by(func.coalesce(func.sum(ZohoCreditNote.total), 0).desc())
                .limit(limit)
            )
        ).all()
        return {
            "ok": True,
            "dataset": "credit_notes",
            "period": period_info,
            "items": [
                {
                    "customer_name": name or "",
                    "customer_id": cid or "",
                    "total": round(float(v or 0), 2),
                    "count": int(n or 0),
                }
                for name, cid, v, n in rows
            ],
            "source": "zoho_credit_notes",
        }

    if dataset == "whatsapp":
        data = await tool_get_whatsapp_connection_status(db, user, {})
        unanswered = await tool_get_unanswered_messages(db, user, {"limit": min(limit, 20)})
        return {
            "ok": True,
            "dataset": "whatsapp",
            "connection": {
                "connected": data.get("connected"),
                "disconnected": data.get("disconnected"),
                "total": data.get("total"),
                "gateway": data.get("gateway"),
            },
            "unanswered": {
                "total_unread_threads": unanswered.get("total_unread_threads"),
                "total_unread_messages": unanswered.get("total_unread_messages"),
                "items": unanswered.get("items") or [],
            },
            "source": "whatsapp_instances + whatsapp_conversations",
        }

    if dataset == "catalog":
        q = select(CatalogProduct).where(CatalogProduct.hidden_from_catalog.is_(False))
        if product_query:
            like = f"%{product_query}%"
            q = q.where(
                or_(
                    CatalogProduct.name.ilike(like),
                    CatalogProduct.sku.ilike(like),
                    CatalogProduct.category_name.ilike(like),
                    CatalogProduct.model_number.ilike(like),
                )
            )
        rows = (await db.execute(q.order_by(CatalogProduct.name.asc()).limit(limit))).scalars().all()
        return {
            "ok": True,
            "dataset": "catalog",
            "count": len(rows),
            "items": [
                {
                    "name": p.name or "",
                    "sku": p.sku or "",
                    "category": p.category_name or "",
                    "model_number": p.model_number or "",
                    "stock_status": p.stock_status or "",
                    "status": p.status or "",
                }
                for p in rows
            ],
            "source": "catalog_products",
        }

    if dataset == "employees":
        if not _is_admin(user):
            return {"ok": False, "error": "employees dataset is admin/management only"}
        q = select(Employee).where(Employee.deactivated_at.is_(None))
        name_q = str(args.get("query") or dealer_query or "").strip()
        if name_q:
            q = q.where(Employee.name.ilike(f"%{name_q}%"))
        rows = (await db.execute(q.order_by(Employee.name.asc()).limit(limit))).scalars().all()
        return {
            "ok": True,
            "dataset": "employees",
            "count": len(rows),
            "items": [
                {
                    "id": e.id,
                    "code": e.code or "",
                    "name": e.name or "",
                    "email": e.email or "",
                    "department": e.department or "",
                    "designation": e.designation or "",
                    "crm_role": e.crm_role or "",
                    "status": e.status or "",
                }
                for e in rows
            ],
            "source": "employees",
            "note": "No salary, Aadhaar, PAN, or secrets included.",
        }

    return {
        "ok": False,
        "error": f"Unknown dataset: {dataset}",
        "allowed": [
            "invoice_lines",
            "zoho_sales",
            "dealers",
            "credit_notes",
            "whatsapp",
            "catalog",
            "employees",
        ],
    }


async def tool_get_unanswered_messages(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    limit = int(args.get("limit") or 15)
    limit = max(1, min(50, limit))
    allowed = await authorized_instance_ids(db, user)
    if allowed is not None and len(allowed) == 0:
        return {"total_unread_threads": 0, "total_unread_messages": 0, "items": [], "note": "No WhatsApp instances assigned"}

    q = (
        select(WhatsAppConversation)
        .where(
            WhatsAppConversation.organization_id == ORG,
            WhatsAppConversation.unread_count > 0,
            WhatsAppConversation.status != "Resolved",
        )
        .order_by(WhatsAppConversation.last_message_at.desc().nullslast())
        .limit(limit)
    )
    if allowed is not None:
        q = q.where(WhatsAppConversation.whatsapp_instance_id.in_(allowed))

    rows = (await db.execute(q)).scalars().all()

    count_q = select(
        func.count(),
        func.coalesce(func.sum(WhatsAppConversation.unread_count), 0),
    ).where(
        WhatsAppConversation.organization_id == ORG,
        WhatsAppConversation.unread_count > 0,
        WhatsAppConversation.status != "Resolved",
    )
    if allowed is not None:
        count_q = count_q.where(WhatsAppConversation.whatsapp_instance_id.in_(allowed))
    thread_n, unread_sum = (await db.execute(count_q)).one()

    items = [
        {
            "conversation_id": c.public_id,
            "customer_name": c.customer_name or "",
            "customer_phone": c.customer_phone or "",
            "unread_count": c.unread_count,
            "status": c.status,
            "last_message_preview": (c.last_message_preview or "")[:160],
            "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
            "instance_id": c.whatsapp_instance_id,
        }
        for c in rows
    ]
    return {
        "total_unread_threads": int(thread_n or 0),
        "total_unread_messages": int(unread_sum or 0),
        "items": items,
    }


async def tool_get_whatsapp_connection_status(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    _ = args
    allowed = await authorized_instance_ids(db, user)
    q = select(WhatsAppInstance).where(
        WhatsAppInstance.organization_id == ORG,
        WhatsAppInstance.deleted_at.is_(None),
    )
    if allowed is not None:
        if not allowed:
            return {"connected": 0, "disconnected": 0, "instances": [], "note": "No WhatsApp instances assigned"}
        q = q.where(WhatsAppInstance.id.in_(allowed))
    rows = (await db.execute(q.order_by(WhatsAppInstance.display_name))).scalars().all()
    instances = [
        {
            "public_id": i.public_id,
            "display_name": i.display_name,
            "employee_code": i.employee_code or "",
            "employee_name": i.employee_name or "",
            "connection_status": i.connection_status or "Disconnected",
            "connected_phone": i.connected_phone_number or "",
        }
        for i in rows
    ]
    connected = sum(1 for i in instances if (i["connection_status"] or "").lower() == "connected")
    return {
        "connected": connected,
        "disconnected": len(instances) - connected,
        "total": len(instances),
        "instances": instances,
        "gateway": "Evolution GO",
    }


async def tool_get_employee_whatsapp_instances(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    _ = args
    # Same safe listing as connection status — focused on employee mapping.
    data = await tool_get_whatsapp_connection_status(db, user, {})
    return {
        "total": data.get("total", 0),
        "instances": [
            {
                "public_id": i["public_id"],
                "display_name": i["display_name"],
                "employee_code": i["employee_code"],
                "employee_name": i["employee_name"],
                "connection_status": i["connection_status"],
                "connected_phone": i["connected_phone"],
            }
            for i in data.get("instances") or []
        ],
        "gateway": "Evolution GO",
    }


def _sql_vendor_from_zoho_dealer(row: ZohoDealer) -> dict[str, Any] | None:
    """Return a vendor-shaped dict only when staging raw_json says contact_type=vendor."""
    raw = (row.raw_json or "").strip()
    contact_type = ""
    if raw:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                contact_type = str(payload.get("contact_type") or "").lower()
        except json.JSONDecodeError:
            contact_type = ""
    if "vendor" not in contact_type:
        return None
    return {
        "vendor_id": row.zoho_contact_id or "",
        "contact_id": row.zoho_contact_id or "",
        "contact_name": row.contact_name or "",
        "company_name": row.company_name or "",
        "email": row.email or "",
        "phone": row.phone or "",
        "mobile": row.mobile or "",
        "gst_no": row.gst_no or "",
        "status": row.status or "",
        "contact_type": contact_type or "vendor",
        "city": row.billing_city or "",
        "state": row.billing_state or "",
        "source": "postgres",
    }


async def _search_vendors_sql(db: AsyncSession, query: str, *, limit: int) -> list[dict[str, Any]]:
    """Postgres probe for vendors (CRM normally syncs customers only — usually empty)."""
    term = (query or "").strip()
    if not term:
        return []
    like = f"%{term}%"
    rows = (
        await db.execute(
            select(ZohoDealer)
            .where(
                or_(
                    ZohoDealer.contact_name.ilike(like),
                    ZohoDealer.company_name.ilike(like),
                    ZohoDealer.gst_no.ilike(like),
                )
            )
            .order_by(ZohoDealer.company_name.asc())
            .limit(80)
        )
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = _sql_vendor_from_zoho_dealer(row)
        if item:
            out.append(item)
        if len(out) >= limit:
            break
    return out


async def tool_search_vendors(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    """SQL first; Zoho Books live API when Postgres has no vendor rows."""
    _ = user
    query = str(args.get("query") or "").strip()
    limit = int(args.get("limit") or 10)
    limit = max(1, min(25, limit))
    if not query:
        return {"ok": False, "query": "", "count": 0, "items": [], "error": "query is required"}

    sql_items = await _search_vendors_sql(db, query, limit=limit)
    if sql_items:
        return {
            "ok": True,
            "query": query,
            "count": len(sql_items),
            "items": sql_items,
            "source": "postgres",
            "sql_checked": True,
            "zoho_fallback_used": False,
            "note": "Vendors found in CRM Postgres.",
        }

    try:
        live = await zs.search_vendors_live(db, query=query, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "query": query,
            "count": 0,
            "items": [],
            "source": "zoho_api",
            "sql_checked": True,
            "sql_count": 0,
            "zoho_fallback_used": True,
            "error": str(exc)[:500],
            "note": "No vendor rows in Postgres; Zoho live search failed.",
        }
    live["sql_checked"] = True
    live["sql_count"] = 0
    live["zoho_fallback_used"] = True
    if not live.get("items"):
        live["note"] = "No vendors in Postgres or Zoho for this query."
    return live


async def tool_get_vendor(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    _ = user
    vendor_id = str(args.get("vendor_id") or "").strip()
    query = str(args.get("query") or "").strip()
    if not vendor_id and not query:
        return {"ok": False, "error": "vendor_id or query is required", "sql_checked": False}

    # SQL first by exact Zoho contact id when present in staging as vendor.
    if vendor_id:
        row = (
            await db.execute(
                select(ZohoDealer).where(ZohoDealer.zoho_contact_id == vendor_id).limit(1)
            )
        ).scalar_one_or_none()
        if row:
            item = _sql_vendor_from_zoho_dealer(row)
            if item:
                return {
                    "ok": True,
                    "resolved": True,
                    "vendor": item,
                    "source": "postgres",
                    "sql_checked": True,
                    "zoho_fallback_used": False,
                    "note": "Vendor found in CRM Postgres.",
                }

    if query and not vendor_id:
        sql_items = await _search_vendors_sql(db, query, limit=10)
        if len(sql_items) == 1:
            return {
                "ok": True,
                "resolved": True,
                "vendor": sql_items[0],
                "source": "postgres",
                "sql_checked": True,
                "zoho_fallback_used": False,
            }
        if len(sql_items) > 1:
            return {
                "ok": True,
                "resolved": False,
                "candidates": sql_items,
                "source": "postgres",
                "sql_checked": True,
                "zoho_fallback_used": False,
                "note": "Multiple SQL vendor matches — pass vendor_id.",
            }

    try:
        live = await zs.get_vendor_live(db, vendor_id=vendor_id, query=query)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc)[:500],
            "source": "zoho_api",
            "sql_checked": True,
            "zoho_fallback_used": True,
        }
    live["sql_checked"] = True
    live["zoho_fallback_used"] = True
    return live


async def tool_get_vendor_related(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    """Vendor bills / POs / payments — not in CRM SQL; live Zoho only after SQL vendor resolve attempt."""
    _ = user
    vendor_id = str(args.get("vendor_id") or "").strip()
    query = str(args.get("query") or "").strip()
    include = str(args.get("include") or "bills,purchaseorders,payments")
    limit = int(args.get("limit") or 15)
    limit = max(1, min(50, limit))

    if not vendor_id and not query:
        return {"ok": False, "error": "vendor_id or query is required", "sql_checked": False}

    # Resolve vendor_id via SQL search first when only a name is given.
    sql_note = "Vendor documents are not stored in CRM Postgres; using Zoho live API."
    if not vendor_id and query:
        sql_items = await _search_vendors_sql(db, query, limit=10)
        if len(sql_items) == 1:
            vendor_id = str(sql_items[0].get("vendor_id") or "").strip()
        elif len(sql_items) > 1:
            return {
                "ok": True,
                "resolved": False,
                "candidates": sql_items,
                "source": "postgres",
                "sql_checked": True,
                "zoho_fallback_used": False,
                "note": "Multiple SQL vendor matches — pass vendor_id, then retry get_vendor_related.",
            }

    try:
        live = await zs.get_vendor_related_live(
            db,
            vendor_id=vendor_id,
            query=query if not vendor_id else "",
            include=include,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc)[:500],
            "source": "zoho_api",
            "sql_checked": True,
            "zoho_fallback_used": True,
            "note": sql_note,
        }
    live["sql_checked"] = True
    live["zoho_fallback_used"] = True
    live["sql_documents"] = False
    if "note" not in live or not live.get("note"):
        live["note"] = sql_note
    return live


TOOL_HANDLERS: dict[str, Callable[[AsyncSession, User, dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "get_sales_summary": tool_get_sales_summary,
    "search_dealers": tool_search_dealers,
    "get_dealer_sales": tool_get_dealer_sales,
    "get_region_sales": tool_get_region_sales,
    "get_product_sales_by_dealer": tool_get_product_sales_by_dealer,
    "query_business_data": tool_query_business_data,
    "get_unanswered_messages": tool_get_unanswered_messages,
    "get_whatsapp_connection_status": tool_get_whatsapp_connection_status,
    "get_employee_whatsapp_instances": tool_get_employee_whatsapp_instances,
    "search_vendors": tool_search_vendors,
    "get_vendor": tool_get_vendor,
    "get_vendor_related": tool_get_vendor_related,
    "remember_fact": tool_remember_fact,
    "search_knowledge": tool_search_knowledge,
    "request_new_tool": tool_request_new_tool,
}

# Hot-load any persisted generated tools (volume / package runtime).
try:
    refresh_generated_handlers()
except Exception:  # noqa: BLE001
    pass


async def execute_tool(
    db: AsyncSession,
    user: User,
    *,
    run_id: int,
    name: str,
    args: dict[str, Any] | None,
    session_id: int | None = None,
) -> dict[str, Any]:
    handler = TOOL_HANDLERS.get(name)
    started = time.perf_counter()
    args = dict(args or {})
    # Inject run/session for knowledge writes (model does not supply these).
    if name in {"remember_fact", "search_knowledge"}:
        args["_run_id"] = run_id
        if session_id is not None:
            args["_session_id"] = session_id
        elif "_session_id" not in args:
            from app.models import AiAgentRun

            run_row = (
                await db.execute(select(AiAgentRun).where(AiAgentRun.id == run_id))
            ).scalar_one_or_none()
            if run_row and run_row.session_id is not None:
                args["_session_id"] = run_row.session_id
    if not handler:
        err = f"Unknown or unauthorized tool: {name}"
        db.add(
            AiToolCall(
                run_id=run_id,
                tool_name=name,
                args_json=json.dumps(args, default=str),
                result_json="",
                success=False,
                error=err,
                duration_ms=0,
            )
        )
        return {"ok": False, "error": err}

    try:
        result = await handler(db, user, args)
        duration_ms = int((time.perf_counter() - started) * 1000)
        # Cap stored result size
        result_json = json.dumps(result, default=str, ensure_ascii=False)
        if len(result_json) > 50_000:
            result_json = result_json[:50_000] + '…"}'
        db.add(
            AiToolCall(
                run_id=run_id,
                tool_name=name,
                args_json=json.dumps(args, default=str)[:8000],
                result_json=result_json,
                success=True,
                error="",
                duration_ms=duration_ms,
            )
        )
        return {"ok": True, "tool": name, "result": result, "duration_ms": duration_ms}
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - started) * 1000)
        db.add(
            AiToolCall(
                run_id=run_id,
                tool_name=name,
                args_json=json.dumps(args, default=str)[:8000],
                result_json="",
                success=False,
                error=str(exc)[:2000],
                duration_ms=duration_ms,
            )
        )
        return {"ok": False, "tool": name, "error": str(exc), "duration_ms": duration_ms}
