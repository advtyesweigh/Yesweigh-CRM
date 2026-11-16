# YesWeigh CRM — Development Status

Last updated: 2026-11-13

## Completed

- [x] VPS inspection (Caddy, Postgres, Docker, ports, resources)
- [x] Postgres DB `yesweigh_crm` + role `crm` on Evolution Postgres host
- [x] Caddy host `crm.yesweigh.in` (HTTPS)
- [x] Initial MVP deploy path `/opt/yesweigh-crm` (FastAPI + React) — transitional
- [x] ADMS live on `:8081` (kept intact)
- [x] `PROJECT_CONTEXT.md` authored
- [x] Monorepo folder scaffold (`apps/`, `packages/`, `infrastructure/`, `services/adms`)
- [x] HTML prototype referenced for UI (source: user-supplied `yesweigh-crm.html`)
- [x] **Employees module** (FastAPI + React): directory, add/edit drawer (4 tabs), profile page, documents API, CRM login link, ADMS emp code mapping, targets/activity tables, private doc volume
- [x] **Evolution API Settings tab**: config (encrypted key), YesWeigh-owned WhatsApp instances only (CRM registry isolation), create/QR/status/restart/disconnect/delete, webhook endpoint per instance

## In progress — Phase 1

- [x] Desktop shell matching prototype (sidebar groups, top bar, dashboard cards)
- [x] NestJS API scaffold + Prisma schema (User/Role/Branch/AuditLog)
- [ ] NestJS auth module + cutover from FastAPI
- [ ] Tailwind + shadcn/ui (palette applied via CSS tokens; shadcn next)
- [ ] Redis container (internal) for future BullMQ
- [ ] Replace transitional FastAPI stack without breaking DNS/Caddy
- [ ] Employees: live ADMS present/absent punch sync, full permission matrix UI, salary encryption at rest

## Pending (later phases)

- Phase 2: Contacts, dealers, CSV import, lead Kanban
- Phase 3: Evolution inbox, assets, campaigns
- Phase 4: Dealer calling, monitoring, automation engine
- Phase 5: Meta Ads, quotations, orders
- Phase 6: ADMS deep attendance analytics + leave workflows
- Phase 7: Service, AI Command Centre, reports
- Phase 8: Security audit, performance, Capacitor

## Known issues

- Transitional FastAPI login had passlib/bcrypt version noise (admin seed still created)
- Prototype HTML file not retained on disk after session; rebuild from design tokens + captured structure
- NestJS monorepo not yet serving production traffic

## Testing status

- Manual: `https://crm.yesweigh.in` loads login
- Manual: ADMS `/health` and mailcow/evolution remain up after CRM deploy
- Automated: not yet (add Jest/e2e in Phase 1)

## Database migration status

- DB exists: `yesweigh_crm`
- Prisma migrations: not yet (Phase 1)
- FastAPI SQLAlchemy tables may exist from MVP seed — will be superseded by Prisma

## Deployment status

| Component | Status |
|-----------|--------|
| crm.yesweigh.in | Live (Caddy → :8101) |
| CRM API :8100 | Live (transitional FastAPI) |
| ADMS :8081 | Live |
| Evolution | Live |
| Mailcow | Live |
| Redis (CRM) | Not yet |
| Worker | Not yet |
