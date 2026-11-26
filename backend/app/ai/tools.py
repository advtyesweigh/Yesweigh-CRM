"""Secure read-only CRM tools for the AI agent (Phase 1 + dealer sales)."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from calendar import monthrange
from datetime import date
from typing import Any, Callable, Awaitable

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.provider import ToolSpec
from app.dealer_exclusions import dealer_not_excluded_clause
from app.dealer_merges import remapped_customer_id_expr, sales_customer_ids_for_dealer
from app.kerala_district_order import (
    UNKNOWN_RANK,
    district_rank_whens,
    kerala_district_city_match_terms,
    resolve_kerala_district_query,
)
from app.models import (
    AiToolCall,
    CampaignMedia,
    CatalogProduct,
    Dealer,
    Employee,
    Lead,
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
_LEAD_SEARCH_LIMIT = 50
_LEAD_LIST_LIMIT = 1500
_QUERY_ROW_LIMIT = 100
# Kerala dealer campaigns are often 200–350 phones; keep headroom for full-state blasts.
_WHATSAPP_BATCH_MAX_RECIPIENTS = 400
_WHATSAPP_BATCH_MIN_DELAY_S = 5
_WHATSAPP_BATCH_DEFAULT_DELAY_S = 10
# Practical agent-send ceiling (base64 in-process + Evolution payload). Not a WhatsApp product rule.
# Override with WHATSAPP_MEDIA_MAX_BYTES (clamped 1–200 MB). Campaign upload limit is separate (2GB).
_WHATSAPP_MEDIA_MAX_BYTES = 64 * 1024 * 1024
_CAMPAIGN_MEDIA_LIST_LIMIT = 40


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
                "Fuzzy/partial search of CRM Dealer menu records by name or company "
                "(e.g. 'meezan', 'Meezan Electronics'). Returns top candidates with city/state/phone. "
                "Use before or instead of get_dealer_sales when the dealer name is unclear. "
                "Do NOT use for CRM left-menu Leads — use search_leads or "
                "query_business_data dataset=leads. "
                "Do NOT use for full state/district dealer contact or WhatsApp lists — use "
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
            name="search_leads",
            description=(
                "Search / list CRM left-drawer **Leads** page (`/leads`, table `leads`) — "
                "the same Leads item in the CRM left sidebar. NOT dealers (use search_dealers). "
                "ALWAYS use this (or query_business_data dataset=leads) for: 'leads', 'find leads', "
                "'show leads', 'all leads', 'lead contacts', 'lead list', 'CRM leads', 'Kerala leads', "
                "or when the user corrects 'not dealers, leads'. "
                "Default 'find leads' / 'all leads' returns the FULL Leads drawer set "
                "(omit limit or use 1500) — never truncate to 25 unless the user asks top/first N. "
                "Optional state/city/district filters (e.g. state=Kerala). "
                "Returns Business Name, City, State, Phone for the CRM Leads (left menu) report card."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "query": {
                        "type": "STRING",
                        "description": "Optional text search across name/company/phone/city/district/state.",
                    },
                    "state": {
                        "type": "STRING",
                        "description": "Filter leads.state (e.g. Kerala, Tamil Nadu).",
                    },
                    "city": {
                        "type": "STRING",
                        "description": "Filter leads.city (partial match).",
                    },
                    "district": {
                        "type": "STRING",
                        "description": "Filter leads.district (e.g. Malappuram, Ernakulam).",
                    },
                    "status": {
                        "type": "STRING",
                        "description": "Optional lead status: new | contacted | qualified | won | lost.",
                    },
                    "limit": {
                        "type": "INTEGER",
                        "description": (
                            "Max rows (1–1500). For find/all leads (no name query) default 1500 — "
                            "do NOT pass 25. Only pass a small limit when the user asks top/first N. "
                            "Name-search default is 50."
                        ),
                    },
                },
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
                "dealers (Dealer menu contact/WhatsApp lists by state/city — Dealer, City, State, Phone), "
                "leads (CRM left-drawer Leads `/leads` — Business Name, City, State, Phone; NOT dealers), "
                "credit_notes, whatsapp, catalog, employees (admin). "
                "CRITICAL map: left-menu Leads → dataset=leads; left-menu Dealer → dataset=dealers. "
                "'leads' / 'find leads' / 'all leads' / 'lead contacts' / 'Kerala leads' → dataset=leads "
                "(never dealers; omit limit or use 1500 — never default 25). "
                "'dealers' / 'Kerala dealers' → dataset=dealers (CRM only). Do not conflate. "
                "For 'all Kerala dealers WhatsApp/phone list' (CRM, no google) → dataset=dealers, state=Kerala. "
                "NEVER use dataset=dealers when the user says google / 'from google' + dealers + a state "
                "(e.g. 'weighing scale dealers list Tamilnadu from google') — that MUST be "
                "start_dealer_finder (Google Places full scan), not this CRM contact dump. "
                "For 'find leads' / 'Kerala leads' / lead contact lists → dataset=leads "
                "(optional state=…; or use search_leads). "
                "CRM UI renders a CRM Leads (left menu) / CRM Dealers report card — reply with one short sentence only. "
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
                            "invoice_lines | zoho_sales | dealers | leads | credit_notes | "
                            "whatsapp | catalog | employees"
                        ),
                    },
                    "product_query": {
                        "type": "STRING",
                        "description": "Filter products/SKUs/categories (invoice_lines / catalog).",
                    },
                    "dealer_query": {
                        "type": "STRING",
                        "description": "Filter by dealer/customer name (dealers / zoho_sales).",
                    },
                    "query": {
                        "type": "STRING",
                        "description": "Text search for dataset=leads (name/company/phone/city).",
                    },
                    "city": {
                        "type": "STRING",
                        "description": "Filter dealers.city / leads.city / region.",
                    },
                    "district": {
                        "type": "STRING",
                        "description": "Filter leads.district (dataset=leads).",
                    },
                    "state": {
                        "type": "STRING",
                        "description": (
                            "Filter dealers.state / leads.state (e.g. Kerala, Tamil Nadu). "
                            "Use for full-state contact / WhatsApp / lead lists."
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
                            "Dealer/lead contact lists (dataset=dealers|leads) default 1500 (cap 1500) "
                            "so full-state WhatsApp/phone and full CRM Leads drawer lists are complete. "
                            "For dataset=leads 'find leads'/'all leads', omit limit or use 1500 — never 25."
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
            name="search_whatsapp_messages",
            description=(
                "Full-text search of WhatsApp CRM messages (body + Sarvam voice transcripts, "
                "English and Malayalam). Use for any topic: container/cargo/stock, warranty, OV, "
                "dispatch, pricing, complaints, etc. Filters: days/date range, instance/agent, "
                "inbound/outbound, contact name/phone, unread/open. "
                "Then summarize findings in a Markdown table for the user. Do NOT invent contacts."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "query": {
                        "type": "STRING",
                        "description": (
                            "Keywords to find (space/comma separated). All tokens must match "
                            "unless match_any=true. Include Malayalam terms when relevant "
                            "(e.g. കണ്ടെയ്നർ, വന്നോ, സ്റ്റോക്ക്)."
                        ),
                    },
                    "keywords": {
                        "type": "STRING",
                        "description": "Optional comma-separated keywords (merged with query).",
                    },
                    "match_any": {
                        "type": "BOOLEAN",
                        "description": "If true, match any keyword (OR). Default false (AND).",
                    },
                    "days": {
                        "type": "INTEGER",
                        "description": "Look back N days (1–180, default 45).",
                    },
                    "date_from": {
                        "type": "STRING",
                        "description": "Optional ISO date start (YYYY-MM-DD) overrides days.",
                    },
                    "date_to": {
                        "type": "STRING",
                        "description": "Optional ISO date end (YYYY-MM-DD).",
                    },
                    "direction": {
                        "type": "STRING",
                        "description": "in | out | any (default any).",
                    },
                    "contact": {
                        "type": "STRING",
                        "description": "Filter by customer name or phone.",
                    },
                    "instance": {
                        "type": "STRING",
                        "description": (
                            "Filter by agent/instance: display name, employee name/code, "
                            "public_id, or connected phone (e.g. Visakh, Biju)."
                        ),
                    },
                    "unread_only": {
                        "type": "BOOLEAN",
                        "description": "Only threads with unread_count > 0.",
                    },
                    "open_only": {
                        "type": "BOOLEAN",
                        "description": "Exclude Resolved threads.",
                    },
                    "limit": {
                        "type": "INTEGER",
                        "description": "Max hits (1–50, default 25).",
                    },
                },
                "required": ["query"],
            },
        ),
        ToolSpec(
            name="list_whatsapp_threads",
            description=(
                "List WhatsApp conversations (threads) the user may access: contact, phone, "
                "agent/instance, status, unread, last preview. Filter by name/phone, instance, "
                "unread/open. Use before get_whatsapp_thread when browsing inbox, not for "
                "keyword-in-message search (use search_whatsapp_messages)."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "query": {
                        "type": "STRING",
                        "description": "Optional contact name, phone, tag, or last-preview text.",
                    },
                    "instance": {
                        "type": "STRING",
                        "description": "Agent/instance filter (name, code, public_id, phone).",
                    },
                    "unread_only": {
                        "type": "BOOLEAN",
                        "description": "Only unread threads.",
                    },
                    "open_only": {
                        "type": "BOOLEAN",
                        "description": "Exclude Resolved.",
                    },
                    "status": {
                        "type": "STRING",
                        "description": "Exact/partial status filter (Open, Pending, Resolved…).",
                    },
                    "limit": {
                        "type": "INTEGER",
                        "description": "Max threads (1–50, default 25).",
                    },
                },
            },
        ),
        ToolSpec(
            name="get_whatsapp_thread",
            description=(
                "Fetch one WhatsApp conversation by thread_id (conversation public_id) plus "
                "recent message history (body + transcripts). Use after search_whatsapp_messages "
                "or list_whatsapp_threads to read context, then summarize for the user."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "thread_id": {
                        "type": "STRING",
                        "description": "Conversation public_id from search/list results.",
                    },
                    "limit": {
                        "type": "INTEGER",
                        "description": "Max messages (1–80, default 40).",
                    },
                },
                "required": ["thread_id"],
            },
        ),
        ToolSpec(
            name="report_enquiries",
            description=(
                "Who asked about a topic on WhatsApp? Thin enquiry report: searches "
                "body+transcripts for user-provided topic keywords (any language), prefers "
                "inbound, dedupes by phone. Not limited to containers — pass any topic "
                "(stock, warranty, OV, payment…). Present results as a Markdown table."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "topic": {
                        "type": "STRING",
                        "description": (
                            "Topic phrase, e.g. 'container arrival', 'stock dispatch', "
                            "'warranty', 'OV'. Tokens become search keywords."
                        ),
                    },
                    "keywords": {
                        "type": "STRING",
                        "description": (
                            "Optional extra comma-separated keywords including Malayalam "
                            "(കണ്ടെയ്നർ, വന്നോ, സ്റ്റോക്ക്…)."
                        ),
                    },
                    "days": {
                        "type": "INTEGER",
                        "description": "Look back N days (default 45).",
                    },
                    "date_from": {"type": "STRING", "description": "Optional YYYY-MM-DD start."},
                    "date_to": {"type": "STRING", "description": "Optional YYYY-MM-DD end."},
                    "instance": {
                        "type": "STRING",
                        "description": "Optional agent/instance filter (Visakh, Biju…).",
                    },
                    "inbound_only": {
                        "type": "BOOLEAN",
                        "description": "Prefer customer inbound (default true; auto-widens if sparse).",
                    },
                    "limit": {
                        "type": "INTEGER",
                        "description": "Max distinct contacts (1–50, default 25).",
                    },
                },
                "required": ["topic"],
            },
        ),
        ToolSpec(
            name="remember_fact",
            description=(
                "Persist a company fact, abbreviation, machine short name, alias, or report-type "
                "definition learned from the user into CRM knowledge (Postgres). "
                "Call when the user teaches something like 'MSB means …', 'short name for X is Y', "
                "or 'remember that …'. Upserts by kind+key. "
                "Requires confirm=true AFTER summarising key→value and user says yes."
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
                    "confirm": {
                        "type": "BOOLEAN",
                        "description": "Must be true after user confirms the fact summary.",
                    },
                    "notes": {
                        "type": "STRING",
                        "description": "Optional context.",
                    },
                },
                "required": ["key", "value", "confirm"],
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
            name="send_whatsapp_text",
            description=(
                "Send ONE WhatsApp text message via Evolution GO from a YesWeigh-owned employee "
                "instance the user is authorized to use, to a customer phone number. "
                "CRITICAL: from_instance_* / 'from Biju' = the SENDING agent line (FROM), NEVER a "
                "recipient. Never put employee/instance phones in to_phone (no self-message, no staff). "
                "Requires confirm_send=true plus to_phone, message, and from_instance_public_id "
                "(or from_connected_phone / from_employee_code). "
                "Use get_employee_whatsapp_instances first to pick the FROM number. "
                "For multiple named recipients (same message, sequential with delay), use "
                "send_whatsapp_text_batch instead — do not refuse multi-recipient requests. "
                "Idempotent: skips if the same/near-same text was already sent from this instance "
                "to this phone within ~24h (normalized + English-core; strike notices). "
                "Never invent sends — only call when the user clearly asks "
                "to send a specific message from a specific agent number to a specific recipient. "
                "Logs the outbound message in CRM inbox history. "
                "Set dry_run=true to validate without sending."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "to_phone": {
                        "type": "STRING",
                        "description": (
                            "Customer recipient mobile (Indian 10-digit or +91…). One number only. "
                            "Must NOT be the FROM instance phone or any employee/staff number."
                        ),
                    },
                    "message": {
                        "type": "STRING",
                        "description": "Exact text body to send (max 4000 chars).",
                    },
                    "from_instance_public_id": {
                        "type": "STRING",
                        "description": (
                            "CRM WhatsApp instance public_id — the SENDING line (FROM). "
                            "Not a recipient."
                        ),
                    },
                    "from_connected_phone": {
                        "type": "STRING",
                        "description": "Agent WhatsApp connected phone when public_id unknown (FROM only).",
                    },
                    "from_employee_code": {
                        "type": "STRING",
                        "description": "Employee code linked to the FROM instance (FROM only).",
                    },
                    "confirm_send": {
                        "type": "BOOLEAN",
                        "description": "Must be true to actually send (or to complete a dry_run check).",
                    },
                    "dry_run": {
                        "type": "BOOLEAN",
                        "description": "If true, validate ACL/number/instance but do not send.",
                    },
                    "customer_name": {
                        "type": "STRING",
                        "description": "Optional display name when creating a new CRM conversation.",
                    },
                },
                "required": ["to_phone", "message", "confirm_send"],
            },
        ),
        ToolSpec(
            name="send_whatsapp_text_batch",
            description=(
                "Send the SAME WhatsApp text sequentially to a named CUSTOMER recipient list via "
                "Evolution GO (one-by-one with delay_seconds between sends; default 10, minimum 5). "
                "CRITICAL: from_instance_* / 'from Biju' / 'from Visakh' selects the SENDING Evolution "
                "instance only — NEVER add that agent (or any staff/instance phone) to recipients[]. "
                "Before confirm_send=true, show the user: FROM: <agent> | TO: N customers (names). "
                "Refuse/strip staff & self numbers automatically. Dedupes phones in one batch. "
                "Idempotent: skips a phone if the same/near-same text was already sent from this "
                "instance within ~24h (Failed/Retry must not double-send). After a Failed run, only confirm "
                "remaining unsent recipients. "
                "Requires confirm_send=true, message, recipients (or to_phones), and FROM instance "
                "(from_instance_public_id preferred). Max 50 recipients. "
                "Do NOT refuse sequential/batch sends when the user explicitly requests them. "
                "Never invent recipients. Care Meta Cloud is blocked. "
                "Set dry_run=true to validate the full list without sending. "
                "Stops on fatal Evolution/instance errors; per-recipient failures are recorded and skipped."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "recipients": {
                        "type": "ARRAY",
                        "description": (
                            "Customer recipients only. Each item: object with to_phone (or phone) and "
                            "optional customer_name / name, OR a phone string. "
                            "Do not include the FROM agent or any employee phones."
                        ),
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "to_phone": {
                                    "type": "STRING",
                                    "description": "Customer recipient mobile.",
                                },
                                "phone": {
                                    "type": "STRING",
                                    "description": "Alias for to_phone.",
                                },
                                "customer_name": {
                                    "type": "STRING",
                                    "description": "Optional display name.",
                                },
                                "name": {
                                    "type": "STRING",
                                    "description": "Alias for customer_name.",
                                },
                            },
                        },
                    },
                    "to_phones": {
                        "type": "ARRAY",
                        "description": "Alternative to recipients: list of customer phone strings only.",
                        "items": {"type": "STRING"},
                    },
                    "message": {
                        "type": "STRING",
                        "description": "Exact text body sent to every recipient (max 4000 chars).",
                    },
                    "from_instance_public_id": {
                        "type": "STRING",
                        "description": (
                            "CRM WhatsApp instance public_id — SENDING line (FROM), never a recipient."
                        ),
                    },
                    "from_connected_phone": {
                        "type": "STRING",
                        "description": "Agent WhatsApp connected phone when public_id unknown (FROM only).",
                    },
                    "from_employee_code": {
                        "type": "STRING",
                        "description": "Employee code linked to the FROM instance (FROM only).",
                    },
                    "batch_id": {
                        "type": "STRING",
                        "description": (
                            "Optional idempotency key for this batch (reuse the same id on Retry). "
                            "Duplicate phone+message skips still apply within ~24h."
                        ),
                    },
                    "delay_seconds": {
                        "type": "NUMBER",
                        "description": (
                            f"Seconds to wait between sends (default {_WHATSAPP_BATCH_DEFAULT_DELAY_S}, "
                            f"minimum {_WHATSAPP_BATCH_MIN_DELAY_S})."
                        ),
                    },
                    "confirm_send": {
                        "type": "BOOLEAN",
                        "description": "Must be true to send (or to complete a dry_run check).",
                    },
                    "dry_run": {
                        "type": "BOOLEAN",
                        "description": "If true, validate ACL/list/instance but do not send.",
                    },
                },
                "required": ["message", "confirm_send"],
            },
        ),
        ToolSpec(
            name="list_campaign_media",
            description=(
                "List/search Campaigns Media library items (videos/images/documents) by title/topic/"
                "tags/UID. **Media UID** is the 4-digit Campaigns Media number (1000–9999), e.g. "
                "uid 1000 = GATC RAM. Use before send_whatsapp_media_batch to pick uid (preferred) "
                "or media_id. Returns uid, media_id, title/topic, media_kind, file_size, tags. "
                "When the user says 'media 1000' / 'UID 1000', filter with uid=1000."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "query": {
                        "type": "STRING",
                        "description": (
                            "Search title/topic/file_name/tags/UID string "
                            "(e.g. GATC RAM or '1000')."
                        ),
                    },
                    "uid": {
                        "type": "INTEGER",
                        "description": (
                            "Exact 4-digit Media UID (1000–9999). Accepts number 1000 when user "
                            "says media/UID 1000."
                        ),
                    },
                    "media_kind": {
                        "type": "STRING",
                        "description": "Optional filter: video | image | document.",
                    },
                    "limit": {
                        "type": "INTEGER",
                        "description": "Max rows (1–40, default 20).",
                    },
                },
            },
        ),
        ToolSpec(
            name="send_whatsapp_media_batch",
            description=(
                "Send the SAME Campaigns Media item (video/image/document) sequentially via Evolution GO "
                "to CUSTOMER recipients with delay_seconds (default 10, min 5). "
                "SHORT COMMAND: user says Media UID + Audience ID + Agent name "
                "(e.g. 'media 1000, audience 11, agent Visakh') → pass uid=1000, audience_uid=11, "
                "from_agent_name='Visakh' (or from_instance_public_id after get_employee_whatsapp_instances). "
                "Media: prefer uid (4-digit string/number OK, e.g. 1000 / '1000' for GATC RAM) "
                "or media_id / media_query. "
                "Recipients: prefer audience_uid (2-digit 11–99; number or string '11' OK) — "
                "live members from Audience page (11=Dealer Kerala, 12=Leads Kerala currently). "
                "Also accepts audience_id/name, recipients[]/to_phones, or "
                "recipient_filter='kerala_dealers'. "
                "CRITICAL: from_instance_* / from_agent_name / 'from Visakh' = SENDING line only, "
                "never a recipient. "
                "PACING: when user gives total_hours=24/36/48 (or 12–48; slow/human without hours → 24), "
                "pass total_hours — backend schedules a background job that spreads sends with "
                "random human-like gaps over that window (NOT fixed 10s blast; model must NOT wait). "
                "Requires confirm_send=true. Never auto-send — show FROM | TO: N (audience) | MEDIA | "
                "window (Nh), then confirm. dry_run=true validates. Idempotent ~24h skip by same media UID only (never skip because of unrelated text like container-strike). When scheduled/paced=true, campaign is RUNNING — not completed. "
                "Care Meta Cloud blocked."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "uid": {
                        "type": "INTEGER",
                        "description": (
                            "4-digit Campaign Media UID (1000–9999). Accept number or digit string "
                            "via media_query — e.g. 1000 when user says media/UID 1000 (GATC RAM)."
                        ),
                    },
                    "media_id": {
                        "type": "INTEGER",
                        "description": "Internal campaign media id (alternative to uid).",
                    },
                    "media_query": {
                        "type": "STRING",
                        "description": (
                            "If uid/media_id unknown, resolve by title/topic/UID string "
                            "(e.g. 'GATC RAM' or '1000'). Must match exactly one item or returns candidates."
                        ),
                    },
                    "caption": {
                        "type": "STRING",
                        "description": "Optional WhatsApp caption (max 1024 chars).",
                    },
                    "audience_uid": {
                        "type": "INTEGER",
                        "description": (
                            "Custom Audience 2-digit ID/uid (11–99) from list_audiences / Audience page. "
                            "Accepts 11 or '11'. Preferred over audience_id / audience_name. "
                            "Live: 11=Dealer Kerala, 12=Leads Kerala."
                        ),
                    },
                    "audience_id": {
                        "type": "INTEGER",
                        "description": (
                            "Custom Audience internal id, or public uid 11–99 if that number is passed. "
                            "Resolves live members with phones."
                        ),
                    },
                    "audience_name": {
                        "type": "STRING",
                        "description": (
                            "Audience name or 2-digit uid string if id unknown "
                            "(e.g. 'Dealer Kerala' or '11')."
                        ),
                    },
                    "recipients": {
                        "type": "ARRAY",
                        "description": "Customer recipients [{to_phone, customer_name}] or phone strings.",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "to_phone": {"type": "STRING"},
                                "phone": {"type": "STRING"},
                                "customer_name": {"type": "STRING"},
                                "name": {"type": "STRING"},
                            },
                        },
                    },
                    "to_phones": {
                        "type": "ARRAY",
                        "description": "Alternative customer phone list.",
                        "items": {"type": "STRING"},
                    },
                    "recipient_filter": {
                        "type": "STRING",
                        "description": (
                            "Use 'kerala_dealers' (or 'dealers' + filter_state) to resolve CRM dealer "
                            "phones instead of passing recipients. Caps at max_recipients (default 50)."
                        ),
                    },
                    "filter_state": {
                        "type": "STRING",
                        "description": "State for recipient_filter=dealers (default Kerala when kerala_dealers).",
                    },
                    "max_recipients": {
                        "type": "INTEGER",
                        "description": f"Cap after filters (default {_WHATSAPP_BATCH_MAX_RECIPIENTS}, hard max {_WHATSAPP_BATCH_MAX_RECIPIENTS}).",
                    },
                    "from_instance_public_id": {
                        "type": "STRING",
                        "description": "CRM WhatsApp instance public_id — SENDING line (FROM).",
                    },
                    "from_agent_name": {
                        "type": "STRING",
                        "description": (
                            "Agent / WhatsApp sender name when public_id unknown "
                            "(e.g. 'Visakh', 'Visakh B', 'Biju'). Matches employee_name / display_name. "
                            "FROM only — never a recipient."
                        ),
                    },
                    "from_connected_phone": {
                        "type": "STRING",
                        "description": "Agent WhatsApp phone when public_id unknown (FROM only).",
                    },
                    "from_employee_code": {
                        "type": "STRING",
                        "description": "Employee code linked to FROM instance.",
                    },
                    "batch_id": {
                        "type": "STRING",
                        "description": "Optional idempotency key; reuse on Retry.",
                    },
                    "total_hours": {
                        "type": "INTEGER",
                        "description": (
                            "Paced send window: 24, 36, or 48 (also accepts 12–48). Spreads N recipients over that many hours "
                            "with random human-like gaps (backend worker). Use when user says "
                            "'24 hours', '48h', or 'slow/human' (default 24). Prefer this over "
                            "delay_seconds for audience campaigns."
                        ),
                    },
                    "delay_seconds": {
                        "type": "NUMBER",
                        "description": (
                            f"Fixed gap for small/sync batches only (default {_WHATSAPP_BATCH_DEFAULT_DELAY_S}, "
                            f"min {_WHATSAPP_BATCH_MIN_DELAY_S}). Ignored when total_hours is set."
                        ),
                    },
                    "confirm_send": {
                        "type": "BOOLEAN",
                        "description": "Must be true to send (or complete dry_run).",
                    },
                    "dry_run": {
                        "type": "BOOLEAN",
                        "description": "Validate ACL/media/recipients without sending.",
                    },
                },
                "required": ["confirm_send"],
            },
        ),
        ToolSpec(
            name="list_audiences",
            description=(
                "List Custom Audiences from the CRM left-menu **Audience** page (`/assets`). "
                "Each has a public 2-digit **Audience ID / uid** (11–99) — prefer this number "
                "(e.g. 11 or '11'). Live examples: 11=Dealer Kerala, 12=Leads Kerala. "
                "Use for: 'list audiences', 'audience 11', 'Dealer Kerala', or before send/edit/delete. "
                "Returns uid, name, source dealers|leads, filters, live count. "
                "Do NOT invent audiences — only return tool results."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "query": {
                        "type": "STRING",
                        "description": (
                            "Optional name or 2-digit Audience ID filter (e.g. 'Kerala' or '11')."
                        ),
                    },
                    "uid": {
                        "type": "INTEGER",
                        "description": (
                            "Exact 2-digit Audience ID/uid (11–99). Accepts 11 when user says audience 11."
                        ),
                    },
                },
            },
        ),
        ToolSpec(
            name="create_audience",
            description=(
                "Create a Custom Audience on the CRM Audience page. "
                "Auto-assigns next free 2-digit Audience ID/uid (11–99). "
                "Examples: name='Dealer Kerala' source=dealers filters.state=Kerala; "
                "name='Leads Kerala' source=leads filters.state=Kerala. "
                "source dealers = left-menu Dealer; source leads = left-menu Leads. "
                "Filters are dynamic (count recomputed at read time). "
                "Requires confirm=true AFTER summarising name/source/filters and user says yes."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "name": {
                        "type": "STRING",
                        "description": "Audience display name (unique), e.g. 'Dealer Kerala'.",
                    },
                    "source": {
                        "type": "STRING",
                        "description": "dealers | leads (CRM left-menu Dealer / Leads).",
                    },
                    "state": {
                        "type": "STRING",
                        "description": "Filter state (e.g. Kerala). Stored in filters JSON.",
                    },
                    "city": {
                        "type": "STRING",
                        "description": "Optional city filter (extensible).",
                    },
                    "district": {
                        "type": "STRING",
                        "description": "Optional district filter (leads; dealers match via city).",
                    },
                    "filters": {
                        "type": "OBJECT",
                        "description": "Optional full filters object {state, city, district}.",
                    },
                    "description": {
                        "type": "STRING",
                        "description": "Optional short description.",
                    },
                    "confirm": {
                        "type": "BOOLEAN",
                        "description": "Must be true after user confirms the create summary.",
                    },
                },
                "required": ["name", "source", "confirm"],
            },
        ),
        ToolSpec(
            name="update_audience",
            description=(
                "Update an existing Custom Audience (name, source, filters, description). "
                "Resolve by Audience ID/uid (preferred, 11–99; e.g. 11), audience_id, or name. "
                "Use after list_audiences when the user says modify/edit/rename an audience. "
                "Requires confirm=true AFTER summarising the change and user says yes."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "uid": {
                        "type": "INTEGER",
                        "description": (
                            "2-digit Audience ID/uid (11–99) from list_audiences / Audience page "
                            "(e.g. 11 = Dealer Kerala)."
                        ),
                    },
                    "audience_id": {
                        "type": "INTEGER",
                        "description": "Internal audience id when known (alternative to uid).",
                    },
                    "name": {
                        "type": "STRING",
                        "description": "Current audience name or uid string to find (if id unknown).",
                    },
                    "new_name": {
                        "type": "STRING",
                        "description": "Rename to this display name.",
                    },
                    "source": {
                        "type": "STRING",
                        "description": "dealers | leads.",
                    },
                    "state": {"type": "STRING", "description": "Set/replace state filter."},
                    "city": {"type": "STRING", "description": "Set/replace city filter."},
                    "district": {"type": "STRING", "description": "Set/replace district filter."},
                    "filters": {
                        "type": "OBJECT",
                        "description": "Replace full filters object {state, city, district}.",
                    },
                    "description": {"type": "STRING"},
                    "clear_filters": {
                        "type": "BOOLEAN",
                        "description": "If true, clear all geo filters.",
                    },
                    "confirm": {
                        "type": "BOOLEAN",
                        "description": "Must be true after user confirms the update summary.",
                    },
                },
                "required": ["confirm"],
            },
        ),
        ToolSpec(
            name="delete_audience",
            description=(
                "Delete a Custom Audience from the Audience page. "
                "Requires confirm_delete=true AFTER summarising which audience and user says yes. "
                "Resolve by uid (preferred), audience_id, or name."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "uid": {
                        "type": "INTEGER",
                        "description": "2-digit audience uid (11–99).",
                    },
                    "audience_id": {"type": "INTEGER"},
                    "name": {
                        "type": "STRING",
                        "description": "Audience name or uid string if id unknown.",
                    },
                    "confirm_delete": {
                        "type": "BOOLEAN",
                        "description": "Must be true to delete.",
                    },
                },
                "required": ["confirm_delete"],
            },
        ),
        ToolSpec(
            name="preview_audience",
            description=(
                "Preview members (name/phone/city/state) for a Custom Audience by uid, id, or name. "
                "Live query against dealers or leads — not a frozen snapshot. "
                "Use before sending to an audience or when the user asks who is in 'Dealer Kerala' / uid 11."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "uid": {
                        "type": "INTEGER",
                        "description": "2-digit audience uid (11–99).",
                    },
                    "audience_id": {"type": "INTEGER"},
                    "name": {"type": "STRING", "description": "Audience name or uid string."},
                    "limit": {
                        "type": "INTEGER",
                        "description": "Max members to return (1–100, default 25).",
                    },
                    "with_phone_only": {
                        "type": "BOOLEAN",
                        "description": "If true, only members with a phone.",
                    },
                },
            },
        ),
        ToolSpec(
            name="search_google_businesses",
            description=(
                "CITY-LEVEL quick Google Places lookup only (keyword + city + state). "
                "HARD RULE: dealer/dealers/distributors/suppliers keywords are ALWAYS coerced to "
                "weighing-scale scope — never generic or other-product dealers. "
                "EXTERNAL discovery — not CRM left-menu Dealers or Leads. "
                "REQUIRES city. The word 'google' + dealers + STATE (no city) is NOT this tool — "
                "use start_dealer_finder (e.g. 'find weighing scale dealer tamilnadu from google'). "
                "Refuse statewide-only; returns redirect_to_dealer_finder. "
                "Returns name/address/city/state/phone. Read-only. For NEW vs CRM prefer "
                "find_new_dealers (also city-required)."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "keyword": {
                        "type": "STRING",
                        "description": (
                            "Weighing-scale business keyword (e.g. 'weighing scale dealers'). "
                            "Bare 'dealers' / 'distributors' is coerced to weighing scale dealers — "
                            "never generic or car/tyre/other-product dealers."
                        ),
                    },
                    "state": {
                        "type": "STRING",
                        "description": "Indian state (e.g. Uttar Pradesh, UP, Kerala, Tamil Nadu).",
                    },
                    "city": {
                        "type": "STRING",
                        "description": (
                            "REQUIRED city/town for this quick lookup "
                            "(e.g. Coimbatore, Lucknow). Without city, call start_dealer_finder."
                        ),
                    },
                    "max_results": {
                        "type": "INTEGER",
                        "description": (
                            "Max Google results (1–60, default 60). "
                            "Google Text Search returns ≤20/page, ≤60 total."
                        ),
                    },
                },
                "required": ["keyword", "state", "city"],
            },
        ),
        ToolSpec(
            name="find_new_dealers",
            description=(
                "CITY-LEVEL quick discovery of NEW weighing-scale dealers: Google Places by "
                "keyword+city+state, then exclude CRM **Dealers** / **Leads** (phone or fuzzy name+city). "
                "HARD RULE: 'dealer(s)' always means weighing-scale — keyword coerced; never generic Places "
                "'dealers' or unrelated product dealers. "
                "REQUIRES city. Do NOT use when user says google/find/search dealers + a STATE "
                "without a city — that MUST be start_dealer_finder, e.g. "
                "'find weighing scale dealer tamilnadu from google'. "
                "City example OK: 'new weighing scale dealers in Coimbatore TN'. "
                "State-only calls return redirect_to_dealer_finder (no 60-result card). "
                "Google caps ~60 hits/query. Read-only. Returns NEW-only table."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "keyword": {
                        "type": "STRING",
                        "description": (
                            "Weighing-scale keyword (default 'weighing scale dealers' if omitted). "
                            "Bare 'dealers' or other-product dealer phrases are coerced — never generic."
                        ),
                    },
                    "state": {
                        "type": "STRING",
                        "description": "Indian state, e.g. Uttar Pradesh, UP, Kerala, Tamil Nadu.",
                    },
                    "city": {
                        "type": "STRING",
                        "description": (
                            "REQUIRED city/town (e.g. Coimbatore, Chennai, Lucknow). "
                            "Omit only if redirecting — tool refuses state-only."
                        ),
                    },
                    "max_results": {
                        "type": "INTEGER",
                        "description": (
                            "Max Google results before CRM filter (1–60, default 60). "
                            "Google hard-caps Text Search at ~60 total."
                        ),
                    },
                },
                "required": ["state", "city"],
            },
        ),
        ToolSpec(
            name="start_dealer_finder",
            description=(
                "DEFAULT for statewide weighing-scale dealer discovery from Google. "
                "HARD RULE: dealer/dealers ALWAYS = weighing scale dealer — uses DEFAULT_CATEGORIES "
                "(7 weighing-scale queries). NEVER generic Places 'dealers' or unrelated categories. "
                "Bare 'dealers' without product words still means weighing-scale categories "
                "(same DEFAULT_CATEGORIES as Tamil Nadu), never generic Places 'dealers'. "
                "Trigger whenever user message has google (any case) + find/search/list dealers + "
                "a state — e.g. 'find weighing scale dealer tamilnadu from google', "
                "'weighing scale dealers list Tamilnadu from google', "
                "'search dealers in Tamil Nadu from Google', 'Google dealers in TN'. "
                "NEVER answer those with query_business_data dataset=dealers (CRM dump ~200 phones). "
                "Also: dealers in Kerala/UP, run dealer finder, all districts. "
                "NOT find_new_dealers (that is city-only ≤60). FULL batch path: Gemini lists ALL "
                "districts → cities/towns per district (cached) → Places district-wise AND "
                "city-wise × categories to completion. No partial place drip while Running. "
                "Flow: confirm start → tool waits for the batch → returns FULL unique-place list "
                "in chat → confirm import_dealer_finder_leads. "
                "Smart Search History cooldown unless force_refresh. "
                "MUTATING / costly — confirm=true after cost summary + user yes. "
                "Show results IN this chat — never send users to a separate page."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "state": {
                        "type": "STRING",
                        "description": "Indian state, e.g. Tamil Nadu, Kerala, UP.",
                    },
                    "district": {
                        "type": "STRING",
                        "description": "Optional: limit to one district.",
                    },
                    "city": {
                        "type": "STRING",
                        "description": "Optional: limit to one city/town.",
                    },
                    "search_depth": {
                        "type": "STRING",
                        "description": "full (district+city, default) | district_only | city_only",
                    },
                    "force_refresh": {
                        "type": "BOOLEAN",
                        "description": "If true, ignore search-history cooldown (costs Places API).",
                    },
                    "confirm": {
                        "type": "BOOLEAN",
                        "description": "Must be true after user confirms starting the batch.",
                    },
                },
                "required": ["state", "confirm"],
            },
        ),
        ToolSpec(
            name="get_dealer_finder_job",
            description=(
                "Poll a Dealer Finder job by public_id (or job_id). Returns status, progress "
                "(units/API), and results_ready. While Running/Pending: report progress ONLY — "
                "do NOT list places. When results_ready, call get_dealer_finder_results for the "
                "full unique-place summary. Read-only."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "public_id": {
                        "type": "STRING",
                        "description": "Job public_id from start_dealer_finder.",
                    },
                    "job_id": {
                        "type": "INTEGER",
                        "description": "Numeric job id if public_id unknown.",
                    },
                },
                "required": [],
            },
        ),
        ToolSpec(
            name="get_dealer_finder_results",
            description=(
                "Fetch the FULL mobile-eligible NEW place list for a finished Dealer Finder job "
                "(website, WhatsApp/mobile, city, district). Excludes dealers, leads, rows "
                "without phone, and landlines (Indian mobile only: 10-digit 6–9). Only when "
                "results_ready (Completed/Paused/Failed/Cancelled). "
                "If still running, returns progress and empty items — do not invent a partial "
                "table. Read-only; does NOT import. Optional list exclusions: exclude_districts, "
                "exclude_cities, exclude_keywords (case-insensitive; keywords match name/city/district "
                "contains). Use when user says remove/exclude district or keyword from the final list — "
                "do NOT re-run start_dealer_finder. After showing results, ask user to confirm "
                "import via import_dealer_finder_leads with with_phone_only=true."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "public_id": {
                        "type": "STRING",
                        "description": "Job public_id.",
                    },
                    "job_id": {
                        "type": "INTEGER",
                        "description": "Numeric job id if public_id unknown.",
                    },
                    "limit": {
                        "type": "INTEGER",
                        "description": (
                            "Max phone-eligible NEW places to return "
                            "(default 10000 — full consolidated list)."
                        ),
                    },
                    "exclude_districts": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "description": "District names to drop from the displayed/import list (exact, case-insensitive).",
                    },
                    "exclude_cities": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "description": "City/locality names to drop (exact, case-insensitive).",
                    },
                    "exclude_keywords": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "description": (
                            "Substring filters on name, city, or district (case-insensitive)."
                        ),
                    },
                    "mobile_only": {
                        "type": "BOOLEAN",
                        "description": (
                            "When true (default), exclude landlines and keep Indian mobiles only."
                        ),
                    },
                },
                "required": [],
            },
        ),
        ToolSpec(
            name="filter_dealer_finder_results",
            description=(
                "Re-fetch and re-show the Dealer Finder phone-eligible NEW list for a finished job "
                "after user-requested exclusions (same as get_dealer_finder_results with exclude_*). "
                "Use when user says remove/exclude district=X, city=Y, or keyword contains Z from "
                "the final list. Read-only; updates the chat card count (unique phone NEW). "
                "Pass the same exclude_* args to import_dealer_finder_leads if user confirms import."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "public_id": {"type": "STRING", "description": "Job public_id."},
                    "job_id": {"type": "INTEGER", "description": "Numeric job id."},
                    "limit": {
                        "type": "INTEGER",
                        "description": "Max rows to return (default 10000).",
                    },
                    "exclude_districts": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                    },
                    "exclude_cities": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                    },
                    "exclude_keywords": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                    },
                    "mobile_only": {
                        "type": "BOOLEAN",
                        "description": (
                            "When true (default), list only Indian mobiles — "
                            "exclude blank phones and landlines (STD)."
                        ),
                    },
                },
                "required": [],
            },
        ),
        ToolSpec(
            name="import_dealer_finder_leads",
            description=(
                "Import eligible NEW Dealer Finder places into CRM Leads (source=Google, "
                "status=New; owner/product optional). MUTATING — confirm-before-act: call with "
                "confirm=false first to preview eligible counts + status report, show the user, "
                "then confirm=true after they say yes / 'import to leads' / 'import … phone only'. "
                "Pass with_phone_only=true when the user asks for phone-only import. Returns job "
                "status report (units, API calls, unique places, existing dealers/leads, eligible, "
                "eligible_with_phone, imported, skipped, estimated cost)."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "public_id": {
                        "type": "STRING",
                        "description": "Job public_id (preferred).",
                    },
                    "job_id": {
                        "type": "INTEGER",
                        "description": "Numeric job id.",
                    },
                    "confirm": {
                        "type": "BOOLEAN",
                        "description": "Must be true after user confirms the import summary.",
                    },
                    "with_phone_only": {
                        "type": "BOOLEAN",
                        "description": (
                            "If true, import only eligible places that have a phone number "
                            "(skip no-phone rows; leave them NEW)."
                        ),
                    },
                },
                "required": ["confirm"],
            },
        ),
        ToolSpec(
            name="request_new_tool",
            description=(
                "Last resort ONLY for truly novel CRM capabilities that builtins cannot answer. "
                "NEVER use for product/SKU/category sales by dealer — call get_product_sales_by_dealer "
                "(or query_business_data dataset=invoice_lines) instead. "
                "NEVER use for vendors/suppliers — use search_vendors / get_vendor / get_vendor_related. "
                "NEVER use for custom audiences — use list/create/update/delete/preview_audience. "
                "NEVER use for Google / new dealer discovery — use find_new_dealers / "
                "search_google_businesses. "
                "Factory can take minutes and often times out. Admin/management only. "
                "Requires confirm=true AFTER summarising the capability and user says yes."
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
                    "confirm": {
                        "type": "BOOLEAN",
                        "description": "Must be true after user confirms starting the tool factory.",
                    },
                },
                "required": ["capability", "user_question", "confirm"],
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
    if not _truthy_confirm(args.get("confirm")):
        return {
            "ok": False,
            "saved": False,
            "needs_confirmation": True,
            "error": (
                "confirm=true required. Summarise the fact to the user "
                f"({kind}: {key} → {value}), ask them to confirm, then call again with confirm=true."
            ),
            "preview": {"kind": kind, "key": key, "value": value, "notes": notes},
        }
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

    # Builtin redirects (no factory) skip confirm; real factory start requires confirm=true.
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
    if not _truthy_confirm(args.get("confirm")):
        return {
            "ok": False,
            "factory": False,
            "needs_confirmation": True,
            "error": (
                "confirm=true required. Summarise the new capability for the user, "
                "ask them to confirm, then call again with confirm=true."
            ),
            "preview": {
                "capability": capability[:500],
                "user_question": user_question[:500],
            },
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


def _lead_candidate(lead: Lead) -> dict[str, Any]:
    company = (lead.company or "").strip()
    name = (lead.name or "").strip()
    return {
        "lead_id": lead.id,
        "name": name,
        "company": company,
        "business_name": company or name or "—",
        "city": (lead.city or "").strip() or (lead.district or "").strip(),
        "district": (lead.district or "").strip(),
        "state": (lead.state or "").strip(),
        "phone": (lead.phone or "").strip(),
        "status": (lead.status or "").strip(),
        "source": (lead.source or "").strip(),
    }


async def _query_leads_list(
    db: AsyncSession,
    *,
    query: str = "",
    state: str = "",
    city: str = "",
    district: str = "",
    status: str = "",
    limit: int = _LEAD_LIST_LIMIT,
) -> dict[str, Any]:
    """Shared leads contact list for search_leads + query_business_data dataset=leads."""
    limit = max(1, min(_LEAD_LIST_LIMIT, int(limit or _LEAD_LIST_LIMIT)))
    q = select(Lead)
    term = (query or "").strip()
    if term:
        like = f"%{term}%"
        q = q.where(
            or_(
                Lead.name.ilike(like),
                Lead.company.ilike(like),
                Lead.phone.ilike(like),
                Lead.city.ilike(like),
                Lead.district.ilike(like),
                Lead.state.ilike(like),
                Lead.pin.ilike(like),
            )
        )
    state_label = ""
    if state:
        state_label, state_vals = _resolve_state_query(state)
        if state_vals:
            q = q.where(or_(*[Lead.state.ilike(v) for v in state_vals]))
            state_label = state_label or state_vals[0]
        else:
            q = q.where(Lead.state.ilike(f"%{state}%"))
            state_label = state
    if city:
        q = q.where(
            or_(
                Lead.city.ilike(f"%{city}%"),
                Lead.district.ilike(f"%{city}%"),
            )
        )
    if district:
        dist = resolve_kerala_district_query(district) or district
        q = q.where(
            or_(
                Lead.district.ilike(f"%{dist}%"),
                Lead.city.ilike(f"%{dist}%"),
            )
        )
    status_filter = (status or "").strip().lower()
    if status_filter and status_filter != "all":
        q = q.where(func.lower(Lead.status) == status_filter)

    total_matching = (
        await db.execute(select(func.count()).select_from(q.subquery()))
    ).scalar() or 0
    district_rank = case(*district_rank_whens(Lead.district), else_=UNKNOWN_RANK)
    rows = (
        await db.execute(
            q.order_by(district_rank.asc(), Lead.name.asc(), Lead.id.asc()).limit(limit)
        )
    ).scalars().all()
    items = [_lead_candidate(lead) for lead in rows]
    with_phone = sum(1 for it in items if str(it.get("phone") or "").strip())
    scope = state_label or city or district or (term or "All")
    truncated = int(total_matching) > len(items)
    return {
        "ok": True,
        "dataset": "leads",
        "report_kind": "lead_contacts",
        "entity": "leads",
        "count": len(items),
        "total_matching": int(total_matching),
        "with_phone": with_phone,
        "truncated": truncated,
        "state": state_label or (state or None),
        "city": city or None,
        "district": district or None,
        "filter_state": state_label or (state or None),
        "filter_city": city or None,
        "scope": scope,
        "query": term or None,
        "items": items,
        "columns": ["business_name", "city", "state", "phone"],
        "note": (
            f"CRM Leads (left menu /leads) for {scope}: {len(items)} of {int(total_matching)} "
            f"({with_phone} with phone). Same set as CRM left-drawer Leads — not dealers."
            + (" Result capped by limit — raise limit to 1500 for full drawer." if truncated else "")
        ),
        "source": "leads",
    }


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


async def tool_search_leads(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    """Search/list CRM left-drawer Leads (`/leads`) — distinct from Dealer menu."""
    _ = user
    query = str(args.get("query") or "").strip()
    state = str(args.get("state") or "").strip()
    city = str(args.get("city") or "").strip()
    district = str(args.get("district") or "").strip()
    status = str(args.get("status") or "").strip()
    has_geo = bool(state or city or district)
    # List-all / find-leads (no name query): return the full drawer set.
    # Models often pass limit=25 by habit (search_dealers default) — that truncates ~400 leads.
    list_all = not query
    raw_limit = args.get("limit")
    if raw_limit is None or str(raw_limit).strip() == "":
        limit = _LEAD_LIST_LIMIT if list_all or has_geo else _LEAD_SEARCH_LIMIT
    else:
        limit = int(raw_limit)
        # Only bump the habitual mistaken default (25), not intentional top-N (e.g. 10).
        if list_all and limit == 25:
            limit = _LEAD_LIST_LIMIT
    return await _query_leads_list(
        db,
        query=query,
        state=state,
        city=city,
        district=district,
        status=status,
        limit=limit,
    )


async def _crm_contacts_for_state(db: AsyncSession, state: str) -> tuple[str, list[Any], int, int]:
    """Load dealers + leads for a state as CrmContact list + counts."""
    from app.google_places_search import CrmContact, resolve_state
    from app.phone_util import normalize_in_mobile

    state_label, state_vals = resolve_state(state)
    # Also merge AI tools state aliases when resolve_state is sparse
    _, tool_vals = _resolve_state_query(state)
    match_vals: list[str] = []
    for v in [*state_vals, *tool_vals, state]:
        vv = (v or "").strip()
        if vv and vv not in match_vals:
            match_vals.append(vv)
    if not match_vals:
        match_vals = [state]
    state_label = state_label or match_vals[0]

    visible = dealer_not_excluded_clause(Dealer.name, Dealer.company, Dealer.zoho_contact_id)
    dealer_q = select(Dealer).where(visible).where(
        or_(*[Dealer.state.ilike(v) for v in match_vals])
    )
    lead_q = select(Lead).where(or_(*[Lead.state.ilike(v) for v in match_vals]))

    dealers = list((await db.execute(dealer_q)).scalars().all())
    leads = list((await db.execute(lead_q)).scalars().all())

    contacts: list[CrmContact] = []
    for d in dealers:
        name = (d.company or d.name or "").strip()
        contacts.append(
            CrmContact(
                name=name or (d.name or ""),
                city=(d.city or "").strip(),
                phone10=normalize_in_mobile(d.phone or ""),
                kind="dealer",
                record_id=int(d.id),
            )
        )
    for lead in leads:
        name = (lead.company or lead.name or "").strip()
        city = (lead.city or lead.district or "").strip()
        contacts.append(
            CrmContact(
                name=name or (lead.name or ""),
                city=city,
                phone10=normalize_in_mobile(lead.phone or ""),
                kind="lead",
                record_id=int(lead.id),
            )
        )
    return state_label, contacts, len(dealers), len(leads)


async def tool_search_google_businesses(
    db: AsyncSession, user: User, args: dict[str, Any]
) -> dict[str, Any]:
    """Google Places Text Search — city-level quick lookup (not CRM)."""
    _ = db, user
    from app.google_places_search import (
        build_text_query,
        place_to_row,
        resolve_state,
        search_places_text,
    )

    from app.google_places_search import (
        default_weighing_places_keyword,
        weighing_places_scope_note,
    )

    raw_keyword = str(args.get("keyword") or "")
    keyword = default_weighing_places_keyword(raw_keyword)
    scope_note = weighing_places_scope_note(raw_keyword, keyword)
    state = str(args.get("state") or "").strip()
    city = str(args.get("city") or "").strip()
    if not state:
        return {"ok": False, "error": "state is required (e.g. Uttar Pradesh / UP)"}
    if not city:
        state_label, _ = resolve_state(state)
        return {
            "ok": False,
            "error": (
                "city is required for quick Google lookup. "
                "For whole-state coverage use start_dealer_finder."
            ),
            "redirect_to_dealer_finder": True,
            "state": state_label or state,
            "keyword": keyword,
            "keyword_requested": raw_keyword.strip() or None,
            "weighing_scope_note": scope_note,
            "items": [],
            "count": 0,
            "report_kind": "google_businesses",
            "note": (
                f"Statewide search for {state_label or state} must use start_dealer_finder "
                "(district→city × categories). Do not run a single Places query."
            ),
        }
    max_results = int(args.get("max_results") or 60)
    state_label, _ = resolve_state(state)
    query = build_text_query(keyword, state, city=city)
    raw = await search_places_text(text_query=query, max_results=max_results)
    if not raw.get("ok") and not raw.get("places"):
        return {
            "ok": False,
            "error": raw.get("error") or "Google Places search failed",
            "query": query,
            "state": state_label or state,
            "city": city,
            "keyword": keyword,
            "items": [],
            "count": 0,
            "report_kind": "google_businesses",
        }

    items = [
        place_to_row(p, state_fallback=state_label or state)
        for p in (raw.get("places") or [])
    ]
    with_phone = sum(1 for it in items if str(it.get("phone") or "").strip())
    scope = f"{city}, {state_label or state}"
    return {
        "ok": True if raw.get("ok") else bool(items),
        "error": None if raw.get("ok") else raw.get("error"),
        "report_kind": "google_businesses",
        "source": "google_places",
        "keyword": keyword,
        "keyword_requested": raw_keyword.strip() or None,
        "weighing_scope_note": scope_note,
        "state": state_label or state,
        "city": city,
        "scope": scope,
        "query": query,
        "count": len(items),
        "with_phone": with_phone,
        "pages": raw.get("pages") or 0,
        "items": items,
        "columns": ["name", "address", "city", "state", "phone"],
        "note": (
            f"Google Places: {len(items)} businesses for {keyword!r} in "
            f"{scope} ({with_phone} with phone). "
            "Not filtered against CRM — use find_new_dealers for NEW only."
            + (f" {scope_note}" if scope_note else "")
        ),
    }


def _dealer_finder_results_payload(
    *,
    job: Any,
    pub: dict[str, Any],
    report: dict[str, Any],
    counts: dict[str, int],
    items: list[dict[str, Any]],
    filtered_total: int,
    incomplete: bool,
    waited: bool = False,
    list_exclusions: dict[str, list[str]] | None = None,
    rows_removed: list[dict[str, Any]] | None = None,
    importable_phone_count: int | None = None,
    mobile_only: bool = True,
) -> dict[str, Any]:
    """Build chat results card payload: phone-eligible NEW only + matching confirm copy."""
    from app.dealer_finder.jobs import build_import_confirm_prompt
    from app.dealer_finder.row_filters import exclusions_active

    err = str(getattr(job, "error", None) or "").strip()
    done = int(getattr(job, "completed_units", 0) or 0)
    total_units = int(getattr(job, "total_units", 0) or 0)
    base_phone_n = int(counts.get("eligible_with_phone") or 0)
    phone_dupes = int(counts.get("phone_dupes_removed") or 0)
    if importable_phone_count is not None and int(filtered_total) != base_phone_n:
        phone_dupes = int(
            counts.get("discovery_mobile_dupes_removed")
            or counts.get("discovery_any_phone_dupes_removed")
            or phone_dupes
        )
    exclusions = list_exclusions or {}
    has_excl = exclusions_active(exclusions)
    phone_n = int(filtered_total)
    removed_n = len(rows_removed or [])
    excl_note = ""
    if has_excl and removed_n:
        excl_note = (
            f" List exclusions removed {removed_n} row(s) "
            f"(showing {phone_n} unique phone NEW)."
        )
    remaining = max(0, total_units - done)
    if incomplete:
        note = (
            f"INCOMPLETE scan ({getattr(job, 'status', '')}"
            f"{': ' + err if err else ''}): {done}/{total_units} units "
            f"({remaining} remaining) → "
            f"{phone_n} unique phone NEW in table"
            f"{f' ({phone_dupes} dupes removed)' if phone_dupes else ''}."
            f"{excl_note} "
            "Not full statewide coverage yet."
        )
    else:
        note = (
            f"{phone_n} unique mobile NEW places "
            f"(of {counts['unique_places']} unique places scanned; "
            f"{counts['eligible_import']} eligible NEW; "
            f"{counts['already_dealer']} dealers, {counts['already_lead']} leads excluded)"
            f"{f'; {phone_dupes} duplicate phones removed from list' if phone_dupes else ''}."
            f"{excl_note} "
            "Ask user to confirm before import_dealer_finder_leads (with_phone_only=true)."
        )
    import_n = (
        int(importable_phone_count)
        if importable_phone_count is not None
        else phone_n
    )
    import_prompt = build_import_confirm_prompt(
        counts,
        incomplete=incomplete,
        done=done,
        total_units=total_units,
        error=err,
        list_phone_count=import_n if (has_excl or importable_phone_count is not None) else None,
    )
    # Chat summary badges must use phone-eligible NEW — not raw with_phone (all statuses).
    summary = {
        **counts,
        "with_phone": phone_n,
        "eligible_with_phone": phone_n,
        "importable_with_phone": import_n,
    }
    if has_excl:
        summary["eligible_with_phone_before_exclusions"] = base_phone_n
        summary["list_exclusions_removed"] = removed_n
    payload: dict[str, Any] = {
        "ok": True,
        "results_ready": True,
        "scan_complete": not incomplete,
        "incomplete": incomplete,
        "waited": waited,
        "report_kind": "dealer_finder_results",
        "job": pub,
        "public_id": getattr(job, "public_id", None) or pub.get("public_id"),
        "status_report": report,
        "summary": summary,
        "with_phone": phone_n,
        "eligible_with_phone": phone_n,
        "importable_with_phone": import_n,
        "count": len(items),
        "total_unique": counts["unique_places"],
        "filtered_total": phone_n,
        "phone_dupes_removed": phone_dupes,
        "eligible_with_phone_raw": int(counts.get("eligible_with_phone_raw") or base_phone_n),
        "phone_list_format_version": int(counts.get("phone_list_format_version") or 2),
        "display_filter": (
            "eligible_new_with_mobile" if mobile_only else "eligible_new_with_phone"
        ),
        "mobile_only": bool(mobile_only),
        "truncated": phone_n > len(items),
        "items": items,
        "columns": ["name", "whatsapp", "website", "city", "district"],
        "note": note,
        "example_import_confirm_prompt": import_prompt,
    }
    if has_excl:
        payload["list_exclusions"] = exclusions
        payload["rows_excluded"] = removed_n
        payload["excluded_rows_preview"] = (rows_removed or [])[:25]
    return payload


async def _await_dealer_finder_results(
    *,
    public_id: str,
    limit: int = 10_000,
    timeout_s: float = 600.0,
    poll_s: float = 3.0,
) -> dict[str, Any]:
    """Poll job in fresh sessions until results_ready, then return phone-eligible NEW list."""
    import asyncio

    from app.db import SessionLocal
    from app.dealer_finder.jobs import (
        DISPLAY_ELIGIBLE_PHONE_LIMIT,
        build_job_status_report,
        job_public,
        job_results_ready,
        job_scan_complete,
        list_eligible_phone_places,
        place_counts_for_job,
        place_row_public,
        wake_dealer_finder_worker,
    )
    from app.models import DealerSearchJob

    deadline = time.perf_counter() + max(30.0, timeout_s)
    last_job: DealerSearchJob | None = None
    while time.perf_counter() < deadline:
        wake_dealer_finder_worker()
        async with SessionLocal() as sdb:
            job = (
                await sdb.execute(
                    select(DealerSearchJob).where(DealerSearchJob.public_id == public_id)
                )
            ).scalar_one_or_none()
            if not job:
                return {"ok": False, "error": f"job {public_id} not found while waiting"}
            last_job = job
            if job_results_ready(job):
                report = await build_job_status_report(sdb, job)
                lim = max(
                    1,
                    min(
                        DISPLAY_ELIGIBLE_PHONE_LIMIT,
                        int(limit or DISPLAY_ELIGIBLE_PHONE_LIMIT),
                    ),
                )
                discovery = job_scan_complete(job)
                rows, filtered_total = await list_eligible_phone_places(
                    sdb,
                    job.id,
                    limit=lim,
                    mobile_only=True,
                    discovery=discovery,
                )
                counts = await place_counts_for_job(sdb, job.id)
                if discovery:
                    filtered_total = int(
                        counts.get("discovery_mobile_new") or filtered_total
                    )
                importable = int(
                    counts.get("importable_with_phone")
                    or counts.get("eligible_with_phone")
                    or 0
                )
                items = [place_row_public(r) for r in rows]
                pub = job_public(job)
                incomplete = not job_scan_complete(job)
                return _dealer_finder_results_payload(
                    job=job,
                    pub=pub,
                    report=report,
                    counts=counts,
                    items=items,
                    filtered_total=filtered_total,
                    incomplete=incomplete,
                    waited=True,
                    importable_phone_count=importable if discovery else None,
                    mobile_only=True,
                )
        await asyncio.sleep(poll_s)

    # Timed out — return progress so chat can keep polling client-side.
    if last_job is None:
        return {"ok": False, "error": f"job {public_id} not found"}
    async with SessionLocal() as sdb:
        job = (
            await sdb.execute(
                select(DealerSearchJob).where(DealerSearchJob.public_id == public_id)
            )
        ).scalar_one_or_none() or last_job
        report = await build_job_status_report(sdb, job) if job.id else {}
        pub = job_public(job)
        return {
            "ok": True,
            "results_ready": False,
            "waited": True,
            "awaiting_completion": True,
            "report_kind": "dealer_finder_progress",
            "job": pub,
            "public_id": public_id,
            "status_report": report,
            "items": [],
            "count": 0,
            "note": (
                f"Still scanning {job.state}: {job.completed_units}/{job.total_units} units "
                f"({job.status}). Keep waiting in chat — results table appears when complete. "
                "Do NOT send the user to another page."
            ),
        }


async def tool_start_dealer_finder(
    db: AsyncSession, user: User, args: dict[str, Any]
) -> dict[str, Any]:
    """Start district→city×category Dealer Finder batch (confirm-before-act).

    After confirm=true, commits the job, waits for completion, and returns the full
    unique-place list for in-chat rendering (ChatGPT-style).
    """
    from app.dealer_finder.india_geo import districts_for_state
    from app.dealer_finder.jobs import create_search_job, job_public, wake_dealer_finder_worker
    from app.dealer_finder.normalize import canon_location_state

    state = str(args.get("state") or "").strip()
    if not state:
        return {"ok": False, "error": "state is required (e.g. Tamil Nadu)"}
    confirm = _truthy_confirm(args.get("confirm"))
    label = canon_location_state(state)
    districts = districts_for_state(label)
    depth = str(args.get("search_depth") or "full").strip() or "full"
    force = bool(args.get("force_refresh"))
    district = str(args.get("district") or "").strip()
    city = str(args.get("city") or "").strip()
    # Statewide jobs always expand district + city (ignore shallow depth args).
    if not district and not city:
        depth = "full"

    if not confirm:
        return {
            "ok": False,
            "needs_confirm": True,
            "report_kind": "dealer_finder_confirm",
            "state": label,
            "district_count_verified": len(districts),
            "search_depth": depth,
            "force_refresh": force,
            "summary": (
                f"Dealer Finder FULL job for {label}: Gemini lists ALL districts "
                f"({len(districts)} verified seed) → cities/towns per district → "
                f"Google Places district-wise AND city-wise × weighing-scale categories "
                f"(depth={depth}) until complete. Results appear in this chat when done; "
                f"then confirm before importing eligible leads. Cooldown skips recent "
                f"searches unless force_refresh."
            ),
            "warning": "This can use many Google Places requests (cost).",
            "please_confirm": "Ask the user to confirm, then call again with confirm=true.",
            "example_confirm_prompt": (
                f"Start full Dealer Finder for {label} (all districts → cities × categories)? "
                "This uses Google Places (API cost). Reply yes to start — I will show the "
                "full results table here in chat when the scan finishes."
            ),
        }

    try:
        job = await create_search_job(
            db,
            user=user,
            state=label,
            district=district,
            city=city,
            search_depth=depth,
            force_refresh=force,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    public_id = job.public_id
    total_units = int(job.total_units or 0)
    # Commit so the in-API worker can claim the job while we wait.
    await db.commit()
    wake_dealer_finder_worker()
    waited = await _await_dealer_finder_results(
        public_id=public_id, limit=10_000, timeout_s=600.0
    )
    if waited.get("results_ready"):
        return waited
    # Still running after wait — chat UI polls by public_id; do not redirect.
    return {
        "ok": True,
        "results_ready": False,
        "awaiting_completion": True,
        "report_kind": "dealer_finder_progress",
        "job": waited.get("job") or job_public(job),
        "public_id": public_id,
        "status_report": waited.get("status_report"),
        "items": [],
        "count": 0,
        "note": (
            waited.get("note")
            or (
                f"Started FULL job {public_id} for {label} ({total_units} units). "
                "Scanning in background — results table will appear in this chat when ready. "
                "Do NOT tell the user to open another page."
            )
        ),
    }


async def _resolve_dealer_finder_job(
    db: AsyncSession, args: dict[str, Any]
):
    from app.models import DealerSearchJob

    public_id = str(args.get("public_id") or "").strip()
    job_id_raw = args.get("job_id")
    if public_id:
        job = (
            await db.execute(
                select(DealerSearchJob).where(DealerSearchJob.public_id == public_id)
            )
        ).scalar_one_or_none()
        if job:
            return job
    if job_id_raw is not None and str(job_id_raw).strip() != "":
        try:
            jid = int(job_id_raw)
        except (TypeError, ValueError):
            return None
        return await db.get(DealerSearchJob, jid)
    state = str(args.get("state") or "").strip()
    if state:
        # Latest completed (or any) job for this state — prefer most places_found.
        rows = (
            await db.execute(
                select(DealerSearchJob)
                .where(DealerSearchJob.state.ilike(state))
                .order_by(
                    DealerSearchJob.places_found.desc().nullslast(),
                    DealerSearchJob.id.desc(),
                )
                .limit(5)
            )
        ).scalars().all()
        if rows:
            completed = [j for j in rows if (j.status or "").lower() == "completed"]
            return completed[0] if completed else rows[0]
    return None


async def tool_get_dealer_finder_job(
    db: AsyncSession, user: User, args: dict[str, Any]
) -> dict[str, Any]:
    """Poll Dealer Finder job progress (no place drip while running)."""
    _ = user
    from app.dealer_finder.jobs import build_job_status_report, job_public, job_results_ready

    job = await _resolve_dealer_finder_job(db, args)
    if not job:
        return {"ok": False, "error": "job not found — pass public_id or job_id"}
    ready = job_results_ready(job)
    report = await build_job_status_report(db, job)
    return {
        "ok": True,
        "report_kind": "dealer_finder_progress",
        "job": job_public(job),
        "results_ready": ready,
        "status_report": report,
        "note": (
            "Results ready — call get_dealer_finder_results for the full unique-place list."
            if ready
            else (
                f"Job {job.status}: {job.completed_units}/{job.total_units} units. "
                "Do NOT list places yet — wait until results_ready."
            )
        ),
    }


async def _dealer_finder_phone_results(
    db: AsyncSession,
    job: Any,
    args: dict[str, Any],
    *,
    waited: bool = False,
) -> dict[str, Any]:
    from app.dealer_finder.jobs import (
        DISPLAY_ELIGIBLE_PHONE_LIMIT,
        build_job_status_report,
        job_public,
        job_results_ready,
        list_eligible_phone_places,
        place_counts_for_job,
        place_row_public,
    )
    from app.dealer_finder.row_filters import (
        exclusions_active,
        filter_rows,
        parse_exclusion_args,
        parse_mobile_only_arg,
    )
    from app.dealer_finder.jobs import job_scan_complete

    ready = job_results_ready(job)
    report = await build_job_status_report(db, job)
    if not ready:
        return {
            "ok": True,
            "results_ready": False,
            "report_kind": "dealer_finder_results_pending",
            "job": job_public(job),
            "status_report": report,
            "items": [],
            "count": 0,
            "note": (
                f"Job still {job.status} — withhold place list until complete. "
                "Poll get_dealer_finder_job."
            ),
        }

    limit = max(
        1,
        min(
            DISPLAY_ELIGIBLE_PHONE_LIMIT,
            int(args.get("limit") or DISPLAY_ELIGIBLE_PHONE_LIMIT),
        ),
    )
    mobile_only = parse_mobile_only_arg(args)
    discovery = job_scan_complete(job)
    rows, filtered_total = await list_eligible_phone_places(
        db,
        job.id,
        limit=DISPLAY_ELIGIBLE_PHONE_LIMIT,
        offset=0,
        mobile_only=mobile_only,
        discovery=discovery,
    )
    exclusions = parse_exclusion_args(args)
    removed_meta: list[dict[str, Any]] = []
    if exclusions_active(exclusions):
        rows, removed_meta = filter_rows(rows, exclusions)
        filtered_total = len(rows)
    rows = rows[:limit]
    counts = await place_counts_for_job(db, job.id)
    importable_phone_n = int(counts.get("importable_with_phone") or counts.get("eligible_with_phone") or 0)
    if discovery:
        if mobile_only:
            display_total = int(counts.get("discovery_mobile_new") or filtered_total)
        else:
            display_total = int(counts.get("discovery_any_phone_new") or filtered_total)
        if not exclusions_active(exclusions):
            filtered_total = display_total
    items = [place_row_public(r) for r in rows]
    pub = job_public(job)
    incomplete = bool(pub.get("incomplete"))
    return _dealer_finder_results_payload(
        job=job,
        pub=pub,
        report=report,
        counts=counts,
        items=items,
        filtered_total=filtered_total,
        incomplete=incomplete,
        waited=waited,
        list_exclusions=exclusions if exclusions_active(exclusions) else None,
        rows_removed=removed_meta,
        importable_phone_count=importable_phone_n if discovery else None,
        mobile_only=mobile_only,
    )


async def tool_get_dealer_finder_results(
    db: AsyncSession, user: User, args: dict[str, Any]
) -> dict[str, Any]:
    """Phone-eligible NEW place list when job finished; empty while running."""
    _ = user
    job = await _resolve_dealer_finder_job(db, args)
    if not job:
        return {"ok": False, "error": "job not found — pass public_id or job_id"}
    return await _dealer_finder_phone_results(db, job, args, waited=False)


async def tool_filter_dealer_finder_results(
    db: AsyncSession, user: User, args: dict[str, Any]
) -> dict[str, Any]:
    """Re-show Dealer Finder list with exclude district/city/keyword filters (no re-scan)."""
    _ = user
    job = await _resolve_dealer_finder_job(db, args)
    if not job:
        return {"ok": False, "error": "job not found — pass public_id or job_id"}
    from app.dealer_finder.row_filters import exclusions_active, parse_exclusion_args

    from app.dealer_finder.row_filters import parse_mobile_only_arg

    exclusions = parse_exclusion_args(args)
    mobile_only = parse_mobile_only_arg(args)
    if not exclusions_active(exclusions) and not mobile_only:
        return {
            "ok": False,
            "error": (
                "Pass exclude_districts, exclude_cities, and/or exclude_keywords "
                "(e.g. exclude_districts=['Mahe']), or mobile_only=true to refresh "
                "the mobile-only phone-NEW list."
            ),
        }
    return await _dealer_finder_phone_results(db, job, args, waited=False)


async def tool_import_dealer_finder_leads(
    db: AsyncSession, user: User, args: dict[str, Any]
) -> dict[str, Any]:
    """Import eligible places as Leads (confirm-before-act) + status report."""
    from app.dealer_finder.jobs import (
        build_job_status_report,
        import_eligible_places,
        place_counts_for_job,
    )

    job = await _resolve_dealer_finder_job(db, args)
    if not job:
        return {"ok": False, "error": "job not found — pass public_id or job_id"}
    counts = await place_counts_for_job(db, job.id)
    report = await build_job_status_report(db, job)
    confirm = _truthy_confirm(args.get("confirm"))
    with_phone_only = _truthy_confirm(args.get("with_phone_only"))
    # Chat results list is phone-eligible NEW; default confirm copy matches that filter.
    if "with_phone_only" not in args:
        with_phone_only = True
    from app.dealer_finder.jobs import build_import_confirm_prompt

    eligible_n = (
        int(counts.get("eligible_with_phone") or 0)
        if with_phone_only
        else int(counts["eligible_import"])
    )
    phone_clause = " phone-eligible" if with_phone_only else ""
    if not confirm:
        return {
            "ok": False,
            "needs_confirm": True,
            "report_kind": "dealer_finder_import_confirm",
            "job_id": job.id,
            "public_id": job.public_id,
            "eligible": eligible_n,
            "eligible_all": counts["eligible_import"],
            "eligible_with_phone": counts.get("eligible_with_phone", 0),
            "with_phone_only": with_phone_only,
            "preview": counts,
            "status_report": report,
            "summary": (
                f"Import {eligible_n}{phone_clause} NEW places from job "
                f"{job.public_id} ({job.state}) as Leads (source=Google, status=New). "
                f"Unique places={counts['unique_places']}; "
                f"eligible NEW={counts['eligible_import']}; "
                f"phone-eligible={counts.get('eligible_with_phone', 0)}; "
                f"existing dealers={counts['already_dealer']}; "
                f"existing leads={counts['already_lead']}."
            ),
            "please_confirm": "Ask the user to confirm, then call again with confirm=true.",
            "example_confirm_prompt": (
                build_import_confirm_prompt(counts)
                if with_phone_only
                else (
                    f"Import {eligible_n} eligible NEW places to Leads "
                    f"(source Google, status New)? "
                    f"{counts.get('eligible_with_phone', 0)} have phone. "
                    'Reply yes for all eligible, or "import phone only".'
                )
            ),
        }

    result = await import_eligible_places(
        db, user=user, job_id=job.id, with_phone_only=with_phone_only
    )
    return {
        "ok": True,
        "report_kind": "dealer_finder_import_done",
        "inserted": result.get("inserted"),
        "skipped_duplicates": result.get("skipped_duplicates"),
        "skipped_no_phone": result.get("skipped_no_phone"),
        "with_phone_only": with_phone_only,
        "received": result.get("received"),
        "status_report": result.get("status_report")
        or await build_job_status_report(db, job, import_result=result),
        "note": (
            f"Imported {result.get('inserted', 0)} leads"
            f"{' (phone only)' if with_phone_only else ''}; "
            f"skipped {result.get('skipped_duplicates', 0)} duplicates"
            + (
                f", {result.get('skipped_no_phone', 0)} without phone"
                if with_phone_only
                else ""
            )
            + "."
        ),
    }


async def tool_find_new_dealers(
    db: AsyncSession, user: User, args: dict[str, Any]
) -> dict[str, Any]:
    """City-level Google Places + CRM → NEW only. State-only → redirect Dealer Finder."""
    _ = user
    from app.google_places_search import (
        build_text_query,
        crm_match_reason,
        place_to_row,
        resolve_state,
        search_places_text,
    )

    from app.google_places_search import (
        default_weighing_places_keyword,
        weighing_places_scope_note,
    )

    raw_keyword = str(args.get("keyword") or "")
    keyword = default_weighing_places_keyword(raw_keyword)
    scope_note = weighing_places_scope_note(raw_keyword, keyword)
    state = str(args.get("state") or "").strip()
    city = str(args.get("city") or "").strip()
    if not state:
        return {"ok": False, "error": "state is required (e.g. Uttar Pradesh / UP)"}
    if not city:
        state_label, _ = resolve_state(state)
        label = state_label or state
        return {
            "ok": False,
            "error": (
                "city is required for find_new_dealers (quick city lookup only). "
                "For whole-state Google discovery use start_dealer_finder."
            ),
            "redirect_to_dealer_finder": True,
            "state": label,
            "keyword": keyword,
            "keyword_requested": raw_keyword.strip() or None,
            "weighing_scope_note": scope_note,
            "items": [],
            "count": 0,
            "report_kind": "new_dealers",
            "note": (
                f"Statewide / 'from google' search for {label} must use start_dealer_finder "
                "(all districts → cities × categories). Do not use the 60-result card."
            ),
            "next_tool": "start_dealer_finder",
            "example_confirm_prompt": (
                f"Start full Dealer Finder for {label} "
                "(all districts → cities × categories)? This uses Google Places (API cost). "
                "Reply yes to start."
            ),
        }
    max_results = int(args.get("max_results") or 60)

    state_label, crm_contacts, dealer_count, lead_count = await _crm_contacts_for_state(
        db, state
    )
    if not state_label:
        state_label, _ = resolve_state(state)
        state_label = state_label or state

    query = build_text_query(keyword, state, city=city)
    raw = await search_places_text(text_query=query, max_results=max_results)
    google_places = list(raw.get("places") or [])
    if not raw.get("ok") and not google_places:
        return {
            "ok": False,
            "error": raw.get("error") or "Google Places search failed",
            "query": query,
            "state": state_label,
            "keyword": keyword,
            "crm_dealers_in_state": dealer_count,
            "crm_leads_in_state": lead_count,
            "items": [],
            "count": 0,
            "report_kind": "new_dealers",
        }

    new_items: list[dict[str, Any]] = []
    existing_items: list[dict[str, Any]] = []
    for p in google_places:
        row = place_to_row(p, state_fallback=state_label)
        is_existing, reason = crm_match_reason(row, crm_contacts)
        if is_existing:
            existing_items.append({**row, "crm_match": reason})
        else:
            new_items.append(row)

    with_phone = sum(1 for it in new_items if str(it.get("phone") or "").strip())
    return {
        "ok": True if raw.get("ok") else bool(google_places),
        "error": None if raw.get("ok") else raw.get("error"),
        "report_kind": "new_dealers",
        "source": "google_places_minus_crm",
        "keyword": keyword,
        "keyword_requested": raw_keyword.strip() or None,
        "weighing_scope_note": scope_note,
        "state": state_label,
        "city": city,
        "scope": f"{city}, {state_label}",
        "query": query,
        "google_total": len(google_places),
        "crm_dealers_in_state": dealer_count,
        "crm_leads_in_state": lead_count,
        "already_in_crm": len(existing_items),
        "count": len(new_items),
        "with_phone": with_phone,
        "pages": raw.get("pages") or 0,
        "items": new_items,
        "columns": ["name", "address", "city", "state", "phone"],
        "note": (
            f"NEW only: {len(new_items)} of {len(google_places)} Google results "
            f"not in CRM Dealers ({dealer_count}) or Leads ({lead_count}) for "
            f"{city}, {state_label}. Matched {len(existing_items)} as already in CRM "
            f"(phone or name+city). Search-only — confirm before any import."
            + (f" {scope_note}" if scope_note else "")
        ),
        "teaching": {
            "crm_dealers": "Left menu → Dealer",
            "crm_leads": "Left menu → Leads",
            "google": "External Places discovery",
            "new": "Google hit not matched to dealers/leads by phone or name+city",
        },
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
    "kerala": ["Keralam", "Kerala"],
    "keralam": ["Keralam", "Kerala"],
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
    "pondichery": ["Puducherry", "Pondicherry", "Pondichery"],
    "puducherry": ["Puducherry", "Pondicherry"],
    "up": ["Uttar Pradesh", "UP", "UttarPradesh", "Uthar Pradesh"],
    "uttarpradesh": ["Uttar Pradesh", "UP", "UttarPradesh", "Uthar Pradesh"],
    "utharpradesh": ["Uttar Pradesh", "UP", "UttarPradesh", "Uthar Pradesh"],
    "madhyapradesh": ["Madhya Pradesh", "MP"],
    "mp": ["Madhya Pradesh", "MP"],
    "westbengal": ["West Bengal"],
    "bihar": ["Bihar"],
    "punjab": ["Punjab"],
    "haryana": ["Haryana"],
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
        "lead": "leads",
        "crm_leads": "leads",
    }
    dataset = aliases.get(dataset, dataset)
    group_by = str(args.get("group_by") or "").strip().lower() or "none"
    product_query = str(args.get("product_query") or "").strip()
    dealer_query = str(args.get("dealer_query") or "").strip()
    city = str(args.get("city") or "").strip()
    state = str(args.get("state") or "").strip()
    # Dealer/lead contact lists need the full filtered set (not top-25 rankings).
    if dataset in {"dealers", "leads"}:
        cap = _DEALER_LIST_LIMIT if dataset == "dealers" else _LEAD_LIST_LIMIT
        raw_lim = args.get("limit")
        if raw_lim is None or str(raw_lim).strip() == "":
            limit = cap
        else:
            limit = max(1, min(cap, int(raw_lim)))
            # dataset=leads "find leads" with model habit limit=25 → full drawer
            # (keep intentional top-N like 10; only rewrite the mistaken default 25)
            if dataset == "leads" and limit == 25 and not str(args.get("query") or "").strip():
                limit = cap
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
                f"CRM Dealers (existing contacts, not Google) for {scope}: "
                f"{len(items)} of {int(total_matching)} ({with_phone} with phone)."
                + (" Result capped by limit — raise limit." if truncated else "")
                + " For Google Places statewide discovery use start_dealer_finder."
            ),
            "source": "dealers",
            "source_label": "CRM Dealers",
        }

    if dataset == "leads":
        return await _query_leads_list(
            db,
            query=str(args.get("query") or dealer_query or "").strip(),
            state=state,
            city=city,
            district=str(args.get("district") or "").strip(),
            status=str(args.get("status") or "").strip(),
            limit=limit,
        )

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
            "leads",
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


def _truthy_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    s = str(value).strip().lower()
    if s in {"true", "yes", "1", "on"}:
        return True
    if s in {"false", "no", "0", "off"}:
        return False
    return default


async def tool_search_whatsapp_messages(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    from app.ai.whatsapp_intel import parse_keywords, search_messages

    allowed = await authorized_instance_ids(db, user)
    kws = parse_keywords(args.get("query"))
    for extra in parse_keywords(args.get("keywords")):
        if extra.casefold() not in {k.casefold() for k in kws}:
            kws.append(extra)
    return await search_messages(
        db,
        allowed=allowed,
        keywords=kws,
        match_any=_truthy_bool(args.get("match_any"), False),
        days=args.get("days"),
        date_from=str(args.get("date_from") or ""),
        date_to=str(args.get("date_to") or ""),
        direction=str(args.get("direction") or ""),
        contact=str(args.get("contact") or ""),
        instance_query=str(args.get("instance") or ""),
        unread_only=_truthy_bool(args.get("unread_only"), False),
        open_only=_truthy_bool(args.get("open_only"), False),
        limit=int(args.get("limit") or 25),
        dedupe_by_phone=False,
    )


async def tool_list_whatsapp_threads(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    from app.ai.whatsapp_intel import list_threads

    allowed = await authorized_instance_ids(db, user)
    return await list_threads(
        db,
        allowed=allowed,
        query=str(args.get("query") or ""),
        instance_query=str(args.get("instance") or ""),
        unread_only=_truthy_bool(args.get("unread_only"), False),
        open_only=_truthy_bool(args.get("open_only"), False),
        status=str(args.get("status") or ""),
        limit=int(args.get("limit") or 25),
    )


async def tool_get_whatsapp_thread(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    from app.ai.whatsapp_intel import get_thread

    allowed = await authorized_instance_ids(db, user)
    return await get_thread(
        db,
        allowed=allowed,
        thread_id=str(args.get("thread_id") or args.get("conversation_id") or ""),
        limit=int(args.get("limit") or 40),
    )


async def tool_report_enquiries(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    from app.ai.whatsapp_intel import parse_keywords, report_enquiries

    allowed = await authorized_instance_ids(db, user)
    return await report_enquiries(
        db,
        allowed=allowed,
        topic=str(args.get("topic") or args.get("query") or ""),
        keywords=parse_keywords(args.get("keywords")),
        days=args.get("days"),
        date_from=str(args.get("date_from") or ""),
        date_to=str(args.get("date_to") or ""),
        instance_query=str(args.get("instance") or ""),
        inbound_only=_truthy_bool(args.get("inbound_only"), True),
        limit=int(args.get("limit") or 25),
    )


def _truthy_confirm(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    s = str(value or "").strip().lower()
    return s in {"true", "yes", "1", "confirm", "confirmed"}


def _instance_name_haystack(inst: WhatsAppInstance) -> str:
    parts = [
        inst.employee_name or "",
        inst.display_name or "",
        getattr(inst, "evolution_instance_name", None) or "",
        inst.employee_code or "",
    ]
    return " ".join(p.strip() for p in parts if p and str(p).strip()).lower()


async def _resolve_send_instance(
    db: AsyncSession,
    user: User,
    args: dict[str, Any],
) -> tuple[WhatsAppInstance | None, str | None]:
    """Pick authorized Evolution instance from public_id / phone / employee code / agent name."""
    from app.phone_util import normalize_in_mobile

    allowed = await authorized_instance_ids(db, user)
    q = select(WhatsAppInstance).where(
        WhatsAppInstance.organization_id == ORG,
        WhatsAppInstance.deleted_at.is_(None),
    )
    if allowed is not None:
        if not allowed:
            return None, "No WhatsApp instances assigned to this user"
        q = q.where(WhatsAppInstance.id.in_(allowed))

    public_id = str(args.get("from_instance_public_id") or "").strip()
    from_phone = normalize_in_mobile(str(args.get("from_connected_phone") or ""))
    emp_code = str(args.get("from_employee_code") or "").strip()
    agent_name = str(
        args.get("from_agent_name")
        or args.get("from_employee_name")
        or args.get("agent_name")
        or args.get("from_name")
        or ""
    ).strip()

    inst: WhatsAppInstance | None = None
    if public_id:
        inst = (
            await db.execute(q.where(WhatsAppInstance.public_id == public_id))
        ).scalar_one_or_none()
        if not inst:
            return None, "FROM instance not found or not authorized"
    elif from_phone:
        rows = list((await db.execute(q)).scalars().all())
        matches = [
            i
            for i in rows
            if normalize_in_mobile(i.connected_phone_number) == from_phone
            or normalize_in_mobile(i.expected_phone_number) == from_phone
        ]
        if len(matches) == 1:
            inst = matches[0]
        elif len(matches) > 1:
            return None, "Multiple instances match from_connected_phone — pass from_instance_public_id"
        else:
            return None, "No authorized instance matches from_connected_phone"
    elif emp_code:
        rows = list(
            (
                await db.execute(
                    q.where(func.lower(WhatsAppInstance.employee_code) == emp_code.lower())
                )
            ).scalars().all()
        )
        if len(rows) == 1:
            inst = rows[0]
        elif len(rows) > 1:
            return None, "Multiple instances for employee_code — pass from_instance_public_id"
        else:
            return None, "No authorized instance for from_employee_code"
    elif agent_name:
        needle = re.sub(r"\s+", " ", agent_name.lower()).strip()
        rows = list((await db.execute(q)).scalars().all())

        def _score(i: WhatsAppInstance) -> int:
            hay = _instance_name_haystack(i)
            if not needle or not hay:
                return 0
            if hay == needle:
                return 100
            # Exact token match on employee/display first word(s), e.g. "Visakh" → "Visakh B"
            tokens = [t for t in re.split(r"[\s._-]+", hay) if t]
            if needle in tokens:
                return 90
            if any(t.startswith(needle) or needle.startswith(t) for t in tokens if len(t) >= 3):
                return 70
            if needle in hay:
                return 50
            return 0

        scored = [( _score(i), i) for i in rows]
        scored = [(s, i) for s, i in scored if s > 0]
        scored.sort(key=lambda t: (-t[0], (t[1].employee_name or t[1].display_name or "").lower()))
        if len(scored) == 1 or (len(scored) > 1 and scored[0][0] > scored[1][0]):
            inst = scored[0][1]
        elif len(scored) > 1:
            labels = [
                (i.employee_name or i.display_name or i.public_id or "?") for _, i in scored[:6]
            ]
            return None, (
                f"Multiple instances match agent '{agent_name}' ({', '.join(labels)}) — "
                "pass from_instance_public_id"
            )
        else:
            return None, (
                f"No authorized instance matches agent name '{agent_name}'. "
                "Call get_employee_whatsapp_instances."
            )
    else:
        return None, (
            "Specify from_instance_public_id (preferred), from_agent_name, from_connected_phone, "
            "or from_employee_code. Call get_employee_whatsapp_instances first."
        )

    if not inst:
        return None, "FROM instance not resolved"
    provider = (inst.provider or "evolution").strip().lower()
    if provider == "meta_cloud":
        return None, (
            "Care (Meta Cloud) send via AI Agent is not enabled here — use an Evolution GO "
            "employee WhatsApp instance instead"
        )
    if (inst.connection_status or "") != "Connected":
        return None, f"Instance is {inst.connection_status or 'Disconnected'} — connect in Settings → WhatsApp"
    return inst, None


async def tool_send_whatsapp_text(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    """Agent-invoked single WhatsApp text send via Evolution GO (explicit confirm required)."""
    import uuid
    from datetime import datetime, timezone

    from app.ai.whatsapp_send_guards import (
        already_sent_same_text,
        blocked_recipient_error,
        blocked_recipient_keys_for_send,
        classify_blocked_recipient,
        phone_key,
    )
    from app.phone_util import normalize_in_mobile

    to_raw = str(args.get("to_phone") or "").strip()
    message = str(args.get("message") or "").strip()
    confirm = _truthy_confirm(args.get("confirm_send"))
    dry_run = _truthy_confirm(args.get("dry_run"))
    customer_name = str(args.get("customer_name") or "").strip()[:160]
    # When batch loops over recipients, skip re-loading staff keys every time.
    _pre_self = args.get("_guard_self_keys")
    _pre_staff = args.get("_guard_staff_keys")

    # Block obvious multi-recipient attempts
    if any(sep in to_raw for sep in [",", ";", "|", "\n", " and ", " & "]):
        return {
            "ok": False,
            "sent": False,
            "error": "Only one recipient phone is allowed (no lists / mass send)",
        }

    to_phone = normalize_in_mobile(to_raw)
    if not to_phone or len(to_phone) != 10:
        # Allow non-IN longer international digits (8–15) when normalize emptied
        digits = re.sub(r"\D+", "", to_raw)
        if digits.startswith("91") and len(digits) > 10:
            digits = digits[2:]
        if 8 <= len(digits) <= 15:
            to_phone = digits[-15:]
        else:
            return {"ok": False, "sent": False, "error": "Valid recipient phone required"}

    if not message:
        return {"ok": False, "sent": False, "error": "message is required"}
    if len(message) > 4000:
        return {"ok": False, "sent": False, "error": "message too long (max 4000 characters)"}
    if not confirm:
        return {
            "ok": False,
            "sent": False,
            "error": (
                "confirm_send must be true. Ask the user to confirm FROM: <agent> | TO: <customer phone>, "
                "and exact message text, then call again with confirm_send=true. "
                "'from X' is the sending instance — never a recipient."
            ),
            "needs_confirmation": True,
            "preview": {
                "to_phone": to_phone,
                "message_preview": message[:200],
                "from_instance_public_id": str(args.get("from_instance_public_id") or "") or None,
                "from_connected_phone": str(args.get("from_connected_phone") or "") or None,
                "from_employee_code": str(args.get("from_employee_code") or "") or None,
                "dry_run": dry_run,
            },
        }

    inst, err = await _resolve_send_instance(db, user, args)
    if err or not inst:
        return {"ok": False, "sent": False, "error": err or "Instance not found"}

    # Never message the FROM line itself or any staff/instance phone.
    if isinstance(_pre_self, set) and isinstance(_pre_staff, set):
        self_keys, staff_keys = _pre_self, _pre_staff
    else:
        self_keys, staff_keys = await blocked_recipient_keys_for_send(db, inst)
    blocked_kind = classify_blocked_recipient(
        to_phone, self_keys=self_keys, staff_keys=staff_keys
    )
    if blocked_kind:
        return {
            "ok": False,
            "sent": False,
            "skipped_self": blocked_kind == "self",
            "skipped_staff": blocked_kind == "staff",
            "error": blocked_recipient_error(blocked_kind, to_phone, inst),
            "from_instance_public_id": inst.public_id,
            "from_display_name": inst.display_name,
            "from_employee_name": inst.employee_name or "",
            "to_phone": to_phone,
        }

    # Find or create CRM conversation on this instance
    existing = (
        await db.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.whatsapp_instance_id == inst.id,
                WhatsAppConversation.customer_phone == to_phone,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        # Also try last-10 match for stored variants
        like_rows = (
            await db.execute(
                select(WhatsAppConversation).where(
                    WhatsAppConversation.organization_id == ORG,
                    WhatsAppConversation.whatsapp_instance_id == inst.id,
                    WhatsAppConversation.customer_phone.ilike(f"%{to_phone[-10:]}"),
                ).limit(5)
            )
        ).scalars().all()
        for c in like_rows:
            if normalize_in_mobile(c.customer_phone) == to_phone[-10:] or c.customer_phone == to_phone:
                existing = c
                break

    if existing and bool(getattr(existing, "ai_opt_out", False)):
        return {
            "ok": False,
            "sent": False,
            "error": "Customer opted out of AI messaging on this conversation",
            "conversation_id": existing.public_id,
        }

    preview = {
        "from_instance_public_id": inst.public_id,
        "from_display_name": inst.display_name,
        "from_connected_phone": inst.connected_phone_number or "",
        "from_employee_code": inst.employee_code or "",
        "from_employee_name": inst.employee_name or "",
        "to_phone": to_phone,
        "to_phone_key": phone_key(to_phone),
        "message_preview": message[:240],
        "gateway": "Evolution GO",
        "dry_run": dry_run,
        "conversation_id": existing.public_id if existing else None,
        "human_takeover": bool(getattr(existing, "ai_human_takeover_at", None)) if existing else False,
        "confirm_summary": (
            f"FROM: {inst.employee_name or inst.display_name} | TO: "
            f"{customer_name or to_phone}"
        ),
    }

    # Idempotency: same instance + phone + exact text within window → skip (not an error).
    prior = await already_sent_same_text(
        db, instance_id=inst.id, to_phone=to_phone, message=message
    )
    if prior:
        return {
            "ok": True,
            "sent": False,
            "skipped_duplicate": True,
            "dry_run": dry_run,
            "error": "",
            "note": (
                f"Skipped duplicate: same/near-same text already sent to this phone from this "
                f"instance within {prior.get('dedup_hours')}h "
                f"(prior message_id={prior.get('message_id')}, status={prior.get('status')}). "
                "Not re-sent (normalized/English-core/strike-notice match)."
            ),
            "prior_message_id": prior.get("message_id"),
            "prior_sent_at": prior.get("sent_at"),
            **preview,
        }

    if dry_run:
        return {
            "ok": True,
            "sent": False,
            "dry_run": True,
            "note": "Validation OK — message was NOT sent. Call again with dry_run=false to send.",
            **preview,
        }

    # Create conversation if needed, then reuse inbox send path
    from app.evolution import assign_conversation_to_instance_owner, primary_owner_employee
    from app.inbox import _send_and_store
    from app.phone_util import to_evolution_number, whatsapp_pn_jid

    emp = await employee_for_user(db, user)
    evo_phone = to_evolution_number(to_phone) or to_phone
    if existing:
        conv = existing
        # Repair bare IN mobiles stored without country code (breaks Evolution formatJid)
        if evo_phone and (conv.customer_phone or "").strip() != evo_phone:
            digits = re.sub(r"\D+", "", conv.customer_phone or "")
            if len(digits) == 10 or (
                (conv.customer_jid or "").endswith("@s.whatsapp.net")
                and len(re.sub(r"\D+", "", (conv.customer_jid or "").split("@", 1)[0])) == 10
            ):
                conv.customer_phone = evo_phone
                conv.customer_jid = whatsapp_pn_jid(evo_phone)
        if not conv.assigned_employee_id:
            await assign_conversation_to_instance_owner(db, conv, inst.id)
    else:
        owner = await primary_owner_employee(db, inst.id)
        conv = WhatsAppConversation(
            public_id=str(uuid.uuid4()),
            organization_id=ORG,
            whatsapp_instance_id=inst.id,
            customer_phone=evo_phone,
            customer_name=customer_name or to_phone,
            customer_jid=whatsapp_pn_jid(evo_phone),
            status="Open",
            tag="AI Agent",
            assigned_user_id=owner.user_id if owner and owner.user_id else user.id,
            assigned_employee_id=owner.id if owner else (emp.id if emp else None),
        )
        db.add(conv)
        await db.flush()

    try:
        msg = await _send_and_store(db, user, conv, inst, message)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "sent": False,
            "error": f"Send failed: {exc}",
            **preview,
            "conversation_id": conv.public_id,
        }

    await db.flush()
    return {
        "ok": True,
        "sent": True,
        "dry_run": False,
        "message_id": msg.public_id,
        "message_status": msg.status,
        "conversation_id": conv.public_id,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "note": "Message sent via Evolution GO and logged in CRM WhatsApp inbox.",
        **preview,
    }


def _parse_batch_recipients(args: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize recipients / to_phones into [{to_phone, customer_name}, ...]."""
    raw = args.get("recipients")
    if raw is None or raw == "":
        raw = args.get("to_phones")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            raw = parsed
        except json.JSONDecodeError:
            # Comma / newline separated phones
            parts = re.split(r"[,;\n|]+", text)
            raw = [p.strip() for p in parts if p.strip()]

    if not isinstance(raw, list):
        return []

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        phone = ""
        name = ""
        if isinstance(item, dict):
            phone = str(
                item.get("to_phone")
                or item.get("phone")
                or item.get("mobile")
                or item.get("customer_phone")
                or ""
            ).strip()
            name = str(
                item.get("customer_name")
                or item.get("name")
                or item.get("contact")
                or item.get("dealer_name")
                or ""
            ).strip()[:160]
        else:
            phone = str(item or "").strip()
        if not phone:
            continue
        # Dedupe by raw digits so +91 / 0 variants collapse later in send
        digits = re.sub(r"\D+", "", phone)
        key = digits[-10:] if len(digits) >= 10 else digits
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        if len(out) >= _WHATSAPP_BATCH_MAX_RECIPIENTS:
            break
        out.append({"to_phone": phone, "customer_name": name})
    return out


def _batch_delay_seconds(args: dict[str, Any]) -> tuple[int, str | None]:
    raw = args.get("delay_seconds")
    if raw is None or raw == "":
        return _WHATSAPP_BATCH_DEFAULT_DELAY_S, None
    try:
        delay = int(float(raw))
    except (TypeError, ValueError):
        return _WHATSAPP_BATCH_DEFAULT_DELAY_S, "Invalid delay_seconds — using default 10"
    if delay < _WHATSAPP_BATCH_MIN_DELAY_S:
        return _WHATSAPP_BATCH_MIN_DELAY_S, (
            f"delay_seconds raised to minimum {_WHATSAPP_BATCH_MIN_DELAY_S}"
        )
    if delay > 300:
        return 300, "delay_seconds capped at 300"
    return delay, None


# Brief Evolution socket flaps — paced worker waits instead of failing the run.
_RECOVERABLE_INSTANCE_DISCONNECT_MARKERS = (
    "instance is disconnected",
    "instance disconnected",
    "connection closed",
)

# Transient CRM → Evolution TCP / DNS — paced worker may poll until gateway is back.
_RECOVERABLE_GATEWAY_OUTAGE_MARKERS = (
    "connection refused",
    "failed to establish a new connection",
    "name or service not known",
    "nodename nor servname",
)


def _is_recoverable_instance_disconnect_error(error: str) -> bool:
    """True when the FROM line is briefly offline (Evolution auto-restart, socket flap)."""
    e = (error or "").lower()
    if not e:
        return False
    # ``Instance is Disconnected`` / ``Instance is Not Connected`` from send guard.
    if "not connected" in e and "instance" in e:
        return True
    return any(m in e for m in _RECOVERABLE_INSTANCE_DISCONNECT_MARKERS)


def _is_recoverable_paced_sender_outage(error: str) -> bool:
    """Errors where paced campaigns should wait and retry, not fail the whole run."""
    e = (error or "").lower()
    if not e:
        return False
    if _is_recoverable_instance_disconnect_error(error):
        return True
    return any(m in e for m in _RECOVERABLE_GATEWAY_OUTAGE_MARKERS)


def _is_fatal_batch_send_error(error: str) -> bool:
    """True only for permanent instance/gateway failures that make further sends pointless.

    Per-recipient errors (not registered, media reject, timeout on one number,
    empty body, generic ``Send failed: …``) must NOT abort the batch — log
    failed and continue. Brief disconnect / gateway blips are NOT fatal (paced
    worker waits for Connected); only logout, missing instance, etc. stop the queue.
    """
    if _is_recoverable_paced_sender_outage(error):
        return False
    e = (error or "").lower()
    if not e:
        return False
    fatal_markers = (
        "instance is close",
        "instance is logout",
        "logged out",
        "not authorized",
        "unauthorized",
        "no whatsapp instances",
        "from instance not",
        "instance not found",
        "care (meta cloud)",
        "connect in settings",
    )
    return any(m in e for m in fatal_markers)


async def tool_send_whatsapp_text_batch(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    """Sequential WhatsApp text sends with delay (explicit confirm; max 400)."""
    import uuid

    from app.ai.whatsapp_send_guards import (
        blocked_recipient_keys_for_send,
        filter_recipients_excluding_blocked,
    )

    message = str(args.get("message") or "").strip()
    confirm = _truthy_confirm(args.get("confirm_send"))
    dry_run = _truthy_confirm(args.get("dry_run"))
    delay_s, delay_note = _batch_delay_seconds(args)
    batch_id = str(args.get("batch_id") or args.get("idempotency_key") or "").strip() or None

    raw_list = args.get("recipients")
    if raw_list is None or raw_list == "":
        raw_list = args.get("to_phones")
    if isinstance(raw_list, str):
        try:
            maybe = json.loads(raw_list.strip())
            if isinstance(maybe, list):
                raw_list = maybe
        except json.JSONDecodeError:
            raw_list = [p for p in re.split(r"[,;\n|]+", raw_list) if p.strip()]
    if isinstance(raw_list, list) and len(raw_list) > _WHATSAPP_BATCH_MAX_RECIPIENTS:
        return {
            "ok": False,
            "sent": False,
            "error": f"Too many recipients (max {_WHATSAPP_BATCH_MAX_RECIPIENTS})",
            "recipient_count": len(raw_list),
            "results": [],
        }

    recipients = _parse_batch_recipients(args)

    if not message:
        return {"ok": False, "sent": False, "error": "message is required", "results": []}
    if len(message) > 4000:
        return {
            "ok": False,
            "sent": False,
            "error": "message too long (max 4000 characters)",
            "results": [],
        }
    if not recipients:
        return {
            "ok": False,
            "sent": False,
            "error": "recipients (or to_phones) required — pass a named list from search/enquiry results",
            "results": [],
            "needs_confirmation": True,
        }

    # Resolve FROM early so we can strip self/staff and show FROM|TO in confirm preview.
    inst, err = await _resolve_send_instance(db, user, args)
    if err or not inst:
        return {
            "ok": False,
            "sent": False,
            "error": err or "Instance not found",
            "results": [],
            "needs_confirmation": not confirm,
        }

    self_keys, staff_keys = await blocked_recipient_keys_for_send(db, inst)
    recipients, excluded = filter_recipients_excluding_blocked(
        recipients, self_keys=self_keys, staff_keys=staff_keys
    )
    from_label = (inst.employee_name or inst.display_name or "agent").strip()
    to_names = [
        (r.get("customer_name") or r.get("to_phone") or "").strip() or "?"
        for r in recipients[:12]
    ]
    confirm_summary = (
        f"FROM: {from_label} | TO: {len(recipients)} customers"
        + (f" ({', '.join(to_names)}{'…' if len(recipients) > 12 else ''})" if recipients else "")
    )

    if not recipients:
        return {
            "ok": False,
            "sent": False,
            "error": (
                "No customer recipients left after removing self/staff/duplicate phones. "
                f"'from {from_label}' means send VIA that Evolution instance — never add "
                "that agent (or any employee/instance number) as a TO recipient."
            ),
            "excluded_recipients": excluded,
            "confirm_summary": f"FROM: {from_label} | TO: 0 customers",
            "from_instance_public_id": inst.public_id,
            "from_display_name": inst.display_name,
            "from_employee_name": inst.employee_name or "",
            "results": [],
        }

    if not batch_id:
        batch_id = str(uuid.uuid4())

    if not confirm:
        return {
            "ok": False,
            "sent": False,
            "error": (
                "confirm_send must be true. Confirm with the user using this summary — "
                f"{confirm_summary} — plus exact message and delay, then call again with "
                "confirm_send=true. Do not include staff in TO."
            ),
            "needs_confirmation": True,
            "preview": {
                "confirm_summary": confirm_summary,
                "recipient_count": len(recipients),
                "recipients": recipients,
                "excluded_recipients": excluded,
                "message_preview": message[:200],
                "delay_seconds": delay_s,
                "batch_id": batch_id,
                "from_instance_public_id": inst.public_id,
                "from_display_name": inst.display_name,
                "from_connected_phone": inst.connected_phone_number or "",
                "from_employee_code": inst.employee_code or "",
                "from_employee_name": inst.employee_name or "",
                "dry_run": dry_run,
            },
        }

    from_args = {
        "from_instance_public_id": inst.public_id,
        "confirm_send": True,
        "dry_run": dry_run,
        "message": message,
        "_guard_self_keys": self_keys,
        "_guard_staff_keys": staff_keys,
    }

    results: list[dict[str, Any]] = []
    sent_count = 0
    failed_count = 0
    skipped_count = 0
    skipped_duplicate_count = 0
    stopped_early = False
    stop_reason = ""
    campaign_run = None
    if not dry_run:
        from app.campaign_reports import start_campaign_run, update_campaign_run_progress

        msg_preview = (message[:80] + "…") if len(message) > 80 else message
        campaign_run = await start_campaign_run(
            db,
            user=user,
            batch_id=batch_id,
            tool_name="send_whatsapp_text_batch",
            agent_name=from_label,
            media_name=f"Text · {msg_preview}" if msg_preview else "Text message",
            media_id=None,
            audience_name=f"{len(recipients)} recipients",
            total_count=len(recipients),
            from_instance_public_id=inst.public_id,
            note=f"Text batch started. {confirm_summary}",
        )
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            try:
                await db.flush()
            except Exception:  # noqa: BLE001
                pass

    # Record pre-filtered exclusions in results for audit transparency.
    for ex in excluded:
        results.append(
            {
                "index": 0,
                "to_phone": ex.get("to_phone") or "",
                "customer_name": ex.get("customer_name") or "",
                "ok": True,
                "sent": False,
                "dry_run": dry_run,
                "skipped_staff": ex.get("reason") in {"self", "staff"},
                "skipped_self": ex.get("reason") == "self",
                "skipped_duplicate": ex.get("reason") == "duplicate_in_batch",
                "error": f"Excluded before send ({ex.get('reason')})",
                "conversation_id": None,
                "message_id": None,
            }
        )
        skipped_count += 1

    for idx, rec in enumerate(recipients):
        one_args = {
            **from_args,
            "to_phone": rec["to_phone"],
            "customer_name": rec.get("customer_name") or "",
        }
        try:
            one = await tool_send_whatsapp_text(db, user, one_args)
        except Exception as exc:  # noqa: BLE001
            one = {"ok": False, "sent": False, "error": f"Send failed: {exc}", "to_phone": rec["to_phone"]}

        entry = {
            "index": idx + 1,
            "to_phone": one.get("to_phone") or rec["to_phone"],
            "customer_name": rec.get("customer_name") or "",
            "ok": bool(one.get("ok")),
            "sent": bool(one.get("sent")),
            "dry_run": bool(one.get("dry_run")),
            "skipped_duplicate": bool(one.get("skipped_duplicate")),
            "skipped_self": bool(one.get("skipped_self")),
            "skipped_staff": bool(one.get("skipped_staff")),
            "error": str(one.get("error") or ""),
            "note": str(one.get("note") or ""),
            "conversation_id": one.get("conversation_id"),
            "message_id": one.get("message_id"),
        }
        results.append(entry)

        if one.get("sent"):
            sent_count += 1
        elif one.get("skipped_duplicate"):
            skipped_duplicate_count += 1
            skipped_count += 1
        elif one.get("dry_run") and one.get("ok"):
            skipped_count += 1  # validated only
        elif one.get("skipped_self") or one.get("skipped_staff"):
            skipped_count += 1
        elif one.get("error"):
            failed_count += 1
            if _is_fatal_batch_send_error(str(one.get("error") or "")):
                stopped_early = True
                stop_reason = str(one.get("error") or "fatal send error")
                break
        else:
            skipped_count += 1

        if campaign_run is not None:
            from app.campaign_reports import update_campaign_run_progress

            await update_campaign_run_progress(
                db,
                campaign_run,
                success_count=sent_count,
                failed_count=failed_count,
                skipped_count=skipped_count,
                note=(
                    f"Text batch in progress: {sent_count}/{len(recipients)} sent, "
                    f"{failed_count} failed, {skipped_count} skipped."
                ),
            )

        # Commit each send so partial progress survives run timeouts / API restarts.
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            try:
                await db.flush()
            except Exception:  # noqa: BLE001
                pass

        if idx < len(recipients) - 1 and not dry_run and not stopped_early:
            # No delay needed when we only skipped a duplicate / staff entry.
            if one.get("sent") or (one.get("error") and not one.get("skipped_duplicate")):
                await asyncio.sleep(delay_s)

    if dry_run:
        ok = (not stopped_early) and failed_count == 0
    else:
        # Partial success OR all remaining were intentional skips counts as ok.
        ok = sent_count > 0 or (
            failed_count == 0 and (skipped_duplicate_count > 0 or skipped_count > 0)
        )
    note_parts = []
    if dry_run:
        note_parts.append(
            f"Dry run OK for {len(recipients)} customer recipients — nothing was sent."
        )
    else:
        note_parts.append(
            f"Sent {sent_count}/{len(recipients)} via Evolution GO "
            f"(delay {delay_s}s between sends). {confirm_summary}."
        )
    if skipped_duplicate_count:
        note_parts.append(
            f"Skipped {skipped_duplicate_count} already-sent (same text within dedup window)."
        )
    if excluded:
        note_parts.append(
            f"Excluded {len(excluded)} self/staff/duplicate-in-batch number(s) from TO list."
        )
    if failed_count:
        note_parts.append(f"Failed: {failed_count}.")
    if stopped_early:
        note_parts.append(f"Stopped early: {stop_reason}")
    if delay_note:
        note_parts.append(delay_note)

    if campaign_run is not None:
        from app.campaign_reports import finish_campaign_run

        final_status = "stopped" if stopped_early else ("completed" if ok else "failed")
        await finish_campaign_run(
            db,
            campaign_run,
            success_count=sent_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            note=" ".join(note_parts),
            status=final_status,
        )
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            try:
                await db.flush()
            except Exception:  # noqa: BLE001
                pass

    return {
        "ok": ok,
        "sent": sent_count > 0 and not dry_run,
        "dry_run": dry_run,
        "batch": True,
        "batch_id": batch_id,
        "campaign_run_id": getattr(campaign_run, "id", None),
        "confirm_summary": confirm_summary,
        "recipient_count": len(recipients),
        "excluded_count": len(excluded),
        "excluded_recipients": excluded,
        "attempted": len([r for r in results if int(r.get("index") or 0) > 0]),
        "sent_count": sent_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "skipped_duplicate_count": skipped_duplicate_count,
        "delay_seconds": delay_s,
        "stopped_early": stopped_early,
        "stop_reason": stop_reason or None,
        "from_instance_public_id": inst.public_id,
        "from_display_name": inst.display_name,
        "from_connected_phone": inst.connected_phone_number or "",
        "from_employee_code": inst.employee_code or "",
        "from_employee_name": inst.employee_name or "",
        "message_preview": message[:240],
        "gateway": "Evolution GO",
        "results": results,
        "note": " ".join(note_parts),
    }


def _media_max_bytes() -> int:
    raw = (os.environ.get("WHATSAPP_MEDIA_MAX_BYTES") or "").strip()
    if not raw:
        return _WHATSAPP_MEDIA_MAX_BYTES
    try:
        return max(1_000_000, min(200 * 1024 * 1024, int(raw)))
    except ValueError:
        return _WHATSAPP_MEDIA_MAX_BYTES


def _campaign_media_item(row: CampaignMedia) -> dict[str, Any]:
    from app.campaigns import _media_kind_of, _parse_tags

    tags = _parse_tags(getattr(row, "tags", None))
    title = (row.title or row.file_name or "").strip()
    kind = _media_kind_of(row)
    uid_val = getattr(row, "uid", None)
    return {
        "uid": int(uid_val) if uid_val is not None else None,
        "media_id": row.id,
        "id": row.id,
        "title": title,
        "topic": title,
        "file_name": row.file_name or "",
        "media_kind": kind,
        "mime_type": row.mime_type or "",
        "tags": tags,
        "file_size": int(row.file_size or 0),
        "file_size_mb": round(int(row.file_size or 0) / (1024 * 1024), 1),
        "duration_seconds": int(getattr(row, "duration_seconds", 0) or 0),
    }


async def tool_list_campaign_media(
    db: AsyncSession, user: User, args: dict[str, Any]
) -> dict[str, Any]:
    """Search Campaigns Media library for AI Agent selection."""
    query = str(args.get("query") or args.get("q") or args.get("title") or "").strip()
    kind = str(args.get("media_kind") or args.get("kind") or "").strip().lower()
    uid_raw = args.get("uid")
    uid_filter: int | None = None
    if uid_raw is not None and str(uid_raw).strip() != "":
        try:
            uid_filter = int(uid_raw)
        except (TypeError, ValueError):
            return {"ok": False, "count": 0, "items": [], "error": "uid must be an integer"}
    try:
        limit = max(1, min(_CAMPAIGN_MEDIA_LIST_LIMIT, int(args.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20

    q = select(CampaignMedia)
    if kind in {"video", "image", "document"}:
        q = q.where(func.lower(CampaignMedia.media_kind) == kind)
    if uid_filter is not None:
        q = q.where(CampaignMedia.uid == uid_filter)
    if query:
        like = f"%{query}%"
        clauses = [
            CampaignMedia.title.ilike(like),
            CampaignMedia.file_name.ilike(like),
            CampaignMedia.tags.ilike(like),
        ]
        if query.isdigit():
            try:
                clauses.append(CampaignMedia.uid == int(query))
            except ValueError:
                pass
        q = q.where(or_(*clauses))
    rows = list(
        (
            await db.execute(q.order_by(CampaignMedia.created_at.desc()).limit(limit))
        ).scalars().all()
    )
    items = [_campaign_media_item(r) for r in rows]
    return {
        "ok": True,
        "count": len(items),
        "query": query or None,
        "uid": uid_filter,
        "media_kind": kind or None,
        "items": items,
        "note": (
            f"Found {len(items)} campaign media item(s)"
            + (f" matching '{query}'" if query else "")
            + ". Prefer uid with send_whatsapp_media_batch (or media_id)."
        ),
    }


async def _resolve_campaign_media_row(
    db: AsyncSession, args: dict[str, Any]
) -> tuple[CampaignMedia | None, dict[str, Any] | None]:
    """Return (row, error_dict). Prefer uid, then media_id, then media_query."""
    uid_raw = args.get("uid")
    if uid_raw is not None and str(uid_raw).strip() != "":
        try:
            uid = int(uid_raw)
        except (TypeError, ValueError):
            return None, {"ok": False, "error": "uid must be an integer"}
        row = (
            await db.execute(select(CampaignMedia).where(CampaignMedia.uid == uid))
        ).scalar_one_or_none()
        if not row:
            return None, {"ok": False, "error": f"Campaign media uid {uid} not found"}
        return row, None

    media_id_raw = args.get("media_id")
    media_id: int | None = None
    if media_id_raw is not None and str(media_id_raw).strip() != "":
        try:
            media_id = int(media_id_raw)
        except (TypeError, ValueError):
            return None, {"ok": False, "error": "media_id must be an integer"}

    if media_id is not None:
        row = (
            await db.execute(select(CampaignMedia).where(CampaignMedia.id == media_id))
        ).scalar_one_or_none()
        if row:
            return row, None
        # Agents may pass the 4-digit UID as media_id — fall back to uid lookup.
        if 1000 <= media_id <= 9999:
            by_uid = (
                await db.execute(select(CampaignMedia).where(CampaignMedia.uid == media_id))
            ).scalar_one_or_none()
            if by_uid:
                return by_uid, None
        return None, {"ok": False, "error": f"Campaign media id {media_id} not found"}

    query = str(
        args.get("media_query") or args.get("media_title") or args.get("title") or ""
    ).strip()
    if not query:
        return None, {
            "ok": False,
            "error": "uid, media_id, or media_query required (e.g. uid=1000 or media_query='GATC RAM')",
        }

    if query.isdigit():
        try:
            q_uid = int(query)
        except ValueError:
            q_uid = None
        if q_uid is not None and 1000 <= q_uid <= 9999:
            by_uid = (
                await db.execute(select(CampaignMedia).where(CampaignMedia.uid == q_uid))
            ).scalar_one_or_none()
            if by_uid:
                return by_uid, None

    like = f"%{query}%"
    rows = list(
        (
            await db.execute(
                select(CampaignMedia)
                .where(
                    or_(
                        CampaignMedia.title.ilike(like),
                        CampaignMedia.file_name.ilike(like),
                        CampaignMedia.tags.ilike(like),
                    )
                )
                .order_by(CampaignMedia.created_at.desc())
                .limit(10)
            )
        ).scalars().all()
    )
    if not rows:
        return None, {"ok": False, "error": f"No campaign media matched '{query}'"}
    # Prefer exact title match (case-insensitive)
    exact = [r for r in rows if (r.title or "").strip().lower() == query.lower()]
    if len(exact) == 1:
        return exact[0], None
    if len(rows) == 1:
        return rows[0], None
    return None, {
        "ok": False,
        "error": (
            f"Multiple campaign media matched '{query}' — topics may repeat; "
            "pass uid (e.g. 1000) or media_id"
        ),
        "candidates": [_campaign_media_item(r) for r in rows],
    }


def _prepare_campaign_media_for_send(row: CampaignMedia) -> dict[str, Any]:
    """Load campaign file once → wa-media copy + Evolution base64 payload."""
    import base64

    from app.campaigns import _media_kind_of, _media_path
    from app.media_store import save_bytes

    path = _media_path(row)
    if not path:
        return {"ok": False, "error": f"Media file missing on disk for id={row.id}"}
    size = int(row.file_size or path.stat().st_size)
    max_bytes = _media_max_bytes()
    if size > max_bytes:
        return {
            "ok": False,
            "error": (
                f"Media too large ({round(size / (1024 * 1024), 1)} MB). "
                f"Agent send cap is {round(max_bytes / (1024 * 1024), 1)} MB "
                f"(raise WHATSAPP_MEDIA_MAX_BYTES if needed)."
            ),
            "file_size": size,
            "max_bytes": max_bytes,
        }
    kind = _media_kind_of(row)
    if kind not in {"video", "image", "document"}:
        kind = "document"
    raw = path.read_bytes()
    filename = (row.file_name or path.name or f"media-{row.id}").strip() or f"media-{row.id}"
    public_url, _stored = save_bytes(raw, media_type=kind, filename=filename)
    evo_b64 = base64.b64encode(raw).decode("ascii")
    title = (row.title or filename).strip()
    # Stable dedupe marker in media_name
    media_name = f"campaign_media:{row.id}:{filename}"[:240]
    uid_val = getattr(row, "uid", None)
    return {
        "ok": True,
        "media_id": row.id,
        "uid": int(uid_val) if uid_val is not None else None,
        "title": title,
        "media_kind": kind,
        "mime_type": row.mime_type or "",
        "file_name": filename,
        "file_size": size,
        "file_size_mb": round(size / (1024 * 1024), 1),
        "public_url": public_url,
        "evo_media_b64": evo_b64,
        "media_name": media_name,
        "size_warning": "",
    }


async def _resolve_dealer_filter_recipients(
    db: AsyncSession,
    *,
    recipient_filter: str,
    filter_state: str,
    max_recipients: int,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Resolve CRM dealers with phones for a state filter (e.g. Kerala)."""
    filt = (recipient_filter or "").strip().lower().replace("-", "_").replace(" ", "_")
    state_raw = (filter_state or "").strip()
    if filt in {"kerala_dealers", "kerala", "all_kerala_dealers"}:
        state_raw = state_raw or "Keralam"
        filt = "dealers"
    if filt not in {"dealers", "dealer", "state_dealers"}:
        return [], {
            "ok": False,
            "error": (
                "Unsupported recipient_filter. Use 'kerala_dealers' or 'dealers' with "
                "filter_state (e.g. Kerala)."
            ),
        }
    if not state_raw:
        return [], {"ok": False, "error": "filter_state required when recipient_filter=dealers"}

    state_label, state_vals = _resolve_state_query(state_raw)
    visible = dealer_not_excluded_clause(Dealer.name, Dealer.company, Dealer.zoho_contact_id)
    q = select(Dealer).where(visible, Dealer.phone != "", Dealer.phone.is_not(None))
    if state_vals:
        q = q.where(or_(*[Dealer.state.ilike(v) for v in state_vals]))
        state_label = state_label or state_vals[0]
    else:
        q = q.where(Dealer.state.ilike(f"%{state_raw}%"))
        state_label = state_raw

    # Fetch a buffer above max so staff/self stripping still leaves enough
    fetch_n = min(_DEALER_LIST_LIMIT, max(max_recipients * 3, max_recipients + 50))
    rows = list(
        (
            await db.execute(
                q.order_by(Dealer.name.asc(), Dealer.company.asc()).limit(fetch_n)
            )
        ).scalars().all()
    )
    total_with_phone = (
        await db.execute(select(func.count()).select_from(q.subquery()))
    ).scalar() or 0

    recipients: list[dict[str, str]] = []
    seen: set[str] = set()
    for d in rows:
        phone = (d.phone or "").strip()
        if not phone:
            continue
        digits = re.sub(r"\D+", "", phone)
        key = digits[-10:] if len(digits) >= 10 else digits
        if not key or key in seen:
            continue
        seen.add(key)
        name = (d.company or d.name or "").strip() or phone
        recipients.append({"to_phone": phone, "customer_name": name[:160]})
        if len(recipients) >= max_recipients:
            break

    meta = {
        "ok": True,
        "filter": "dealers",
        "state": state_label,
        "total_with_phone": int(total_with_phone),
        "returned": len(recipients),
        "capped": int(total_with_phone) > len(recipients),
        "max_recipients": max_recipients,
    }
    return recipients, meta


async def tool_send_whatsapp_media(
    db: AsyncSession, user: User, args: dict[str, Any]
) -> dict[str, Any]:
    """Send one campaign media message via Evolution GO (confirm required)."""
    import uuid
    from datetime import datetime, timezone

    from app.ai.whatsapp_send_guards import (
        already_sent_same_media,
        blocked_recipient_error,
        blocked_recipient_keys_for_send,
        classify_blocked_recipient,
        phone_key,
        phone_marked_spam,
    )
    from app.phone_util import normalize_in_mobile

    to_raw = str(args.get("to_phone") or "").strip()
    caption = str(args.get("caption") or args.get("message") or "").strip()
    confirm = _truthy_confirm(args.get("confirm_send"))
    dry_run = _truthy_confirm(args.get("dry_run"))
    customer_name = str(args.get("customer_name") or "").strip()[:160]
    _pre_self = args.get("_guard_self_keys")
    _pre_staff = args.get("_guard_staff_keys")
    prepared = args.get("_prepared_media")

    if any(sep in to_raw for sep in [",", ";", "|", "\n", " and ", " & "]):
        return {
            "ok": False,
            "sent": False,
            "error": "Only one recipient phone is allowed (no lists / mass send)",
        }

    to_phone = normalize_in_mobile(to_raw)
    if not to_phone or len(to_phone) != 10:
        digits = re.sub(r"\D+", "", to_raw)
        if digits.startswith("91") and len(digits) > 10:
            digits = digits[2:]
        if 8 <= len(digits) <= 15:
            to_phone = digits[-15:]
        else:
            return {"ok": False, "sent": False, "error": "Valid recipient phone required"}

    if len(caption) > 1024:
        return {"ok": False, "sent": False, "error": "caption too long (max 1024 characters)"}

    if not isinstance(prepared, dict) or not prepared.get("ok"):
        row, err = await _resolve_campaign_media_row(db, args)
        if err or not row:
            return {**(err or {"ok": False, "error": "Media not found"}), "sent": False}
        prepared = _prepare_campaign_media_for_send(row)
        if not prepared.get("ok"):
            return {**prepared, "sent": False}

    if not confirm:
        return {
            "ok": False,
            "sent": False,
            "error": (
                "confirm_send must be true. Confirm FROM: <agent> | TO: <customer>, "
                f"media '{prepared.get('title')}', then call again with confirm_send=true."
            ),
            "needs_confirmation": True,
            "preview": {
                "to_phone": to_phone,
                "media_id": prepared.get("media_id"),
                "media_title": prepared.get("title"),
                "media_kind": prepared.get("media_kind"),
                "file_size_mb": prepared.get("file_size_mb"),
                "caption_preview": caption[:200],
                "size_warning": prepared.get("size_warning") or None,
            },
        }

    inst, err = await _resolve_send_instance(db, user, args)
    if err or not inst:
        return {"ok": False, "sent": False, "error": err or "Instance not found"}

    if isinstance(_pre_self, set) and isinstance(_pre_staff, set):
        self_keys, staff_keys = _pre_self, _pre_staff
    else:
        self_keys, staff_keys = await blocked_recipient_keys_for_send(db, inst)
    blocked_kind = classify_blocked_recipient(
        to_phone, self_keys=self_keys, staff_keys=staff_keys
    )
    if blocked_kind:
        return {
            "ok": False,
            "sent": False,
            "skipped_self": blocked_kind == "self",
            "skipped_staff": blocked_kind == "staff",
            "error": blocked_recipient_error(blocked_kind, to_phone, inst),
            "from_instance_public_id": inst.public_id,
            "from_display_name": inst.display_name,
            "from_employee_name": inst.employee_name or "",
            "to_phone": to_phone,
        }

    if await phone_marked_spam(db, to_phone):
        return {
            "ok": True,
            "sent": False,
            "skipped_spam": True,
            "error": "Excluded: phone marked spam (WhatsApp conversation)",
            "from_instance_public_id": inst.public_id,
            "from_display_name": inst.display_name,
            "from_employee_name": inst.employee_name or "",
            "to_phone": to_phone,
        }

    existing = (
        await db.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.whatsapp_instance_id == inst.id,
                WhatsAppConversation.customer_phone == to_phone,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        like_rows = (
            await db.execute(
                select(WhatsAppConversation)
                .where(
                    WhatsAppConversation.organization_id == ORG,
                    WhatsAppConversation.whatsapp_instance_id == inst.id,
                    WhatsAppConversation.customer_phone.ilike(f"%{to_phone[-10:]}"),
                )
                .limit(5)
            )
        ).scalars().all()
        for c in like_rows:
            if normalize_in_mobile(c.customer_phone) == to_phone[-10:] or c.customer_phone == to_phone:
                existing = c
                break

    if existing and bool(getattr(existing, "ai_opt_out", False)):
        return {
            "ok": False,
            "sent": False,
            "error": "Customer opted out of AI messaging on this conversation",
            "conversation_id": existing.public_id,
        }

    preview = {
        "from_instance_public_id": inst.public_id,
        "from_display_name": inst.display_name,
        "from_connected_phone": inst.connected_phone_number or "",
        "from_employee_code": inst.employee_code or "",
        "from_employee_name": inst.employee_name or "",
        "to_phone": to_phone,
        "to_phone_key": phone_key(to_phone),
        "media_id": prepared.get("media_id"),
        "media_title": prepared.get("title"),
        "media_kind": prepared.get("media_kind"),
        "file_size_mb": prepared.get("file_size_mb"),
        "caption_preview": caption[:240],
        "size_warning": prepared.get("size_warning") or None,
        "gateway": "Evolution GO",
        "dry_run": dry_run,
        "conversation_id": existing.public_id if existing else None,
        "confirm_summary": (
            f"FROM: {inst.employee_name or inst.display_name} | TO: "
            f"{customer_name or to_phone} | MEDIA: {prepared.get('title')}"
        ),
    }

    prior = await already_sent_same_media(
        db,
        instance_id=inst.id,
        to_phone=to_phone,
        media_name=str(prepared.get("media_name") or ""),
        caption=caption,
        campaign_media_id=int(prepared.get("media_id") or 0) or None,
    )
    if prior:
        return {
            "ok": True,
            "sent": False,
            "skipped_duplicate": True,
            "dry_run": dry_run,
            "error": "",
            "note": (
                "Skipped duplicate: same campaign media already sent to this phone from "
                f"any WhatsApp line (dedup "
                f"{'all-time' if not prior.get('dedup_hours') else str(prior.get('dedup_hours')) + 'h'}"
                f"; prior message_id={prior.get('message_id')}, status={prior.get('status')})."
            ),
            "prior_message_id": prior.get("message_id"),
            "prior_sent_at": prior.get("sent_at"),
            **preview,
        }

    if dry_run:
        return {
            "ok": True,
            "sent": False,
            "dry_run": True,
            "note": "Validation OK — media was NOT sent. Call again with dry_run=false to send.",
            **preview,
        }

    from app.evolution import assign_conversation_to_instance_owner, primary_owner_employee
    from app.inbox import _send_and_store
    from app.phone_util import to_evolution_number, whatsapp_pn_jid

    emp = await employee_for_user(db, user)
    evo_phone = to_evolution_number(to_phone) or to_phone
    if existing:
        conv = existing
        if evo_phone and (conv.customer_phone or "").strip() != evo_phone:
            digits = re.sub(r"\D+", "", conv.customer_phone or "")
            if len(digits) == 10 or (
                (conv.customer_jid or "").endswith("@s.whatsapp.net")
                and len(re.sub(r"\D+", "", (conv.customer_jid or "").split("@", 1)[0])) == 10
            ):
                conv.customer_phone = evo_phone
                conv.customer_jid = whatsapp_pn_jid(evo_phone)
        if not conv.assigned_employee_id:
            await assign_conversation_to_instance_owner(db, conv, inst.id)
    else:
        owner = await primary_owner_employee(db, inst.id)
        conv = WhatsAppConversation(
            public_id=str(uuid.uuid4()),
            organization_id=ORG,
            whatsapp_instance_id=inst.id,
            customer_phone=evo_phone,
            customer_name=customer_name or to_phone,
            customer_jid=whatsapp_pn_jid(evo_phone),
            status="Open",
            tag="AI Agent",
            assigned_user_id=owner.user_id if owner and owner.user_id else user.id,
            assigned_employee_id=owner.id if owner else (emp.id if emp else None),
        )
        db.add(conv)
        await db.flush()

    try:
        msg = await _send_and_store(
            db,
            user,
            conv,
            inst,
            caption,
            media_url=str(prepared.get("public_url") or ""),
            media_type=str(prepared.get("media_kind") or "document"),
            media_name=str(prepared.get("media_name") or prepared.get("file_name") or ""),
            evo_media_b64=str(prepared.get("evo_media_b64") or "") or None,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "sent": False,
            "error": f"Send failed: {exc}",
            **preview,
            "conversation_id": conv.public_id,
        }

    await db.flush()
    return {
        "ok": True,
        "sent": True,
        "dry_run": False,
        "message_id": msg.public_id,
        "message_status": msg.status,
        "conversation_id": conv.public_id,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "note": "Media sent via Evolution GO and logged in CRM WhatsApp inbox.",
        **preview,
    }


async def tool_send_whatsapp_media_batch(
    db: AsyncSession, user: User, args: dict[str, Any]
) -> dict[str, Any]:
    """Sequential campaign-media WhatsApp sends (confirm required; max 400)."""
    import uuid
    from datetime import datetime, timezone

    from app.ai.whatsapp_send_guards import (
        blocked_recipient_keys_for_send,
        filter_recipients_excluding_blocked,
        filter_recipients_excluding_spam,
        filter_recipients_for_campaign_media_send,
        load_spam_phone_keys,
    )
    from app.paced_media_batch import parse_total_hours

    caption = str(args.get("caption") or args.get("message") or "").strip()
    confirm = _truthy_confirm(args.get("confirm_send"))
    dry_run = _truthy_confirm(args.get("dry_run"))
    delay_s, delay_note = _batch_delay_seconds(args)
    total_hours = parse_total_hours(args)
    # Audience/media campaigns default to paced 24h when user said slow/human or large list + hours omitted
    # is handled by parse_total_hours; if still None but args ask for paced, leave sync path.
    batch_id = str(args.get("batch_id") or args.get("idempotency_key") or "").strip() or None
    recipient_filter = str(args.get("recipient_filter") or args.get("filter") or "").strip()
    filter_state = str(args.get("filter_state") or args.get("state") or "").strip()
    audience_id_arg = args.get("audience_id")
    # Only audience_uid — never reuse media `uid` (4-digit campaign media public id).
    audience_uid_arg = args.get("audience_uid")
    audience_name_arg = str(args.get("audience_name") or "").strip()

    try:
        max_recipients = int(args.get("max_recipients") or _WHATSAPP_BATCH_MAX_RECIPIENTS)
    except (TypeError, ValueError):
        max_recipients = _WHATSAPP_BATCH_MAX_RECIPIENTS
    max_recipients = max(1, min(_WHATSAPP_BATCH_MAX_RECIPIENTS, max_recipients))

    if len(caption) > 1024:
        return {
            "ok": False,
            "sent": False,
            "error": "caption too long (max 1024 characters)",
            "results": [],
        }

    row, media_err = await _resolve_campaign_media_row(db, args)
    if media_err or not row:
        return {**(media_err or {"ok": False, "error": "Media not found"}), "sent": False, "results": []}

    prepared = _prepare_campaign_media_for_send(row)
    if not prepared.get("ok"):
        return {**prepared, "sent": False, "results": []}

    filter_meta: dict[str, Any] = {}
    recipients: list[dict[str, str]] = []
    if audience_id_arg is not None or audience_uid_arg is not None or audience_name_arg:
        from app.audiences import fmt_audience_uid, resolve_audience_members

        # Do not pass media uid into audience resolver — only audience_uid / id / name.
        aud_row, aud_err = await _resolve_audience_row(
            db,
            {
                "audience_uid": audience_uid_arg,
                "audience_id": audience_id_arg,
                "name": audience_name_arg,
            },
        )
        if aud_err or not aud_row:
            return {
                **(aud_err or {"ok": False, "error": "Audience not found"}),
                "sent": False,
                "results": [],
            }
        members, total, with_phone = await resolve_audience_members(
            db, aud_row, limit=max_recipients, with_phone_only=True
        )
        seen: set[str] = set()
        for m in members:
            phone = (m.phone or "").strip()
            if not phone:
                continue
            digits = re.sub(r"\D+", "", phone)
            key = digits[-10:] if len(digits) >= 10 else digits
            if not key or key in seen:
                continue
            seen.add(key)
            recipients.append({"to_phone": phone, "customer_name": (m.name or phone)[:160]})
            if len(recipients) >= max_recipients:
                break
        from app.audiences import normalize_filters as _norm_aud_filt

        aud_filt = _norm_aud_filt(aud_row.filters_json)
        filter_meta = {
            "ok": True,
            "filter": "audience",
            "audience_id": aud_row.id,
            "audience_uid": aud_row.uid,
            "audience_uid_label": fmt_audience_uid(aud_row.uid),
            "audience_name": aud_row.name,
            "source": aud_row.source,
            "state": aud_filt.get("state"),
            "total_with_phone": with_phone,
            "total": total,
            "returned": len(recipients),
            "capped": with_phone > len(recipients),
            "max_recipients": max_recipients,
        }
    elif recipient_filter:
        recipients, filter_meta = await _resolve_dealer_filter_recipients(
            db,
            recipient_filter=recipient_filter,
            filter_state=filter_state,
            max_recipients=max_recipients,
        )
        if not filter_meta.get("ok"):
            return {
                "ok": False,
                "sent": False,
                "error": filter_meta.get("error") or "Dealer filter failed",
                "results": [],
            }
    else:
        raw_list = args.get("recipients")
        if raw_list is None or raw_list == "":
            raw_list = args.get("to_phones")
        if isinstance(raw_list, str):
            try:
                maybe = json.loads(raw_list.strip())
                if isinstance(maybe, list):
                    raw_list = maybe
            except json.JSONDecodeError:
                raw_list = [p for p in re.split(r"[,;\n|]+", raw_list) if p.strip()]
        if isinstance(raw_list, list) and len(raw_list) > _WHATSAPP_BATCH_MAX_RECIPIENTS:
            return {
                "ok": False,
                "sent": False,
                "error": f"Too many recipients (max {_WHATSAPP_BATCH_MAX_RECIPIENTS})",
                "recipient_count": len(raw_list),
                "results": [],
            }
        recipients = _parse_batch_recipients(args)

    if not recipients:
        return {
            "ok": False,
            "sent": False,
            "error": (
                "recipients (or to_phones) required — or pass audience_uid / audience_id / "
                "recipient_filter='kerala_dealers'"
            ),
            "results": [],
            "needs_confirmation": True,
            "media_id": prepared.get("media_id"),
            "media_title": prepared.get("title"),
        }

    inst, err = await _resolve_send_instance(db, user, args)
    if err or not inst:
        return {
            "ok": False,
            "sent": False,
            "error": err or "Instance not found",
            "results": [],
            "needs_confirmation": not confirm,
        }

    self_keys, staff_keys = await blocked_recipient_keys_for_send(db, inst)
    mid_i = int(prepared.get("media_id") or 0) or None
    if mid_i is not None:
        recipients, excluded = await filter_recipients_for_campaign_media_send(
            db,
            recipients,
            campaign_media_id=mid_i,
            self_keys=self_keys,
            staff_keys=staff_keys,
        )
    else:
        recipients, excluded = filter_recipients_excluding_blocked(
            recipients, self_keys=self_keys, staff_keys=staff_keys
        )
        spam_keys = await load_spam_phone_keys(db)
        recipients, spam_excluded = filter_recipients_excluding_spam(
            recipients, spam_phone_keys=spam_keys
        )
        excluded.extend(spam_excluded)
    if len(recipients) > max_recipients:
        recipients = recipients[:max_recipients]

    from_label = (inst.employee_name or inst.display_name or "agent").strip()
    to_names = [
        (r.get("customer_name") or r.get("to_phone") or "").strip() or "?"
        for r in recipients[:12]
    ]
    scope_note = ""
    if filter_meta.get("filter") == "audience" and filter_meta.get("audience_name"):
        scope_note = f" audience “{filter_meta.get('audience_name')}”"
        if filter_meta.get("capped"):
            scope_note += (
                f" (capped {len(recipients)} of {filter_meta.get('total_with_phone')} with phone)"
            )
    elif filter_meta.get("state"):
        scope_note = f" {filter_meta.get('state')} dealers"
        if filter_meta.get("capped"):
            scope_note += (
                f" (capped {len(recipients)} of {filter_meta.get('total_with_phone')} with phone)"
            )
    pace_note = f" | WINDOW: {total_hours}h paced (random human gaps)" if total_hours else ""
    confirm_summary = (
        f"FROM: {from_label} | TO: {len(recipients)}{scope_note or ' customers'}"
        + (f" ({', '.join(to_names)}{'…' if len(recipients) > 12 else ''})" if recipients else "")
        + f" | MEDIA: {prepared.get('title')} ({prepared.get('media_kind')}, "
        f"{prepared.get('file_size_mb')} MB)"
        + pace_note
    )

    if not recipients:
        return {
            "ok": False,
            "sent": False,
            "error": (
                "No customer recipients left after removing self/staff/duplicate phones. "
                f"'from {from_label}' means send VIA that Evolution instance — never add "
                "that agent (or any employee/instance number) as a TO recipient."
            ),
            "excluded_recipients": excluded,
            "confirm_summary": f"FROM: {from_label} | TO: 0 | MEDIA: {prepared.get('title')}",
            "from_instance_public_id": inst.public_id,
            "results": [],
        }

    if not batch_id:
        batch_id = str(uuid.uuid4())

    # Large audience without hours → default paced 24h (WhatsApp block risk if blasted).
    if (
        total_hours is None
        and len(recipients) >= 20
        and (
            filter_meta.get("filter") == "audience"
            or audience_uid_arg is not None
            or audience_id_arg is not None
            or audience_name_arg
        )
    ):
        total_hours = 24
        confirm_summary = (
            f"FROM: {from_label} | TO: {len(recipients)}{scope_note or ' customers'}"
            + (f" ({', '.join(to_names)}{'…' if len(recipients) > 12 else ''})" if recipients else "")
            + f" | MEDIA: {prepared.get('title')} ({prepared.get('media_kind')}, "
            f"{prepared.get('file_size_mb')} MB)"
            + " | WINDOW: 24h paced (default for large audience)"
        )

    if not confirm:
        return {
            "ok": False,
            "sent": False,
            "error": (
                "confirm_send must be true. Confirm with the user using this summary — "
                f"{confirm_summary} — plus caption"
                + (f"/window {total_hours}h" if total_hours else "/delay")
                + ", then call again with confirm_send=true."
            ),
            "needs_confirmation": True,
            "preview": {
                "confirm_summary": confirm_summary,
                "recipient_count": len(recipients),
                "recipients": recipients,
                "excluded_recipients": excluded,
                "media_id": prepared.get("media_id"),
                "uid": prepared.get("uid"),
                "media_title": prepared.get("title"),
                "media_kind": prepared.get("media_kind"),
                "file_size_mb": prepared.get("file_size_mb"),
                "size_warning": prepared.get("size_warning") or None,
                "caption_preview": caption[:200],
                "delay_seconds": delay_s if not total_hours else None,
                "total_hours": total_hours,
                "paced": bool(total_hours),
                "batch_id": batch_id,
                "filter_meta": filter_meta or None,
                "from_instance_public_id": inst.public_id,
                "from_display_name": inst.display_name,
                "from_connected_phone": inst.connected_phone_number or "",
                "from_employee_code": inst.employee_code or "",
                "from_employee_name": inst.employee_name or "",
                "dry_run": dry_run,
            },
        }

    # --- Paced background job (24h / 48h) — return immediately; worker sends with jitter ---
    if total_hours and not dry_run:
        from app.paced_media_batch import create_paced_campaign_run, wake_paced_worker

        aud_label = ""
        if filter_meta.get("filter") == "audience":
            uid_lab = filter_meta.get("audience_uid_label") or filter_meta.get("audience_uid") or ""
            name_lab = filter_meta.get("audience_name") or ""
            aud_label = f"{name_lab} · {uid_lab}".strip(" ·") if name_lab or uid_lab else "Custom audience"
        elif filter_meta.get("state"):
            aud_label = f"{filter_meta.get('state')} dealers"
        else:
            aud_label = "Custom recipients"

        run_row = await create_paced_campaign_run(
            db,
            user=user,
            batch_id=batch_id,
            media_id=int(prepared.get("media_id") or 0),
            media_title=str(prepared.get("title") or ""),
            media_uid=prepared.get("uid"),
            audience_label=aud_label,
            agent_name=from_label,
            from_instance_public_id=inst.public_id,
            caption=caption,
            recipients=recipients,
            total_hours=int(total_hours),
            filter_meta=filter_meta or None,
            confirm_summary=confirm_summary,
        )
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.flush()
        wake_paced_worker()
        return {
            "ok": True,
            "sent": False,
            "paced": True,
            "scheduled": True,
            "status": "running",
            "dry_run": False,
            "batch": True,
            "batch_id": batch_id,
            "campaign_run_id": run_row.id,
            "confirm_summary": confirm_summary,
            "recipient_count": len(recipients),
            "excluded_count": len(excluded),
            "excluded_recipients": excluded,
            "filter_meta": filter_meta or None,
            "media_id": prepared.get("media_id"),
            "uid": prepared.get("uid"),
            "media_title": prepared.get("title"),
            "media_kind": prepared.get("media_kind"),
            "file_size_mb": prepared.get("file_size_mb"),
            "sent_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "skipped_duplicate_count": 0,
            "total_hours": int(total_hours),
            "from_instance_public_id": inst.public_id,
            "from_display_name": inst.display_name,
            "from_connected_phone": inst.connected_phone_number or "",
            "from_employee_code": inst.employee_code or "",
            "from_employee_name": inst.employee_name or "",
            "caption_preview": caption[:240],
            "gateway": "Evolution GO",
            "results": [],
            "note": (
                f"STARTED / RUNNING: paced WhatsApp media campaign over {total_hours}h for "
                f"{len(recipients)} recipients from {from_label}. "
                "Not completed yet — background worker sends with random human-like gaps. "
                "Tell the user it started/is running; do NOT say job completed. "
                f"Track progress in Campaigns → Report (run #{run_row.id}) and Work Reports. "
                f"{confirm_summary}."
            ),
        }

    # Drop heavy base64 from per-recipient args copy — keep on prepared dict
    from_args = {
        "from_instance_public_id": inst.public_id,
        "confirm_send": True,
        "dry_run": dry_run,
        "caption": caption,
        "_guard_self_keys": self_keys,
        "_guard_staff_keys": staff_keys,
        "_prepared_media": prepared,
        "media_id": prepared.get("media_id"),
    }

    results: list[dict[str, Any]] = []
    sent_count = 0
    failed_count = 0
    skipped_count = 0
    skipped_duplicate_count = 0
    stopped_early = False
    stop_reason = ""
    campaign_run = None
    if not dry_run:
        from app.campaign_reports import (
            format_media_display_name,
            start_campaign_run,
            update_campaign_run_progress,
        )

        aud_label = ""
        if filter_meta.get("filter") == "audience":
            uid_lab = filter_meta.get("audience_uid_label") or filter_meta.get("audience_uid") or ""
            name_lab = filter_meta.get("audience_name") or ""
            aud_label = f"{name_lab} · {uid_lab}".strip(" ·") if name_lab or uid_lab else "Custom audience"
        elif filter_meta.get("state"):
            aud_label = f"{filter_meta.get('state')} dealers"
        else:
            aud_label = f"{len(recipients)} recipients"
        media_row = None
        mid = prepared.get("media_id")
        try:
            mid_i = int(mid) if mid is not None else None
        except (TypeError, ValueError):
            mid_i = None
        if mid_i is not None:
            media_row = await db.get(CampaignMedia, mid_i)
        media_label = format_media_display_name(
            media_row, fallback=str(prepared.get("title") or "")
        )
        campaign_run = await start_campaign_run(
            db,
            user=user,
            batch_id=batch_id,
            tool_name="send_whatsapp_media_batch",
            agent_name=from_label,
            media_name=media_label,
            media_id=mid_i,
            audience_name=aud_label,
            total_count=len(recipients),
            from_instance_public_id=inst.public_id,
            note=f"Media batch started. {confirm_summary}",
        )
        if campaign_run is not None:
            import json as _json

            queue = [
                {
                    "to_phone": r.get("to_phone") or "",
                    "customer_name": r.get("customer_name") or "",
                    "status": "pending",
                    "error": "",
                }
                for r in recipients
            ]
            for ex in excluded:
                queue.append(
                    {
                        "to_phone": ex.get("to_phone") or "",
                        "customer_name": ex.get("customer_name") or "",
                        "status": "skipped",
                        "error": f"Excluded before send ({ex.get('reason')})",
                    }
                )
            campaign_run.recipients_json = _json.dumps(queue, ensure_ascii=False)
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            try:
                await db.flush()
            except Exception:  # noqa: BLE001
                pass

    for ex in excluded:
        results.append(
            {
                "index": 0,
                "to_phone": ex.get("to_phone") or "",
                "customer_name": ex.get("customer_name") or "",
                "ok": True,
                "sent": False,
                "dry_run": dry_run,
                "skipped_staff": ex.get("reason") in {"self", "staff"},
                "skipped_self": ex.get("reason") == "self",
                "skipped_spam": ex.get("reason") == "spam",
                "skipped_duplicate": ex.get("reason")
                in {"duplicate_in_batch", "duplicate_prior_media"},
                "error": f"Excluded before send ({ex.get('reason')})",
                "conversation_id": None,
                "message_id": None,
            }
        )
        skipped_count += 1
        if ex.get("reason") in {"duplicate_in_batch", "duplicate_prior_media"}:
            skipped_duplicate_count += 1

    for idx, rec in enumerate(recipients):
        one_args = {
            **from_args,
            "to_phone": rec["to_phone"],
            "customer_name": rec.get("customer_name") or "",
        }
        try:
            one = await tool_send_whatsapp_media(db, user, one_args)
        except Exception as exc:  # noqa: BLE001
            one = {
                "ok": False,
                "sent": False,
                "error": f"Send failed: {exc}",
                "to_phone": rec["to_phone"],
            }

        entry = {
            "index": idx + 1,
            "to_phone": one.get("to_phone") or rec["to_phone"],
            "customer_name": rec.get("customer_name") or "",
            "ok": bool(one.get("ok")),
            "sent": bool(one.get("sent")),
            "dry_run": bool(one.get("dry_run")),
            "skipped_duplicate": bool(one.get("skipped_duplicate")),
            "skipped_self": bool(one.get("skipped_self")),
            "skipped_staff": bool(one.get("skipped_staff")),
            "skipped_spam": bool(one.get("skipped_spam")),
            "error": str(one.get("error") or ""),
            "note": str(one.get("note") or ""),
            "conversation_id": one.get("conversation_id"),
            "message_id": one.get("message_id"),
        }
        results.append(entry)

        if one.get("sent"):
            sent_count += 1
        elif one.get("skipped_duplicate"):
            skipped_duplicate_count += 1
            skipped_count += 1
        elif one.get("dry_run") and one.get("ok"):
            skipped_count += 1
        elif one.get("skipped_self") or one.get("skipped_staff") or one.get("skipped_spam"):
            skipped_count += 1
        elif one.get("error"):
            failed_count += 1
            if _is_fatal_batch_send_error(str(one.get("error") or "")):
                stopped_early = True
                stop_reason = str(one.get("error") or "fatal send error")
                break
        else:
            skipped_count += 1

        if campaign_run is not None:
            import json as _json

            from app.campaign_reports import update_campaign_run_progress

            try:
                queue = _json.loads(campaign_run.recipients_json or "[]")
            except Exception:  # noqa: BLE001
                queue = []
            if isinstance(queue, list) and idx < len(recipients) and idx < len(queue):
                item = queue[idx] if isinstance(queue[idx], dict) else {
                    "to_phone": rec["to_phone"],
                    "customer_name": rec.get("customer_name") or "",
                }
                if one.get("sent"):
                    item["status"] = "sent"
                    item["sent_at"] = str(one.get("sent_at") or datetime.now(timezone.utc).isoformat())
                    if one.get("conversation_id"):
                        item["conversation_id"] = str(one.get("conversation_id"))
                    if one.get("message_id"):
                        item["message_id"] = str(one.get("message_id"))
                elif (
                    one.get("skipped_duplicate")
                    or one.get("skipped_self")
                    or one.get("skipped_staff")
                    or one.get("skipped_spam")
                ):
                    item["status"] = "skipped"
                    item["error"] = str(one.get("error") or "")[:400]
                elif one.get("error"):
                    item["status"] = "failed"
                    item["error"] = str(one.get("error") or "")[:400]
                else:
                    item["status"] = "skipped"
                queue[idx] = item
                campaign_run.recipients_json = _json.dumps(queue, ensure_ascii=False)

            await update_campaign_run_progress(
                db,
                campaign_run,
                success_count=sent_count,
                failed_count=failed_count,
                skipped_count=skipped_count,
                note=(
                    f"Media batch in progress: {sent_count}/{len(recipients)} sent, "
                    f"{failed_count} failed, {skipped_count} skipped."
                ),
                refresh_replied=(sent_count > 0 and sent_count % 5 == 0),
            )

        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            try:
                await db.flush()
            except Exception:  # noqa: BLE001
                pass

        if idx < len(recipients) - 1 and not dry_run and not stopped_early:
            if one.get("sent") or (one.get("error") and not one.get("skipped_duplicate")):
                await asyncio.sleep(delay_s)

    if dry_run:
        ok = (not stopped_early) and failed_count == 0
    else:
        ok = sent_count > 0 or (
            failed_count == 0 and (skipped_duplicate_count > 0 or skipped_count > 0)
        )

    note_parts: list[str] = []
    if dry_run:
        note_parts.append(
            f"Dry run OK for {len(recipients)} recipients + media '{prepared.get('title')}' — nothing sent."
        )
    else:
        note_parts.append(
            f"Sent media '{prepared.get('title')}' to {sent_count}/{len(recipients)} "
            f"via Evolution GO (delay {delay_s}s). {confirm_summary}."
        )
    if prepared.get("size_warning"):
        note_parts.append(str(prepared["size_warning"]))
    if skipped_duplicate_count:
        note_parts.append(
            f"Skipped {skipped_duplicate_count} duplicate(s) — same campaign media "
            f"already on that phone (any WhatsApp line)."
        )
    if excluded:
        note_parts.append(
            f"Excluded {len(excluded)} self/staff/duplicate-in-batch number(s) from TO list."
        )
    if filter_meta.get("capped"):
        note_parts.append(
            f"Dealer list capped at {max_recipients} of {filter_meta.get('total_with_phone')} "
            f"{filter_meta.get('state')} dealers with phone."
        )
    if failed_count:
        note_parts.append(f"Failed: {failed_count}.")
    if stopped_early:
        note_parts.append(f"Stopped early: {stop_reason}")
    if delay_note:
        note_parts.append(delay_note)

    if campaign_run is not None:
        from app.campaign_reports import finish_campaign_run

        final_status = "stopped" if stopped_early else ("completed" if ok else "failed")
        await finish_campaign_run(
            db,
            campaign_run,
            success_count=sent_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            note=" ".join(note_parts),
            status=final_status,
        )
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            try:
                await db.flush()
            except Exception:  # noqa: BLE001
                pass

    return {
        "ok": ok,
        "sent": sent_count > 0 and not dry_run,
        "dry_run": dry_run,
        "batch": True,
        "batch_id": batch_id,
        "campaign_run_id": getattr(campaign_run, "id", None),
        "confirm_summary": confirm_summary,
        "recipient_count": len(recipients),
        "excluded_count": len(excluded),
        "excluded_recipients": excluded,
        "filter_meta": filter_meta or None,
        "media_id": prepared.get("media_id"),
        "uid": prepared.get("uid"),
        "media_title": prepared.get("title"),
        "media_kind": prepared.get("media_kind"),
        "file_size_mb": prepared.get("file_size_mb"),
        "size_warning": prepared.get("size_warning") or None,
        "attempted": len([r for r in results if int(r.get("index") or 0) > 0]),
        "sent_count": sent_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "skipped_duplicate_count": skipped_duplicate_count,
        "delay_seconds": delay_s,
        "stopped_early": stopped_early,
        "stop_reason": stop_reason or None,
        "from_instance_public_id": inst.public_id,
        "from_display_name": inst.display_name,
        "from_connected_phone": inst.connected_phone_number or "",
        "from_employee_code": inst.employee_code or "",
        "from_employee_name": inst.employee_name or "",
        "caption_preview": caption[:240],
        "gateway": "Evolution GO",
        "results": results,
        "note": " ".join(note_parts),
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


def _audience_filters_from_args(args: dict[str, Any]) -> dict[str, Any]:
    filters = args.get("filters")
    if isinstance(filters, str):
        try:
            filters = json.loads(filters)
        except json.JSONDecodeError:
            filters = {}
    if not isinstance(filters, dict):
        filters = {}
    out = dict(filters)
    for key in ("state", "city", "district"):
        val = str(args.get(key) or "").strip()
        if val:
            out[key] = val
    return out


async def _resolve_audience_row(db: AsyncSession, args: dict[str, Any]):
    from app.audiences import (
        AUDIENCE_UID_MAX,
        AUDIENCE_UID_MIN,
        find_audience_by_name,
        find_audience_by_uid,
        fmt_audience_uid,
        parse_audience_uid_token,
    )
    from app.models import CustomAudience

    def _cand(r: CustomAudience) -> dict[str, Any]:
        return {
            "id": r.id,
            "uid": r.uid,
            "uid_label": fmt_audience_uid(r.uid),
            "name": r.name,
            "source": r.source,
        }

    # Prefer explicit public uid (or audience_uid from media batch).
    uid_raw = args.get("uid") if args.get("uid") is not None else args.get("audience_uid")
    parsed_uid = parse_audience_uid_token(uid_raw)
    if parsed_uid is not None:
        row = await find_audience_by_uid(db, parsed_uid)
        if row:
            return row, None
        return None, {"ok": False, "error": f"Audience uid {fmt_audience_uid(parsed_uid)} not found"}

    aid = args.get("audience_id") or args.get("id")
    if aid is not None and str(aid).strip() != "":
        try:
            aid_int = int(aid)
        except (TypeError, ValueError):
            aid_int = None
        if aid_int is not None:
            row = await db.get(CustomAudience, aid_int)
            if row:
                return row, None
            # Agents may pass the 2-digit public UID as audience_id.
            if AUDIENCE_UID_MIN <= aid_int <= AUDIENCE_UID_MAX:
                by_uid = await find_audience_by_uid(db, aid_int)
                if by_uid:
                    return by_uid, None
            return None, {"ok": False, "error": f"Audience id={aid} not found"}

    name = str(args.get("name") or args.get("audience_name") or args.get("query") or "").strip()
    if not name:
        return None, {"ok": False, "error": "uid, audience_id, or name required"}

    name_uid = parse_audience_uid_token(name)
    if name_uid is not None:
        by_uid = await find_audience_by_uid(db, name_uid)
        if by_uid:
            return by_uid, None

    exact = await find_audience_by_name(db, name)
    if exact:
        return exact, None

    rows = list(
        (
            await db.execute(
                select(CustomAudience)
                .where(CustomAudience.name.ilike(f"%{name}%"))
                .order_by(CustomAudience.uid.asc().nulls_last(), CustomAudience.name.asc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) == 1:
        return rows[0], None
    if not rows:
        return None, {"ok": False, "error": f"No audience matched '{name}'"}
    return None, {
        "ok": False,
        "error": f"Multiple audiences matched '{name}' — pass uid (preferred) or audience_id",
        "candidates": [_cand(r) for r in rows],
    }


async def tool_list_audiences(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    from app.audiences import fmt_audience_uid, list_audiences_with_counts, parse_audience_uid_token

    items = await list_audiences_with_counts(db)
    uid_filter = parse_audience_uid_token(args.get("uid"))
    if uid_filter is not None:
        items = [a for a in items if a.uid == uid_filter]
    query = str(args.get("query") or args.get("name") or "").strip().lower()
    if query:
        q_uid = parse_audience_uid_token(query)
        items = [
            a
            for a in items
            if query in (a.name or "").lower()
            or (q_uid is not None and a.uid == q_uid)
            or (fmt_audience_uid(a.uid) and query == fmt_audience_uid(a.uid))
        ]
    return {
        "ok": True,
        "count": len(items),
        "audiences": [
            {
                "id": a.id,
                "uid": a.uid,
                "uid_label": fmt_audience_uid(a.uid),
                "name": a.name,
                "source": a.source,
                "description": a.description,
                "filters": a.filters,
                "filter_summary": a.filter_summary,
                "count": a.count,
            }
            for a in items
        ],
        "note": (
            "Custom audiences on CRM left-menu Audience (/assets). "
            "Prefer uid (2-digit 11–99) when referencing. "
            "source dealers = left Dealer; source leads = left Leads. Counts are live."
        ),
    }


async def tool_create_audience(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    from app.audiences import create_audience_row, filter_summary, fmt_audience_uid, normalize_filters

    name = str(args.get("name") or "").strip()
    source = str(args.get("source") or "").strip()
    description = str(args.get("description") or "").strip()
    filters = _audience_filters_from_args(args)
    if not name:
        return {"ok": False, "error": "name is required"}
    if not source:
        return {"ok": False, "error": "source is required (dealers | leads)"}
    if not _truthy_confirm(args.get("confirm")):
        return {
            "ok": False,
            "created": False,
            "needs_confirmation": True,
            "error": (
                "confirm=true required. Summarise NAME / SOURCE / FILTERS for the user, "
                "ask them to confirm, then call again with confirm=true."
            ),
            "preview": {
                "name": name,
                "source": source,
                "filters": filters,
                "description": description,
            },
        }
    try:
        row = await create_audience_row(
            db,
            name=name,
            source=source,
            description=description,
            filters=filters,
            created_by=user.id,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    filt = normalize_filters(row.filters_json)
    return {
        "ok": True,
        "created": True,
        "audience": {
            "id": row.id,
            "uid": row.uid,
            "uid_label": fmt_audience_uid(row.uid),
            "name": row.name,
            "source": row.source,
            "description": row.description or "",
            "filters": filt,
            "filter_summary": filter_summary(filt),
            "count": int(row.last_count or 0),
        },
        "note": "Visible on CRM Audience page. Member count is dynamic. Prefer uid for later tools.",
    }


async def tool_update_audience(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    from app.audiences import filter_summary, fmt_audience_uid, normalize_filters, update_audience_row

    row, err = await _resolve_audience_row(db, args)
    if err or not row:
        return err or {"ok": False, "error": "Audience not found"}

    new_name = args.get("new_name")
    source = args.get("source")
    description = args.get("description")
    clear_filters = _truthy_confirm(args.get("clear_filters"))
    filters: dict[str, Any] | None = None
    if clear_filters:
        filters = {}
    elif args.get("filters") is not None or any(args.get(k) for k in ("state", "city", "district")):
        # Merge with existing when only partial keys provided
        existing = normalize_filters(row.filters_json)
        incoming = _audience_filters_from_args(args)
        if isinstance(args.get("filters"), (dict, str)) and args.get("filters") not in (None, ""):
            filters = incoming
        else:
            merged = dict(existing)
            merged.update(incoming)
            filters = merged

    if not _truthy_confirm(args.get("confirm")):
        return {
            "ok": False,
            "updated": False,
            "needs_confirmation": True,
            "error": (
                "confirm=true required. Summarise the audience change for the user, "
                "ask them to confirm, then call again with confirm=true."
            ),
            "preview": {
                "uid": row.uid,
                "name": row.name,
                "new_name": new_name,
                "source": source or row.source,
                "filters": filters,
                "description": description,
                "clear_filters": clear_filters,
            },
        }

    try:
        await update_audience_row(
            db,
            row,
            name=str(new_name).strip() if new_name is not None else None,
            source=str(source).strip() if source is not None else None,
            description=str(description) if description is not None else None,
            filters=filters,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    filt = normalize_filters(row.filters_json)
    return {
        "ok": True,
        "updated": True,
        "audience": {
            "id": row.id,
            "uid": row.uid,
            "uid_label": fmt_audience_uid(row.uid),
            "name": row.name,
            "source": row.source,
            "description": row.description or "",
            "filters": filt,
            "filter_summary": filter_summary(filt),
            "count": int(row.last_count or 0),
        },
    }


async def tool_delete_audience(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    if not _truthy_confirm(args.get("confirm_delete")):
        return {
            "ok": False,
            "deleted": False,
            "error": "confirm_delete=true required to delete an audience",
        }
    row, err = await _resolve_audience_row(db, args)
    if err or not row:
        return err or {"ok": False, "error": "Audience not found"}
    from app.audiences import fmt_audience_uid

    aid, uid, name, source = row.id, row.uid, row.name, row.source
    await db.delete(row)
    await db.flush()
    return {
        "ok": True,
        "deleted": True,
        "deleted_id": aid,
        "deleted_uid": uid,
        "uid_label": fmt_audience_uid(uid),
        "name": name,
        "source": source,
    }


async def tool_preview_audience(db: AsyncSession, user: User, args: dict[str, Any]) -> dict[str, Any]:
    from app.audiences import filter_summary, fmt_audience_uid, normalize_filters, resolve_audience_members

    row, err = await _resolve_audience_row(db, args)
    if err or not row:
        return err or {"ok": False, "error": "Audience not found"}
    try:
        limit = int(args.get("limit") or 25)
    except (TypeError, ValueError):
        limit = 25
    limit = max(1, min(100, limit))
    with_phone_only = _truthy_confirm(args.get("with_phone_only"))
    items, total, with_phone = await resolve_audience_members(
        db, row, limit=limit, with_phone_only=with_phone_only
    )
    row.last_count = total
    filt = normalize_filters(row.filters_json)
    return {
        "ok": True,
        "audience_id": row.id,
        "uid": row.uid,
        "uid_label": fmt_audience_uid(row.uid),
        "name": row.name,
        "source": row.source,
        "filters": filt,
        "filter_summary": filter_summary(filt),
        "total": total,
        "with_phone": with_phone,
        "returned": len(items),
        "truncated": len(items) < total,
        "members": [m.model_dump() for m in items],
        "note": "Live resolve from dealers/leads — not a frozen snapshot.",
    }


TOOL_HANDLERS: dict[str, Callable[[AsyncSession, User, dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "get_sales_summary": tool_get_sales_summary,
    "search_dealers": tool_search_dealers,
    "search_leads": tool_search_leads,
    "search_google_businesses": tool_search_google_businesses,
    "find_new_dealers": tool_find_new_dealers,
    "start_dealer_finder": tool_start_dealer_finder,
    "get_dealer_finder_job": tool_get_dealer_finder_job,
    "get_dealer_finder_results": tool_get_dealer_finder_results,
    "filter_dealer_finder_results": tool_filter_dealer_finder_results,
    "import_dealer_finder_leads": tool_import_dealer_finder_leads,
    "get_dealer_sales": tool_get_dealer_sales,
    "get_region_sales": tool_get_region_sales,
    "get_product_sales_by_dealer": tool_get_product_sales_by_dealer,
    "query_business_data": tool_query_business_data,
    "get_unanswered_messages": tool_get_unanswered_messages,
    "get_whatsapp_connection_status": tool_get_whatsapp_connection_status,
    "get_employee_whatsapp_instances": tool_get_employee_whatsapp_instances,
    "search_whatsapp_messages": tool_search_whatsapp_messages,
    "list_whatsapp_threads": tool_list_whatsapp_threads,
    "get_whatsapp_thread": tool_get_whatsapp_thread,
    "report_enquiries": tool_report_enquiries,
    "send_whatsapp_text": tool_send_whatsapp_text,
    "send_whatsapp_text_batch": tool_send_whatsapp_text_batch,
    "list_campaign_media": tool_list_campaign_media,
    "send_whatsapp_media_batch": tool_send_whatsapp_media_batch,
    "list_audiences": tool_list_audiences,
    "create_audience": tool_create_audience,
    "update_audience": tool_update_audience,
    "delete_audience": tool_delete_audience,
    "preview_audience": tool_preview_audience,
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


# Mutating tools require an explicit confirm flag (soft guard; handlers also check).
_MUTATING_CONFIRM_KEYS: dict[str, tuple[str, ...]] = {
    "send_whatsapp_text": ("confirm_send",),
    "send_whatsapp_text_batch": ("confirm_send",),
    "send_whatsapp_media_batch": ("confirm_send",),
    "create_audience": ("confirm",),
    "update_audience": ("confirm",),
    "delete_audience": ("confirm_delete", "confirm"),
    "remember_fact": ("confirm",),
    "request_new_tool": ("confirm",),
}


def _mutating_confirm_ok(name: str, args: dict[str, Any]) -> bool:
    keys = _MUTATING_CONFIRM_KEYS.get(name)
    if not keys:
        return True
    return any(_truthy_confirm(args.get(k)) for k in keys)


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
    # Soft confirm contract (_MUTATING_CONFIRM_KEYS): handlers return needs_confirmation
    # previews when confirm / confirm_send / confirm_delete is missing.
    async def _log_work_report(call: AiToolCall, result_payload: dict[str, Any] | None) -> None:
        try:
            from app.ai.work_reports import record_from_tool_call

            await record_from_tool_call(
                db,
                user=user,
                run_id=run_id,
                tool_call=call,
                args=args,
                result=result_payload if isinstance(result_payload, dict) else None,
            )
        except Exception:  # noqa: BLE001
            # Work reports must never break tool execution.
            pass
        if name in {"send_whatsapp_media_batch", "send_whatsapp_text_batch"} and isinstance(
            result_payload, dict
        ):
            try:
                from app.campaign_reports import record_campaign_run_from_batch

                await record_campaign_run_from_batch(
                    db,
                    user=user,
                    args=args,
                    result=result_payload,
                    tool_name=name,
                )
            except Exception:  # noqa: BLE001
                pass

    if not handler:
        err = f"Unknown or unauthorized tool: {name}"
        call = AiToolCall(
            run_id=run_id,
            tool_name=name,
            args_json=json.dumps(args, default=str),
            result_json="",
            success=False,
            error=err,
            duration_ms=0,
        )
        db.add(call)
        await db.flush()
        return {"ok": False, "error": err}

    try:
        result = await handler(db, user, args)
        duration_ms = int((time.perf_counter() - started) * 1000)
        # Cap stored result size (Dealer Finder full phone lists need a large ceiling)
        result_json = json.dumps(result, default=str, ensure_ascii=False)
        max_store = 2_500_000 if name in {
            "start_dealer_finder",
            "get_dealer_finder_results",
            "filter_dealer_finder_results",
            "get_dealer_finder_job",
            "import_dealer_finder_leads",
        } else 50_000
        if len(result_json) > max_store:
            # Prefer keeping public_id + summary so the chat UI can re-fetch full rows.
            if isinstance(result, dict):
                slim = {
                    k: result.get(k)
                    for k in (
                        "ok",
                        "report_kind",
                        "results_ready",
                        "awaiting_completion",
                        "public_id",
                        "job",
                        "status_report",
                        "summary",
                        "count",
                        "total_unique",
                        "filtered_total",
                        "eligible_with_phone",
                        "with_phone",
                        "display_filter",
                        "truncated",
                        "note",
                        "example_import_confirm_prompt",
                        "error",
                    )
                    if k in result
                }
                # Do not store a misleading partial table — live card re-fetches by public_id.
                slim["items"] = []
                slim["truncated"] = True
                slim["stored_partial"] = True
                result_json = json.dumps(slim, default=str, ensure_ascii=False)
                if len(result_json) > max_store:
                    result_json = result_json[:max_store] + '…"}'
            else:
                result_json = result_json[:max_store] + '…"}'
        call = AiToolCall(
            run_id=run_id,
            tool_name=name,
            args_json=json.dumps(args, default=str)[:8000],
            result_json=result_json,
            success=True,
            error="",
            duration_ms=duration_ms,
        )
        db.add(call)
        await db.flush()
        await _log_work_report(call, result if isinstance(result, dict) else None)
        return {"ok": True, "tool": name, "result": result, "duration_ms": duration_ms}
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - started) * 1000)
        call = AiToolCall(
            run_id=run_id,
            tool_name=name,
            args_json=json.dumps(args, default=str)[:8000],
            result_json="",
            success=False,
            error=str(exc)[:2000],
            duration_ms=duration_ms,
        )
        db.add(call)
        await db.flush()
        await _log_work_report(call, None)
        return {"ok": False, "tool": name, "error": str(exc), "duration_ms": duration_ms}
