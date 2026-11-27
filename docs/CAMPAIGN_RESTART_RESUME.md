# Campaign Pause, Restart & Resume

Campaigns → **Campaign** tab → **Report** shows WhatsApp paced blast results.

## When to use which

| Action | Use when | Instance |
| --- | --- | --- |
| **Pause** | Operator wants to temporarily stop a healthy **Running** / **Queued** blast | Same (unchanged) |
| **Resume** (after Pause) | Continue a **Paused** blast from `next_index` | Same |
| **Restart** | Agent WhatsApp reconnected to the **same** Evolution instance (socket flap / logout / wait timeout) | Same `from_instance` as the failed run |
| **Resume on new instance** | Number is blocked ~24h, or you need a **different** WhatsApp / Evolution line | Pick another **Connected** instance |

Pause does **not** auto-apply — only when the operator clicks **Pause**. Other active campaigns are left alone.

## Pause / Resume (operator)

- **Pause** sets status → `paused`. Stops further paced sends.
- Keeps `next_index`, success/failed/skipped counts, `recipients_json`, `gaps_json`, and `filter_meta_json` daily caps unchanged.
- Quiet hours (23:50–05:50 IST) and random interval model stay the same.
- **Resume** (`/unpause`) sets status → `running` and wakes the paced worker. Continues from where it left off (no recipient rebuild, no cap change).
- **Est. end** is hidden while paused. On resume it is **recomputed from now** via `estimate_paced_run_end_at` (remaining recipients + same per-day max + sleep window + mean gap model), so paused wall-clock time is excluded.

## Restart / Resume on new instance (failure recovery)

Both actions rebuild the queue from `recipients_json`:

- **Keep** `sent` (Success) and `skipped` — never re-blasted
- **Reset** `failed` (and any other non-terminal) → `pending`
- Recount: `success_count` / `skipped_count` preserved; `failed_count` → 0
- `next_index` → first pending slot
- Status → `running`; paced worker is woken

Media-UID dedupe in the paced worker still skips numbers that already received this campaign media on any line.

**Spam exclusion:** at each paced send attempt (and when building/filtering recipients), phones with any WhatsApp conversation `is_spam=true` are matched by normalized 10-digit IN key (`91…` / `+91…` / local) and marked `skipped` with reason spam — not failed. Newly marked spam stops remaining queue items mid-campaign; the run itself continues.

## API (auth required)

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `GET` | `/api/campaigns/send-instances` | — | Authorized Evolution lines (Campaigns ACL; not Meta Care) |
| `POST` | `/api/campaigns/runs/{run_id}/pause` | `{}` | Running/Queued → Paused; 409 otherwise |
| `POST` | `/api/campaigns/runs/{run_id}/unpause` | `{}` | Paused → Running; Est. end reschedules; 409 otherwise |
| `POST` | `/api/campaigns/runs/{run_id}/restart` | `{}` | Same instance; 409 if not Connected / not Failed\|Stopped |
| `POST` | `/api/campaigns/runs/{run_id}/resume` | `{ "from_instance_public_id": "<uuid>" }` | Rebind + continue remaining |

Report list (`GET /api/campaigns/runs`) includes `from_instance_public_id`, `can_restart`, `can_pause`, `can_unpause`, and `estimated_end_at` (running/queued only).

## UI placement

Report table → under the date cell:

1. **Running/Queued** → **Pause** (+ live play icon + Est. end)
2. **Paused** → **Resume** (+ Paused badge)
3. **Failed/Stopped** → **Restart** + **Resume on new instance**

## Ops notes

- Reconnect the original agent in Evolution Settings first if you only need **Restart**.
- **Resume on new instance** queues behind any other paced job already running on that sender (FIFO per Evolution line).
- Pause/Resume never change messages-per-day overrides (e.g. campaign 114 at 100/day stays 100/day).

## Pacing / quiet hours (IST)

- Quiet window (no paced sends): **23:50 → 05:50** Asia/Kolkata.
- Leads audiences default to **250 sends / IST day** (`LEADS_MAX_SENDS_PER_IST_DAY`).
- Per-run overrides via `campaign_runs.filter_meta_json`:
  - `max_sends_per_ist_day` — ongoing daily cap (e.g. `100`)
  - `max_sends_by_ist_day` — map of `YYYY-MM-DD` → cap for that day (e.g. today-only `25`)
- Random human-like gaps come from `gaps_json` / paced worker jitter (`MIN_GAP_SECONDS` 45s).

## Est. end computation

`estimate_paced_run_end_at(run)` (only for `running` / `queued`):

1. Remaining ≈ `total − success − failed − skipped`
2. Start from **now** (IST); advance past quiet hours / today’s daily cap if already full
3. For each remaining send: respect daily cap + sleep window; add mean gap (`mean_runtime_gap_seconds`) between sends
4. Return projected finish datetime (shown as Est. end in Report)
