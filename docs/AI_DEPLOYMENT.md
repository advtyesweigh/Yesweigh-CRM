# YesWeigh AI — Deployment

Last updated: 2026-11-16

## Environments

| Env | Path | Notes |
|-----|------|--------|
| Local | `/Users/mac/Projects/yesweigh-crm` | Dev against local or tunneled API |
| VPS | `/opt/yesweigh-crm` | Production; Docker Compose |

SSH (from workstation):

```bash
ssh -i ~/.ssh/vps_10317685102 -p 22318 root@103.176.85.102
```

## Env vars (AI)

Add to `.env` (never commit real keys). See `.env.example`.

```env
AI_PROVIDER=gemini
AI_MODEL=gemini-2.5-flash-lite
GEMINI_API_KEY=
AI_ENABLED=true
AI_MONTHLY_BUDGET_INR=500
AI_DAILY_BUDGET_INR=25
AI_MAX_OUTPUT_TOKENS=500
AI_WORKER_CONCURRENCY=2
```

On VPS: leave `GEMINI_API_KEY` empty until a real key is provisioned. UI shows **Not configured**.

## Deploy CRM only (do not touch ADMS / Mailcow / Evolution)

```bash
# From local CRM root
RSYNC_SSH="ssh -i $HOME/.ssh/vps_10317685102 -p 22318 -o IdentitiesOnly=yes"
HOST=root@103.176.85.102

rsync -az --delete \
  --exclude '__pycache__' --exclude '.venv' --exclude '*.pyc' \
  -e "$RSYNC_SSH" \
  ./backend/ "$HOST:/opt/yesweigh-crm/backend/"

rsync -az --delete \
  --exclude 'node_modules' --exclude 'dist' \
  -e "$RSYNC_SSH" \
  ./frontend/ "$HOST:/opt/yesweigh-crm/frontend/"

rsync -az -e "$RSYNC_SSH" \
  ./docker-compose.yml ./docs/ ./.env.example \
  "$HOST:/opt/yesweigh-crm/"

ssh -i ~/.ssh/vps_10317685102 -p 22318 root@103.176.85.102 \
  'cd /opt/yesweigh-crm && docker compose build migrate api web && docker compose up -d migrate api web'
```

Ensure AI env keys exist on VPS `.env` (empty `GEMINI_API_KEY` is OK). Do **not** restart `adms`, `mailcow`, or `evolution-go` for AI deploys.

## Migrations

Compose `migrate` service runs `python -m app.migrate` (`Base.metadata.create_all` + column ensures). New AI tables are created automatically on deploy.

## After deploy

1. Hard-refresh the browser (Ctrl/Cmd+Shift+R) so the new `/work` AI Agent UI loads.
2. Open **AI agent** in the sidebar.
3. If key missing → status **Not configured**.
4. Set `GEMINI_API_KEY` in `/opt/yesweigh-crm/.env`, then `docker compose up -d api` (recreate api only).

## Resource notes

- Keep worker concurrency low when introduced
- Prefer cloud Gemini; do not install local LLM stacks
- Inspect RAM/CPU before raising limits (`docker stats`, `free -h`)
