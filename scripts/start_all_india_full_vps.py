#!/usr/bin/env python3
"""Ops helper — run inside yesweigh-crm-api container on VPS.

Enables all-India (after pilot), optional Jaipur pilot import, starts full-country run.
"""
from __future__ import annotations

import asyncio
import json
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.dealer_finder.all_india import full_india_state_queue
from app.dealer_finder.all_india_jobs import create_full_run, run_public
from app.dealer_finder.jobs import import_eligible_places, wake_dealer_finder_worker
from app.dealer_finder.settings_svc import get_or_create_settings, patch_settings, usage_totals
from app.dealer_finder.places_pricing import estimated_billable_usd
from app.models import DealerAllIndiaRun, DealerSearchJob, User


async def main() -> int:
    async with SessionLocal() as db:
        admin = (
            await db.execute(select(User).where(User.email == "admin@yesweigh.in"))
        ).scalar_one()
        settings = await get_or_create_settings(db)
        usage = await usage_totals(db)
        billable = float(usage.get("month_billable_estimated_usd") or 0)
        cap = float(settings.max_monthly_spend_usd or 100)
        print(
            "GATES",
            json.dumps(
                {
                    "pilot_district_completed": bool(settings.pilot_district_completed),
                    "all_india_enabled": bool(settings.all_india_enabled),
                    "month_billable_usd": round(billable, 2),
                    "max_monthly_spend_usd": cap,
                }
            ),
        )
        if not settings.pilot_district_completed:
            print("ERROR pilot not complete", file=sys.stderr)
            return 2
        if not settings.all_india_enabled:
            await patch_settings(db, admin, {"all_india_enabled": True})
            print("SET all_india_enabled=true")

        active = (
            await db.execute(
                select(DealerAllIndiaRun).where(
                    DealerAllIndiaRun.status.in_(["Pending", "Running", "Paused"])
                )
            )
        ).scalar_one_or_none()
        if active:
            print(
                "ACTIVE_RUN",
                active.public_id,
                active.mode,
                active.status,
                active.active_job_public_id,
            )
            await db.commit()
            return 0

        pilot_job = (
            await db.execute(
                select(DealerSearchJob)
                .where(
                    DealerSearchJob.state == "Rajasthan",
                    DealerSearchJob.district_filter.ilike("%Jaipur%"),
                    DealerSearchJob.status == "Completed",
                )
                .order_by(DealerSearchJob.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if pilot_job:
            imp = await import_eligible_places(
                db,
                user=admin,
                job_id=pilot_job.id,
                with_phone_only=True,
            )
            print(
                "PILOT_IMPORT",
                pilot_job.public_id,
                json.dumps(
                    {
                        k: imp[k]
                        for k in (
                            "inserted",
                            "skipped_duplicates",
                            "skipped_no_phone",
                            "skipped_job_phone_dupes",
                        )
                        if k in imp
                    }
                ),
            )

        queue = full_india_state_queue()
        first_state = queue[0] if queue else ""
        run = await create_full_run(db, user=admin)
        await db.commit()
        wake_dealer_finder_worker()
        pub = run_public(run)
        print("FULL_STARTED", run.public_id)
        print("FIRST_STATE", first_state)
        print("RUN", json.dumps(pub, default=str))
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))