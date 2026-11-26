# Campaign Restart & Resume on new instance

Campaigns → **Campaign** tab → **Report** shows WhatsApp paced blast results. Failed (or Stopped) rows now have actions so ops can continue after Evolution reconnects or a WhatsApp 24h send block.

## When to use which

| Action | Use when | Instance |
| --- | --- | --- |
| **Restart** | Agent WhatsApp reconnected to the **same** Evolution instance (socket flap / logout / wait timeout) | Same `from_instance` as the failed run |
| **Resume on new instance** | Number is blocked ~24h, or you need a **different** WhatsApp / Evolution line | Pick another **Connected** instance |

Active healthy runs (status `running` / `queued`) have **no** action buttons — they are not interrupted.

## Recipient selection (no double-send)

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
| `POST` | `/api/campaigns/runs/{run_id}/restart` | `{}` | Same instance; 409 if not Connected / not Failed\|Stopped |
| `POST` | `/api/campaigns/runs/{run_id}/resume` | `{ "from_instance_public_id": "<uuid>" }` | Rebind + continue remaining |

Report list (`GET /api/campaigns/runs`) includes `from_instance_public_id` and `can_restart`.

## UI placement

Report table → last column **Actions** on Failed/Stopped rows:

1. **Restart**
2. **Resume on new instance** → modal to pick a Connected Evolution instance → **Resume campaign**

## Ops notes

- Reconnect the original agent in Evolution Settings first if you only need **Restart**.
- **Resume on new instance** queues behind any other paced job already running on that sender (FIFO per Evolution line).
- Does not stop or touch other campaigns (e.g. leave an active paced run alone).
