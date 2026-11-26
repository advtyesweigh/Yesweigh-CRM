# Gemini AI Dealer Finder

## Expansion path (primary)

For **Tamil Nadu** (same for any state):

1. **Gemini lists ALL districts** (merged with verified geo directory so none are missed).
2. **For each district**, Gemini lists important **cities / towns / commercial centres**.
3. **Google Places** runs **district-wise AND city-wise × categories** → maximum results.

One statewide query is **not** enough. The district→cities plan is **cached in PostgreSQL** (`dealer_location_plans`).

## How to run — Tamil Nadu

### UI

1. Open **Dealer Finder** (`/dealer-finder`).
2. State = **Tamil Nadu**.
3. Click **Build location plan** (Gemini + verified districts; cached after first run).
4. Click **Search** → read cost summary → **Confirm & Search** (starts FULL job).
5. Watch **progress tiles only** while Running — place list is withheld (no partial drip).
6. When complete (or pause/fail), review **all unique places** (website / WhatsApp / city / district / CRM status).
7. **Import eligible** → confirm → Leads (source Google, status New) + **job status report**.

### AI Agent

Ask (canonical): *find weighing scale dealer tamilnadu from google*

Also: *Find weighing scale dealers in Tamil Nadu* — any statewide + optional **google**, no city.

Flow: `start_dealer_finder` (confirm) → poll `get_dealer_finder_job` until `results_ready` →
`get_dealer_finder_results` (FULL unique list only) → `import_dealer_finder_leads` (confirm) →
status report. Never the old `find_new_dealers` ~60-result card for statewide.
See `docs/AI_DEALER_FINDER_SKILL.md`.

### API

```bash
# 1) Plan
curl -X POST /api/dealer-finder/plans -H "Authorization: Bearer …" \
  -d '{"state":"Tamil Nadu"}'

# 2) Preview then start
curl -X POST /api/dealer-finder/jobs -d '{"state":"Tamil Nadu","confirm":false}'
curl -X POST /api/dealer-finder/jobs -d '{"state":"Tamil Nadu","confirm":true,"search_depth":"full"}'
```

## Smart Search History

Before each Places call, identity is checked:

`org + country + state + district + city + category + language + plan_version + API params`

- Default **30-day** cooldown (admin: 7/15/30/60/90/custom).
- If recent → **Already Searched** (no auto Google call); Refresh requires cost confirm.
- District / city / category tracked **independently** (statewide ≠ all cities covered).

## Cost controls (two-tier monthly)

Dealer Finder → **Settings → Cost controls** (table `dealer_finder_settings`):

| Setting | Default | Meaning |
|---------|---------|---------|
| `free_requests_per_month` | legacy | Legacy combined hint; billing UI uses **per-SKU** India caps (Text Search Pro 35k, Place Details Enterprise 7k). |
| `max_monthly_spend_usd` | **100** | **Ops throttle** — pauses on CRM **billable-after-caps** USD estimate. Console *usage cost* (gross INR) and *subtotal* (after savings) are separate; subtotal can be **₹0**. |
| `max_requests_per_job` / `_day` | High | Operational safety only — must not block the paid allowance. |

**How enforcement works**

1. Each India SKU has its own monthly free cap (Text Search Pro **35k**, Place Details Enterprise **7k**).
2. CRM estimates **billable** spend using [India Places pricing](https://developers.google.com/maps/billing-and-pricing/pricing-india-ondc) — not US list price.
3. New Places requests **stop** when India-estimated billable USD hits **`max_monthly_spend_usd`** (ops throttle). **Google Cloud Console** shows actual INR.
4. Do **not** treat 7000 as a dollar amount. Do **not** set spend to $7000.

Legacy field `max_requests_per_month` is an alias of the free quota (same value), not a hard monthly request stop.

## Schema (new tables)

| Table | Role |
|-------|------|
| `dealer_location_plans` | Cached Gemini district→cities plan |
| `dealer_search_jobs` | Batch job + resumable unit queue |
| `dealer_search_logs` | Per location×category history |
| `dealer_search_places` | Results for review/import |
| `dealer_finder_settings` | Cooldown + two-tier budgets |
| `dealer_places_usage` | Daily Stage-1/2 request tracking |

`leads.customer_website` already exists — reused on import when allowed.

## Reused existing code

- `google_places_search.py` pagination rules (20/page, ≤60)
- `find_new_dealers` / CRM match helpers
- `state_normalize` / Places state aliases
- `phone_util.normalize_in_mobile`
- Frontend `indiaStatesDistricts.json` (copied to backend `app/data/`)

## Categories

weighing scale dealers · electronic weighing scale dealers · label printing scale dealers · digital weighing machine distributors · retail billing scale suppliers · industrial weighing scale suppliers · weighbridge dealers
