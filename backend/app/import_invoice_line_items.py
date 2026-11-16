"""Import Zoho Invoice CSV line items into zoho_invoice_line_items.

Usage (API container):
  python -m app.import_invoice_line_items /data/zoho_backup
Expects Invoice*.csv (or Invoice/Invoice*.csv) with Item Name / Item Total / Quantity columns.
Replaces all rows (full refresh) so re-runs stay idempotent.
"""
from __future__ import annotations

import asyncio
import csv
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import delete, text

from app.db import SessionLocal
from app.models import ZohoInvoiceLineItem
from app.product_categories import is_skipped_invoice_status, map_product_category


def _s(row: dict[str, str], *keys: str) -> str:
    for k in keys:
        if k in row and row[k] is not None:
            v = str(row[k]).strip()
            if v:
                return v
    return ""


def _dec(raw: str) -> Decimal | None:
    s = (raw or "").replace(",", "").replace("INR", "").strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _find_invoice_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for pat in ("Invoice*.csv", "invoice/*.csv", "Invoice/*.csv"):
        found.extend(sorted(root.glob(pat)))
        found.extend(sorted(root.glob(f"**/{pat}")))
    seen: set[Path] = set()
    out: list[Path] = []
    for p in found:
        rp = p.resolve()
        if rp not in seen and p.is_file():
            seen.add(rp)
            out.append(p)
    return out


def _iter_csv(path: Path) -> Iterable[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield {k: (v if v is not None else "") for k, v in row.items()}


def _row_to_item(row: dict[str, str], line_index: int) -> ZohoInvoiceLineItem | None:
    iid = _s(row, "Invoice ID")
    if not iid:
        return None
    status = _s(row, "Invoice Status")
    if is_skipped_invoice_status(status):
        return None
    item_name = _s(row, "Item Name")
    zoho_cat = _s(row, "Category Name")
    # Skip blank line rows with no name and no amount
    item_total = _dec(_s(row, "Item Total"))
    qty = _dec(_s(row, "Quantity"))
    if not item_name and (item_total is None or item_total == 0) and (qty is None or qty == 0):
        return None
    return ZohoInvoiceLineItem(
        zoho_invoice_id=iid,
        invoice_number=_s(row, "Invoice Number"),
        invoice_date=_s(row, "Invoice Date"),
        invoice_status=status.lower() or "",
        line_index=line_index,
        item_name=item_name[:300],
        item_desc=_s(row, "Item Desc"),
        zoho_category=zoho_cat[:160],
        product_category=map_product_category(item_name, zoho_cat),
        product_id=_s(row, "Product ID"),
        quantity=qty,
        item_total=item_total,
        currency_code=_s(row, "Currency Code") or "INR",
        customer_id=_s(row, "Customer ID"),
        customer_name=_s(row, "Customer Name")[:200],
        source="backup",
    )


async def import_line_items(root: Path) -> dict[str, Any]:
    files = _find_invoice_files(root)
    stats: dict[str, Any] = {
        "files": [p.name for p in files],
        "csv_rows": 0,
        "inserted": 0,
        "skipped_status": 0,
        "skipped_empty": 0,
    }
    if not files:
        raise SystemExit(f"No Invoice*.csv under {root}")

    batch: list[ZohoInvoiceLineItem] = []
    line_counters: dict[str, int] = {}

    async with SessionLocal() as db:
        await db.execute(delete(ZohoInvoiceLineItem))
        await db.commit()
        print("cleared zoho_invoice_line_items", flush=True)

        for path in files:
            print(f"reading {path.name}…", flush=True)
            for row in _iter_csv(path):
                stats["csv_rows"] += 1
                iid = _s(row, "Invoice ID")
                if not iid:
                    continue
                status = _s(row, "Invoice Status")
                if is_skipped_invoice_status(status):
                    stats["skipped_status"] += 1
                    continue
                idx = line_counters.get(iid, 0)
                line_counters[iid] = idx + 1
                item = _row_to_item(row, idx)
                if item is None:
                    stats["skipped_empty"] += 1
                    continue
                batch.append(item)
                if len(batch) >= 1000:
                    db.add_all(batch)
                    await db.commit()
                    stats["inserted"] += len(batch)
                    print(f"  inserted {stats['inserted']}…", flush=True)
                    batch = []

        if batch:
            db.add_all(batch)
            await db.commit()
            stats["inserted"] += len(batch)

        # Helpful indexes already on model; ensure year queries are snappy
        await db.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_zoho_line_items_date_cat
                ON zoho_invoice_line_items (invoice_date, product_category)
                """
            )
        )
        await db.commit()

    return stats


async def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/data/zoho_backup")
    print(f"importing invoice line items from {root}", flush=True)
    stats = await import_line_items(root)
    print("done", stats, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
