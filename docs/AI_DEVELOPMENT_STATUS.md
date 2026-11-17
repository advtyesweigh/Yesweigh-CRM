# YesWeigh AI — Development Status

Last updated: 2026-11-16

## Phase 0 — Documentation

- [x] `docs/AI_ARCHITECTURE.md`
- [x] `docs/AI_AGENT_TOOLS.md`
- [x] `docs/AUTOMATION_ENGINE.md`
- [x] `docs/AI_SECURITY.md`
- [x] `docs/AI_DEPLOYMENT.md`
- [x] This status file

## Phase 1 — AI Foundation (this run)

- [x] Env knobs: `AI_PROVIDER`, `AI_MODEL`, `GEMINI_API_KEY`, `AI_ENABLED`, budgets, max tokens (config + `.env.example`)
- [x] Provider abstraction + Gemini via `google-genai`
- [x] DB models/migrations: `ai_settings`, `ai_sessions`, `ai_messages`, `ai_agent_runs`, `ai_tool_calls`, `ai_usage_logs`
- [x] Read-only tools: `get_sales_summary`, `get_unanswered_messages`, `get_whatsapp_connection_status`, `get_employee_whatsapp_instances`
- [x] Phase 2 dealer sales: `search_dealers`, `get_dealer_sales` (FY YTD / MTD / custom; ambiguous → candidates)
- [x] Region/district sales: `get_region_sales` (e.g. `malappuram dt` → Malappuram FY YTD aggregate; no dealer-loop)
- [x] APIs: `POST /api/ai/command`, `GET /api/ai/runs`, `GET /api/ai/runs/{id}`, `GET|PATCH /api/ai/settings`, `GET /api/ai/usage`
- [x] `/work` AI Agent command centre UI (royal blue / white)
- [x] Budget reservation + usage logging
- [x] Missing key → **Not configured** in UI
- [x] Deployed to VPS `/opt/yesweigh-crm` (migrate + api + web). ADMS/Mailcow/Evolution not restarted.
- [x] AI tables verified on production Postgres

### Deploy notes

- Script: `scripts/deploy_crm_ai.sh`
- Model id: `gemini-3.5-flash-lite` (env `AI_MODEL`)
- VPS `.env` already contained a `GEMINI_API_KEY` (not invented this run); budget knobs were appended if missing
- Hard-refresh browser after deploy to load new `/work` UI

## Deferred — Phase 2+

- Postgres job queue + Python worker
- Automation rules UI + WHEN/CONDITION/ACTION
- Dealer Finder, lead import preview/confirm
- Auto-reply, knowledge base, campaigns AI prep
- Write tools, approvals, SSE live progress
- Full AI Agent tabs (Dealer Finder, Campaigns, …) beyond Assistant + placeholders
- Admin alert on budget exhaustion
- Cost-per-employee charts / daily spend charts (usage API is minimal for Phase 1)

## Notes

- Live stack remains FastAPI + React; NestJS scaffold unused for AI.
- Evolution is **GO**, not Node.
- No commit made this run (working tree left for user review).
