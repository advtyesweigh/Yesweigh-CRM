# Intelligence Phase 1 — Observe

Shipped after approval of `docs/AI_INTELLIGENCE_UPGRADE_PLAN.md` (Phase A).

## How to open Command Centre

1. Sign in to [https://crm.yesweigh.in](https://crm.yesweigh.in) as Management, a sales manager, or an admin.
2. Open **Intelligence** in the sidebar (Overview group), or go to `/intelligence`.
3. Reply Stats stays a separate page at `/whatsapp/reply-stats` (also linked from the Intelligence header and the Staff tab).

The board is SQL. It does not call Gemini. If `ai_settings.intel_enabled` is false, the page says intelligence is not enabled and `/api/intel/findings` returns 404.

Service managers see the service queue only. Campaigns-only roles do not see Intelligence.

## What shipped

- Findings: unreplied DMs (same `awaiting_staff_reply` + DM filters as Reply Stats), inactive dealers (6-month Zoho rule), open invoice balances (countable invoices, display only), unresolved service threads (tags only).
- Each finding has a source link: inbox thread (`/whatsapp?thread=…`), dealer search, or a Zoho invoice number to copy.
- Dealer health snapshot (`GET /api/intel/dealers/{id}/health`) and a Health block on the dealer detail dialog. Band is a rule: attention / quiet / steady.
- Funnel with enquiry, quotation language (outline label, not a Zoho quote), sales order, invoice open, invoice settled. No dispatch stage.
- Rule-based commitments and secondary intents. They do not write `awaiting_staff_reply` or `service_category`. Low-confidence commitments stay off the high lane. A due date in the past can be marked missed by the nightly job; no due date stays open.
- Postgres tables: `ai_jobs`, `ai_findings`, `ai_commitments`, `ai_thread_insights`, `ai_dealer_health`, `ai_job_checkpoints`.
- Additive `ai_settings` columns: `intel_enabled` (default false), intel daily/monthly budgets (₹10 / ₹150) and reserved counters. Existing ₹25 / ₹500 caps are unchanged. `intel_*` usage purposes sit in their own bucket.
- One worker coroutine in the API process. It idles while the flag is off. Webhooks only enqueue a job after commit, and only for DMs when the flag is on. A failed enqueue does not fail the webhook.
- Sidebar groups: Overview, Conversations, Growth, Admin. Placeholder routes (Call List, Quotations, Meta Ads, Performance, Reports) still open if bookmarked.

## Move-to learning

When an agent uses **Move to…** on a chat modal, the API (`POST /api/intel/stage-move`) does more than override that one thread:

1. Writes `ai_thread_insights.stage_override` (forces that chat into the chosen stage / Closed).
2. Inserts a training row in `ai_stage_moves` with `from_stage`, `to_stage`, actor, jid, timestamp, recent chat `snippet`, and `signals_json` (phrases + inbound/outbound).
3. Upserts labeled phrases into `ai_stage_kb` (positive for `to_stage`, negative for `from_stage`, `exclude` polarity when Closed).
4. Next funnel / enquiry / stamping / closed-exclude classify loads `ai_stage_kb` and boosts similar chats that match those phrases (Closed exclude phrases require weight ≥ 2).

## Endpoints

All require a JWT. They return 404 while intelligence is off.

| Method | Path |
| --- | --- |
| GET | `/api/intel/findings` |
| GET | `/api/intel/commitments` |
| GET | `/api/intel/dealers/{id}/health` |
| GET | `/api/intel/funnel` |
| GET | `/api/intel/service` |
| GET | `/api/intel/staff-reply` |
| POST | `/api/intel/stage-move` |
| GET | `/api/intel/stage-options` |
| POST | `/api/intel/jobs/rebuild` |
| GET | `/api/intel/jobs` |

## Deferred (not in this phase)

- Gemini narration, growth wording, Intel Ask, KB drafts from repeated questions (Recommend).
- Create task, dismiss, human commitment status, inbox Next step buttons (Assist).
- Any automatic WhatsApp send, Zoho write-back, dealer merge, dispatch stage, credit/dunning, calibration or firmware advice, salary or employee scores, free SQL, competitor crawl, digital twin, historical Sarvam or outline backfill.
- Execute phase.

Backfill checkpoints exist and stay paused. Deploy does not scan old chats with a model.

## Flag

Code and migration default `intel_enabled` to false. This rollout turns it on for `organization_id = yesweigh` so Command Centre loads. To hide it again:

```sql
UPDATE ai_settings SET intel_enabled = false WHERE organization_id = 'yesweigh';
```

The worker stops leasing and the routes 404. Inbox behaviour does not change.
