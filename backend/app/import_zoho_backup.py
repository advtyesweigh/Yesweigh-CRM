"""Import Zoho Books CSV backup into zoho_sales / zoho_credit_notes / dealers.

Usage (inside API container):
  python -m app.import_zoho_backup /data/zoho_backup
Expects:
  Invoice00.csv, Invoice01.csv (or Invoice/*.csv)
  Credit_Note.csv
  Contacts.csv (optional — fills dealer city/address fields)
"""
from __future__ import annotations

import asyncio
import csv
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select

from app.db import SessionLocal
from app.dealer_exclusions import is_excluded_contact
from app.models import Dealer, ZohoCreditNote, ZohoDealer, ZohoSale
from app.phone_util import normalize_in_mobile
from app.state_normalize import canon_state
from app.zoho_service import _dec, upsert_credit_note, upsert_sale


def _s(row: dict[str, str], *keys: str) -> str:
    for k in keys:
        if k in row and row[k] is not None:
            v = str(row[k]).strip()
            if v:
                return v
    return ""


def _find_files(root: Path, patterns: list[str]) -> list[Path]:
    found: list[Path] = []
    for pat in patterns:
        found.extend(sorted(root.glob(pat)))
        found.extend(sorted(root.glob(f"**/{pat}")))
    # de-dupe preserving order
    seen: set[Path] = set()
    out: list[Path] = []
    for p in found:
        rp = p.resolve()
        if rp not in seen and p.is_file():
            seen.add(rp)
            out.append(p)
    return out


def _iter_csv_dicts(path: Path) -> Iterable[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield {k: (v if v is not None else "") for k, v in row.items()}


def invoice_row_to_payload(row: dict[str, str]) -> dict[str, Any] | None:
    iid = _s(row, "Invoice ID")
    if not iid:
        return None
    return {
        "invoice_id": iid,
        "invoice_number": _s(row, "Invoice Number"),
        "date": _s(row, "Invoice Date"),
        "customer_id": _s(row, "Customer ID"),
        "customer_name": _s(row, "Customer Name"),
        "status": _s(row, "Invoice Status").lower() or "draft",
        "currency_code": _s(row, "Currency Code") or "INR",
        "total": _s(row, "Total") or None,
        # Zoho Books CSV: SubTotal is tax-exclusive; Total includes GST
        "sub_total": _s(row, "SubTotal", "Sub Total", "sub_total") or None,
        "balance": _s(row, "Balance") or None,
        "salesperson_name": _s(row, "Sales person", "Salesperson", "salesperson_name") or None,
        "gst_no": _s(row, "GST Identification Number (GSTIN)"),
        "place_of_supply": _s(row, "Place of Supply", "Place of Supply(With State Code)"),
        "due_date": _s(row, "Due Date"),
        "branch_name": _s(row, "Branch Name"),
        "notes": _s(row, "Notes"),
        # Keep a compact line-item snapshot from this CSV row
        "line_item": {
            "item_name": _s(row, "Item Name"),
            "item_desc": _s(row, "Item Desc"),
            "quantity": _s(row, "Quantity"),
            "item_total": _s(row, "Item Total"),
            "product_id": _s(row, "Product ID"),
        },
        "_backup_csv": True,
    }


def credit_row_to_payload(row: dict[str, str]) -> dict[str, Any] | None:
    cid = _s(row, "CreditNotes ID")
    if not cid:
        return None
    return {
        "creditnote_id": cid,
        "creditnote_number": _s(row, "Credit Note Number"),
        "date": _s(row, "Credit Note Date"),
        "customer_id": _s(row, "Customer ID"),
        "customer_name": _s(row, "Customer Name"),
        "status": _s(row, "Credit Note Status").lower() or "",
        "currency_code": _s(row, "Currency Code") or "INR",
        "total": _s(row, "Total") or None,
        "balance": _s(row, "Balance") or None,
        "invoice_id": "",  # associated invoice number is present, not id
        "reference_invoice_number": _s(row, "Associated Invoice Number"),
        "billing_city": _s(row, "Billing City"),
        "billing_state": _s(row, "Billing State"),
        "_backup_csv": True,
    }


def contact_row_to_fields(row: dict[str, str]) -> dict[str, Any] | None:
    cid = _s(row, "Contact ID")
    if not cid:
        return None
    name = _s(row, "Contact Name", "Display Name", "Company Name")
    company = _s(row, "Company Name", "Display Name")
    billing_city = _s(row, "Billing City")
    shipping_city = _s(row, "Shipping City")
    city = billing_city or shipping_city
    state = _s(row, "Billing State") or _s(row, "Shipping State") or _s(
        row, "Place of Contact(With State Code)", "Place Of Contact"
    )
    phone = normalize_in_mobile(_s(row, "MobilePhone", "Phone", "Billing Phone"))
    email = _s(row, "EmailID")
    gst = _s(row, "GST Identification Number (GSTIN)")
    status = (_s(row, "Status") or "active").lower()
    # Build a minimal address-shaped raw for SQL detail popup
    billing = {
        "attention": _s(row, "Billing Attention"),
        "address": _s(row, "Billing Address"),
        "street2": _s(row, "Billing Street2"),
        "city": billing_city,
        "state": _s(row, "Billing State"),
        "zip": _s(row, "Billing Code"),
        "country": _s(row, "Billing Country"),
        "phone": _s(row, "Billing Phone"),
        "fax": _s(row, "Billing Fax"),
    }
    shipping = {
        "attention": _s(row, "Shipping Attention"),
        "address": _s(row, "Shipping Address"),
        "street2": _s(row, "Shipping Street2"),
        "city": shipping_city,
        "state": _s(row, "Shipping State"),
        "zip": _s(row, "Shipping Code"),
        "country": _s(row, "Shipping Country"),
        "phone": _s(row, "Shipping Phone"),
        "fax": _s(row, "Shipping Fax"),
    }
    raw = {
        "contact_id": cid,
        "contact_name": name,
        "company_name": company,
        "email": email,
        "phone": _s(row, "Phone"),
        "mobile": _s(row, "MobilePhone"),
        "status": status,
        "gst_no": gst,
        "place_of_contact": _s(row, "Place Of Contact"),
        "place_of_contact_formatted": _s(row, "Place of Contact(With State Code)"),
        "billing_address": billing,
        "shipping_address": shipping,
        "_backup_csv": True,
    }
    return {
        "zoho_contact_id": cid,
        "contact_name": name,
        "company_name": company,
        "email": email,
        "phone": phone,
        "mobile": normalize_in_mobile(_s(row, "MobilePhone")),
        "gst_no": gst,
        "billing_city": city[:80] if city else "",
        "billing_state": state[:80] if state else "",
        "status": status,
        "raw": raw,
        "excluded": is_excluded_contact(name=name, company=company, zoho_id=cid),
    }


async def import_invoices(db, files: list[Path]) -> dict[str, int]:
    stats = {"files": len(files), "csv_rows": 0, "unique": 0, "inserted": 0, "updated": 0}
    # First pass: keep one header-level payload per invoice id (last line item wins for raw line_item)
    by_id: dict[str, dict[str, Any]] = {}
    for path in files:
        print(f"reading invoices {path.name}…", flush=True)
        for row in _iter_csv_dicts(path):
            stats["csv_rows"] += 1
            payload = invoice_row_to_payload(row)
            if not payload:
                continue
            iid = payload["invoice_id"]
            if iid in by_id:
                # accumulate line items
                prev = by_id[iid]
                items = prev.setdefault("line_items", [])
                if prev.get("line_item"):
                    items.append(prev.pop("line_item"))
                if payload.get("line_item"):
                    items.append(payload["line_item"])
                # keep header totals from first occurrence (identical across lines)
            else:
                by_id[iid] = payload
    stats["unique"] = len(by_id)
    print(f"upserting {len(by_id)} invoices…", flush=True)
    for i, payload in enumerate(by_id.values(), start=1):
        rid = payload["invoice_id"]
        existing = (
            await db.execute(select(ZohoSale).where(ZohoSale.zoho_record_id == rid))
        ).scalar_one_or_none()
        await upsert_sale(db, payload, source="backup", document_type="invoice")
        if existing:
            stats["updated"] += 1
        else:
            stats["inserted"] += 1
        if i % 500 == 0:
            await db.commit()
            print(f"  invoices {i}/{len(by_id)}", flush=True)
    await db.commit()
    return stats


async def import_credit_notes(db, files: list[Path]) -> dict[str, int]:
    stats = {"files": len(files), "csv_rows": 0, "unique": 0, "inserted": 0, "updated": 0}
    by_id: dict[str, dict[str, Any]] = {}
    for path in files:
        print(f"reading credit notes {path.name}…", flush=True)
        for row in _iter_csv_dicts(path):
            stats["csv_rows"] += 1
            payload = credit_row_to_payload(row)
            if not payload:
                continue
            by_id[payload["creditnote_id"]] = payload
    stats["unique"] = len(by_id)
    print(f"upserting {len(by_id)} credit notes…", flush=True)
    for i, payload in enumerate(by_id.values(), start=1):
        rid = payload["creditnote_id"]
        existing = (
            await db.execute(select(ZohoCreditNote).where(ZohoCreditNote.zoho_record_id == rid))
        ).scalar_one_or_none()
        await upsert_credit_note(db, payload, source="backup")
        if existing:
            stats["updated"] += 1
        else:
            stats["inserted"] += 1
        if i % 200 == 0:
            await db.commit()
    await db.commit()
    return stats


async def import_contacts(db, files: list[Path]) -> dict[str, int]:
    stats = {
        "files": len(files),
        "rows": 0,
        "excluded": 0,
        "staging_upserted": 0,
        "dealers_updated": 0,
        "dealers_created": 0,
        "cities_filled": 0,
    }
    for path in files:
        print(f"reading contacts {path.name}…", flush=True)
        for row in _iter_csv_dicts(path):
            stats["rows"] += 1
            fields = contact_row_to_fields(row)
            if not fields:
                continue
            if fields["excluded"]:
                stats["excluded"] += 1
                # Ensure no dealer row
                dealer = (
                    await db.execute(
                        select(Dealer).where(Dealer.zoho_contact_id == fields["zoho_contact_id"])
                    )
                ).scalar_one_or_none()
                if dealer:
                    await db.delete(dealer)
                staging = (
                    await db.execute(
                        select(ZohoDealer).where(
                            ZohoDealer.zoho_contact_id == fields["zoho_contact_id"]
                        )
                    )
                ).scalar_one_or_none()
                if staging:
                    staging.status = "excluded"
                    staging.dealer_id = None
                    staging.raw_json = json.dumps(fields["raw"], default=str)
                continue

            cid = fields["zoho_contact_id"]
            staging = (
                await db.execute(select(ZohoDealer).where(ZohoDealer.zoho_contact_id == cid))
            ).scalar_one_or_none()
            if not staging:
                staging = ZohoDealer(zoho_contact_id=cid)
                db.add(staging)
            staging.contact_name = fields["contact_name"] or staging.contact_name
            staging.company_name = fields["company_name"] or staging.company_name
            staging.email = fields["email"] or staging.email
            staging.phone = fields["phone"] or staging.phone
            staging.mobile = fields["mobile"] or staging.mobile
            staging.gst_no = fields["gst_no"] or staging.gst_no
            if fields["billing_city"]:
                staging.billing_city = fields["billing_city"]
            if fields["billing_state"]:
                staging.billing_state = canon_state(fields["billing_state"])[:80]
            staging.status = fields["status"] or staging.status or "active"
            # Prefer backup raw if existing has no address object
            existing_raw = staging.raw_json or ""
            has_addr = '"billing_address"' in existing_raw or '"shipping_address"' in existing_raw
            if not has_addr or fields["billing_city"]:
                # Merge: keep existing detail if richer, else use backup
                try:
                    prev = json.loads(existing_raw) if existing_raw.strip() else {}
                except json.JSONDecodeError:
                    prev = {}
                if not isinstance(prev, dict) or not prev.get("billing_address"):
                    staging.raw_json = json.dumps(fields["raw"], default=str)
                else:
                    # fill empty city on existing raw
                    ba = prev.get("billing_address") if isinstance(prev.get("billing_address"), dict) else {}
                    if fields["billing_city"] and not str(ba.get("city") or "").strip():
                        ba["city"] = fields["billing_city"]
                        prev["billing_address"] = ba
                        staging.raw_json = json.dumps(prev, default=str)
            stats["staging_upserted"] += 1

            dealer = (
                await db.execute(select(Dealer).where(Dealer.zoho_contact_id == cid))
            ).scalar_one_or_none()
            created = False
            if not dealer:
                dealer = Dealer(zoho_contact_id=cid, source="zoho_backup")
                db.add(dealer)
                created = True
                stats["dealers_created"] += 1
            else:
                stats["dealers_updated"] += 1
            before_city = (dealer.city or "").strip()
            dealer.name = fields["contact_name"] or dealer.name
            dealer.company = fields["company_name"] or dealer.company
            if fields["phone"]:
                dealer.phone = fields["phone"]
            elif fields["mobile"]:
                dealer.phone = fields["mobile"]
            if fields["email"]:
                dealer.email = fields["email"]
            if fields["gst_no"]:
                dealer.gstin = fields["gst_no"]
            if fields["billing_city"] and not before_city:
                dealer.city = fields["billing_city"]
                stats["cities_filled"] += 1
            elif fields["billing_city"] and not dealer.city:
                dealer.city = fields["billing_city"]
                stats["cities_filled"] += 1
            if fields["billing_state"] and not (dealer.state or "").strip():
                dealer.state = canon_state(fields["billing_state"])[:80]
            dealer.zoho_contact_id = cid
            dealer.status = (
                "inactive"
                if fields["status"] in {"inactive", "deleted", "excluded"}
                else "active"
            )
            dealer.source = dealer.source or "zoho_backup"
            await db.flush()
            staging.dealer_id = dealer.id
            if stats["rows"] % 200 == 0:
                await db.commit()
                print(f"  contacts {stats['rows']}", flush=True)
    await db.commit()
    return stats


async def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/data/zoho_backup")
    if not root.exists():
        print(f"backup root not found: {root}", file=sys.stderr)
        return 1

    invoice_files = _find_files(root, ["Invoice*.csv", "invoice/*.csv"])
    credit_files = _find_files(root, ["Credit_Note.csv", "Credit_Notes.csv"])
    contact_files = _find_files(root, ["Contacts.csv"])

    print("invoice files:", [p.name for p in invoice_files], flush=True)
    print("credit files:", [p.name for p in credit_files], flush=True)
    print("contact files:", [p.name for p in contact_files], flush=True)

    async with SessionLocal() as db:
        before_sales = (
            await db.execute(select(func.count()).select_from(ZohoSale))
        ).scalar() or 0
        before_cn = (
            await db.execute(select(func.count()).select_from(ZohoCreditNote))
        ).scalar() or 0
        before_city = (
            await db.execute(select(func.count()).select_from(Dealer).where(Dealer.city != ""))
        ).scalar() or 0
        print(f"before sales={before_sales} credit_notes={before_cn} dealers_city={before_city}", flush=True)

        inv_stats = await import_invoices(db, invoice_files) if invoice_files else {}
        cn_stats = await import_credit_notes(db, credit_files) if credit_files else {}
        ct_stats = await import_contacts(db, contact_files) if contact_files else {}

        after_sales = (
            await db.execute(select(func.count()).select_from(ZohoSale))
        ).scalar() or 0
        after_cn = (
            await db.execute(select(func.count()).select_from(ZohoCreditNote))
        ).scalar() or 0
        after_city = (
            await db.execute(select(func.count()).select_from(Dealer).where(Dealer.city != ""))
        ).scalar() or 0
        dmin, dmax = (
            await db.execute(select(func.min(ZohoSale.date), func.max(ZohoSale.date)))
        ).one()
        print("invoice_stats", inv_stats, flush=True)
        print("credit_stats", cn_stats, flush=True)
        print("contact_stats", ct_stats, flush=True)
        print(
            f"after sales={after_sales} (+{after_sales - before_sales}) "
            f"credit_notes={after_cn} (+{after_cn - before_cn}) "
            f"dealers_city={after_city} (+{after_city - before_city})",
            flush=True,
        )
        print(f"sales date range: {dmin} → {dmax}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
