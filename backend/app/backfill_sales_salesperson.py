"""Backfill zoho_sales.salesperson_name from Zoho Books Invoice CSV backup.

Usage (API container / one-off):
  python -m app.backfill_sales_salesperson /data/zoho_backup
"""
from __future__ import annotations

import asyncio
import csv
import sys
from pathlib import Path

from sqlalchemy import select, update

from app.db import SessionLocal
from app.models import ZohoSale


def _find_invoice_csvs(root: Path) -> list[Path]:
    found: list[Path] = []
    for pat in ("Invoice*.csv", "**/Invoice*.csv"):
        found.extend(sorted(root.glob(pat)))
    seen: set[Path] = set()
    out: list[Path] = []
    for p in found:
        rp = p.resolve()
        if rp not in seen and p.is_file():
            seen.add(rp)
            out.append(p)
    return out


def _salesperson(row: dict[str, str]) -> str:
    for key in ("Sales person", "Salesperson", "salesperson_name", "Sales Person"):
        v = (row.get(key) or "").strip()
        if v:
            return v
    return ""


async def backfill(root: Path) -> dict[str, int]:
    files = _find_invoice_csvs(root)
    by_id: dict[str, str] = {}
    csv_rows = 0
    for path in files:
        print(f"reading {path}…", flush=True)
        with path.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                csv_rows += 1
                iid = (row.get("Invoice ID") or "").strip()
                if not iid:
                    continue
                # Header-level Sales person repeats on every line item — last write wins
                by_id[iid] = _salesperson(row)

    stats = {
        "files": len(files),
        "csv_rows": csv_rows,
        "unique_invoices": len(by_id),
        "with_name": sum(1 for v in by_id.values() if v),
        "updated": 0,
        "missing_in_db": 0,
    }
    if not by_id:
        print("No invoice rows found in CSV")
        return stats

    async with SessionLocal() as db:
        ids = list(by_id.keys())
        chunk = 500
        for i in range(0, len(ids), chunk):
            batch = ids[i : i + chunk]
            existing = (
                await db.execute(select(ZohoSale.zoho_record_id).where(ZohoSale.zoho_record_id.in_(batch)))
            ).scalars().all()
            existing_set = set(existing)
            for iid in batch:
                if iid not in existing_set:
                    stats["missing_in_db"] += 1
                    continue
                await db.execute(
                    update(ZohoSale)
                    .where(ZohoSale.zoho_record_id == iid)
                    .values(salesperson_name=by_id[iid])
                )
                stats["updated"] += 1
            await db.commit()
            print(f"  committed {min(i + chunk, len(ids))}/{len(ids)}", flush=True)

    return stats


async def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/yesweigh-crm/data/zoho_backup")
    if not root.exists():
        for alt in (
            Path("/data/zoho_backup"),
            Path("/app/data/zoho_backup"),
            Path("/opt/yesweigh-crm/data/zoho_backup"),
        ):
            if alt.exists():
                root = alt
                break
    print(f"Backfilling salesperson_name from {root}")
    stats = await backfill(root)
    print("DONE", stats)


if __name__ == "__main__":
    asyncio.run(main())
