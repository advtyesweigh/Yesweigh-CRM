"""Reclassify order-cycle outline statuses from chat rules. No Gemini."""
from __future__ import annotations

import asyncio

from app.db import SessionLocal
from app.intel.order_cycle import reclassify_order_outlines


async def main_async() -> None:
    async with SessionLocal() as db:
        counts = await reclassify_order_outlines(db)
        if counts.get("updated"):
            await db.commit()
        else:
            await db.rollback()
        print(
            "order_cycle_backfill "
            f"sales_order={counts.get('sales_order', 0)} "
            f"waiting={counts.get('waiting', 0)} "
            f"paid={counts.get('paid', 0)} "
            f"updated={counts.get('updated', 0)}"
        )


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
