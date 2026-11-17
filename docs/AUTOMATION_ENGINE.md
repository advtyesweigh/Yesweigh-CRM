# YesWeigh Automation Engine

Last updated: 2026-11-16

## Status

**Phase 1:** not implemented (foundation only — AI command path is request/response).  
**Phase 2:** PostgreSQL-backed job queue + lightweight Python worker.

## Design decisions

| Choice | Reason |
|--------|--------|
| Postgres job queue | Already have Postgres; avoid Redis/Celery on busy 8 GB VPS |
| No n8n | Extra stack + RAM; UI rules can be WHEN → CONDITION → ACTION in CRM |
| No Ollama | No GPU; cloud Gemini only |
| Worker concurrency default 2 | Configurable via `AI_WORKER_CONCURRENCY` |

## Planned job model (Phase 2)

- Priority, scheduled_at, lease / `FOR UPDATE SKIP LOCKED`
- Heartbeat, timeout, limited retries, idempotency keys
- Types: scheduled rules, approved campaigns, auto-reply, follow-ups, AI batch, alerts

## Rule engine (Phase 2+)

Visual editor: **WHEN → CONDITION → ACTION**.

Example triggers: new WhatsApp message, follow-up due, WhatsApp disconnected, scheduled time.  
Example actions: create task, notify manager, prepare campaign draft, suggest AI reply.

External communication and spend require approval + consent checks.

## Emergency controls

- Global automation pause (admin)
- Pause campaign jobs when WhatsApp instance disconnects
- Budget exhaustion pauses **paid AI** only — rule-based CRM automation continues

## Relationship to AI Agent

Routine monitoring should prefer **rules + SQL/Python**, not Gemini. Gemini interprets questions and explains verified tool results.
