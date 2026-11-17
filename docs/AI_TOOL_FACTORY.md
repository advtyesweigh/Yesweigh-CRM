# YesWeigh AI — Live Tool Factory

Last updated: 2026-11-16

## What it does

When Gemini cannot answer a CRM question (missing tool / “I don’t have a tool…”), the agent can:

1. Call meta-tool `request_new_tool` (admin/management), **or** gap-detect from the final reply
2. Launch a **Cursor Cloud Agent** (`CURSOR_FAISAL_API_KEY` / `CURSOR_API_KEY`) with a no-repo prompt
3. Parse a read-only Python tool module (`TOOL_META` + `async def run`)
4. Validate (SELECT-only, allowlisted tables, no secrets / exec / network)
5. Persist under `/data/ai-generated-tools/` (Docker volume) and **hot-register**
6. Auto-retry the user question once with the new tool

## Env (VPS `/opt/yesweigh-crm/.env`)

```env
AI_TOOL_FACTORY_ENABLED=true
AI_TOOL_FACTORY_TIMEOUT_S=180
AI_GENERATED_TOOLS_DIR=/data/ai-generated-tools
CURSOR_FAISAL_API_KEY=…   # already on VPS; never commit
# CURSOR_API_KEY=…        # optional alias
```

## Safety

- Admin / management only
- Feature flag `AI_TOOL_FACTORY_ENABLED`
- AST + regex gate: no mutations, no secrets, allowlisted imports/tables
- Generated tools stay read-only CRM queries

## How to test on `/work`

1. Log in as **admin** or **management**
2. Open **AI Agent** (`/work`) → New Chat
3. Ask something unsupported today, e.g.  
   **“Label print scale sales by dealer this FY”**  
   (needs `zoho_invoice_line_items` — not a builtin tool)
4. Wait ~1–3 minutes (“Preparing answer…” while the factory runs)
5. Reply should say a tool was added and include an answer, **or** ask once more after the tool is live
6. Confirm tool file on VPS:  
   `docker exec yesweigh-crm-api ls -la /data/ai-generated-tools/`

## Limitations

- Sync wait on the same `/api/ai/command` request (proxy/browser may feel slow; timeout default 180s)
- No human approve-gate in MVP (admin-only + static safety)
- Hot-register survives process restarts via the volume; full image rebuilds still keep `/data/ai-generated-tools`
- Cursor no-repo agent returns code as text — quality varies; failed parse surfaces an error in the reply
- Non-admin users never see `request_new_tool` / factory

## Code map

| Piece | Path |
|-------|------|
| Factory + Cursor client | `backend/app/ai/tool_factory.py` |
| Loader / hot-register | `backend/app/ai/generated_tools/` |
| Meta-tool + specs | `backend/app/ai/tools.py` |
| Gap detect + retry loop | `backend/app/ai/service.py` |
| Volume | `docker-compose.yml` → `ai_generated_tools` |
