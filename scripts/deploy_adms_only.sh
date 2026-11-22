#!/usr/bin/env bash
# Deploy ADMS gateway only (no CRM rebuild, no Free443, no Caddy/Mailcow touch).
set -euo pipefail

HOST="${DEPLOY_HOST:-root@103.176.85.102}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/vps_10317685102}"
SSH_PORT="${DEPLOY_SSH_PORT:-22318}"
SSH_OPTS=(-i "$SSH_KEY" -p "$SSH_PORT" -o IdentitiesOnly=yes)
RSYNC_SSH="ssh -i $SSH_KEY -p $SSH_PORT -o IdentitiesOnly=yes"
CRM_LOCAL="${CRM_LOCAL:-$(cd "$(dirname "$0")/.." && pwd)}"
ADMS_LOCAL="${ADMS_LOCAL:-$(cd "$CRM_LOCAL/../adms" && pwd)}"

echo "==> Sync ADMS backend → /opt/adms/backend"
rsync -az --delete \
  --exclude '.venv' --exclude 'data' --exclude '.env' --exclude '__pycache__' --exclude '*.pyc' --exclude '*.bak*' \
  -e "$RSYNC_SSH" \
  "$ADMS_LOCAL/backend/" "$HOST:/opt/adms/backend/"

echo "==> Sync ADMS docs"
rsync -az -e "$RSYNC_SSH" \
  "$ADMS_LOCAL/docs/" "$HOST:/opt/adms/docs/" || true

ssh "${SSH_OPTS[@]}" "$HOST" 'bash -s' <<'EOF'
set -euo pipefail
# Ensure resilience env knobs exist (do not overwrite custom values)
if [[ -f /opt/adms/.env ]]; then
  grep -q '^SENT_COMMAND_EXPIRE_MINUTES=' /opt/adms/.env || echo 'SENT_COMMAND_EXPIRE_MINUTES=30' >> /opt/adms/.env
  grep -q '^RECONNECT_SILENT_ESCALATE_AFTER=' /opt/adms/.env || echo 'RECONNECT_SILENT_ESCALATE_AFTER=3' >> /opt/adms/.env
  grep -q '^RECONNECT_SILENT_BACKOFF_SECONDS=' /opt/adms/.env || echo 'RECONNECT_SILENT_BACKOFF_SECONDS=900' >> /opt/adms/.env
  grep -q '^OFFLINE_ALERT_AFTER_SECONDS=' /opt/adms/.env || echo 'OFFLINE_ALERT_AFTER_SECONDS=300' >> /opt/adms/.env
fi
# Expire stuck sent commands immediately (idempotent)
sqlite3 /opt/adms/data/adms.db \
  "UPDATE device_commands SET status='timeout', completed_at=datetime('now'), result='expired-unacked-predeploy'
   WHERE status='sent' AND (sent_at IS NULL OR sent_at < datetime('now','-30 minutes'));" || true
# One-shot: advance stale OpStamp so reconnect does not re-flood OPERLOG.
# Device last posted OpStamp=864212264 while DB stayed at 863882227 (Stamp= bug).
sqlite3 /opt/adms/data/adms.db \
  "UPDATE devices SET stamp_operlog='864212264', updated_at=datetime('now')
   WHERE serial_number='0IN7010596123100370'
     AND CAST(COALESCE(NULLIF(stamp_operlog,''),'0') AS INTEGER) < 864212264;" || true
systemctl restart adms.service
sleep 2
systemctl is-active adms.service
curl -sS http://127.0.0.1:8081/health | python3 -c 'import sys,json; d=json.load(sys.stdin); print("ok",d.get("ok"),"offline",d.get("device_offline_seconds"),"silent",d.get("silent_devices"),"handshake",d.get("handshake"),"resilience",d.get("resilience"))'
sqlite3 /opt/adms/data/adms.db "SELECT serial_number,stamp_attlog,stamp_operlog,last_heartbeat_at FROM devices WHERE serial_number!='TEST';"
# Confirm unrelated listeners still up
ss -lntp | grep -E ':3389|:443|:25|:8081' || true
EOF

echo "==> ADMS-only deploy finished"
