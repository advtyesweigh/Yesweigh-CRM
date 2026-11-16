#!/usr/bin/env bash
# Hourly weekday catalog sync (products + stock) for YesWeigh CRM.
# Invoked by /etc/cron.d/yesweigh-crm-catalog-sync — Mon–Fri Asia/Kolkata.
#
# Credentials: /opt/yesweigh-crm/.env (root-only; never git).
# Logs: /var/log/yesweigh-crm-catalog-sync.log
set -euo pipefail

CRM_ROOT="${CRM_ROOT:-/opt/yesweigh-crm}"
ENV_FILE="${CATALOG_SYNC_ENV:-$CRM_ROOT/.env}"
LOG_FILE="${CATALOG_SYNC_LOG:-/var/log/yesweigh-crm-catalog-sync.log}"
LOCK_FILE="${CATALOG_SYNC_LOCK:-/var/lock/yesweigh-crm-catalog-sync.lock}"
CONTAINER="${CATALOG_SYNC_CONTAINER:-yesweigh-crm-api}"

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date -Is) skip: previous sync still running" >>"$LOG_FILE"
  exit 0
fi

{
  echo "======== $(date -Is) catalog sync start ========"
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: missing env file $ENV_FILE" >&2
    exit 1
  fi
  # Load only catalog-sync keys — full .env may contain unquoted spaces
  # (e.g. APP_NAME=YesWeigh CRM) which break `source`.
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      FIREBASE_*|DOWNLOAD_IMAGES=*|CATALOG_IMAGE_DIR=*|DRY_RUN=*)
        key="${line%%=*}"
        val="${line#*=}"
        # strip optional surrounding quotes
        if [[ "$val" == \"*\" && "$val" == *\" ]]; then
          val="${val:1:${#val}-2}"
        elif [[ "$val" == \'*\' && "$val" == *\' ]]; then
          val="${val:1:${#val}-2}"
        fi
        export "$key=$val"
        ;;
    esac
  done <"$ENV_FILE"

  if [[ -z "${FIREBASE_PASSWORD:-}" ]]; then
    echo "ERROR: FIREBASE_PASSWORD not set in $ENV_FILE" >&2
    exit 1
  fi
  if [[ -z "${FIREBASE_LOGIN_ID:-}${FIREBASE_EMAIL:-}" ]]; then
    echo "ERROR: set FIREBASE_LOGIN_ID or FIREBASE_EMAIL in $ENV_FILE" >&2
    exit 1
  fi

  if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -qx true; then
    echo "ERROR: container $CONTAINER is not running" >&2
    exit 1
  fi

  # Always skip image downloads on cron (OOM risk on 512m API container).
  docker exec \
    -e FIREBASE_LOGIN_ID="${FIREBASE_LOGIN_ID:-}" \
    -e FIREBASE_EMAIL="${FIREBASE_EMAIL:-}" \
    -e FIREBASE_PASSWORD="${FIREBASE_PASSWORD}" \
    -e FIREBASE_API_KEY="${FIREBASE_API_KEY:-}" \
    -e FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-}" \
    -e DOWNLOAD_IMAGES=0 \
    -e CATALOG_IMAGE_DIR="${CATALOG_IMAGE_DIR:-/data/catalog-images}" \
    -e DRY_RUN="${DRY_RUN:-0}" \
    "$CONTAINER" \
    python -m app.sync_firebase_catalog

  echo "======== $(date -Is) catalog sync ok ========"
} >>"$LOG_FILE" 2>&1
