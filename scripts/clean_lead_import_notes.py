#!/usr/bin/env python3
"""Clear or trim auto-import junk in leads.notes (LMOMS CSV, Google Places enrich, Dealer Finder).

Usage (production in API container):
  python3 scripts/clean_lead_import_notes.py --dry-run
  python3 scripts/clean_lead_import_notes.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

import asyncpg

LMOMS_CHUNK = re.compile(
    r"^\s*(?:role=(?:dealer|repairer|manufacturer)\s*;\s*)?"
    r"(?:District:\s*[^;|]+\s*;\s*)?"
    r"(?:app_id=[^;|]+\s*;\s*)?"
    r"(?:sl_no=[^;|]+\s*;\s*)?"
    r"(?:approved=[^;|]+\s*;\s*)?"
    r"from=LMOMS[^;|]+\.csv\s*$",
    re.I,
)
GOOGLE_PLACES = re.compile(r"^\[Google Places\].*$", re.I | re.S)
DEALER_FINDER = re.compile(r"^\[Dealer Finder\].*$", re.I)


def split_chunks(notes: str) -> list[str]:
    parts: list[str] = []
    for chunk in re.split(r"\s*\|\s*", notes):
        for line in chunk.split("\n"):
            line = line.strip()
            if line:
                parts.append(line)
    return parts


def is_auto_chunk(chunk: str) -> bool:
    c = chunk.strip()
    if LMOMS_CHUNK.match(c):
        return True
    if GOOGLE_PLACES.match(c):
        return True
    if DEALER_FINDER.match(c):
        return True
    if "from=LMOMS" in c and "app_id=" in c:
        return True
    if c.startswith("District:") and "sl_no=" in c:
        return True
    return False


def clean_notes(notes: str) -> tuple[str, str]:
    if not (notes or "").strip():
        return "", "empty"
    human = [c for c in split_chunks(notes) if not is_auto_chunk(c)]
    if not human:
        return "", "clear"
    new = " | ".join(human)[:2000]
    if new.strip() == notes.strip():
        return notes, "keep"
    return new, "trim"


def _pg_dsn() -> str:
    raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        raise RuntimeError("DATABASE_URL required")
    return raw.replace("postgresql+asyncpg://", "postgresql://")


async def _run(dry_run: bool) -> int:
    conn = await asyncpg.connect(_pg_dsn())
    try:
        rows = await conn.fetch("SELECT id, notes FROM leads ORDER BY id")
        stats = {"clear": 0, "trim": 0, "keep": 0, "empty": 0, "updated": 0}
        trim_examples: list[tuple[int, str, str]] = []
        keep_examples: list[tuple[int, str]] = []

        async with conn.transaction():
            for row in rows:
                lead_id = int(row["id"])
                notes = row["notes"] or ""
                new_notes, action = clean_notes(notes)
                stats[action] = stats.get(action, 0) + 1
                if action == "trim" and len(trim_examples) < 5:
                    trim_examples.append((lead_id, notes[:100], new_notes[:100]))
                if action == "keep" and len(keep_examples) < 5:
                    keep_examples.append((lead_id, notes[:120]))
                if not dry_run and action in ("clear", "trim"):
                    await conn.execute("UPDATE leads SET notes = $1 WHERE id = $2", new_notes, lead_id)
                    stats["updated"] += 1

        print(f"leads_total={len(rows)}")
        print(
            f"action_clear={stats['clear']} action_trim={stats['trim']} "
            f"action_keep={stats['keep']} action_empty={stats['empty']}"
        )
        print(f"dry_run={dry_run} updated={stats['updated']}")
        if trim_examples:
            print("trim_examples:")
            for ex in trim_examples:
                print(f"  id={ex[0]}")
                print(f"    before: {ex[1]!r}")
                print(f"    after:  {ex[2]!r}")
        if keep_examples:
            print("keep_examples:")
            for ex in keep_examples:
                print(f"  id={ex[0]} notes={ex[1]!r}")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean auto-prefilled lead notes")
    parser.add_argument("--apply", action="store_true", help="Write changes (default is dry-run)")
    args = parser.parse_args()
    return asyncio.run(_run(dry_run=not args.apply))


if __name__ == "__main__":
    raise SystemExit(main())