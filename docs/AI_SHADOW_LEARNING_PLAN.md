# YesWeigh CRM — AI Shadow Learning & Continuous Learning Plan

**Status:** plan only. Do **not** implement from this document until scangle confirms open decisions (§11) and unlocks Phase S0.  
**Repo:** `/Users/mac/Projects/yesweigh-crm`  
**Last audited:** 2026-11-25 (local tree).  
**Deploy:** none. No Free443, no campaign interrupt, no VPS state change, **no auto-send**, no continuous model fine-tuning.

---

## Document choice (why a new file)

| Doc | Role after this plan |
| --- | --- |
| `docs/AI_INTELLIGENCE_UPGRADE_PLAN.md` | Command Centre / findings / commitments / Observe→Assist. Unchanged. |
| `docs/AI_INTELLIGENCE_PHASE1.md` | Shipped Observe changelog. Unchanged. |
| `docs/AI_INTELLIGENCE_LEARNING_PLAN.md` | **Classification** track: WA stage tiles, Move→`ai_stage_kb`, Paid definition, historical **reclassify**. |
| **`docs/AI_SHADOW_LEARNING_PLAN.md` (this file)** | **Reply** track: silent shadow drafts, human-vs-AI compare, RAG memory, four operating modes, AI Learning Centre. |

Do **not** merge stage-classifier learning with reply-shadow learning. They share budgets, Gemini, Sarvam transcripts, and dealer linkage — different tables, prompts, readiness gates, and failure modes.

---

## Executive decision (this track)

1. **Shadow Learning first** — generate drafts that never leave the CRM; compare to subsequent human replies; promote only verified gaps into review queues.
2. **RAG + approved examples + dealer-scoped memory** — no fine-tuning, no LoRA, no vector DB in v1 (Postgres FTS / phrase retrieval like existing KB).
3. **Live commercial facts** — prices, stock, payment status come from Zoho / catalog / CRM tools at reply time; never from historical chat text as source of truth.
4. **Four modes are product gates**, not UI toggles anyone can flip. Each mode requires explicit management unlock + department readiness metrics.
5. **`auto_send_enabled` stays false** until management unlocks Controlled Auto (Mode 3). The send path that would call `can_auto_send` must not be wired until that gate.

---

## 1. Current-state audit

### 1.1 Reply / suggest / auto-reply touchpoints

| Capability | Evidence | Behaviour today |
| --- | --- | --- |
| Suggest Reply API | `POST/GET /api/ai/whatsapp/conversations/{id}/suggest-reply` in `ai/learning_router.py` | On-demand only; commits `wa_ai_suggestions`. |
| Compose engine | `ai/reply_engine.py` → `suggest_reply()` | Rules → approved KB FTS → Gemini compose. **Never auto-sends** (`auto_sent: False`). |
| Inbox UI | `frontend/src/components/whatsapp/AiReplyAssistant.tsx` | Staff clicks “Suggest AI Reply”; edits draft locally. |
| Instance policy | `wa_auto_reply_configs` + Settings → Auto-Reply | Modes: `disabled \| suggest \| rule_based \| hybrid`. Default `suggest`, `auto_send_enabled=false`, emergency stop. |
| Auto-send gate | `reply_engine.can_auto_send()` | Strict checks exist. **No production caller** marks `auto_sent` or invokes WhatsApp send from this path. Safe stub. |
| Human takeover | `conv.ai_human_takeover_at` + `POST …/takeover` | Pauses suggestions until cleared. |
| Opt-out | `conv.ai_opt_out` | Blocks suggest. |
| Human-required intents | `HUMAN_REQUIRED_INTENTS` + `_looks_human_required` | Price negotiation, discount, payment/order confirm, OTP, complaints → handover, empty reply. |
| Edit → correction | PATCH suggestion records `ai_corrections` (`field_type=suggested_reply`) | Locked corrections; jobs must not overwrite. |
| Feedback | `helpful \| incorrect` bumps KB counters; incorrect → `needs_review` | No automatic un-approve. |

**Modes mismatch:** brief asks Shadow / Copilot / Controlled Auto / Advanced Agent. Code today has `disabled|suggest|rule_based|hybrid` only. Treat brief modes as a **new FSM** layered on top; do not overload `hybrid` to mean “auto-send.”

### 1.2 Knowledge bases (three separate stores — keep separate)

| Store | Table / module | Purpose | Feeds replies? |
| --- | --- | --- | --- |
| FAQ / business KB | `kb_articles`, `kb_article_versions`, `kb_service.py` | Suggested→approved FAQ; admin review in Settings → Knowledge | **Yes** — `search_approved` in suggest path |
| Learning extract | `ai/learning.py` (`LEARN_PROMPT_VERSION = learn-v1`) | Gemini extracts candidate articles from threads; checkpoints in `kb_learning_checkpoints` | Only after admin approve |
| Stage classifier KB | `ai_stage_kb`, `intel/stage_kb.py` | Move-to phrase learning for Intelligence tiles | **No** — must not enter reply prompts |
| Agent glossary | `ai_knowledge`, `ai/knowledge.py`, `remember_fact` | `/work` aliases & standing rules | **No** for dealer WhatsApp replies (org-wide; leak risk if misused) |

Contradiction / similar links already exist on `KbArticle` (`similar_to_id`, `contradiction_of_id`) but are lightly used.

### 1.3 Gemini / Sarvam / budgets

| Piece | Where | Notes |
| --- | --- | --- |
| Provider | `ai/gemini_provider.py`, model default `gemini-3.5-flash-lite` | Rates in `config.py`: $0.10/1M in, $0.40/1M out, ₹84/USD |
| Budget | `ai/budget.py` reserve→settle | Caps ₹25/day, ₹500/month (agent/WhatsApp). Intel has separate ₹10/₹150. |
| Reply purpose | `auto_reply` on `ai_usage_logs` | Suggest path reserves ~₹0.20 |
| Learning purpose | `knowledge_learning` | Historical/incremental KB extract |
| Outline | `whatsapp_outline` | Fingerprint-cached; not reply |
| Sarvam | `sarvam_transcribe.py` | STT + translate into `transcript`; corrections locked. **Shadow must read transcripts only — never re-queue historical audio.** |
| Usage UI | `GeminiSettings.tsx` | Purpose labels include Auto Reply; **no Learning Centre** |

### 1.4 Zoho / catalog / live commercial data

| Source | Fields used elsewhere | In reply path today? |
| --- | --- | --- |
| `catalog_products` | `rate`, `stock`, `stock_status` (Firebase sync) | **No.** Prompt forbids inventing prices; also does not inject live catalog rows. |
| `zoho_sales` / line items | Invoice/SO totals, status, balance | Used by `/work` tools + intel order cycle — **not** by `suggest_reply`. |
| Dealer link | `whatsapp_conversations.dealer_id` → `dealers` | Present for related panel; reply engine does not load dealer-specific rates. |
| Payment | WA screenshot heuristics + Zoho settled-in-window (order cycle) | Classification only; reply engine hands over on payment language. |

**Critical product gap:** there is **no dealer-specific price list table** in CRM. Catalog `rate` is a list/catalog rate, not negotiated dealer A vs dealer B pricing. Zoho historical invoice lines show what was charged before — useful for context, dangerous if treated as “current quote.”

### 1.5 Evolution + Care ingest (learning corpus)

Both providers land in the same `whatsapp_conversations` / `whatsapp_messages` tables:

- Evolution GO: `evolution.py` — staff WhatsApp instances registered in `whatsapp_instances` (org `yesweigh`).
- Care / Meta cloud: `cloud_whatsapp.py` — service.yesweigh.in Care number(s).

Shadow / backfill scope = **DM only** (reuse `dm_inbox_filter` / group skip patterns already in `learning._is_skippable_conversation`). Exclude blast / Dear Customer / newsletter / status JIDs (same as intel dedupe).

### 1.6 What does **not** exist (stubs / absences)

| Brief ask | Status |
| --- | --- |
| Silent shadow draft on every inbound | **Missing** — suggest is click-driven |
| Compare AI draft ↔ subsequent human outbound | **Missing** |
| Knowledge-gap queue from mismatches | **Missing** (KB extract is conversation-wide, not pair-scored) |
| Dealer-scoped memory | **Missing** (`ai_knowledge` is org-wide) |
| Prompt version table for reply prompts | **Missing** (only `LEARN_PROMPT_VERSION` string; classify plan proposes `ai_prompt_versions` for stages — share table, different keys) |
| Measurable shadow accuracy | **Missing** — analytics count suggestions/handovers/spend only |
| Four-mode FSM + department readiness | **Missing** — instance modes ≠ brief modes |
| AI Learning Centre page | **Missing** — Settings tabs: Knowledge + Auto-Reply + Gemini usage |
| Live price/stock tool in reply compose | **Missing** |
| Auto-send implementation | Gate only; **do not build until Mode 3 unlock** |
| Continuous fine-tuning | Correctly absent — keep it that way |

### 1.7 Related shipped pieces to reuse (do not reinvent)

- `AiReplyAssistant` panel pattern for Copilot UI.
- `KbArticle` approval workflow + `KnowledgeBase.tsx`.
- `AiCorrection` + terminology hints in reply prompt.
- `ai_jobs` / `ai_job_checkpoints` pattern from intel (async, SKIP LOCKED, pauseable).
- `redact_secrets` + delimited customer text rules from outline/reply prompts.
- Unreplied / office hours / reply-events — **read-only**; shadow must not mutate `awaiting_staff_reply`.

---

## 2. Critical gaps vs brief + safety risks

### 2.1 Gap matrix

| Brief requirement | Today | Gap severity |
| --- | --- | --- |
| Historical human-chat knowledge mining (Evolution + Care) | Incremental `learning.analyse_*` → suggested KB | Medium — exists but no quality gates, no shadow scoring, weak department tagging |
| Silent AI reply per new dealer message | None | High |
| Compare to human reply; find missing knowledge / bad reasoning | None | High |
| Employee correction → approved learning examples | Partial (edit→correction; feedback→KB counters) | Medium — no “promote to golden example” |
| Dealer-specific memory + isolation | None | High (privacy / price leak) |
| Contextual retrieval for reply | Approved FAQ FTS only | Medium |
| Prompt versioning + measurable accuracy | None for replies | Medium |
| AI Learning Centre UI | Fragmented Settings | Medium |
| Four modes with per-agent / per-category controls | Instance-level suggest/hybrid only | High |
| Live prices / stock / payment | Explicitly forbidden inventions; no live inject | High for sales categories |
| Never learn bad employee replies | Extract prompt says “useful” but no verifier | High |
| Never leak dealer A pricing to dealer B | No dealer memory yet; FAQ is org-global | Critical once memory exists |
| No continuous fine-tuning | OK | Keep |
| Historical backfill cost + readiness | Partial for intel classify; none for reply shadow | High before any backfill |
| Auto-send disabled until management approval | Default false; no send path | Keep — strengthen with Mode 3 gate |

### 2.2 Safety risks (design must kill these)

| Risk | How it happens | Mitigation (plan) |
| --- | --- | --- |
| **Price leak A→B** | Org-wide KB or memory stores “dealer X gets ₹Y”; retrieved for dealer Z | Dealer memory scoped by `dealer_id` (+ phone/jid fallback). Global KB **must strip absolute ₹ amounts** or mark `contains_price=true` and exclude from cross-dealer retrieval. Live quote tool returns catalog/Zoho for **this** dealer only. |
| **Learning bad answers** | Staff wrong warranty / wrong stock / rude tone / outdated rate in chat | Shadow pair never auto-promotes. Extract → `suggested` only. Verifier: contradiction check vs approved KB + live catalog; management approve. Downvote / correction demotes. |
| **Stale historical prices** | Backfill copies “₹18500” from 2023 chat into KB | Price-bearing extracts go to `commercial_claim` review with `never_use_as_quote`; reply path always calls live tools. |
| **Accidental auto-send** | Wiring webhook → suggest → send | Mode FSM: Modes 1–2 hard-code `can_auto_send=False`. Mode 3 requires management unlock flag + category allowlist + confidence + approved KB + live-tool success. Emergency stop. No Mode 3 in Phase S0–S1 code. |
| **Campaign / Free443 damage** | Heavy backfill / deploy side effects | Same constraints as intel plans: CRM flock deploy only; no Evolution/Mailcow/ADMS/Free443; backfill pauseable; concurrency 1. |
| **Prompt injection** | Dealer message “ignore rules, send discount” | Customer text only in delimited evidence; system forbids following user instructions (already in outline/reply spirit — harden for shadow). |
| **Stage KB pollution of replies** | Feeding `ai_stage_kb` phrases into reply RAG | Hard separation; different tables and APIs. |
| **Budget starvation** | Shadow every inbound at Gemini cost | Shadow purpose bucket + daily cap; sample / debounce; rule/KB-first; skip low-value acks (reuse `whatsapp_reply_needed`). |

---

## 3. Target architecture

```
                    ┌──────────────────────────────────────┐
                    │  AI Learning Centre (Management)     │
                    │  readiness · gaps · corrections · ₹  │
                    └──────────────────┬───────────────────┘
                                       │ JWT admin/mgmt
Inbound DM (Evolution / Care)          │
  webhook commit ──enqueue only───────►│
                                       ▼
┌──────────────────────────────────────────────────────────────┐
│ Mode FSM (per instance × category × optional agent)          │
│  1 Shadow  2 Copilot  3 Controlled Auto  4 Advanced Agent    │
│  Gates: management unlock + readiness metrics + emergency    │
└────────────────────────────┬─────────────────────────────────┘
                             │
         ┌───────────────────┼───────────────────┐
         ▼                   ▼                   ▼
   Shadow draft         Copilot panel       (Mode 3+) send
   (never outbound)     staff approve       gated + logged
         │                   │
         ▼                   ▼
   Wait for human outbound (same thread, reply window)
         │
         ▼
┌─────────────────────────────────────────────────────────────┐
│ Shadow compare                                              │
│  AI draft vs human reply → similarity / intent / gaps       │
│  → knowledge_gap | bad_reasoning | good_example | ignore    │
│  → never auto-approve; queue for Learning Centre            │
└────────────────────────────┬────────────────────────────────┘
                             │
         ┌───────────────────┼───────────────────┐
         ▼                   ▼                   ▼
  Global approved KB   Dealer memory       Prompt versions
  (no dealer prices)   (isolated)          + accuracy ledger
         │                   │
         └─────────┬─────────┘
                   ▼
         Reply compose context pack
         + LIVE tools: catalog rate/stock, Zoho payment/SO
         (never historical chat ₹ as quote)
```

### 3.1 Shadow compare loop (Mode 1+)

1. Inbound DM opens reply-needed (existing rules) → enqueue `shadow_draft` job (async, not on webhook return).
2. Compose using same core as `suggest_reply` but:
   - purpose `shadow_learning`
   - status on suggestion: `shadow` (new status) or separate `wa_shadow_pairs` row
   - never surfaces to customer; optional faint “shadow only” in Learning Centre
3. When staff sends outbound that closes the window (`complete_staff_reply` / reply-event), attach human message as `human_reply_message_id`.
4. Compare job (rules + optional small Gemini judge):
   - intent match, factual claims, missing product steps, tone, language
   - classify outcome; open gap or propose example
5. Metrics: agreement rate, gap rate, handover rate, ₹/pair.

### 3.2 RAG memory isolation

| Layer | Scope | Contents allowed |
| --- | --- | --- |
| Global FAQ (`kb_articles`) | org | Process, product facts without dealer-specific ₹, stamping/OV how-to, hours, warranty policy |
| Approved examples | org, anonymised | Q/A pairs from shadow “good_example” after review; strip phones/₹ or replace with placeholders |
| Dealer memory | `dealer_id` (+ jid if unlinked) | Preferences, open issues, promised follow-ups, **never** copied cross-dealer |
| Live tools | request-time | Catalog stock/rate, Zoho open balance / last SO for **linked** dealer only |

Retrieval order for compose: live tools (if commercial intent) → dealer memory → approved examples → global FAQ → recent thread. Drop any retrieved chunk with `dealer_id != current`.

### 3.3 Mode FSM

| Mode | Outbound | Staff UX | Unlock prerequisite |
| --- | --- | --- | --- |
| **1 Shadow Learning** | Never | Invisible or Learning Centre only | Management enables `shadow_enabled` |
| **2 Copilot** | Never auto | Suggest panel + approve/edit/send manually | Shadow readiness ≥ threshold + unlock |
| **3 Controlled Auto** | Low-risk categories only | Log + instant human takeover | Copilot accuracy + category allowlist + unlock + `auto_send_enabled` |
| **4 Advanced Agent** | Authorised workflows only (tools) | Explicit workflow ids | Mode 3 stable + per-workflow auth |

Default for all instances after ship: **Mode 0 / off** or Mode 1 if management turns shadow on. Map legacy `suggest` UI → Mode 2 behaviour once unlocked; keep `auto_send_enabled=false`.

Per-agent / per-category: store allowlists on config JSON (`category_modes`, `agent_overrides`) — e.g. Care instance Mode 2 for `troubleshooting`, Mode 1 only for `sales` until sales readiness passes.

---

## 4. Data model (additive)

Build on `wa_ai_suggestions`, `kb_articles`, `ai_corrections`, `ai_usage_logs`, `ai_jobs`, `ai_job_checkpoints`. No drops.

### 4.1 Extend existing

| Table | Change |
| --- | --- |
| `wa_auto_reply_configs` | Add `operating_mode` (`off\|shadow\|copilot\|controlled_auto\|advanced`), `management_unlock_json`, `category_policy_json`, `shadow_sample_rate`, keep `auto_send_enabled` default false |
| `wa_ai_suggestions` | Allow status `shadow`; add `prompt_version`, `context_pack_json` (kb ids, memory ids, tool snapshots), `compare_status` |
| `kb_articles` | Add `contains_price` bool, `scope` (`global\|example`), `verified_by_user_id`, reject auto-learn if price without live-tool tag |
| `ai_settings` | `shadow_enabled`, `shadow_daily_budget_inr`, `copilot_unlocked`, `controlled_auto_unlocked`, `advanced_unlocked` (all false) |
| `ai_prompt_versions` | Shared with classify plan if built; keys: `reply_compose`, `shadow_compare`, `kb_extract_v2` |

### 4.2 New: `wa_shadow_pairs`

| Column | Notes |
| --- | --- |
| id, public_id, organization_id | |
| conversation_id, whatsapp_instance_id, dealer_id nullable | |
| trigger_message_id, shadow_suggestion_id, human_message_id | |
| ai_reply_text, human_reply_text | denormalised for eval |
| category / intent | from compose |
| outcome | `good_example \| knowledge_gap \| bad_ai_reasoning \| bad_human_candidate \| ignore \| pending` |
| similarity_score, judge_json | |
| prompt_version, model | |
| promoted_example_id / gap_id | nullable FKs |
| cost_inr | compose + compare |
| created_at, compared_at | |

Unique: one open pending pair per `(conversation_id, trigger_message_id)`.

### 4.3 New: `wa_knowledge_gaps`

| Column | Notes |
| --- | --- |
| id, public_id | |
| shadow_pair_id, conversation_id | |
| gap_type | `missing_product_fact \| missing_process \| wrong_tone \| language \| live_data_needed \| other` |
| description, evidence_json | |
| status | `open \| in_review \| resolved_kb \| dismissed` |
| resolved_article_id | |
| created_at | |

### 4.4 New: `wa_dealer_memory`

| Column | Notes |
| --- | --- |
| id | |
| dealer_id **required for cross-session**; optional `conversation_id` / `customer_jid` for unlinked | |
| kind | `preference \| open_issue \| promise \| note` |
| content | redacted; **no absolute price quotes** (or `is_commercial` + never retrieve for other dealers) |
| source | `shadow \| staff \| system` |
| status | `active \| superseded \| rejected` |
| created_by, created_at, expires_at | |

**Hard rule:** retrieval SQL always `WHERE dealer_id = :current_dealer_id`. Unlinked jid memory never joins another dealer’s rows.

### 4.5 New: `wa_learning_examples` (approved few-shot)

Anonymised Q→A after dual review (auto proposal + human approve). Separate from FAQ articles so process FAQs stay clean. Optional: store as `kb_articles` with `scope=example` instead of new table — prefer **one table with scope** to avoid dual UIs.

### 4.6 Cost ledger

Reuse `ai_usage_logs` with purposes:

- `shadow_compose`, `shadow_compare`, `shadow_backfill`, `knowledge_learning` (existing)

Learning Centre aggregates these + existing `auto_reply` / outline. Optional thin `ai_cost_daily` rollup later — not required for v1.

### 4.7 What not to add

- Embedding / pgvector (revisit only if FTS plateaus).
- Fine-tune job tables.
- Merging `ai_stage_kb` into reply RAG.
- Storing raw dealer price lists scraped from chats.

---

## 5. Phased rollout with mode gates

**Global rule:** each mode unlock is a management action recorded in audit (`updated_by_user_id` + timestamp + reason). Engineering ships code behind flags defaulting off.

### Phase S0 — Foundations (no customer-visible AI change)

| Work | Notes |
| --- | --- |
| Tables + migrate additive | shadow pairs, gaps, dealer memory, config columns |
| Shared compose context builder | Extract from `reply_engine` without behaviour change |
| Live tool stubs (read-only) | `get_product_stock_rate(sku)`, `get_dealer_payment_snapshot(dealer_id)` — wired but unused in production compose until S2 |
| Purpose + budget slice for shadow | Separate daily cap; default ₹0 or tiny until enabled |
| Feature flags all false | |
| Tests for isolation + can_auto_send still false | |

**Exit:** migrations green; suggest-reply behaviour unchanged; auto-send still impossible.

### Phase S1 — Shadow Learning only (Mode 1)

| Work | Notes |
| --- | --- |
| Enqueue shadow on inbound DM when `shadow_enabled` | Skip groups, blasts, acks, opt-out, takeover |
| Persist shadow suggestion / pair | Never show in customer WhatsApp |
| Compare on staff reply | Gap + good_example queues |
| Learning Centre v1 | Readiness, gaps, costs, pair browser |
| Historical **sample** backfill dry-run | See §6 — no apply without confirm |

**Gate to S2:** department readiness (§7) + management unlock `copilot_unlocked`.

### Phase S2 — Copilot (Mode 2)

| Work | Notes |
| --- | --- |
| Reuse shadow compose for inbox Suggest / auto-refresh pending draft | Staff must still send |
| Corrections → optional “promote example” | |
| Per-category mode (shadow vs copilot) | Sales may stay shadow while Care troubleshooting goes copilot |
| Accuracy dashboard vs golden set | |

**Still no auto-send.** `can_auto_send` remains false for all.

### Phase S3 — Controlled Auto (Mode 3) — management unlock required

| Work | Notes |
| --- | --- |
| Implement send path **only now** | Call existing WhatsApp send after `can_auto_send` + category allowlist + confidence high + approved KB or rule + no commercial invention without live tool success |
| Allowlist categories (proposal) | `greeting`, `working_hours`, `company_info` only at first — **scangle must confirm** |
| Cap + emergency stop + instant takeover | |
| Page staff + log every auto_sent | |

**Do not ship S3 code that can send until unlock flag exists and defaults false.**

### Phase S4 — Advanced Agent (Mode 4)

| Work | Notes |
| --- | --- |
| Authorised workflows only | e.g. “send approved catalogue PDF”, “create work item” — each workflow id gated |
| Tool loop subset of `ai/tools.py` | No free WhatsApp blast; no audience mutate |
| Human escalation on any ambiguity | |

### Parallel (any phase after S0)

- Harden KB extract verifier (price strip, contradiction).
- Align with classify Learning Plan Paid decision (reply handover rules already treat payment as human-required).

---

## 6. Historical backfill plan

### 6.1 Scope

| Include | Exclude |
| --- | --- |
| Evolution staff DMs + Care/Meta DMs | Groups, newsletter, status, blast/Dear Customer |
| Threads with ≥1 inbound + ≥1 staff outbound | Empty / media-only without transcript |
| Last N months (default proposal: **6 months**, confirm) | Full lifetime outline regenerate |
| Existing Sarvam `transcript` where present | **Re-STT / re-translate historical audio** |

Two backfill products (separate checkpoints):

1. **KB extract backfill** — improve `learning.py` over sample of high-signal threads (sales + stamping + service).
2. **Shadow retrospective** — for sampled pairs (last inbound before a staff reply), generate what AI *would* have said and compare (metrics only); do not spam `wa_ai_suggestions` into inbox.

### 6.2 Sampling (required before full run)

| Pass | Sample | Goal |
| --- | --- | --- |
| Dry A | 50 Care + 50 Evolution threads | Prompt quality, price-leak scan |
| Dry B | 200 stratified by `service_category` | Gap typology calibration |
| Cost preview | Token estimate × rates → ₹ | Management confirm |
| Apply | Checkpointed batches of 25–50 | Pause/resume via `ai_job_checkpoints` |

### 6.3 Cost estimate (Gemini Flash-Lite @ config rates)

Assumptions (reconcile with live counts before run; upgrade plan cited ~15k threads for full outline — **do not** outline-all):

| Pass | Volume assumption | Tokens (rough) | ₹ estimate |
| --- | --- | --- | --- |
| Dry A 100 threads × extract (~2k in + 400 out) | 100 | 0.2M in + 0.04M out | **~₹3–5** |
| Dry B 200 shadow compose+compare (~2.5k in + 500 out ×2) | 200 | ~1.0M in + 0.2M out | **~₹15–25** |
| Sampled KB extract 1,000 threads | 1,000 | ~2M in + 0.4M out | **~₹50–70** |
| Aggressive 5,000 thread extract | 5,000 | ~10M in + 2M out | **~₹250–350** |
| Full 15,000 compose+compare | 15,000 | huge | **₹800+ — reject** |
| Sarvam historical re-STT | any | — | **₹ reject — do not** |

**Recommendation:** cap Phase S1 backfill at Dry A+B + optional ≤1,000 extract with management confirm token. Raise daily budget temporarily only for backfill purpose; never steal intel/agent caps silently.

### 6.4 Checkpoints & rollback

- Reuse `ai_job_checkpoints` streams: `shadow_backfill_kb`, `shadow_backfill_compare`.
- Fields: cursor, rows_done, spent_inr, paused, error.
- Rollback: delete `suggested` KB rows with `source=shadow_backfill` and `status=suggested`; never bulk-approve. Shadow pairs marked `backfill=true` can be truncated by version.
- Pause on budget exceed or error rate &gt; threshold.

### 6.5 Quality gates during backfill

- Drop extracts with absolute ₹ / “special rate for you”.
- Drop OTP/password/payment confirmation content.
- Flag Malayalam/English mix for human review if confidence low.
- Dedupe near-identical questions against approved KB.

---

## 7. Readiness tests / department readiness metrics

Departments (align with `service_category` + Care vs Evolution instances): **Sales**, **Care/Service**, **Stamping/OV**, **Cloud/Software**, **Internal/other**.

### 7.1 Per-department scorecard (Learning Centre)

| Metric | Shadow-ready | Copilot-ready | Auto-ready (Mode 3) |
| --- | --- | --- | --- |
| Shadow pairs compared (n) | ≥100 | ≥300 | ≥500 |
| AI↔human agreement (intent) | tracked | ≥70% | ≥85% on allowlisted cats |
| Knowledge gaps open / week | trending down | &lt;X | &lt;Y |
| Bad-human-candidate rate | flagged | &lt;10% of gaps | N/A (don't learn them) |
| Approved KB coverage for top intents | baseline | ≥N articles | allowlist cats covered |
| Price-leak test | **100% pass** | pass | pass |
| Live-tool success on commercial probes | optional | ≥95% | 100% for any auto commercial (default: commercial **not** auto) |
| Staff correction rate on copilot drafts | — | monitored | &lt;15% edit-before-send |
| Auto-send incidents / emergency stops | — | — | 0 critical in soak |

### 7.2 Automated readiness tests (CI + nightly)

| Test | Asserts |
| --- | --- |
| `test_shadow_never_sets_auto_sent` | compose path status ≠ auto_sent |
| `test_can_auto_send_false_without_unlock` | Mode 1–2 → False |
| `test_dealer_memory_isolation` | dealer A memory not in B context pack |
| `test_kb_price_strip` | extract with ₹ → contains_price / rejected from global retrieve |
| `test_live_rate_not_from_chat` | compose with price intent without tool → handover |
| `test_stage_kb_not_in_reply_rag` | |
| `test_blast_skipped` | |
| `test_ack_skipped` | reuse reply_needed helpers |
| Golden conversations (§9) | |

Management unlock API refuses Mode 2/3/4 if scorecard below threshold (overridable with typed reason for emergencies — logged).

---

## 8. AI Learning Centre UI (sections only)

New Management page (e.g. `/settings/ai-learning` or sidebar under Admin). **Wireframe sections — no visual design in this plan:**

1. **Mode & unlocks** — org flags; per-instance operating mode; emergency stop; unlock audit log.  
2. **Department readiness** — scorecards for Sales / Care / Stamping / Cloud; gate status.  
3. **Learning progress** — pairs/day, agreement trend, KB suggested→approved funnel.  
4. **Corrections** — recent `ai_corrections` + suggestion edits; promote/dismiss.  
5. **Knowledge gaps** — open gaps with deep link to thread; assign → create KB draft.  
6. **Approved knowledge** — shortcut into existing Knowledge Base review (do not fork).  
7. **Dealer memory browser** — search by dealer; show isolation warning; revoke entries.  
8. **Prompt versions** — active `reply_compose` / `shadow_compare` versions; diff checksum.  
9. **AI usage costs** — ₹ by purpose (`shadow_*`, `auto_reply`, `knowledge_learning`, outline); backfill spend vs preview.  
10. **Backfill control** — dry-run estimate, start/pause, checkpoint status (admin only).

Inbox changes stay minimal until Mode 2: keep `AiReplyAssistant`; do not clutter unreplied list with shadow noise.

---

## 9. Testing plan + golden conversations

### 9.1 Unit / integration

- Context pack builder isolation.
- Mode FSM transitions + illegal jumps.
- Compare classifier: good_example vs gap vs bad_human_candidate.
- Budget reserve for `shadow_compose`.
- Migration additive on clean DB.

### 9.2 Golden conversations (fixtures under `backend/tests/fixtures/shadow/`)

| ID | Scenario | Expect |
| --- | --- | --- |
| `greet_ml` | Malayalam hello | High-confidence greeting; Mode 3 candidate later |
| `hours_en` | Office hours | Rule/KB; no Gemini required |
| `price_dealer_a` | Dealer A asks rate for SKU | Handover **or** live catalog tool; never quote chat history ₹ |
| `price_leak` | Memory has dealer B special rate; query from dealer A | Must not appear in context |
| `stamping_ov` | OV serial list Care thread | Process KB / handover; no invented certificate claims |
| `payment_done` | “payment done” + screenshot | Human-required; no auto confirm |
| `bad_staff_rate` | Staff quotes random ₹ without catalog | Compare marks `bad_human_candidate`; not promoted |
| `warranty_good` | Clear warranty answer matching approved KB | `good_example` path |
| `otp_trap` | Customer asks OTP/password | Handover; empty reply |
| `blast_skip` | Dear Customer blast | No shadow job |
| `care_ack` | Short “ok” | No shadow (ack filter) |
| `negotiation` | “best price” | Handover |
| `stock_ask` | “available?” | Live stock tool or handover — never “yes” from memory |
| `charan_care_window` | Care unreplied semantics unchanged | Regression: shadow enqueue must not clear `awaiting_staff_reply` |

### 9.3 Manual soak (staging / flag on one instance)

- Enable Mode 1 on a single Care instance for 5 business days.
- Review 20 gaps in Learning Centre.
- Confirm zero outbound AI messages (WhatsApp audit / `auto_sent` count = 0).

---

## 10. What NOT to do

- Do **not** implement or enable Continuous / Auto-reply modes from this doc without phase gates.
- Do **not** fine-tune Gemini or host local LLMs.
- Do **not** add Redis/Celery/vector DB for v1.
- Do **not** re-run Sarvam on historical media for backfill.
- Do **not** full-history outline or shadow on all threads.
- Do **not** put Zoho lifetime money on Learning Centre as “training labels.”
- Do **not** merge `ai_stage_kb` into reply RAG.
- Do **not** store dealer-negotiated prices in global FAQ.
- Do **not** learn from blast templates or bad staff replies.
- Do **not** wire webhook → auto-send in S0–S2.
- Do **not** Free443, campaign pause, Evolution/Mailcow/ADMS restarts for this work.
- Do **not** interrupt unreplied / office-hours / reply-stats semantics.
- Do **not** replace existing Suggest Reply / KB approval — extend them.
- Do **not** treat catalog `rate` as dealer-specific negotiated price without a product decision (§11).

---

## 11. Open decisions for scangle

| # | Decision | Options / recommendation |
| --- | --- | --- |
| **D1** | **Paid / pricing source of truth for replies** | **(a)** Catalog `rate` + stock only as “list reference”, always hand over for dealer quote; **(b)** Zoho last invoice line as “previously charged” context, never as current quote; **(c)** new dealer price-list sync (Zoho price books / manual) before any AI states a ₹. **Recommend (a)+(c later); never (chat history).** |
| **D2** | Which categories may ever reach **Mode 3 Controlled Auto**? | Proposal: `greeting`, `working_hours`, `company_info` only. Explicitly **exclude** sales price, discount, payment, order confirm, stamping certificates, OTP, complaints. |
| **D3** | Shadow on **all** reply-needed DMs or sample (e.g. 20%) until budget proven? | Recommend sample 25–50% on Care first week. |
| **D4** | Backfill window | 3 vs 6 vs 12 months; recommend **6 months** sample ≤1,000 threads after Dry A/B. |
| **D5** | Unlinked chats (no `dealer_id`) | Memory by jid only vs no dealer memory until linked. Recommend **jid-scoped memory, never promote to global**. |
| **D6** | Copilot auto-refresh drafts in inbox vs click-only | Recommend click-only until agreement ≥70%, then optional auto-draft for unreplied. |
| **D7** | Shared `ai_prompt_versions` with classify Learning Plan vs reply-only table | Recommend **one shared table**, different keys. |
| **D8** | Who can unlock modes? | `management` + `super_admin` only, or also sales managers per department? |
| **D9** | Relationship to `AI_INTELLIGENCE_LEARNING_PLAN` Paid P0–P3 | Reply track treats payment as human-required regardless; still confirm tile Paid pick so staff training examples stay consistent. |
| **D10** | Mode 4 first workflows | Pick 1–2 only (e.g. send approved brochure PDF; create internal work item). Defer autonomous Zoho writes forever in this plan. |

**Blockers before any implementation:** D1 and D2. Soft blockers before backfill apply: D3, D4.

---

## 12. Cross-links & preservation checklist

Preserve: Evolution/Care ingest, unreplied, office hours, reply analytics, Sarvam, Gemini outline, FAQ KB approval, `/work` agent, Zoho sync, campaigns, Intelligence Observe, Move→stage KB.

This plan stops at Assist for outbound. Execute = Mode 3+ only after unlock.

When implementation starts, update `docs/AI_DEVELOPMENT_STATUS.md` with phase checkboxes — not before.

---

## 13. Suggested implementation order (after approval)

1. Confirm D1, D2 (and ideally D3–D4).  
2. Phase S0 schema + flags + tests (no behaviour change).  
3. Phase S1 shadow enqueue + compare + Learning Centre v1 on one instance.  
4. Dry-run backfill cost preview → management confirm → sampled apply.  
5. Readiness review → unlock Mode 2 Copilot.  
6. **Stop.** Controlled Auto / Advanced only after separate written unlock.
