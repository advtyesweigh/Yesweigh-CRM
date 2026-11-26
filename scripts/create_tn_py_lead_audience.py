#!/usr/bin/env python3
"""Ops: create TN PY lead audience (Leads, Tamil Nadu|Puducherry) if missing."""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.audiences import (
    count_audience_members,
    create_audience_row,
    filters_to_json,
    find_audience_by_name,
    fmt_audience_uid,
    normalize_filters,
)
from app.db import async_session_factory


async def main() -> None:
    name = "TN PY lead"
    filt = normalize_filters({"state": "Tamil Nadu|Puducherry"})
    async with async_session_factory() as db:
        row = await find_audience_by_name(db, name)
        if row:
            count = await count_audience_members(db, row)
            print(
                f"EXISTS id={row.id} uid={fmt_audience_uid(row.uid)} "
                f"count={count} filters={row.filters_json}"
            )
            await db.commit()
            return
        row = await create_audience_row(
            db,
            name=name,
            source="leads",
            description="CRM Leads Tamil Nadu + Puducherry",
            filters=filt,
            created_by=None,
        )
        await db.flush()
        count = await count_audience_members(db, row)
        row.last_count = count
        row.filters_json = filters_to_json(filt)
        await db.commit()
        await db.refresh(row)
        print(
            f"CREATED id={row.id} uid={fmt_audience_uid(row.uid)} "
            f"count={count} source=leads state={filt.get('state')}"
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc