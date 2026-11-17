# YesWeigh AI Agent — Tool Registry

Last updated: 2026-11-16

## Principles

1. The model **never** executes arbitrary Python, SQL, shell, or unrestricted HTTP.
2. Every tool is a **predefined** FastAPI-backed function with Pydantic input validation.
3. Permissions are decided by CRM code (auth + role + instance ACL), **not** by the model.
4. Tool results shown in the UI are **real CRM data**, not fabricated narrative.
5. Secrets (Gemini key, Evolution tokens, DB URL) are never passed into prompts or tool results.

## Phase 1 tools (implemented)

| Tool name | Purpose | Data source | Permission |
|-----------|---------|-------------|------------|
| `get_sales_summary` | Company-wide MTD / FY YTD / prior-month same-day sales | `zoho_sales` via same logic as `/api/dashboard/sales-overview` | Authenticated user |
| `get_unanswered_messages` | Open inbox threads with unread / awaiting reply | `whatsapp_conversations` | Admin: all org instances; else assigned instances only |
| `get_whatsapp_connection_status` | Per-instance connection status summary | `whatsapp_instances` | Same instance ACL |
| `get_employee_whatsapp_instances` | Employee ↔ instance mapping (safe fields) | `whatsapp_instances` + links | Admin: all; else own assignments |
| `request_new_tool` | Meta-tool: ask Cursor tool factory to build a new read-only tool | Cursor Cloud Agents API | Admin/management + `AI_TOOL_FACTORY_ENABLED` |

Generated tools (hot-loaded from `/data/ai-generated-tools`) appear alongside builtins after a factory run. See `docs/AI_TOOL_FACTORY.md`.

## Phase 2 tools (dealer / customer sales)

| Tool name | Purpose | Data source | Permission |
|-----------|---------|-------------|------------|
| `search_dealers` | Fuzzy/partial dealer name or company search | `dealers` (exclusions + merges respected) | Authenticated user |
| `get_dealer_sales` | Per-dealer totals for `fy_ytd` / `mtd` / `custom` | `zoho_sales` tax-exclusive `sub_total`; resolve via `dealers` then `customer_name` fallback | Authenticated user |
| `get_region_sales` | District / area / DT or state sales aggregate (one call) | `zoho_sales` joined to `dealers` on remapped `customer_id`; match `dealers.city` (CRM District) or `dealers.state` | Authenticated user |

**Name matching:** Token AND on `dealers.name` / `dealers.company` (plural stems so `Electronics`↔`ELECTRONIC`), scored (exact → starts-with → contains → all-tokens). Unique or multi-token clear winner → totals; short/ambiguous queries return candidates only. Optional `customer_id` / `dealer_id` skips search. Fallback: `zoho_sales.customer_name`. Merge sources fold via `sales_customer_ids_for_dealer`.

**Region matching:** `malappuram dt` / `Malappuram district` resolve via Kerala district aliases + town→district map (e.g. Manjeri → Malappuram). CRM stores district on `dealers.city`. Prefer `get_region_sales` over looping dealers.

## Vendor / supplier tools (Zoho live fallback)

CRM Postgres syncs Zoho **customers** into `dealers` / `zoho_dealers` only. Vendors are **not** persisted. Tools try SQL first, then call Zoho Books live (OAuth tokens from DB; client id/secret from env).

| Tool name | Purpose | Data source | Permission |
|-----------|---------|-------------|------------|
| `search_vendors` | Fuzzy vendor/supplier search by name | Postgres vendor probe → Zoho `GET /books/v3/contacts?contact_type=vendor` | Authenticated user |
| `get_vendor` | One vendor profile (GST, address, contact persons, payables) | Postgres if staged as vendor → Zoho `GET /contacts/{id}` | Authenticated user |
| `get_vendor_related` | Bills, purchase orders, vendor payments | Zoho live (`/bills`, `/purchaseorders`, `/vendorpayments`) | Authenticated user |

## Planned tools (later)

`search_existing_leads`, `preview_lead_import`, `import_approved_leads`, `find_duplicate_contacts`, `find_marketing_assets`, `create_campaign_draft`, `get_campaign_performance`, `create_followup_task`, `get_employee_activity`, `get_product_sales`, `get_inactive_dealers`, `get_product_details`, `get_approved_dealer_price`, `get_stock_availability`, `create_management_alert`, …

Write / send / spend tools require **approval gates** (documented in AI_SECURITY.md).

## Invocation path

```
Gemini function call
  → validate name ∈ registry
  → validate args (schema)
  → enforce auth context (user, employee, instance ACL)
  → execute tool
  → persist ai_tool_calls row
  → return JSON result to model / UI timeline
```

## Audit

Every tool call stores: run id, tool name, args (redacted), result summary, duration, success/failure, user id.
