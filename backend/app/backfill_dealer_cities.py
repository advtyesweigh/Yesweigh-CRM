"""CLI: fetch full Zoho contacts into SQL (raw_json + dealers columns).

  docker exec yesweigh-crm-api python -m app.backfill_dealer_cities
  docker exec yesweigh-crm-api python -m app.backfill_dealer_cities 100
  docker exec yesweigh-crm-api python -m app.backfill_dealer_cities --force-all
"""
from __future__ import annotations

import asyncio
import sys


async def main() -> int:
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import Dealer, ZohoDealer
    from app import zoho_service as zs
    from app.zoho_service import _raw_has_detail_payload

    limit = None
    force_all = False
    for arg in sys.argv[1:]:
        if arg in {"--force-all", "-f"}:
            force_all = True
        else:
            limit = int(arg)

    async with SessionLocal() as db:
        integ = await zs.get_integration(db)
        if not integ or not integ.zoho_org_id:
            print("Zoho not connected / no org", file=sys.stderr)
            return 1
        before_city = (
            await db.execute(
                select(func.count()).select_from(Dealer).where(Dealer.city != "")
            )
        ).scalar() or 0
        all_z = (await db.execute(select(ZohoDealer.raw_json))).scalars().all()
        before_detail = sum(1 for r in all_z if _raw_has_detail_payload(r or ""))
        print(
            f"before: dealers_with_city={before_city} zoho_with_detail_payload={before_detail}/{len(all_z)}",
            flush=True,
        )
        stats = await zs.enrich_dealer_cities(
            db, integ, limit=limit, force_all=force_all
        )
        await db.commit()
        after_city = (
            await db.execute(
                select(func.count()).select_from(Dealer).where(Dealer.city != "")
            )
        ).scalar() or 0
        all_z2 = (await db.execute(select(ZohoDealer.raw_json))).scalars().all()
        after_detail = sum(1 for r in all_z2 if _raw_has_detail_payload(r or ""))
        print("enrich stats:", stats, flush=True)
        print(
            f"after: dealers_with_city={after_city} (+{after_city - before_city}) "
            f"zoho_with_detail_payload={after_detail}/{len(all_z2)}",
            flush=True,
        )
        rows = (
            await db.execute(
                select(Dealer.name, Dealer.city, Dealer.state, Dealer.phone)
                .where(Dealer.city != "")
                .order_by(Dealer.name.asc())
                .limit(12)
            )
        ).all()
        print("sample:", flush=True)
        for name, city, state, phone in rows:
            print(f"  {name} | {phone} | {city} | {state}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
