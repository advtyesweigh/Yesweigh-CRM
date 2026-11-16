#!/usr/bin/env bash
# Sync YesWeigh Firebase catalog into CRM Postgres (VPS).
# Credentials via env only — never commit passwords.
#
# Example (VPS):
#   set -a && source /opt/yesweigh-crm/.env && set +a
#   DOWNLOAD_IMAGES=0 ./scripts/sync_firebase_catalog.sh
#
# Cron uses scripts/run_catalog_sync_cron.sh instead.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -z "${FIREBASE_PASSWORD:-}" && -f "$ROOT/.env" ]]; then
  # Only FIREBASE_* / DOWNLOAD_* — avoid sourcing whole .env (unquoted spaces).
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      FIREBASE_*|DOWNLOAD_IMAGES=*|CATALOG_IMAGE_DIR=*|DRY_RUN=*)
        key="${line%%=*}"
        val="${line#*=}"
        if [[ "$val" == \"*\" && "$val" == *\" ]]; then
          val="${val:1:${#val}-2}"
        elif [[ "$val" == \'*\' && "$val" == *\' ]]; then
          val="${val:1:${#val}-2}"
        fi
        export "$key=$val"
        ;;
    esac
  done <"$ROOT/.env"
fi

if [[ -z "${FIREBASE_PASSWORD:-}" ]]; then
  echo "Set FIREBASE_PASSWORD (and FIREBASE_LOGIN_ID or FIREBASE_EMAIL)" >&2
  exit 1
fi

docker exec \
  -e FIREBASE_LOGIN_ID="${FIREBASE_LOGIN_ID:-}" \
  -e FIREBASE_EMAIL="${FIREBASE_EMAIL:-}" \
  -e FIREBASE_PASSWORD="${FIREBASE_PASSWORD}" \
  -e FIREBASE_API_KEY="${FIREBASE_API_KEY:-}" \
  -e FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-}" \
  -e DOWNLOAD_IMAGES="${DOWNLOAD_IMAGES:-0}" \
  -e CATALOG_IMAGE_DIR="${CATALOG_IMAGE_DIR:-/data/catalog-images}" \
  -e DRY_RUN="${DRY_RUN:-0}" \
  yesweigh-crm-api \
  python -m app.sync_firebase_catalog
