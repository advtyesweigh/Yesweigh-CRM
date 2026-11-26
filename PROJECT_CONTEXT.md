# YesWeigh CRM — Project Context

## Business objective

**No missed lead. No forgotten follow-up. No unattended customer. No neglected business opportunity.**

YesWeigh sells weighing scales, POS systems, billing software and related products across India via dealers, distributors and direct customers. CRM must automate sales, marketing, WhatsApp (15k contacts), multilingual ads, staff activity, attendance (ADMS), work and management intelligence.

Production URL: **https://crm.yesweigh.in**

## Non‑negotiables

- Self-hosted on existing VPS; Caddy HTTPS.
- Separate Postgres DB: `yesweigh_crm` (already created on Evolution Postgres host).
- Do **not** use Firebase.
- Do **not** interrupt Mailcow, Evolution Go, ADMS (`:8081`), or host Caddy.
- Common REST API for future Capacitor Android/iOS; Phase 1 priority = desktop web.
- No unsolicited bulk WhatsApp / anti-detection; consent + approval required for campaigns.
- Automation must run on the server without employees logged in.

## Employees module

Central staff master under **Settings → Employees** (profile `/employees/:id`). Persisted in Postgres `employees` (+ documents/targets/activity). Documents under `/data/employee-docs`.

## Evolution / WhatsApp

**Settings → Evolution API**. CRM owns only instances registered in `whatsapp_instances` (org `yesweigh`). Never list raw Evolution `/instance/all`. Adapter talks to Evolution GO (`/instance/create|connect|qr|status|delete`). Webhooks: `/api/webhooks/evolution/{public_id}`.

**History sync (on LoggedIn + manual Sync history):** Evolution GO has no `findMessages` dump API. CRM calls `GET /user/contacts`, optionally `GET /group/myall`, and `POST /chat/history-sync` per chat with `messageInfo.chat` as a **JID string**. Message bodies arrive as MESSAGE webhooks and are upserted into `whatsapp_conversations` / `whatsapp_messages` for that instance only (media auto-downloaded to `/data/wa-media`). Opening an empty thread also POSTs `/api/whatsapp/conversations/{id}/sync-history`. WhatsApp typically shares only recent linked-device history (not multi-year archives); on-demand requests need a Signal session and may partially fail. Profile DPs use `/user/avatar` (soft-fail → initials).

## Visual reference

Primary UI reference: `docs/prototypes/yesweigh-crm.html`

Palette:

| Token | Hex |
|-------|-----|
| Primary blue | `#2563EB` / prototype `#1859e8` |
| Dark blue / navy | `#1E3A8A` / `#101b32` |
| Ink | `#0F172A` / `#152238` |
| Background | `#F5F7FB` |
| Cards | `#FFFFFF` |
| Borders | `#E2E8F0` / `#e5eaf1` |

Layout: collapsible sidebar, sticky top bar, compact tables, minimal whitespace, status colours only for meaning.

## Architecture (target)

```
apps/web      React + TypeScript + Vite + Tailwind (+ shadcn/ui)
apps/api      NestJS + Prisma + PostgreSQL
apps/worker   BullMQ workers (automation, campaigns)
services/adms Existing ADMS gateway on :8081 (integrate, do not rewrite)
packages/ui   Shared UI primitives
packages/shared  Shared types/DTOs
infrastructure   Docker Compose, Caddy snippets, backups
```

Integrations:

| System | Role | Status |
|--------|------|--------|
| Evolution API | WhatsApp gateway | Live on VPS (`127.0.0.1:18082`) |
| Meta Marketing API | Ads + lead webhooks | Not configured |
| ADMS | Fingerprint attendance push | Live `:8081` |
| Redis + BullMQ | Jobs / automation | Phase 1 scaffold |
| Capacitor | Mobile client | Later; API-ready |

## Database

- Host: existing `evolution-go-postgres` (Docker network `evolution_net`)
- Database: `yesweigh_crm`
- App role: `crm` (separate from Evolution credentials)
- Timezone storage: UTC; display default Asia/Kolkata
- UUID PKs, migrations via Prisma

## Auth & roles (Phase 1)

Roles: Super Admin, Management, Sales Manager, Sales Executive, Marketing Manager, HR Manager, Service Manager, Service Engineer, Accounts, Employee.

Permissions enforced on API (never UI-only).

## Sidebar (product IA)

Overview · Sales · Marketing · Operations · People · Administration  
(as specified in master prompt §4)

## Implementation phases

1. Architecture, DB, auth, roles, desktop shell, dashboard  
2. Contacts, dealers, import, lead pipeline  
3. Evolution inbox, assets, campaigns  
4. Dealer calling, monitoring, automation  
5. Meta Ads, quotations, orders  
6. ADMS/HRM/work deep integration  
7. Service, AI Command Centre, reports  
8. Security audit, performance, mobile readiness  

## VPS notes (inspected)

- Keep `/opt/adms`, `/opt/mailcow-dockerized`, `/opt/evolution-go`, host Caddy.
- CRM deploy path: `/opt/yesweigh-crm`
- Caddy `crm.yesweigh.in`: `/api/*` → `127.0.0.1:8100` (API direct); other paths → `127.0.0.1:8101` (nginx SPA). Keeps sends/webhooks alive during web-only deploy blips. Snippet: `deployment/vps-caddy-crm.snippet`.
- Port plan: web `127.0.0.1:8101`, api `127.0.0.1:8100`, redis internal only.
- CRM deploy uses flock (`/var/lock/yesweigh-crm-deploy.lock` on VPS; local tmp lock) — do not run overlapping `deploy_crm_ai.sh`.

## Legacy MVP

An early FastAPI + React MVP existed under `backend/` + `frontend/`. Phase 1 NestJS monorepo **supersedes** that stack for production architecture; migrate carefully without downtime where possible.

## All-India Dealer Discovery (2026-11)

- Extends **Dealer Finder** (`backend/app/dealer_finder/`) — Gemini district→city plans, Places New staged search, SQL history cooldown, lead import.
- **Excluded states (all-India only):** Tamil Nadu, Kerala/Keralam, Puducherry — omitted from full-India queue; manual / AI single-state jobs still allowed.
- **Rollout:** pilot one district (default Rajasthan · Jaipur) → verify → admin `all_india_enabled` → sequential full-India run (one state at a time). **Never** auto-start on deploy.
- UI: `/dealer-finder` → **All India** tab (tiles + pilot controls). Docs: `docs/ALL_INDIA_DEALER_DISCOVERY.md`.
