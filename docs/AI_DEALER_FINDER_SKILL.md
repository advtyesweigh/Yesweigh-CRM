# AI chat skill — Dealer Finder (full job → confirm import)

## Hard rule — dealer = weighing scale dealer

In YesWeigh CRM, **dealer / dealers** always means **weighing scale dealer** (DEFAULT_CATEGORIES).
Never Google Places or Dealer Finder for generic dealers, car/tyre/other-product dealers, or unrelated categories.

## When to use (REQUIRED)

User asks for weighing-scale dealers across a **whole state** — with or without the word **google**.

**Canonical pattern → always `start_dealer_finder`:**

- contains **google** (any case) + find/search/**list** dealers + a **state** (no city)
- Example: `find weighing scale dealer tamilnadu from google`
- Also: `weighing scale dealers list Tamilnadu from google`
- Also: `search weighing scale dealers in Tamil Nadu from Google`
- Also without “google”: `Find weighing scale dealers in Tamil Nadu`, `run dealer finder for TN`

**Never** answer these with CRM `query_business_data dataset=dealers` (e.g. “Dealer Contacts – Tamil Nadu: 203”). That is existing CRM phones, not Google.
**Never** answer these with `find_new_dealers` / `search_google_businesses` (old ~60-result “New Dealers (Google – CRM)” card). That card is wrong for statewide coverage.

## Tools

| Tool | Role |
|------|------|
| `start_dealer_finder` | Start FULL batch (confirm-before-act) |
| `get_dealer_finder_job` | Poll progress; no place drip while running |
| `get_dealer_finder_results` | FULL unique-place list when `results_ready` |
| `import_dealer_finder_leads` | Import eligible → Leads (confirm-before-act) |

`find_new_dealers` / `search_google_businesses` = **city-level quick lookup only** (city required). State-only calls redirect here.

## Expansion path (must explain in confirm summary)

1. Gemini lists **ALL districts**
2. For each district, Gemini lists **important cities/towns/commercial centres** (cached in Postgres)
3. Google Places runs **district-wise AND city-wise × ALL categories** with **full pagination per unit** to **completion**

One statewide Places query (~60 cap) is **not** enough. User wants whole-state data, not 34/60.

## UX flow (required)

1. **Confirm start** — summarise districts/cities/categories + cost → `confirm=true`.
2. **Run full job** — `start_dealer_finder` waits / poll until `results_ready` (or Paused/Failed).
   Do **NOT** list partial district/city batches while Running.
3. **Show all results IN CHAT** — full unique-place table (name, phone/WhatsApp, website, city, district, crm status). Never tell the user to open `/dealer-finder`.
4. **Confirm import** — summarise eligible count → `import_dealer_finder_leads` `confirm=true`.
   Confirm import only after explicit `yes` / `confirm` (or **Imports to Leads** on the card).
   `import to leads` / `import … phone only` routes to import **preview** — never CRM Leads list.
   Phone-only → pass `with_phone_only=true` (import only eligible rows with a phone).
5. **Status report** — units, API calls, unique places, existing dealers/leads, eligible, imported, skipped, est. cost — also in chat.

### Example confirm prompts

**Start:**  
“Start full Dealer Finder for Tamil Nadu (all districts → cities × categories)? This uses Google Places (API cost). Reply yes to start.”

**Import:**  
“Import 42 eligible NEW places to Leads (source Google, status New)? Existing dealers/leads stay skipped. Reply yes to import.”

**Phone-only import:**  
“Import 232 eligible NEW places **with phone** to Leads? Reply yes to import.”

## UI

Primary UX is **AI Agent chat**. Optional admin page `/dealer-finder` may exist but agents must not redirect users there.

## Do not

- Do not invent websites/phones
- Do not drip incomplete place lists while the job is running
- Do not import leads without a separate confirmed import
- Do not skip cooldown Refresh without cost warning (`force_refresh`)
- Do not show the old find_new_dealers 60-cap card for statewide / “… from google” state queries
- Do not tell users to check `/dealer-finder` — render the table in chat
