#!/usr/bin/env bash
# Deploy CRM (including AI Agent Phase 1) to VPS — does NOT restart ADMS/Mailcow/Evolution.
set -euo pipefail

HOST="${DEPLOY_HOST:-root@103.176.85.102}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/vps_10317685102}"
SSH_PORT="${DEPLOY_SSH_PORT:-22318}"
SSH_OPTS=(-i "$SSH_KEY" -p "$SSH_PORT" -o IdentitiesOnly=yes)
RSYNC_SSH="ssh -i $SSH_KEY -p $SSH_PORT -o IdentitiesOnly=yes"
CRM_LOCAL="${CRM_LOCAL:-$(cd "$(dirname "$0")/.." && pwd)}"

# One deploy at a time (parallel agents / overlapping rsync+compose caused Created containers).
LOCAL_LOCK_DIR="${TMPDIR:-/tmp}/yesweigh-crm-deploy.lockdir"
_local_lock_release() {
  if [[ -n "${LOCAL_LOCK_GOT:-}" ]]; then
    rmdir "$LOCAL_LOCK_DIR" 2>/dev/null || true
  fi
}
if command -v flock >/dev/null 2>&1; then
  exec 9>"${TMPDIR:-/tmp}/yesweigh-crm-deploy.lock"
  if ! flock -n 9; then
    echo "Another yesweigh-crm deploy is already running locally (flock). Exit." >&2
    exit 1
  fi
else
  if ! mkdir "$LOCAL_LOCK_DIR" 2>/dev/null; then
    echo "Another yesweigh-crm deploy is already running locally ($LOCAL_LOCK_DIR). Exit." >&2
    exit 1
  fi
  LOCAL_LOCK_GOT=1
  trap _local_lock_release EXIT
fi

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
rsync -az -e "$RSYNC_SSH" \
  "$CRM_LOCAL/deployment/" "$HOST:/opt/yesweigh-crm/deployment/"
rsync -az -e "$RSYNC_SSH" \
  "$CRM_LOCAL/scripts/deploy_crm_ai.sh" "$HOST:/opt/yesweigh-crm/scripts/deploy_crm_ai.sh"

ssh "${SSH_OPTS[@]}" "$HOST" 'bash -s' <<'EOF'
set -euo pipefail
exec 8>/var/lock/yesweigh-crm-deploy.lock
if ! flock -n 8; then
  echo "Another CRM deploy is running on VPS (flock /var/lock/yesweigh-crm-deploy.lock). Exit." >&2
  exit 1
fi

ensure_caddy_crm_api_split() {
  local cf=/opt/caddy/Caddyfile
  local snippet=/opt/yesweigh-crm/deployment/vps-caddy-crm.snippet
  [[ -f "$cf" ]] || { echo "WARN: no $cf — skip Caddy CRM hardening"; return 0; }
  if grep -q 'crm.yesweigh.in' "$cf" && grep -A12 'crm.yesweigh.in' "$cf" | grep -q '127.0.0.1:8100'; then
    echo "Caddy crm.yesweigh.in already routes /api → 8100"
    return 0
  fi
  cp -a "$cf" "${cf}.bak.crm-api.$(date +%Y%m%d%H%M%S)"
  if [[ -f "$snippet" ]]; then
    python3 - "$cf" "$snippet" <<'PY'
import pathlib, re, sys
cf, sn = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]).read_text()
text = cf.read_text()
block = sn.strip() + "\n"
pat = re.compile(
    r"crm\.yesweigh\.in\s*\{[^}]*\}\s*\n",
    re.M | re.S,
)
if not pat.search(text):
    print("WARN: crm.yesweigh.in block not found in Caddyfile", file=sys.stderr)
    sys.exit(0)
new = pat.sub(block + "\n", text, count=1)
cf.write_text(new)
print("Updated Caddy CRM block (/api→8100, else→8101)")
PY
    if docker ps --format '{{.Names}}' | grep -qx caddy; then
      docker exec caddy caddy validate --config /etc/caddy/Caddyfile
      docker exec caddy caddy reload --config /etc/caddy/Caddyfile
      echo "Caddy reloaded"
    fi
  fi
}

ensure_caddy_crm_api_split

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
# If GEMINI_API_KEY was wiped (empty) by a recreate, restore from newest .env.bak* that still has it.
if grep -q '^GEMINI_API_KEY=$' .env || grep -qE '^GEMINI_API_KEY=[[:space:]]*$' .env; then
  restored=""
  for bak in $(ls -t .env.bak* 2>/dev/null || true); do
    val=$(grep '^GEMINI_API_KEY=' "$bak" 2>/dev/null | head -1 | cut -d= -f2- || true)
    if [ -n "${val}" ]; then
      # Escape for sed replacement safely via temp rewrite
      awk -v v="$val" 'BEGIN{done=0} /^GEMINI_API_KEY=/{print "GEMINI_API_KEY=" v; done=1; next} {print} END{if(!done) print "GEMINI_API_KEY=" v}' .env > .env.tmp.gemini && mv .env.tmp.gemini .env
      chmod 600 .env
      restored=1
      echo "Restored GEMINI_API_KEY from $bak (len=${#val})"
      break
    fi
  done
  if [ -z "$restored" ]; then
    echo "WARN: GEMINI_API_KEY empty and no backup found — AI Agent will show Not configured"
  fi
fi
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
ensure_kv AI_RUN_TIMEOUT_S 900
ensure_kv AI_RUN_STALE_S 1200
# Bump legacy short timeouts so WhatsApp batch (delay between sends) can finish
if grep -qE '^AI_RUN_TIMEOUT_S=(180|300|600)$' .env; then
  sed -i 's/^AI_RUN_TIMEOUT_S=.*/AI_RUN_TIMEOUT_S=900/' .env
  echo "Updated AI_RUN_TIMEOUT_S → 900"
fi
if grep -qE '^AI_RUN_STALE_S=(600|900)$' .env; then
  sed -i 's/^AI_RUN_STALE_S=.*/AI_RUN_STALE_S=1200/' .env
  echo "Updated AI_RUN_STALE_S → 1200"
fi
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
# Google Places Text Search (AI dealer discovery) — never invent a key
ensure_kv GOOGLE_MAPS_API_KEY ""
ensure_kv GOOGLE_PLACES_API_KEY ""
# Restore Google Maps/Places key from newest .env.bak* if empty
restore_google_key() {
  local key="$1"
  if grep -qE "^${key}=[[:space:]]*$" .env || grep -q "^${key}=$" .env; then
    for bak in $(ls -t .env.bak* 2>/dev/null || true); do
      val=$(grep "^${key}=" "$bak" 2>/dev/null | head -1 | cut -d= -f2- || true)
      if [ -n "${val}" ]; then
        awk -v k="$key" -v v="$val" 'BEGIN{done=0} $0 ~ ("^" k "="){print k "=" v; done=1; next} {print} END{if(!done) print k "=" v}' .env > .env.tmp.google && mv .env.tmp.google .env
        chmod 600 .env
        echo "Restored ${key} from $bak (len=${#val})"
        return 0
      fi
    done
    echo "WARN: ${key} empty and no backup found — Google dealer discovery will fail"
  fi
}
restore_google_key GOOGLE_MAPS_API_KEY
restore_google_key GOOGLE_PLACES_API_KEY
# If Maps empty but Places has a value (or vice versa), leave as-is — tools accept either.
# Voice notes: STT then translate displayed transcript to Malayalam
ensure_kv SARVAM_STT_MODE transcribe
ensure_kv SARVAM_TRANSCRIPT_TARGET_LANGUAGE ml-IN
ensure_kv SARVAM_TRANSLATE_MODEL mayura:v1

# Force web rebuild: BuildKit can keep a stale frontend layer when only TS/CSS change.
docker compose build migrate api
docker compose build --no-cache web

# Retry up -d: concurrent recreates (parallel agents) can race with
# "Error response from daemon: No such container: <id>".
up_ok=0
for attempt in 1 2 3; do
  if docker compose up -d migrate api && docker compose up -d api && docker compose up -d web; then
    up_ok=1
    break
  fi
  echo "compose up failed (attempt ${attempt}); waiting and retrying…"
  sleep 3
done
if [[ "$up_ok" -ne 1 ]]; then
  echo "compose up failed after retries" >&2
  exit 1
fi

# Containers stuck in Created (interrupted deploy) — force start.
for svc in api web; do
  cid="$(docker compose ps -q "$svc" 2>/dev/null || true)"
  if [[ -n "$cid" ]]; then
    state="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || true)"
    if [[ "$state" == "created" ]]; then
      echo "WARN: $svc was Created — docker compose up -d --force-recreate $svc"
      docker compose up -d --force-recreate "$svc"
    fi
  fi
done

wait_health() {
  local url="$1" label="$2" tries="${3:-30}"
  local i=1
  while [[ "$i" -le "$tries" ]]; do
    if curl -sfS --max-time 3 "$url" >/dev/null; then
      echo "Health OK: $label ($url)"
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  echo "Health FAILED: $label ($url)" >&2
  return 1
}

wait_health "http://127.0.0.1:8100/api/health" "api:8100" 45
wait_health "http://127.0.0.1:8101/api/health" "web:8101" 45
wait_health "https://crm.yesweigh.in/api/health" "public HTTPS" 20

docker compose ps
curl -sS http://127.0.0.1:8100/api/health
echo
curl -sS http://127.0.0.1:8101/api/health
echo
# One-time ffmpeg poster generation for campaign videos missing covers (non-blocking for sends).
docker compose exec -T api python - <<'PY' || true
import asyncio
from sqlalchemy import select
from app.db import SessionLocal
from app.models import CampaignMedia
from app.campaigns import (
    _cover_path,
    _media_kind_of,
    _media_path,
    _set_cover_from_first_frame,
    _set_cover_from_image_file,
)

async def main() -> None:
    async with SessionLocal() as db:
        rows = (await db.execute(select(CampaignMedia).order_by(CampaignMedia.id.asc()))).scalars().all()
        generated = 0
        for row in rows:
            if _cover_path(row):
                continue
            kind = _media_kind_of(row)
            ok = False
            if kind == "video":
                ok = _set_cover_from_first_frame(row, force=False)
            elif kind == "image":
                src = _media_path(row)
                if src:
                    ok = _set_cover_from_image_file(row, src)
            if ok:
                generated += 1
        if generated:
            await db.commit()
        print(f"backfill_media_covers generated={generated} total={len(rows)}")

asyncio.run(main())
PY
docker compose exec -T api python -m app.backfill_awaiting_reply || true
docker compose exec -T api python -m app.intel.enquiry_backfill || true
docker compose exec -T api python -m app.intel.order_cycle_backfill || true
docker compose exec -T api python -m app.backfill_service_meta --limit 4000 || true

# ADMS independence guard — CRM must never restart/block :8081.
# Verify only; do not systemctl touch adms.service from this script.
if curl -sfS --max-time 3 http://127.0.0.1:8081/health 2>/dev/null | grep -q '"ok"[[:space:]]*:[[:space:]]*true'; then
  echo "ADMS :8081 health OK (untouched by CRM deploy)"
else
  echo "WARN: ADMS :8081 health check failed after CRM deploy — investigate adms.service separately (do not Free443 / Mailcow)." >&2
fi
ss -lntp 2>/dev/null | grep -F ':8081' || echo "WARN: nothing listening on :8081" >&2
EOF

echo "==> CRM AI deploy finished (ADMS/Mailcow/Evolution untouched)"
