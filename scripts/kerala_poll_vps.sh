#!/usr/bin/env bash
# Run on VPS: poll Kerala dealer job until Completed/Failed
set -euo pipefail
PID=757936a8-fedf-43ec-b478-7a1e74e5fead
LOG=/tmp/kerala_poll.log
for _ in $(seq 1 120); do
  line=$(docker exec yesweigh-crm-api python -c "
import asyncio
from sqlalchemy import select
from app.db import SessionLocal
from app.models import DealerSearchJob
async def main():
    async with SessionLocal() as db:
        j = (await db.execute(select(DealerSearchJob).where(DealerSearchJob.public_id=='${PID}'))).scalar_one_or_none()
        if not j:
            print('MISSING')
            return
        print(j.status, j.completed_units, j.total_units)
asyncio.run(main())
")
  echo "$(date -Iseconds) $line" >> "$LOG"
  SYNC=/opt/yesweigh-crm/scripts/sync_dealer_finder_chat_card.py
  if [ -f "$SYNC" ]; then
    docker cp "$SYNC" yesweigh-crm-api:/app/scripts/sync_dealer_finder_chat_card.py 2>/dev/null || true
    docker exec -w /app yesweigh-crm-api python scripts/sync_dealer_finder_chat_card.py \
      --public-id "$PID" --run-id 139 >> "$LOG" 2>&1 || true
  fi
  st=$(echo "$line" | awk '{print $1}')
  if [ "$st" = "Completed" ]; then
    if [ -f "$SYNC" ]; then
      docker cp "$SYNC" yesweigh-crm-api:/app/scripts/sync_dealer_finder_chat_card.py 2>/dev/null || true
      docker exec -w /app yesweigh-crm-api python scripts/sync_dealer_finder_chat_card.py \
        --public-id "$PID" --run-id 139 >> "$LOG" 2>&1 || true
    fi
    echo DONE >> "$LOG"
    exit 0
  fi
  if [ "$st" = "Failed" ]; then
    echo FAILED >> "$LOG"
    exit 1
  fi
  sleep 60
done