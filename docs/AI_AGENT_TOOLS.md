# YesWeigh AI Agent — Tool Registry

Last updated: 2026-11-22

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
| `search_whatsapp_messages` | Keyword search across message `body` + voice `transcript` (EN/ML); filters: days/dates, instance, direction, contact, unread/open | `whatsapp_messages` + conversations | Same instance ACL as other WA tools |
| `list_whatsapp_threads` | List conversations (contact, agent, unread, preview) | `whatsapp_conversations` | Same instance ACL |
| `get_whatsapp_thread` | One thread + recent message history (body + transcripts) | conversations + messages | Same instance ACL; 403 if not authorized |
| `report_enquiries` | “Who asked about X?” — topic keywords, prefer inbound, dedupe by phone | wraps `search_whatsapp_messages` | Same instance ACL |
| `request_new_tool` | Meta-tool: ask Cursor tool factory to build a new read-only tool | Cursor Cloud Agents API | Admin/management + `AI_TOOL_FACTORY_ENABLED` |

Generated tools (hot-loaded from `/data/ai-generated-tools`) appear alongside builtins after a factory run. See `docs/AI_TOOL_FACTORY.md`.

## Phase 2 tools (dealer / customer sales)

| Tool name | Purpose | Data source | Permission |
|-----------|---------|-------------|------------|
| `search_dealers` | Fuzzy/partial dealer name or company search (Dealer menu — not Leads) | `dealers` (exclusions + merges respected) | Authenticated user |
| `search_leads` | Search/list CRM left-menu Leads by name/phone/state/city/district | `leads` (distinct from `dealers`) | Authenticated user |
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

`preview_lead_import`, `import_approved_leads`, `find_duplicate_contacts`, `find_marketing_assets`, `create_campaign_draft`, `get_campaign_performance`, `create_followup_task`, `get_employee_activity`, `get_product_sales`, `get_inactive_dealers`, `get_product_details`, `get_approved_dealer_price`, `get_stock_availability`, `create_management_alert`, …

**Leads vs dealers (explicit map):** left-drawer **Leads** (`/leads`) → table `leads` → `search_leads` / `query_business_data dataset=leads`. Left-drawer **Dealer** → `search_dealers` / `dataset=dealers`. Phrases `leads` / `find leads` / `lead contacts` always mean CRM Leads, never dealers. Default find/all leads returns the full drawer set (limit 1500) — not truncated to 25 unless the user asks top N.

### WhatsApp intelligence (search → summarize)

Do **not** add topic-specific tools (container, warranty, OV…). Use the flexible search/thread tools; the model summarizes.

| Example prompt on `/work` | Tools the agent should use |
|---------------------------|----------------------------|
| Who asked about container arrival last week? | `report_enquiries` topic=`container arrival` + Malayalam keywords, `days=7` or `date_from`/`date_to` |
| Dealers asking about stock / dispatch | `report_enquiries` topic=`stock dispatch` or `search_whatsapp_messages` query=`stock,dispatch,വന്നോ,സ്റ്റോക്ക്` match_any=true |
| Summarize unread for Visakh | `list_whatsapp_threads` instance=`Visakh` unread_only=true → optional `get_whatsapp_thread` on top threads |
| What did Safe Electronics say about the strike? | `search_whatsapp_messages` contact=`Safe Electronics` query=`strike,container` → `get_whatsapp_thread` |
| Is Visakh’s WhatsApp connected? | `get_whatsapp_connection_status` / `get_employee_whatsapp_instances` |

Default lookback **~45 days** (max 180). Results are ACL-scoped to the user’s assigned WhatsApp instances (admins: all YesWeigh instances).

### WhatsApp send (agent-invoked, explicit confirm)

| Tool name | Purpose | Data source | Permission |
|-----------|---------|-------------|------------|
| `send_whatsapp_text` | Send **one** text via Evolution GO from an authorized YesWeigh employee instance to a **customer** phone; logs outbound in CRM inbox | Evolution GO `send_text` + `whatsapp_messages` | Same instance ACL as inbox; `confirm_send=true` required; `dry_run` supported; blocks multi-phone strings / Care Meta Cloud / AI opt-out / **self-message** / **staff phones**; skips duplicate same text→same phone within ~24h |
| `send_whatsapp_text_batch` | Send the **same** text **sequentially** to a named **customer** list (max **50**) with `delay_seconds` between sends (default **10**, min **5**); stops on fatal Evolution/instance errors; per-recipient status in result | Same Evolution path as single send | Same ACL; `confirm_send=true` required; `dry_run` supported; Care Meta Cloud blocked; strips self/staff from TO; phone dedupe in-batch; duplicate-skip ~24h; optional `batch_id`; each send logged to inbox + Work Reports |
| `list_campaign_media` | Search Campaigns Media library by title/topic/tags (e.g. **GATC RAM**) | `campaign_media` | Any authenticated CRM user |
| `send_whatsapp_media_batch` | Send the **same** campaign media (video/image/document) **sequentially** via Evolution GO; recipients[] **or** `recipient_filter=kerala_dealers` (max **400**); `delay_seconds` default **10** | Campaigns media disk + Evolution `send/media` + inbox | Same ACL/guards as text batch; `confirm_send=true` required; dry_run; no auto-send; media dedupe ~24h (any instance success skips); **spam phones** (`is_spam` conversations, normalized phone match) skipped at filter + send time |

**FROM vs TO:** “from Biju / Visakh” = `from_instance_*` (sending Evolution line). Never put that agent (or any employee/instance phone) in `to_phone` / `recipients[]`. Confirm as `FROM: <agent> | TO: N customers (names)`.

**Single prompt pattern:** “List my WhatsApp instances, then send ‘…’ from \<agent number/public_id\> to \<phone\>” — confirm details, optionally `dry_run=true`, then `confirm_send=true`.

**Search → sequential send pattern:** “Who asked about container?” → show name/phone list → “Send ‘…’ to those from Visakh, 10 sec delay, confirm” → `send_whatsapp_text_batch` with `recipients=[{to_phone, customer_name}, …]`, `from_instance_public_id` (or Visakh phone/code), `delay_seconds=10`, `confirm_send=true`. Agent must **not** refuse sequential sends or tell the user to use Cursor. After **Failed / Retry**, only confirm remaining unsent customers (tools skip already-sent).

**Campaign media → Kerala dealers:** “Send media GATC RAM to all Kerala dealers from Visakh, 10 sec delay, confirm” → `list_campaign_media` + `send_whatsapp_media_batch` (`recipient_filter=kerala_dealers`, max 400). Send at actual file size via Evolution (e.g. GATC RAM ~30 MB); no invented WhatsApp video size refuse/warn. Indian phones are sent as `91…` (`to_evolution_number`). Per-recipient failures continue; only instance/gateway fatals stop the batch.

Other write / spend tools still require **approval gates** (documented in AI_SECURITY.md).

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
