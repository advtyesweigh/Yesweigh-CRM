# YesWeigh AI Agent — Architecture

Last updated: 2026-11-16 (Phase 0 / Phase 1 foundation)

## Goal

Embed a lightweight **AI Agent & Automation Engine** inside the existing YesWeigh CRM so staff can ask natural-language questions, run secure CRM tools, and (later) automate approved workflows — without replacing the live stack.

## What exists today (production)

| Layer | Technology | Notes |
|-------|------------|--------|
| URL | https://crm.yesweigh.in | Caddy → `127.0.0.1:8101` (web), API `:8100` |
| Frontend | React + Vite + TypeScript | `frontend/` — sidebar **AI agent** → `/work` |
| Backend | **FastAPI** (Python) | `backend/app/` — live API |
| DB | PostgreSQL `yesweigh_crm` | On Evolution Postgres host; SQLAlchemy async |
| WhatsApp | **Evolution GO** (not Node Evolution) | Adapter: `evolution_service.py` / `evolution.py` |
| Sales | Zoho Books sync | `zoho_sales`, dashboard `/api/dashboard/sales-*` |
| Attendance | ADMS `:8081` | Keep intact; do not restart casually |
| Auth | JWT Bearer | `auth.py` — `get_current_user` |

**Do not replace:** FastAPI, Postgres, Evolution GO, Caddy, Mailcow, ADMS, GATC.

**Do not install (Phase 1–2):** n8n, Ollama, Redis, Celery, RabbitMQ, vector DB, NestJS cutover for AI.

Scaffold under `apps/` (NestJS) is **not** the live API. All AI work targets `backend/` + `frontend/`.

## Target AI stack (lightweight)

```
React (/work AI Agent UI)
        │  JWT
        ▼
FastAPI  /api/ai/*
        │
        ├─ Provider adapter ──► Gemini (cloud) via official google-genai SDK
        ├─ Secure tool registry (validated, permission-checked)
        ├─ Budget reservation + usage logs (Postgres)
        └─ (Phase 2+) Postgres job queue + Python worker process
```

- Primary model: **Gemini 3.5 Flash-Lite** (`AI_MODEL=gemini-3.5-flash-lite`)
- Provider abstraction so another cloud provider can be added later
- All LLM compute in Google cloud — **no local GPU / heavy ML**
- Tools are predefined CRM functions; the model never runs arbitrary SQL/shell/HTTP

## Evolution GO note

Evolution on this VPS is **Evolution GO**, not the original Node.js Evolution API.

- CRM registry isolates YesWeigh instances (`whatsapp_instances`, org `yesweigh`)
- Never call raw Evolution `/instance/all` for multi-tenant listing
- History sync uses GO-specific endpoints (`/user/contacts`, `/chat/history-sync`, webhooks) — see `PROJECT_CONTEXT.md`

## Phase map (summary)

| Phase | Scope |
|-------|--------|
| **0** | Architecture / security / tool / automation / deploy docs |
| **1** | Gemini adapter, settings, budgets, read-only tools, AI Assistant UI, usage APIs |
| **2** | Postgres job queue + Python worker, automation rules |
| **3** | Dealer Finder + lead import preview/confirm |
| **4** | WhatsApp auto-reply + knowledge base |
| **5** | Campaign prep + approval |
| **6+** | Sales monitor deep rules, SSE progress, full tab set |

## Data ownership

AI tables are **additive** in `yesweigh_crm`. They reference existing `users`, `employees`, WhatsApp, leads, dealers, Zoho sales — they do not duplicate CRM masters.

Organization scope: YesWeigh only (`organization_id` / instance filters already used in WhatsApp modules).

## Cost & capacity (VPS)

VPS: `103.176.85.102`, ~8 GB RAM, busy (Mailcow + Evolution + ADMS + CRM).

- API container already `mem_limit: 512m`
- AI path must stay request-scoped; worker concurrency later defaults to **2**
- Default budgets: ₹500 / month, ₹25 / day (configurable)

## Related docs

- [AI_AGENT_TOOLS.md](./AI_AGENT_TOOLS.md)
- [AUTOMATION_ENGINE.md](./AUTOMATION_ENGINE.md)
- [AI_SECURITY.md](./AI_SECURITY.md)
- [AI_DEPLOYMENT.md](./AI_DEPLOYMENT.md)
- [AI_DEVELOPMENT_STATUS.md](./AI_DEVELOPMENT_STATUS.md)
