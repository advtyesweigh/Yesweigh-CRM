#!/usr/bin/env bash
# Deploy CRM (including AI Agent Phase 1) to VPS — does NOT restart ADMS/Mailcow/Evolution.
set -euo pipefail

HOST="${DEPLOY_HOST:-root@103.176.85.102}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/vps_10317685102}"
SSH_PORT="${DEPLOY_SSH_PORT:-22318}"
SSH_OPTS=(-i "$SSH_KEY" -p "$SSH_PORT" -o IdentitiesOnly=yes)
RSYNC_SSH="ssh -i $SSH_KEY -p $SSH_PORT -o IdentitiesOnly=yes"
CRM_LOCAL="${CRM_LOCAL:-$(cd "$(dirname "$0")/.." && pwd)}"

echo "==> Sync CRM → /opt/yesweigh-crm"
rsync -az --delete \
  --exclude '__pycache__' --exclude '.venv' --exclude '*.pyc' \
  -e "$RSYNC_SSH" \
  "$CRM_LOCAL/backend/" "$HOST:/opt/yesweigh-crm/backend/"
rsync -az --delete \
  --exclude 'node_modules' --exclude 'dist' \
  -e "$RSYNC_SSH" \
  "$CRM_LOCAL/frontend/" "$HOST:/opt/yesweigh-crm/frontend/"
rsync -az -e "$RSYNC_SSH" \
  "$CRM_LOCAL/docker-compose.yml" "$HOST:/opt/yesweigh-crm/docker-compose.yml"
rsync -az -e "$RSYNC_SSH" \
  "$CRM_LOCAL/.env.example" "$HOST:/opt/yesweigh-crm/.env.example"
rsync -az --delete -e "$RSYNC_SSH" \
  "$CRM_LOCAL/docs/" "$HOST:/opt/yesweigh-crm/docs/"

ssh "${SSH_OPTS[@]}" "$HOST" 'bash -s' <<'EOF'
set -euo pipefail
cd /opt/yesweigh-crm
# Ensure AI env knobs exist; do NOT invent GEMINI_API_KEY
touch .env
ensure_kv() {
  local key="$1" val="$2"
  if ! grep -q "^${key}=" .env; then
    echo "${key}=${val}" >> .env
    echo "Added ${key} to .env"
  fi
}
ensure_kv AI_PROVIDER gemini
ensure_kv AI_MODEL gemini-2.5-flash-lite
ensure_kv GEMINI_API_KEY ""
ensure_kv AI_ENABLED true
ensure_kv AI_MONTHLY_BUDGET_INR 500
ensure_kv AI_DAILY_BUDGET_INR 25
ensure_kv AI_MAX_OUTPUT_TOKENS 2048
# Bump existing low defaults so contact-list short replies are not token-starved
if grep -q '^AI_MAX_OUTPUT_TOKENS=500$' .env; then
  sed -i 's/^AI_MAX_OUTPUT_TOKENS=500$/AI_MAX_OUTPUT_TOKENS=2048/' .env
  echo "Updated AI_MAX_OUTPUT_TOKENS 500 → 2048"
fi
ensure_kv AI_WORKER_CONCURRENCY 2
ensure_kv AI_TOOL_FACTORY_ENABLED true
ensure_kv AI_TOOL_FACTORY_TIMEOUT_S 90
ensure_kv AI_RUN_TIMEOUT_S 180
ensure_kv AI_RUN_STALE_S 600
ensure_kv AI_GENERATED_TOOLS_DIR /data/ai-generated-tools
ensure_kv CURSOR_FAISAL_API_KEY ""
ensure_kv CURSOR_API_KEY ""
ensure_kv CARE_WEBHOOK_SECRET ""
ensure_kv CARE_META_VERIFY_TOKEN ""
ensure_kv YESONE_CLOUD_WEBHOOK_URL "https://asia-south1-yesweigh-service.cloudfunctions.net/ingestWhatsAppCloudWebhook"
ensure_kv YESONE_CARE_SEND_URL "https://asia-south1-yesweigh-service.cloudfunctions.net/crmCareSendWhatsApp"
ensure_kv WHATSAPP_CLOUD_PHONE_NUMBER_ID "1288718687667400"
ensure_kv WHATSAPP_CLOUD_ACCESS_TOKEN ""
ensure_kv FIREBASE_LOGIN_ID ""
ensure_kv FIREBASE_PASSWORD ""
ensure_kv FIREBASE_PROJECT_ID "yesweigh-service"

# Force web rebuild: BuildKit can keep a stale frontend layer when only TS/CSS change.
docker compose build migrate api
docker compose build --no-cache web
docker compose up -d migrate api web
docker compose ps
curl -sS http://127.0.0.1:8100/api/health || true
echo
EOF

echo "==> CRM AI deploy finished (ADMS/Mailcow/Evolution untouched)"
