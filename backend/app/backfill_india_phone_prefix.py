"""Backfill bare Indian 10-digit phones → 91XXXXXXXXXX (no double-prefix).

Run:
  python -m app.backfill_india_phone_prefix
  python -m app.backfill_india_phone_prefix --dry-run
  python -m app.backfill_india_phone_prefix --state Goa
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import func, or_, select

from app.db import SessionLocal
from app.models import Dealer, Lead, WhatsAppConversation, ZohoDealer
from app.phone_util import repair_stored_in_mobile, repair_stored_whatsapp_jid
from app.state_normalize import state_match_values

logger = logging.getLogger("backfill_india_phone_prefix")


def _state_clause(model, state: str | None):
    if not state or not (state := state.strip()):
        return None
    vals = state_match_values(state) or [state]
    lowered = state.lower()
    parts = [model.state.in_(vals)]
    if lowered:
        parts.append(func.lower(model.state) == lowered)
    return or_(*parts)


async def _repair_table(
    db,
    *,
    rows,
    phone_attrs: tuple[str, ...],
    jid_attr: str | None = None,
    phone_for_jid_attr: str | None = None,
    dry_run: bool,
    label: str,
    samples: list[dict[str, str]] | None = None,
) -> dict[str, int]:
    repaired = 0
    skipped = 0
    jid_repaired = 0
    for row in rows:
        row_changed = False
        for attr in phone_attrs:
            old = getattr(row, attr, None) or ""
            new, changed = repair_stored_in_mobile(old)
            if changed:
                if samples is not None and len(samples) < 8:
                    samples.append({"table": label, "field": attr, "before": old, "after": new})
                if not dry_run:
                    setattr(row, attr, new)
                repaired += 1
                row_changed = True
            else:
                skipped += 1
        if jid_attr:
            phone_val = getattr(row, phone_for_jid_attr or phone_attrs[0], None) if phone_attrs else None
            old_jid = getattr(row, jid_attr, None) or ""
            new_jid, jid_changed = repair_stored_whatsapp_jid(old_jid, phone_val)
            if jid_changed:
                if not dry_run:
                    setattr(row, jid_attr, new_jid)
                jid_repaired += 1
                row_changed = True
        if row_changed and not dry_run and repaired % 200 == 0:
            await db.flush()
    if not dry_run:
        await db.commit()
    return {
        "label": label,
        "phone_fields_repaired": repaired,
        "phone_fields_unchanged": skipped,
        "jid_repaired": jid_repaired,
        "rows_scanned": len(rows),
    }


async def run(*, dry_run: bool = False, state: str | None = None) -> dict[str, object]:
    samples: list[dict[str, str]] = []
    async with SessionLocal() as db:
        convs = list((await db.execute(select(WhatsAppConversation))).scalars().all())

        dealer_q = select(Dealer)
        lead_q = select(Lead)
        zoho_q = select(ZohoDealer)
        dealer_clause = _state_clause(Dealer, state)
        if dealer_clause is not None:
            dealer_q = dealer_q.where(dealer_clause)
        lead_clause = _state_clause(Lead, state)
        if lead_clause is not None:
            lead_q = lead_q.where(lead_clause)
        if state and (state := state.strip()):
            vals = state_match_values(state) or [state]
            zoho_q = zoho_q.where(
                or_(
                    ZohoDealer.billing_state.in_(vals),
                    func.lower(ZohoDealer.billing_state) == state.lower(),
                )
            )

        dealers = list((await db.execute(dealer_q)).scalars().all())
        leads = list((await db.execute(lead_q)).scalars().all())
        zoho = list((await db.execute(zoho_q)).scalars().all()) if state else list(
            (await db.execute(select(ZohoDealer))).scalars().all()
        )

        results = []
        if not state:
            results.append(
                await _repair_table(
                    db,
                    rows=convs,
                    phone_attrs=("customer_phone",),
                    jid_attr="customer_jid",
                    phone_for_jid_attr="customer_phone",
                    dry_run=dry_run,
                    label="whatsapp_conversations",
                    samples=samples,
                )
            )
        results.extend(
            [
                await _repair_table(
                    db,
                    rows=dealers,
                    phone_attrs=("phone",),
                    dry_run=dry_run,
                    label="dealers",
                    samples=samples,
                ),
                await _repair_table(
                    db,
                    rows=leads,
                    phone_attrs=("phone",),
                    dry_run=dry_run,
                    label="leads",
                    samples=samples,
                ),
            ]
        )
        if not state:
            results.append(
                await _repair_table(
                    db,
                    rows=zoho,
                    phone_attrs=("phone", "mobile"),
                    dry_run=dry_run,
                    label="zoho_dealers",
                    samples=samples,
                )
            )
        total_repaired = sum(int(r["phone_fields_repaired"]) for r in results)
        total_jid = sum(int(r["jid_repaired"]) for r in results)
        return {
            "dry_run": dry_run,
            "state_filter": state or "",
            "tables": results,
            "samples": samples,
            "total_phone_fields_repaired": total_repaired,
            "total_jid_repaired": total_jid,
        }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--state", default="", help="Only dealers/leads in this state (e.g. Goa)")
    args = parser.parse_args()
    out = asyncio.run(run(dry_run=args.dry_run, state=args.state or None))
    import json

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
