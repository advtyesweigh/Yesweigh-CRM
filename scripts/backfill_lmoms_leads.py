#!/usr/bin/env python3
"""Backfill LMOMS lead fields from CSVs: district, state=Kerala, name/company, source=LMOMS.

Run inside API container or with DATABASE_URL. Does not insert new firms; updates existing.
Also collapses true duplicates (phone/email/firm).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Lead

LMOMS_SOURCE = "LMOMS"
KERALA_STATE = "Kerala"

CSV_FILES = [
    "LMOMS-Kerala-repairer-approved-list.csv",
    "LMOMS-Kerala-dealer-approved-list.csv",
    "LMOMS-Kerala-manufacturer-approved-list.csv",
    "LMOMS-Kerala-manufacturers-dealers-repairers.csv",
]


def norm_text(v: str | None) -> str:
    return re.sub(r"\s+", " ", (v or "").strip())


def firm_key(v: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", "", norm_text(v).upper())


def phone_key(v: str | None) -> str:
    d = re.sub(r"\D+", "", v or "")
    return d[-10:] if len(d) >= 10 else d


def load_csv_map(csv_dir: Path) -> dict[str, dict[str, str]]:
    """firm_key -> {firm, district, phone, city} (last non-empty wins; districts prefer first)."""
    out: dict[str, dict[str, str]] = {}
    for name in CSV_FILES:
        path = csv_dir / name
        if not path.exists():
            continue
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                firm = norm_text(row.get("firm_name"))
                if not firm:
                    continue
                k = firm_key(firm)
                cur = out.get(k) or {"firm": firm, "district": "", "phone": "", "city": ""}
                dist = norm_text(row.get("district"))
                phone = norm_text(row.get("phone") or row.get("mobile") or row.get("contact"))
                city = norm_text(row.get("city") or row.get("town"))
                if dist and not cur["district"]:
                    cur["district"] = dist
                if phone and not cur["phone"]:
                    cur["phone"] = phone
                if city and not cur["city"]:
                    cur["city"] = city
                cur["firm"] = firm
                out[k] = cur
    return out


def dedupe_key(r: Lead) -> str:
    p = phone_key(r.phone)
    if p:
        return f"phone:{p}"
    e = (r.email or "").strip().lower()
    if e:
        return f"email:{e}"
    firm = firm_key(r.company) or firm_key(r.name)
    return f"firm:{firm}"


async def run(csv_dir: Path) -> None:
    csv_map = load_csv_map(csv_dir)
    print(f"csv_firms={len(csv_map)}")

    async with SessionLocal() as db:
        rows = list((await db.execute(select(Lead).order_by(Lead.id))).scalars().all())
        before = len(rows)
        print(f"before={before}")

        # Dedupe
        buckets: dict[str, list[Lead]] = defaultdict(list)
        for r in rows:
            buckets[dedupe_key(r)].append(r)
        removed = 0
        for group in buckets.values():
            keep = group[0]
            for other in group[1:]:
                if other.notes and other.notes not in (keep.notes or ""):
                    keep.notes = ((keep.notes or "") + " | " + other.notes)[:2000]
                if other.phone and not keep.phone:
                    keep.phone = other.phone
                if other.email and not keep.email:
                    keep.email = other.email
                if other.district and not keep.district:
                    keep.district = other.district
                if other.city and not keep.city:
                    keep.city = other.city
                if other.state and not keep.state:
                    keep.state = other.state
                await db.delete(other)
                removed += 1

        await db.flush()
        remaining = list((await db.execute(select(Lead))).scalars().all())

        name_fixed = 0
        geo_fixed = 0
        source_fixed = 0
        phone_fixed = 0
        matched = 0

        for r in remaining:
            firm = norm_text(r.company) or norm_text(r.name)
            k = firm_key(firm)
            info = csv_map.get(k)

            # Source
            src_u = (r.source or "").upper()
            if "LMOMS" in src_u or src_u.strip() == "LMOS" or (info is not None):
                if r.source != LMOMS_SOURCE:
                    r.source = LMOMS_SOURCE
                    source_fixed += 1

            # Name for list column: firm name; company kept as firm for search
            if info or "LMOMS" in src_u or src_u.strip() == "LMOS":
                if firm:
                    if r.name != firm or r.company != firm:
                        r.name = firm[:160]
                        r.company = firm[:160]
                        name_fixed += 1
                if not (r.state or "").strip():
                    r.state = KERALA_STATE
                    geo_fixed += 1
                elif r.state.strip().lower() != "kerala" and info:
                    # leave non-Kerala manual edits; only set if empty above
                    pass

            if info:
                matched += 1
                if info["district"] and r.district != info["district"]:
                    r.district = info["district"][:80]
                    geo_fixed += 1
                if info["city"] and not (r.city or "").strip():
                    r.city = info["city"][:80]
                    geo_fixed += 1
                if info["phone"] and not (r.phone or "").strip():
                    r.phone = info["phone"][:40]
                    phone_fixed += 1
                if not (r.state or "").strip():
                    r.state = KERALA_STATE
                    geo_fixed += 1
            else:
                # Parse district from notes "District: X"
                m = re.search(r"District:\s*([^;|]+)", r.notes or "", re.I)
                if m and not (r.district or "").strip():
                    r.district = m.group(1).strip()[:80]
                    geo_fixed += 1
                if ("LMOMS" in src_u or src_u.strip() == "LMOS") and not (r.state or "").strip():
                    r.state = KERALA_STATE
                    geo_fixed += 1

        await db.commit()
        after = list((await db.execute(select(Lead))).scalars().all())
        lmoms = sum(1 for r in after if r.source == LMOMS_SOURCE)
        with_district = sum(1 for r in after if (r.district or "").strip())
        with_state = sum(1 for r in after if (r.state or "").strip())
        with_phone = sum(1 for r in after if (r.phone or "").strip())
        print(f"duplicates_removed={removed}")
        print(f"after={len(after)}")
        print(f"csv_matched={matched}")
        print(f"name_company_fixed={name_fixed}")
        print(f"geo_fixed_ops={geo_fixed}")
        print(f"phone_fixed={phone_fixed}")
        print(f"source_fixed={source_fixed}")
        print(f"source_LMOMS={lmoms}")
        print(f"with_district={with_district}")
        print(f"with_state={with_state}")
        print(f"with_phone={with_phone}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--csv-dir", required=True)
    args = p.parse_args()
    asyncio.run(run(Path(args.csv_dir)))


if __name__ == "__main__":
    main()
