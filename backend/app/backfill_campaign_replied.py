"""Recompute campaign_runs.replied_count (inbound strictly after each outbound).

  PYTHONPATH=/app python -m app.backfill_campaign_replied
  PYTHONPATH=/app python -m app.backfill_campaign_replied 4 8
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.campaign_reports import backfill_campaign_run_replied_counts
from app.db import SessionLocal
from app.models import CampaignRun


async def main(argv: list[str]) -> None:
    run_ids = [int(x) for x in argv if x.isdigit()] or None
    async with SessionLocal() as db:
        if run_ids is None:
            # Default: all runs with successful sends (or still running).
            rows = (
                await db.execute(
                    select(CampaignRun.id).where(
                        (CampaignRun.success_count > 0)
                        | (CampaignRun.status.in_(("running", "queued")))
                    )
                )
            ).all()
            run_ids = [int(r[0]) for r in rows]
        changes = await backfill_campaign_run_replied_counts(db, run_ids=run_ids)
    for rid, old, new in changes:
        flag = " *" if old != new else ""
        print(f"run {rid}: replied {old} → {new}{flag}")
    print(f"done ({len(changes)} runs)")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
