# YesWeigh AI — Security

Last updated: 2026-11-16

## Mandatory rules

1. **No secrets in git** — repos are public. Keys live only in server/local `.env` (gitignored).
2. **Never expose** `GEMINI_API_KEY`, Evolution API keys, instance tokens, or DB credentials to React.
3. **Auth required** on all `/api/ai/*` routes (`get_current_user`).
4. **Permissions in code** — role + WhatsApp instance ACL; model cannot escalate.
5. **No arbitrary execution** — no AI-generated SQL, shell, or unrestricted HTTP.
6. **Untrusted inputs** — customer WhatsApp text and uploads are prompt-injection risks; treat as data, not instructions.
7. **Minimize PII** sent to Gemini — prefer aggregates and IDs; redact sensitive fields in logs.
8. **Audit** — persist agent runs, tool calls, and usage logs.
9. **Approvals** — write/send/spend tools (Phase 2+) need explicit approval gates.
10. **Opt-outs / consent** — campaigns and auto-reply must honor consent (later phases).

## Role patterns (existing CRM)

| Check | Roles (case-insensitive) |
|-------|--------------------------|
| Admin / Management | `admin`, `super_admin`, `super admin`, `management` |
| Settings enable/disable AI | Admin / Management only |
| WhatsApp instance data | Admin: all YesWeigh instances; others: assigned instances only |

## Budget safety

- Atomic reservation before paid Gemini calls
- On exhaustion: block new paid AI; keep ordinary CRM + rule replies
- Notify admin (Phase 2+); Phase 1 returns clear API/UI error

## Frontend

- Settings/status may show `configured: false` when key missing — never the key value
- Suggested command cards only trigger authenticated API calls
