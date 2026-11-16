#!/usr/bin/env python3
"""Import LMOMS Kerala CSV firm lists into YesWeigh CRM Leads.

Dedupes by phone (if present), else email, else name+company (normalized).
Merges overlapping roles into one lead with combined source tags.

Usage:
  python3 scripts/import_lmoms_leads.py \\
    --base-url https://crm.yesweigh.in \\
    --email admin@yesweigh.in \\
    --password '...' \\
    --csv-dir /path/to/csvs

Env alternatives: CRM_BASE_URL, CRM_EMAIL, CRM_PASSWORD, CRM_CSV_DIR
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
import httpx

# Single source label for all LMOMS Kerala firm lists (user: keep LMOMS / LMOS).
LMOMS_SOURCE = "LMOMS"

SOURCE_BY_FILE = {
    "LMOMS-Kerala-repairer-approved-list.csv": LMOMS_SOURCE,
    "LMOMS-Kerala-dealer-approved-list.csv": LMOMS_SOURCE,
    "LMOMS-Kerala-manufacturer-approved-list.csv": LMOMS_SOURCE,
    "LMOMS-Kerala-manufacturers-dealers-repairers.csv": LMOMS_SOURCE,
}


def norm_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def norm_key_part(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", "", norm_text(value).upper())


def norm_phone(value: str | None) -> str:
    digits = re.sub(r"\D+", "", value or "")
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def norm_email(value: str | None) -> str:
    return norm_text(value).lower()


def dedupe_key(phone: str, email: str, name: str, company: str) -> str:
    p = norm_phone(phone)
    if p:
        return f"phone:{p}"
    e = norm_email(email)
    if e:
        return f"email:{e}"
    return f"nameco:{norm_key_part(name)}|{norm_key_part(company)}"


def source_for_row(filename: str, row: dict[str, str]) -> str:
    _ = row
    return SOURCE_BY_FILE.get(filename) or LMOMS_SOURCE


def merge_sources(existing: str, new: str) -> str:
    """Collapse any LMOMS/LMOS variant to the single LMOMS label."""
    parts: list[str] = []
    for chunk in f"{existing};{new}".split(";"):
        c = chunk.strip()
        if not c:
            continue
        if "LMOMS" in c.upper() or c.upper() == "LMOS":
            c = LMOMS_SOURCE
        if c not in parts:
            parts.append(c)
    return "; ".join(parts)[:80]


def merge_notes(existing: str, addition: str) -> str:
    if not addition:
        return existing
    if not existing:
        return addition
    if addition in existing:
        return existing
    return f"{existing} | {addition}"[:2000]


def load_csv_rows(csv_dir: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Load all four CSVs; return merged unique leads + stats."""
    stats = {
        "rows_read": 0,
        "files": 0,
        "skipped_blank": 0,
        "csv_dupes_merged": 0,
    }
    by_key: dict[str, dict[str, Any]] = {}

    for filename in SOURCE_BY_FILE:
        path = csv_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Missing CSV: {path}")
        stats["files"] += 1
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                stats["rows_read"] += 1
                firm = norm_text(row.get("firm_name"))
                if not firm:
                    stats["skipped_blank"] += 1
                    continue

                # Firm-only: Name column shows firm; company mirrors firm; geo from CSV.
                name = firm[:160]
                company = firm[:160]
                phone = norm_text(row.get("phone") or row.get("mobile") or row.get("contact"))
                email = norm_text(row.get("email"))
                city = norm_text(row.get("city") or row.get("town"))
                district = norm_text(row.get("district"))
                state = "Kerala"
                app_id = norm_text(row.get("app_id"))
                sl_no = norm_text(row.get("sl_no"))
                approved = norm_text(row.get("approved_on"))
                row_type = norm_text(row.get("type"))
                source = source_for_row(filename, row)
                note_bits = []
                if row_type:
                    note_bits.append(f"role={row_type}")
                elif "repairer" in filename:
                    note_bits.append("role=repairer")
                elif "dealer" in filename:
                    note_bits.append("role=dealer")
                elif "manufacturer" in filename and "dealers" not in filename:
                    note_bits.append("role=manufacturer")
                if district:
                    note_bits.append(f"District: {district}")
                if app_id:
                    note_bits.append(f"app_id={app_id}")
                if sl_no:
                    note_bits.append(f"sl_no={sl_no}")
                if approved:
                    note_bits.append(f"approved={approved}")
                note_bits.append(f"from={filename}")
                notes = "; ".join(note_bits)

                key = dedupe_key(phone, email, name, company)
                if key in by_key:
                    stats["csv_dupes_merged"] += 1
                    cur = by_key[key]
                    cur["source"] = merge_sources(cur["source"], source)
                    cur["notes"] = merge_notes(cur["notes"], notes)
                    if phone and not cur["phone"]:
                        cur["phone"] = phone[:40]
                    if email and not cur["email"]:
                        cur["email"] = email[:255]
                    if city and not cur.get("city"):
                        cur["city"] = city[:80]
                    if district and not cur.get("district"):
                        cur["district"] = district[:80]
                else:
                    by_key[key] = {
                        "name": name,
                        "phone": phone[:40],
                        "email": email[:255],
                        "company": company,
                        "city": city[:80],
                        "district": district[:80],
                        "state": state,
                        "source": source[:80],
                        "status": "new",
                        "notes": notes,
                        "_key": key,
                    }

    return list(by_key.values()), stats


def login(client: httpx.Client, base_url: str, email: str, password: str) -> str:
    r = client.post(
        f"{base_url}/api/auth/login",
        data={"username": email, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_existing_keys(client: httpx.Client, base_url: str, token: str) -> set[str]:
    """Fetch leads for dedupe. API historically capped at 200; request higher limit if supported."""
    keys: set[str] = set()
    # Try unlimited / high limit first (patched API); fall back to default.
    for url in (
        f"{base_url}/api/leads?limit=10000",
        f"{base_url}/api/leads",
    ):
        r = client.get(url, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 422:
            continue
        r.raise_for_status()
        rows = r.json()
        for lead in rows:
            keys.add(
                dedupe_key(
                    lead.get("phone") or "",
                    lead.get("email") or "",
                    lead.get("name") or "",
                    lead.get("company") or "",
                )
            )
        # If we got a full high-limit response, stop; otherwise keep the default fetch keys.
        if "limit=" in url or len(rows) < 200:
            break
        # Default endpoint returned exactly 200 — keep keys but warn later via count.
        break
    return keys


def create_lead(client: httpx.Client, base_url: str, token: str, payload: dict[str, Any]) -> tuple[bool, str]:
    body = {k: v for k, v in payload.items() if not k.startswith("_")}
    r = client.post(
        f"{base_url}/api/leads",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
    )
    if r.status_code >= 400:
        return False, f"{r.status_code}: {r.text[:300]}"
    return True, "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description="Import LMOMS Kerala CSVs into CRM leads")
    parser.add_argument("--base-url", default=os.environ.get("CRM_BASE_URL", "https://crm.yesweigh.in"))
    parser.add_argument("--email", default=os.environ.get("CRM_EMAIL", "admin@yesweigh.in"))
    parser.add_argument("--password", default=os.environ.get("CRM_PASSWORD", ""))
    parser.add_argument(
        "--csv-dir",
        default=os.environ.get(
            "CRM_CSV_DIR",
            "",
        ),
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.csv_dir:
        print("ERROR: --csv-dir or CRM_CSV_DIR required", file=sys.stderr)
        return 2
    if not args.dry_run and not args.password:
        print("ERROR: --password or CRM_PASSWORD required", file=sys.stderr)
        return 2

    csv_dir = Path(args.csv_dir)
    leads, stats = load_csv_rows(csv_dir)
    print(
        f"CSV: files={stats['files']} rows_read={stats['rows_read']} "
        f"blank={stats['skipped_blank']} within_csv_dupes_merged={stats['csv_dupes_merged']} "
        f"unique_candidates={len(leads)}"
    )

    if args.dry_run:
        by_src: dict[str, int] = defaultdict(int)
        for L in leads:
            by_src[L["source"]] += 1
        for s, n in sorted(by_src.items(), key=lambda x: -x[1]):
            print(f"  source {n:5d}  {s}")
        print("Dry run — no API writes.")
        return 0

    base = args.base_url.rstrip("/")
    failures: list[str] = []
    inserted = 0
    skipped_existing = 0

    with httpx.Client(timeout=60.0) as client:
        token = login(client, base, args.email, args.password)
        existing = fetch_existing_keys(client, base, token)
        print(f"Existing DB leads keyed for dedupe: {len(existing)}")

        to_insert = []
        for lead in leads:
            if lead["_key"] in existing:
                skipped_existing += 1
            else:
                to_insert.append(lead)
                existing.add(lead["_key"])  # avoid self-dupes if API lag

        print(f"To insert: {len(to_insert)}  skip_existing: {skipped_existing}")

        def _one(lead: dict[str, Any]) -> tuple[str, bool, str]:
            ok, msg = create_lead(client, base, token, lead)
            return lead["name"], ok, msg

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = [pool.submit(_one, L) for L in to_insert]
            done = 0
            for fut in as_completed(futs):
                name, ok, msg = fut.result()
                done += 1
                if ok:
                    inserted += 1
                else:
                    failures.append(f"{name}: {msg}")
                if done % 100 == 0 or done == len(futs):
                    print(f"  progress {done}/{len(futs)} inserted={inserted} fails={len(failures)}")

        # Verify via dashboard counts when available
        dash = client.get(f"{base}/api/dashboard", headers={"Authorization": f"Bearer {token}"})
        dash_total = None
        if dash.status_code == 200:
            dash_total = dash.json().get("leads_total")

    elapsed = time.time() - t0
    print("---")
    print(f"rows_read={stats['rows_read']}")
    print(f"unique_after_csv_dedupe={len(leads)}")
    print(f"inserted={inserted}")
    print(f"skipped_duplicates={stats['csv_dupes_merged'] + skipped_existing}")
    print(f"  (within_csv_merged={stats['csv_dupes_merged']}, against_db={skipped_existing})")
    print(f"failures={len(failures)}")
    if dash_total is not None:
        print(f"dashboard_leads_total={dash_total}")
    print(f"elapsed_sec={elapsed:.1f}")
    print(
        "dedupe_rule=phone(last10) else email(lower) else name+company "
        "(alnum upper); multi-role firms merge sources"
    )
    if failures:
        print("First failures:")
        for line in failures[:15]:
            print(f"  {line}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
