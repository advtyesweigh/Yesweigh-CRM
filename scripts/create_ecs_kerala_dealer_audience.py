#!/usr/bin/env python3
"""Ops: create static phone-list audience 'ECS Kerala dealer' if missing."""
from __future__ import annotations

import asyncio
import sys

from app.audiences import (
    count_audience_members,
    create_audience_row,
    filter_summary,
    filters_to_json,
    find_audience_by_name,
    fmt_audience_uid,
    normalize_filters,
    normalize_phone_list,
)
from app.db import SessionLocal

# Filtered ECS Kerala dealers (36 rows / 33 unique phones). Do not add removed ones.
ECS_KERALA_DEALER_PHONES = [
    "919947550783",  # UNITECH WEIGHING SYSTEM
    "918129216679",  # VIRTUE SYSTEM & TECHNOLOGIES
    "919895075360",  # SOUTHERN SCALE SERVICES
    "919846145868",  # ALUVA SCALES INC
    "917034014130",  # YESWEIGH SOLUTIONS LLP
    "919447291424",  # YELLOW SCALES
    "919447922223",  # METRO ENTERPRISES
    "919447291424",  # YELLOW SCALES & SYSTEMS (same phone)
    "919349388806",  # MASS TECHNOLOGY
    "919847460630",  # TRAAS TECH
    "919895339647",  # ROYAL SCALES
    "919745204910",  # DIGITAL SCALE
    "919656488821",  # LULU SCALES CALICUT
    "919447743162",  # S&S WEIGHING SYSTEM
    "919605955088",  # MICRO WEIGHING SYSTEMS
    "919562754666",  # SPIDER TECHNO SOFT
    "919744391168",  # GLOBAL DIGITAL SYSTEM
    "919744144510",  # Sonic Systems
    "919074192546",  # Eva Scales
    "919349193000",  # Minar scales -PKD
    "919847179343",  # MARVEL ELECTRONICS
    "918593875050",  # Deepa weighing services
    "919895362362",  # HAMDHA SCALES
    "919495977119",  # G Tech Automation
    "919349139513",  # Modern machines
    "919846845483",  # Thrass World Waynad
    "919656119575",  # MILLI MEASUREMENTS
    "919349183000",  # MINAR SCALES PALAKKAD
    "918086568201",  # GK electronics
    "919744391168",  # best weigh technologies (same as GLOBAL)
    "918089577835",  # JAAS ENTERPRISES
    "917012804569",  # Weigh India
    "919544152408",  # V K ELECTRONICS - THAMARASSERY
    "918086568201",  # GK SCALES (same as GK electronics)
    "919072676703",  # E WAY SOLUTIONS
    "919846500877",  # ALFA SYSTEMS-KODAKARA
]

NAME = "ECS Kerala dealer"
DESCRIPTION = "Kerala dealers who bought ECS (filtered list)"


async def main() -> None:
    phones = normalize_phone_list(ECS_KERALA_DEALER_PHONES)
    filt = normalize_filters({"phones": phones})
    async with SessionLocal() as db:
        row = await find_audience_by_name(db, NAME)
        if row:
            # Refresh phones if audience already exists but filters empty/outdated
            existing = normalize_filters(row.filters_json)
            if existing.get("phones") != filt.get("phones"):
                row.filters_json = filters_to_json(filt)
                row.description = DESCRIPTION
                count = await count_audience_members(db, row)
                row.last_count = count
                await db.commit()
                await db.refresh(row)
                print(
                    f"UPDATED id={row.id} uid={fmt_audience_uid(row.uid)} "
                    f"count={count} phones={len(phones)} summary={filter_summary(filt)}"
                )
                return
            count = await count_audience_members(db, row)
            print(
                f"EXISTS id={row.id} uid={fmt_audience_uid(row.uid)} "
                f"count={count} phones={len(phones)} summary={filter_summary(filt)}"
            )
            await db.commit()
            return
        row = await create_audience_row(
            db,
            name=NAME,
            source="dealers",
            description=DESCRIPTION,
            filters=filt,
            created_by=None,
        )
        await db.commit()
        await db.refresh(row)
        count = int(row.last_count or 0)
        print(
            f"CREATED id={row.id} uid={fmt_audience_uid(row.uid)} "
            f"count={count} source=dealers phones={len(phones)} "
            f"summary={filter_summary(filt)}"
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
