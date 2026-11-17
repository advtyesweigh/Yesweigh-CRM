#!/usr/bin/env bash
# Deploy CRM + ADMS employee-import changes to the YesWeigh VPS.
# Requires SSH to 103.176.85.102 (port 22 was closed from this workstation on 2026-11-14).
set -euo pipefail

HOST="${DEPLOY_HOST:-root@103.176.85.102}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/vps_10317685102}"
SSH_PORT="${DEPLOY_SSH_PORT:-22318}"
SSH_OPTS=(-i "$SSH_KEY" -p "$SSH_PORT" -o IdentitiesOnly=yes)
RSYNC_SSH="ssh -i $SSH_KEY -p $SSH_PORT -o IdentitiesOnly=yes"
CRM_LOCAL="${CRM_LOCAL:-$(cd "$(dirname "$0")/.." && pwd)}"
ADMS_LOCAL="${ADMS_LOCAL:-$(cd "$CRM_LOCAL/../adms" && pwd)}"

echo "==> Sync ADMS → /opt/adms"
rsync -az --delete \
  --exclude '.venv' --exclude 'data' --exclude '.env' --exclude '__pycache__' \
  -e "$RSYNC_SSH" \
  "$ADMS_LOCAL/backend/" "$HOST:/opt/adms/backend/"
rsync -az \
  -e "$RSYNC_SSH" \
  "$ADMS_LOCAL/docs/" "$HOST:/opt/adms/docs/" \
  "$ADMS_LOCAL/deploy/" "$HOST:/opt/adms/deploy/" 2>/dev/null || true

ssh "${SSH_OPTS[@]}" "$HOST" 'bash -s' <<'EOF'
set -euo pipefail
# Ensure auto-sync stays off until X305 USERINFO verified
if [[ -f /opt/adms/.env ]] && ! grep -q '^AUTO_SYNC_USERS=' /opt/adms/.env; then
  echo 'AUTO_SYNC_USERS=false' >> /opt/adms/.env
fi
systemctl restart adms.service
curl -sS http://127.0.0.1:8081/health | head -c 400; echo
EOF

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

ssh "${SSH_OPTS[@]}" "$HOST" 'bash -s' <<'EOF'
set -euo pipefail
cd /opt/yesweigh-crm
docker compose build migrate api web
docker compose up -d migrate api web
docker compose ps
EOF

echo "==> Deploy finished"
