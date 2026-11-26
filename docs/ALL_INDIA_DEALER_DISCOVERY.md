# All-India Dealer Discovery

Extends **Dealer Finder** (`/dealer-finder` → **All India** tab). Does not replace Places, pricing, or import paths.

## Excluded (all-India orchestration only)

Tamil Nadu, Kerala/Keralam, Puducherry — omitted from pilot/full all-India queues and `assert_state_allowed`. Manual Dealer Finder / AI `start_dealer_finder` for those states is allowed.

## Pilot → full country

1. **Preflight** — `POST /api/dealer-finder/all-india/preflight?mode=pilot&state=…&district=…`
2. **Start pilot** — `POST /api/dealer-finder/all-india/pilot` with `confirm=true`, `acknowledge_cost=true` (default: Rajasthan · Jaipur).
3. When the pilot job completes, `pilot_district_completed` is set automatically.
4. **Admin** enables `all_india_enabled` under Dealer Finder → Cost controls (or `PATCH /api/dealer-finder/settings`).
5. **Full India** — `POST /api/dealer-finder/all-india/full` (admin, confirm + cost ack). One state job at a time until the eligible queue finishes.

Nothing auto-starts on deploy.

## Controls

| Action | Endpoint |
|--------|----------|
| Dashboard tiles | `GET /api/dealer-finder/all-india/dashboard` |
| Pause / resume / cancel / skip state | `POST …/all-india/runs/{id}/pause` etc. |

Worker resumes child `DealerSearchJob` units after VPS restart; orchestration advances via `tick_all_india_runs`.

## Cost

India SKU model in `places_pricing.py`; monthly spend cap in settings (admin required to **raise** cap).