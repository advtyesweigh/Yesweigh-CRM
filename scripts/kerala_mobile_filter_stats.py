#!/usr/bin/env python3
"""Ops: before/after mobile-only counts for a Dealer Finder job."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
for _base in (_root / "backend", _root, _root.parent):
    if (_base / "app").is_dir():
        sys.path.insert(0, str(_base))
        break

from sqlalchemy import select

from app.db import SessionLocal
from app.dealer_finder.jobs import (
    dedupe_places_by_phone,
    place_is_eligible_new,
    place_counts_for_job,
)
from app.phone_util import normalize_in_mobile
from app.dealer_finder.phone_filters import count_phone_kinds_for_places
from app.models import DealerSearchJob, DealerSearchPlace
from app.phone_util import is_indian_mobile


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-id", required=True)
    args = parser.parse_args()

    async with SessionLocal() as db:
        job = (
            await db.execute(
                select(DealerSearchJob).where(
                    DealerSearchJob.public_id == args.public_id.strip()
                )
            )
        ).scalar_one_or_none()
        if not job:
            print(json.dumps({"error": "job not found"}))
            return 1

        places = (
            await db.execute(
                select(DealerSearchPlace)
                .where(DealerSearchPlace.job_id == job.id)
                .order_by(DealerSearchPlace.id.asc())
            )
        ).scalars().all()

        eligible_any_phone = [
            p
            for p in places
            if place_is_eligible_new(p) and (p.phone or "").strip()
        ]
        eligible_mobile = [p for p in eligible_any_phone if is_indian_mobile(p.phone or "")]
        # Before filter: unique by any normalized 10-digit key (legacy phone-NEW list).
        seen_any: set[str] = set()
        before_unique = 0
        for p in eligible_any_phone:
            key = normalize_in_mobile(p.phone or "") or (p.phone or "").strip()
            if key in seen_any:
                continue
            seen_any.add(key)
            before_unique += 1
        _, dupes_mobile = dedupe_places_by_phone(eligible_mobile)
        after_unique = len(eligible_mobile) - dupes_mobile
        kinds = count_phone_kinds_for_places(eligible_any_phone)

        counts = await place_counts_for_job(db, job.id)

        print(
            json.dumps(
                {
                    "public_id": job.public_id,
                    "state": job.state,
                    "status": job.status,
                    "before_unique_phone_new": before_unique,
                    "after_unique_mobile_new": after_unique,
                    "removed": before_unique - after_unique,
                    "discovery_any_phone_new": counts.get("discovery_any_phone_new"),
                    "discovery_mobile_new": counts.get("discovery_mobile_new"),
                    "importable_mobile_new": counts.get("importable_with_phone"),
                    "eligible_new_no_phone": sum(
                        1
                        for p in places
                        if place_is_eligible_new(p) and not (p.phone or "").strip()
                    ),
                    "phone_kinds_on_eligible_with_any_phone": kinds,
                    "detection": (
                        "mobile: 10-digit national starting 6–9 after stripping "
                        "+91 or trunk 0; landline: STD 0+area (e.g. 0484) or "
                        "10-digit not starting 6–9; shorter ≥6 digits → landline"
                    ),
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))