# YesWeigh CRM — Advanced AI Learning & Conversation Intelligence Plan

**Status:** plan only. Do **not** implement from this document until scangle confirms the Paid decision and phase order.  
**Repo:** `/Users/mac/Projects/yesweigh-crm`  
**Last audited:** 2026-11-25 (local tree + live correction history).  
**Deploy:** none. No Free443, no campaign interrupt, no VPS state change from this plan.

---

## Document choice (why a new file)

| Doc | Role after this plan |
| --- | --- |
| `docs/AI_INTELLIGENCE_UPGRADE_PLAN.md` | Keep as the **Observe → Recommend → Assist** Command Centre / findings / commitments architecture (Nov 24). Still authoritative for SQL findings, unreplied isolation, budgets, and Execute rejections. |
| `docs/AI_INTELLIGENCE_PHASE1.md` | Keep as the **shipped Observe changelog** (findings board, Move-to learning sketch, WA-stage tiles). |
| **`docs/AI_INTELLIGENCE_LEARNING_PLAN.md` (this file)** | New track: **classification quality, continuous Move learning, self-review, summaries, historical reclassification**. Builds on what Phase 1 already shipped for WA stages — does not reinvent tiles, Move UI, or blast exclusion. |

Updating the upgrade plan would mix “deferred Phase B/C narration/Ask” with “learning-first ROI on wrong tiles,” which is the actual live pain. Cross-links below; do not duplicate the full findings schema here.

---

## Executive decision (this track)

1. **Treat Intelligence stages as a WA conversation classifier with human overrides**, not a Zoho accounting board (already live after Nov 25 corrections).
2. **Invest first in Move → retrieval learning + confidence/review**, not fine-tuning or keyword spam.
3. **Resolve Paid with an explicit product decision** (options in §2) before any backfill that writes stages.
4. **Gemini is a secondary, structured, budgeted judge** for ambiguous threads and summaries — rules/SQL remain the default hot path.
5. **Historical reclassification is dry-run → checkpoint → optional apply**, never a deploy-time scan.

Phases stop at **Assist** for this track too. No auto outbound. No campaign audience from sentiment. No Fine-tune-first.

---

## 1. Current-state audit

### 1.1 What already exists (do not reinvent)

| Capability | Evidence | Notes |
| --- | --- | --- |
| Intelligence page + fit-to-screen paging | `frontend/src/pages/Intelligence.tsx` | Viewport-fitted page size; tiles for Enquiry / SO / Waiting / Paid / Stamping / Service / Commitments. |
| Paid ₹ / Stamping shield icons | Live UI fix (Nov 25) | Shipped; leave icons alone unless product changes labels. |
| WA-only stage lists | `queries.funnel_snapshot` docstring + `wa_stages.py` | Explicitly **no Zoho lifetime dumps** on tiles after Zoho “22k Paid” failure. |
| Move-to + Closed | `stage_move.py`, `ChatPreviewModal.tsx`, `POST /api/intel/stage-move` | Writes `ai_thread_insights.stage_override`, `ai_stage_moves`, learns into `ai_stage_kb`. |
| Blast / strike exclusion | `wa_stages.blast_only_conv_ids_sql`, `dedupe.is_blast_summary` | Container-strike / Dear Customer out of all stages; Closed KB exclude weight ≥ 2. |
| Enquiry rules + Gemini refine | `enquiry.py` (`RULES_VERSION = 10`), `enquiry_gemini.py` | Rules first; Gemini only for ambiguous unanswered prices, intel budget, worker/async. |
| Order cycle (SO → Waiting → Paid) | `order_cycle.py` | Mutually exclusive chat cycle; Zoho only corroborates **inside SO date window**. |
| Payment-screenshot Paid path | `wa_stages.payment_screenshot_conv_ids_sql` | Inbound **image** + UPI/PhonePe-style caption. |
| Stamping / OV heuristics | `whatsapp_service_meta.stamping_signal`, `wa_stages.stamping_msg_conv_ids_sql` | Serial+OV (Sandhya), sealed paper, Certificate of Verification; price-line “including stamping” excluded. |
| Deduping | `intel/dedupe.py` | One row per conv/phone/jid; Royal-style duplicates + blast drop. |
| Stage KB learning | `stage_kb.py` | Phrase upsert + polarity; funnel/enquiry/stamping consult KB. |
| Observe findings / commitments / health | Phase 1 tables + `jobs.py` worker | Flag `intel_enabled`; enqueue after webhook commit; concurrency 1. |
| Outline summaries | `whatsapp_outline.py` + `AiSummaryCard` | Separate budget purpose `whatsapp_outline`; fingerprint skip. |
| Sarvam STT | `sarvam_transcribe.py` | Intel **reads** `transcript`; must not re-queue historical audio. |
| AI Settings / usage | `GeminiSettings.tsx`, `ai_settings`, `budget.py` | Agent/outline budgets; intel caps exist; **no Learning Dashboard yet**. |
| FAQ KB (separate) | `kb_articles` + learning router | Suggested → approve. Do not merge with `ai_stage_kb`. |

One-shot live corrections script (ops, not product UI): `backend/scripts/intel_stage_fixes.py` — Marvel / Admali → Stamping, Royal commitment dedupe helpers.

### 1.2 Classification entrypoints (map)

```
Webhook (Evolution / Care)
  └─ commit message → schedule Sarvam / outline (existing)
  └─ intel.jobs.enqueue_thread (DM only, flag on) → rule extract commitments / secondary intents
        (does NOT recompute funnel tiles synchronously)

GET /api/intel/funnel  (and related stage lists)
  ├─ load_stage_overrides (stage_override wins)
  ├─ closed_or_blast_exclude_ids (+ KB exclude)
  ├─ select_sales_enquiries (enquiry.py cache + RULES_VERSION)
  ├─ load_order_cycles (order_cycle.py) + KB boost for SO/waiting/paid
  ├─ payment_screenshot / stamping SQL (wa_stages) for paid/stamping membership
  └─ dedupe_intel_rows

POST /api/intel/stage-move
  └─ override + AiStageMove + learn_from_move → ai_stage_kb

Heuristic service_category chip
  └─ whatsapp_service_meta (sales|service|cloud|stamping) — inbox chip, not tile sole source
```

**Keyword vs LLM ratio (today):** ~90%+ rules/SQL/regex for stage membership; Gemini used for (a) outline JSON when fingerprint changes, (b) enquiry ambiguous price refine under intel budget, (c) `/work` tools (out of tile path). Stage Move learning is **phrase KB**, not embeddings or fine-tune.

### 1.3 Gaps vs the Advanced Learning brief

| Brief ask | Today | Gap |
| --- | --- | --- |
| Deep contextual reasoning (not keyword-only) | Heavy regex + outline status + KB phrases | No structured “evidence bundle → judge” for hard cases; serial lists / Malayalam need context. |
| Continuous learning from Move | Phrase weight upsert | No retrieval of **full example threads**, no contradiction detection, no confidence decay, no admin curation UI. |
| Better summaries | Outline + enquiry summary cache | No stage-aware summary contract; voice-heavy threads still thin. |
| AI self-review + confidence + review queue | Enquiry has `confidence`; commitments hide `low` | Stages have **no** confidence / review queue. Overrides are silent force-ins. |
| Historical reclassification dry-run / cost / rollback | `ai_job_checkpoints` + order/enquiry backfill scripts | No productized dry-run with accuracy metrics vs Move gold; no rollback of applied labels. |
| AI Settings & Learning Dashboard | Gemini usage page only | No Move stats, KB phrase browser, prompt version, golden-set score, review queue. |
| Multi-stage support | Funnel forces mutual exclusivity for SO/waiting/paid; overrides one stage | Secondary intents exist on insights; tiles still one primary. Brief may want “stamping + waiting” visibility — needs product call. |
| Workflow intelligence | Order cycle is the start | Commitments still rule/Gemini-proposal; not tied to stage machine. |

### 1.4 Known failure modes (cite code / live history)

| Failure | Evidence | Lesson for this plan |
| --- | --- | --- |
| Zoho lifetime “Paid 22k” on tiles | Live correction Nov 25; fixed by WA-only funnel | Never put countable Zoho lifetime on stage tiles again. |
| `func.case(..., else_=…)` TypeError | Comment in `wa_stages.blast_only_conv_ids_sql` | Prefer `count().filter(...)`; regression test any new HAVING aggregates. |
| Postgres `~*` + Python `(?i)` | Python `re` uses `(?i)`; SQL paths correctly omit it and use `~*` | **Never** pass `(?i)` into `op("~*")`. New KB→SQL matchers must escape user phrases and use `ilike` / `~*` without PCRE flags. |
| Strike blast in Commitments / stages | Live + `dedupe` / blast SQL | Exclude before learn; Closed moves teach exclude. |
| Royal duplicate commitment rows | `intel_stage_fixes.ROYAL_PHONE` + `dedupe` | Golden fixture: one row per phone. |
| Admali / Marvel OV without saying “stamping” | `intel_stage_fixes.STAMPING_MOVES`, Sandhya tests | Serial+OV / ayachilla context — but **do not** treat every `Y#####` as stamping. |
| Meezan spare-parts SO | Live correction → `sales_order_msg` patterns | Freight / spare / “S” order language. |
| Promise-to-pay ≠ Paid | `test_promise_to_pay_stays_waiting` | Keep; aligns with new brief. |
| Paid definition divergence | See §2 | Must resolve before learning/backfill. |
| Enquiry Gemini on hot path | Designed worker-only; cache on insight | Keep; page loads must not call Gemini (`allow_sync=False`). |

### 1.5 Constraints (carry forward — hard)

- Preserve unreplied, office hours, Sarvam, Gemini outline, FAQ KB, Zoho sync, inbox.
- No Free443 changes; no campaign interrupt; no auto outbound from intel.
- Intel async off inbox critical path (enqueue insert only).
- Untrusted message content: redact, ignore instructions in customer text.
- No vector DB / Redis / Celery / NestJS cutover / Fine-tune-first.

---

## 2. Resolved product decisions (and open Paid choice)

### 2.1 Already resolved by live corrections + code

| Topic | Decision | Evidence |
| --- | --- | --- |
| Stage data source | WhatsApp AI-read chats only for tile lists | `queries.py` header; funnel after Nov 25 |
| Closed / blast | Exclude from all stages | `closed_or_blast_exclude_ids` |
| Stamping examples | OV serial lists, CoV PDF, sealed paper, Malayalam OV send | Sandhya tests; Admali/Marvel moves |
| Sales order examples | Spare parts / freight / SO PDF / Order Confirmed | Meezan + `order_cycle` |
| Move-to | Human override is ground truth for that thread + teaches KB | `stage_move.py` |
| Zoho on tiles | Not for Paid/Waiting counts | Live reject of 22k Paid |
| Promise to pay | Not Paid | `test_promise_to_pay_stays_waiting` |
| Renewal “please pay” | Not order cycle | `test_renewal_please_pay_is_not_an_order_stage` |

### 2.2 Paid definition — **conflict (must confirm)**

**A. Live user rule (Nov 25):**  
> Paid = dealer **made payment AND shared a payment screenshot** on WhatsApp.

Implemented as: inbound image + proof caption (`payment_screenshot_conv_ids_sql`). Funnel note: *“dealer paid and shared a payment screenshot”*.

**B. This Advanced Learning brief:**  
> Payment promise **or screenshot alone** must **not** establish Paid; need **authorised staff** and/or **verified financial records**.

**C. Code also has a third path** in `order_cycle.classify_order_cycle`: after SO PDF, inbound/outbound **text receipt** (`Paid using PhonePe…`, staff “payment received”) **or** Zoho settled **in the SO window** → Paid — **without requiring an image**.

| Option | Definition | Pros | Cons |
| --- | --- | --- | --- |
| **P0 (recommended default until scangle picks)** | **Paid = (screenshot proof) OR (staff explicit “payment received” on WA) OR (Zoho settled in SO window after SO PDF).** Promise / “will pay” / screenshot without payment language → not Paid. Screenshot alone with no payment cue → not Paid. | Matches operational CRM + Zoho corroboration already coded; stricter than text-only; keeps WA tile spirit. | Slightly looser than “screenshot always required”; brief’s “authorised staff” is approximated by outbound staff confirmation. |
| **P1 (strict screenshot)** | Paid **only** if inbound payment **image + proof caption** (current `wa_stages` path). Drop text-only and Zoho-window Paid from **tiles**. | Exact Nov 25 wording. | Misses dealers who UPI-text without image; diverges from `order_cycle` tests; more Move corrections. |
| **P2 (finance-verified)** | Paid only if Zoho settled in SO window **and** staff WA confirmation (or screenshot). Screenshot alone never enough. | Closest to brief’s “verified financial records.” | Needs reliable dealer link; silent misses when Zoho not linked; more “Waiting” noise. |
| **P3 (review queue)** | Auto signals (screenshot / text / Zoho) → `proposed_paid` with confidence; **Paid tile only shows human-confirmed + high-confidence**. | Safest; teaches learning. | Extra UI; temporary undercount. |

**Recommendation:** Implement **P0** for auto classification, and add **P3 review** for medium/low confidence (text-only receipt without image, or Zoho-only without WA confirmation). Ask scangle to pick P0–P3 in one line before Phase B code.

### 2.3 Stamping

- **Include:** serial lists + OV / “ov please send” / ayachilla in OV context / CoV / sealed paper / yesgatc download / camp inspector (Admali, Marvel, Sandhya).
- **Exclude:** bare serial numbers with no OV/metrology context; “stamping plate”; price lines “including stamping and GST”; every `\bov\b` in unrelated English.
- **Learning risk:** Move phrases like lone `"ov"` or `"yz01414"` can over-generalize — Phase A must add **min token specificity + negative examples + weight gates** (see §5).

### 2.4 Multi-stage

**Recommendation:** Keep **one primary tile membership** (current UX). Store `secondary_stages[]` on insight for Learning Dashboard / filters (“also stamping”). Do not show the same chat as two full primary tiles without an explicit product change.

### 2.5 Summaries

Stage-aware, evidence-cited summaries (quote message ids / media names). Prefer regenerating only when fingerprint or stage_override changes. Malayalam: keep body original; use transcript when present. Never invent ₹ amounts not in evidence.

---

## 3. Target architecture

```
                    ┌─────────────────────────────┐
                    │  Learning Dashboard (UI)    │
                    │  moves · KB · review · cost │
                    └─────────────┬───────────────┘
                                  │ JWT admin/mgmt
┌──────────────┐   enqueue only   │
│ WA webhooks  ├──────────────────┼──► ai_jobs (SKIP LOCKED, conc=1)
└──────────────┘                  │
                                  ▼
┌─────────────────────────────────────────────────────────────┐
│ Classifier pipeline (async worker / on-demand funnel)       │
│  1. Hard excludes (blast, closed override, newsletter)      │
│  2. stage_override (human)                                  │
│  3. Deterministic detectors (SO PDF, screenshot, OV ctx)    │
│  4. Retrieval: similar ai_stage_moves + ai_stage_kb         │
│  5. Optional Gemini structured judge (budgeted, schema)     │
│  6. Self-review: confidence + contradiction vs Move gold    │
│  7. Persist: stage_prediction + evidence + prompt_version   │
└─────────────────────────────────────────────────────────────┘
          │                              │
          ▼                              ▼
   Intelligence tiles              Review queue
   (high confidence / override)    (medium/low)
```

### Design rules

- **Retrieval learning, not Fine-tune-first:** store Move examples; retrieve top-k similar snippets/phrases; feed as few-shot into structured Gemini **only when rules disagree or confidence &lt; threshold**.
- **Structured outputs:** JSON schema `{primary_stage, secondary_stages[], confidence, evidence_message_ids[], rationale_short, needs_review}`. Numbers guard for ₹.
- **Versioning:** `RULES_VERSION` (enquiry already), plus `STAGE_PROMPT_VERSION`, `CLASSIFIER_VERSION` on predictions; bump invalidates caches like enquiry_fingerprint.
- **Async:** webhook never awaits Gemini. Funnel may read cached predictions; miss → rules-only fallback (same as today).
- **Untrusted content:** customer text only inside delimited evidence blocks; system prompt forbids following user instructions.

---

## 4. Phased implementation

Prioritize **high ROI on real pain:** wrong tiles, bad/thin summaries, Move learning that over/under-generalizes.

### Phase A — Harden learning loop (1–1.5 week, medium risk, high ROI)

**Goal:** Make Move → KB safe and measurable without new Gemini spend.

| Work | Depends | Effort | Risk |
| --- | --- | --- | --- |
| Paid decision wired once in shared `paid_evidence()` helper used by `wa_stages` + `order_cycle` | scangle Paid pick | S | Med (behavior change) |
| KB phrase quality: reject serial-only / stop-word / too-short; require OV context for stamping serials; raise exclude weight rules | stage_kb | S | Low |
| Persist `ai_stage_predictions` optional cache (rules-only first) with confidence | models/migrate | M | Low |
| Learning metrics API: moves/day, top phrases, override rate, KB size | router | S | Low |
| Golden fixture pack from live examples (see §7) | tests | S | Low |
| Fix any remaining `(?i)`-in-SQL / `func.case` footguns with tests | — | S | Low |

**Out of Phase A:** Gemini judge, backfill apply, new nav page (can be a thin Settings tab).

### Phase B — Contextual judge + summaries (1.5–2 weeks, medium–high ROI)

| Work | Depends | Effort | Risk |
| --- | --- | --- | --- |
| Retrieval pack from `ai_stage_moves` (same to_stage / similar phrases) | Phase A KB hygiene | M | Med |
| Gemini structured stage judge for ambiguous / low-confidence only | intel budget | M | Med cost |
| Self-review: if judge ≠ override or ≠ deterministic detector → `needs_review` | predictions | S | Low |
| Stage-aware summary refresh (fingerprint + stage) | outline or intel narrate | M | Med |
| Review queue API + minimal UI on Intelligence or Settings | Phase A metrics | M | Low |
| Voice: ensure STT transcript included in evidence window (no re-STT) | Sarvam read-only | S | Low |

### Phase C — Historical reclassification (ops-heavy, do after A/B accuracy)

| Work | Depends | Effort | Risk |
| --- | --- | --- | --- |
| Dry-run backfill: score last N days vs Move gold + held-out fixtures | checkpoints | M | Cost |
| Cost preview (tokens ₹) + confirm token | budget | S | Low |
| Apply with `CLASSIFIER_VERSION` stamp; write prediction rows only by default | dry-run | M | Med |
| Optional apply to `stage_override` **never automatic** — only explicit admin “promote prediction” | — | S | High if misused |
| Rollback: revert predictions by version; overrides untouched unless promoted | versioning | S | Low |

### Phase D — Learning Dashboard polish (parallelizable with C)

| Work | Depends | Effort | Risk |
| --- | --- | --- | --- |
| Full Learning Dashboard page | APIs from A/B | M | Low |
| Prompt/rules version browser; phrase approve/reject | KB rows | M | Low |
| Accuracy charts (Move agreement, tile precision on gold set) | dry-run metrics | S | Low |

### Deferred / not this track

- Upgrade-plan Phase B narration of SQL findings / Intel Ask (still valid, separate).
- Assist Create-task / dismiss finding (upgrade plan Phase C).
- Fine-tuning Gemini / local LoRA.
- Embedding vector DB (revisit only if phrase KB plateaus).
- Execute / auto-send.

---

## 5. Data model (additive)

Build on existing `ai_stage_moves`, `ai_stage_kb`, `ai_thread_insights`, `ai_jobs`, `ai_job_checkpoints`. Additive only.

### 5.1 Extend / use existing

| Table | Change |
| --- | --- |
| `ai_stage_kb` | Add `curated` bool (default false), `disabled` bool, `specificity` smallint (optional). Admin can disable poison phrases (e.g. bare `ov`). |
| `ai_stage_moves` | Add `used_in_eval` bool; keep snippet/signals. Treat as gold labels. |
| `ai_thread_insights` | Add `secondary_stages` jsonb/text default `[]`; `predicted_stage` / `prediction_confidence` / `prediction_version` (or prefer separate table). |
| `ai_settings` | `stage_learning_enabled`, `stage_judge_enabled`, `stage_review_required_below` (`medium`), optional judge daily cap slice of intel budget. |

### 5.2 New: `ai_stage_predictions`

| Column | Notes |
| --- | --- |
| id, conversation_id (unique per version or upsert current) | |
| primary_stage, secondary_stages jsonb | |
| confidence `low\|medium\|high` | |
| needs_review bool | |
| evidence_json | message ids, media_names, detector hits, retrieved move ids |
| source `rules\|kb\|judge\|hybrid` | |
| classifier_version, prompt_version, rules_version | |
| status `active\|superseded\|rejected\|promoted` | |
| created_at | |

Unique practical approach: one **active** row per conversation; history via superseded copies or `ai_stage_prediction_events` if audit needed.

### 5.3 New: `ai_stage_review_queue`

| Column | Notes |
| --- | --- |
| id, conversation_id, prediction_id | |
| reason | `low_confidence`, `judge_vs_rules`, `judge_vs_override`, `stamping_serial_weak`, `paid_ambiguous` |
| status `open\|resolved\|dismissed` | |
| resolved_by, resolved_stage, resolved_at | |
| created_at | |

Resolving can call existing `move_conversation_stage` (trains KB).

### 5.4 New: `ai_prompt_versions` (lightweight)

| Column | Notes |
| --- | --- |
| key (`stage_judge`, `stage_summary`, `enquiry_refine`) | |
| version string | |
| body hash / path | store prompt text in repo; DB holds version + checksum |
| active bool | |

### 5.5 What not to add

- No second FAQ article merge into stage KB.
- No sentiment→campaign table.
- No drop of `stage_override` (human remains sovereign).

---

## 6. API / UI

### 6.1 APIs (all gated by `intel_enabled` + management/admin)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/intel/learning/summary` | Moves count, KB size, open reviews, override rate, spend `intel_%` |
| GET | `/api/intel/learning/kb` | List phrases; filter stage/polarity/disabled |
| PATCH | `/api/intel/learning/kb/{id}` | Disable / curate / weight clamp |
| GET | `/api/intel/learning/moves` | Recent Moves with snippet |
| GET | `/api/intel/review-queue` | Open ambiguous stages |
| POST | `/api/intel/review-queue/{id}/resolve` | Wraps stage-move |
| POST | `/api/intel/reclassify/preview` | Dry-run counts + ₹ estimate + confusion vs gold |
| POST | `/api/intel/reclassify/start` | Checkpointed apply of **predictions only** |
| POST | `/api/intel/reclassify/pause` | Existing checkpoint pattern |
| GET | `/api/intel/reclassify/status` | Cursor, spent, errors |

Keep existing: `stage-move`, `stage-options`, funnel, findings, jobs rebuild.

### 6.2 UI surfaces

1. **Intelligence** — badge on rows `needs_review`; optional secondary stage chips (read-only). Move UI unchanged.
2. **Learning Dashboard** (new under Overview or Admin) — summary cards, KB browser, review queue, reclassify dry-run panel.
3. **Gemini / AI Settings** — show intel + stage-judge spend separately; link to Learning Dashboard. Do not bury learning only under Gemini.

Reuse existing CSS tokens; no restyle of the product.

---

## 7. Testing plan

### 7.1 Unit fixtures (from real corrections)

| Fixture id | Expect | Source |
| --- | --- | --- |
| `royal_dedupe` | One Intelligence/commitment row per Royal phone | `intel_stage_fixes.ROYAL_PHONE`, `test_intel_dedupe` |
| `admali_ov` | Stamping; serial+OV / ayachilla / go26/26 | `STAMPING_MOVES` Admali |
| `marvel_ov` | Stamping | Marvel Scales-Aluva |
| `sandhya_cov` | Stamping from Y01277… ov + CoV language | `test_whatsapp_service_meta` |
| `meezan_so` | Sales order (spare / freight / S) | Live Meezan correction |
| `strike_closed` | Excluded from all stages + commitments | Blast / Dear Customer |
| `charan_care_window` | Care/cloud thread reply-window / unreplied semantics **unchanged** by classifier | Protect `whatsapp_reply_needed` + Care ingest; name as regression even if not a stage fixture |
| `promise_not_paid` | Waiting, not Paid | `test_promise_to_pay_stays_waiting` |
| `phonepe_after_so` | Paid per **chosen** Paid option (assert image requirement if P1) | `test_phonepe_after_so_is_paid` — **update when Paid decision lands** |
| `screenshot_paid` | Paid when inbound image + proof caption | `payment_screenshot_conv_ids_sql` |
| `serial_only_no_ov` | **Not** stamping | New negative fixture |
| `stamp_price_line` | Not stamping / may be enquiry | `stamp_price_only` |
| `renewal_pay` | Not order cycle | existing test |

### 7.2 Golden set

- 50–100 labeled DMs (from Moves + curated). Split train (retrieval) / eval (never phrase-upsert from eval).
- Metrics: primary-stage accuracy, Paid precision/recall, Stamping precision (penalize serial-only FPs), blast exclusion recall.
- Store under `backend/tests/fixtures/intel_golden/` (anonymize phones if needed).

### 7.3 Dry-run backfill tests

- Preview returns eligible count, estimated ₹, **zero writes**.
- Apply writes predictions with version; second run idempotent.
- Rollback supersedes version; overrides untouched.
- Budget exhaustion stops judge calls; rules predictions still written.

### 7.4 Regression (must stay green)

- `test_whatsapp_reply_needed`, office hours, reply latency, dealer enquiry, service_meta, intel_enquiry, order_cycle, intel_observe, intel_dedupe.
- Webhook success when enqueue fails.
- No `(?i)` in SQL regex helper unit test.
- `count().filter` blast having clause.

### 7.5 Cost estimates (order of magnitude)

| Pass | Assumption | ₹ |
| --- | --- | --- |
| Phase A rules/KB only | 0 Gemini | ~0 |
| Phase B judge 500 ambiguous DMs/month × ~2k in + 400 out | ~₹5–15 | within ₹150 intel cap |
| Summary refresh 200/month | ~₹3–10 | |
| Full-history outline regenerate | **Do not** | ~₹450+ |
| Historical Sarvam | **Do not** | unbounded |

Exact preview must use live counts + `estimate_cost_inr` at apply time.

---

## 8. Rollout & rollback

1. Ship code with `stage_judge_enabled=false`, learning metrics on.
2. Deploy CRM only via existing flock script; no Evolution/Mailcow/ADMS/Free443/campaign restarts.
3. Confirm webhook ingest, Reply Stats, Intelligence tiles load, Move still works.
4. Enable KB hygiene flags; watch false Stamping for 1–2 days.
5. Enable judge for `needs_review` population only (predictions not auto-forcing tiles).
6. Dry-run reclassify → read report → optional prediction apply.
7. Never auto-write `stage_override` from backfill.

**Rollback:** disable judge + learning flags; tiles fall back to current rules/overrides; leave tables. Redeploy previous image if needed — webhooks have no sync Gemini dependency.

**Checkpoints:** reuse `ai_job_checkpoints` streams e.g. `stage_reclassify_90d`.

---

## 9. What NOT to do

- Superficial keyword spam (adding every dealer name / bare serial to regex).
- Blocking inbox / webhook on Gemini or full-history scans.
- Fine-tune-first or local LLM on the 8 GB VPS.
- Zoho lifetime Paid/Waiting on tiles (already burned once).
- `(?i)` inside Postgres `~*` patterns; `func.case` in HAVING aggregates.
- Auto outbound, campaign triggers, Free443, campaign container restarts.
- Re-transcribe historical voice with Sarvam for backfill.
- Merging FAQ `kb_articles` with `ai_stage_kb`.
- Replacing Move UI or reinventing WA-stage tiles.
- Treating payment **promise** or **screenshot without payment cue** as Paid (regardless of P0–P2 choice).
- Classifying every number / every `Y#####` as Stamping without OV/metrology context.
- Execute phase / Zoho write-back / digital twin / competitor crawl (upgrade plan rejections still stand).

---

## 10. Reconciliation with upgrade plan Phase B/C

| Upgrade plan item | Relation to this learning plan |
| --- | --- |
| Findings narration / Intel Ask | Still valid; schedule **after** Phase A/B learning if tile accuracy is the live fire. |
| Commitment Assist buttons | Independent; do not block learning. |
| SQL findings board | Keep; learning plan does not replace Observe findings. |
| Execute | Still rejected. |

**Suggested combined roadmap:** Learning Phase A → Learning Phase B → Upgrade Recommend (narration/Ask) → Learning Phase C dry-run → Assist.

---

## 11. Open questions for scangle

1. **Paid option P0 / P1 / P2 / P3?** (recommendation: P0 + review queue)
2. Allow **secondary stage chips** on rows, or primary-only forever?
3. May Learning Dashboard live under **Admin** or must it be under **Overview** next to Intelligence?
4. Approve **golden-set export** from production Moves (snippet redaction rules)?

---

## Appendix A — Module ownership (implementation map)

| Concern | Primary modules |
| --- | --- |
| Paid evidence helper | new `intel/paid_evidence.py` → used by `wa_stages`, `order_cycle` |
| Stamping context | `whatsapp_service_meta` + `wa_stages` + KB specificity |
| Move learning | `stage_move`, `stage_kb` |
| Predictions / review | new `intel/stage_predict.py`, `intel/review_queue.py` |
| Judge | new `intel/stage_gemini.py` (mirror `enquiry_gemini` budget pattern) |
| Funnel read path | `queries.funnel_snapshot` |
| Jobs | `jobs.py` new job types `stage_predict`, `stage_reclassify_tick` |
| UI | `Intelligence.tsx`, new `LearningDashboard.tsx`, `GeminiSettings` links |

## Appendix B — Evidence index (Paid / Stamping conflict)

- Live Paid = screenshot: agent correction thread 2026-11-25 (`payment_screenshot_conv_ids_sql`, funnel note).
- Brief Paid = staff/finance verified: this planning request.
- Code Paid text/Zoho path: `order_cycle._is_payment_done`, `zoho_settled_for_cycle`, `test_phonepe_after_so_is_paid`.
- Stamping serial without word “stamping”: Sandhya test + Admali/Marvel `intel_stage_fixes.py`.
