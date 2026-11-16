#!/usr/bin/env python3
"""One-shot: fetch full Zoho contacts and backfill dealers.city / zoho_dealers.billing_city.

Run inside the API container:
  docker exec yesweigh-crm-api python -m scripts.backfill_dealer_cities
or:
  docker exec yesweigh-crm-api python /app/../scripts/...  (prefer copying into image)

Preferred: copy this file into the backend package path, or:
  docker exec -i yesweigh-crm-api python - <<'PY'
  ...
"""
from __future__ import annotations

import asyncio
import sys


async def main() -> int:
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import Dealer
    from app import zoho_service as zs

    limit = None
    if len(sys.argv) > 1:
        limit = int(sys.argv[1])

    async with SessionLocal() as db:
        integ = await zs.get_integration(db)
        if not integ or not integ.zoho_org_id:
            print("Zoho not connected / no org", file=sys.stderr)
            return 1
        before = (
            await db.execute(
                select(func.count()).select_from(Dealer).where(Dealer.city != "")
            )
        ).scalar() or 0
        print(f"dealers with city before: {before}")
        stats = await zs.enrich_dealer_cities(db, integ, limit=limit)
        await db.commit()
        after = (
            await db.execute(
                select(func.count()).select_from(Dealer).where(Dealer.city != "")
            )
        ).scalar() or 0
        print("enrich stats:", stats)
        print(f"dealers with city after: {after} (+{after - before})")
        rows = (
            await db.execute(
                select(Dealer.name, Dealer.city, Dealer.state, Dealer.phone)
                .where(Dealer.city != "")
                .order_by(Dealer.name.asc())
                .limit(15)
            )
        ).all()
        print("sample:")
        for name, city, state, phone in rows:
            print(f"  {name} | {phone} | {city} | {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
