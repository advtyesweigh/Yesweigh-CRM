#!/usr/bin/env bash
# Deploy CRM + ADMS employee-import changes to the YesWeigh VPS.
# Requires SSH to 103.176.85.102 (port 22 was closed from this workstation on 2026-11-14).
set -euo pipefail

HOST="${DEPLOY_HOST:-root@103.176.85.102}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/vps_10317685102}"
CRM_LOCAL="${CRM_LOCAL:-$(cd "$(dirname "$0")/.." && pwd)}"
ADMS_LOCAL="${ADMS_LOCAL:-$(cd "$CRM_LOCAL/../adms" && pwd)}"

echo "==> Sync ADMS → /opt/adms"
rsync -az --delete \
  --exclude '.venv' --exclude 'data' --exclude '.env' --exclude '__pycache__' \
  -e "ssh -i $SSH_KEY -o IdentitiesOnly=yes" \
  "$ADMS_LOCAL/backend/" "$HOST:/opt/adms/backend/"
rsync -az \
  -e "ssh -i $SSH_KEY -o IdentitiesOnly=yes" \
  "$ADMS_LOCAL/docs/" "$HOST:/opt/adms/docs/" \
  "$ADMS_LOCAL/deploy/" "$HOST:/opt/adms/deploy/" 2>/dev/null || true

ssh -i "$SSH_KEY" -o IdentitiesOnly=yes "$HOST" 'bash -s' <<'EOF'
set -euo pipefail
# Ensure auto-sync stays off until X305 USERINFO verified
if [[ -f /opt/adms/.env ]] && ! grep -q '^AUTO_SYNC_USERS=' /opt/adms/.env; then
  echo 'AUTO_SYNC_USERS=false' >> /opt/adms/.env
fi
systemctl restart adms.service
curl -sS http://127.0.0.1:8081/health | head -c 400; echo
EOF

echo "==> Sync CRM → /opt/yesweigh-crm"
rsync -az \
  --exclude 'node_modules' --exclude '.env' --exclude '__pycache__' --exclude 'dist' \
  -e "ssh -i $SSH_KEY -o IdentitiesOnly=yes" \
  "$CRM_LOCAL/backend/" "$HOST:/opt/yesweigh-crm/backend/" \
  "$CRM_LOCAL/frontend/" "$HOST:/opt/yesweigh-crm/frontend/" \
  "$CRM_LOCAL/docker-compose.yml" "$HOST:/opt/yesweigh-crm/docker-compose.yml"

ssh -i "$SSH_KEY" -o IdentitiesOnly=yes "$HOST" 'bash -s' <<'EOF'
set -euo pipefail
cd /opt/yesweigh-crm
docker compose build api web
docker compose up -d migrate api web
docker compose ps
EOF

echo "==> Deploy finished"
