"""Duplicate check vs CRM Dealers + Leads (WhatsApp/phone normalize)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.google_places_search import CrmContact, crm_match_reason, resolve_state
from app.models import Dealer, Lead
from app.phone_util import normalize_in_mobile


async def load_crm_contacts(
    db: AsyncSession, *, state: str = ""
) -> tuple[list[CrmContact], int, int]:
    """Load dealers+leads for matching. If state set, prefer that state but keep phones global."""
    state_label, spellings = resolve_state(state) if state else ("", [])
    dealer_q = select(Dealer)
    lead_q = select(Lead)
    if spellings:
        dealer_q = dealer_q.where(or_(*[Dealer.state.ilike(s) for s in spellings]))
        lead_q = lead_q.where(or_(*[Lead.state.ilike(s) for s in spellings]))

    dealers = (await db.execute(dealer_q)).scalars().all()
    leads = (await db.execute(lead_q)).scalars().all()

    contacts: list[CrmContact] = []
    for d in dealers:
        contacts.append(
            CrmContact(
                name=(d.name or d.company or "").strip(),
                city=(d.city or "").strip(),
                phone10=normalize_in_mobile(d.phone or ""),
                kind="dealer",
                record_id=int(d.id),
            )
        )
    for lead in leads:
        contacts.append(
            CrmContact(
                name=(lead.name or lead.company or "").strip(),
                city=(lead.city or lead.district or "").strip(),
                phone10=normalize_in_mobile(lead.phone or ""),
                kind="lead",
                record_id=int(lead.id),
            )
        )

    # Also load all phones org-wide for stronger dup check (lightweight second pass)
    if spellings:
        all_dealers = (await db.execute(select(Dealer.id, Dealer.phone))).all()
        all_leads = (await db.execute(select(Lead.id, Lead.phone))).all()
        seen_phones = {c.phone10 for c in contacts if c.phone10}
        for did, phone in all_dealers:
            p10 = normalize_in_mobile(phone or "")
            if p10 and p10 not in seen_phones:
                contacts.append(
                    CrmContact(name="", city="", phone10=p10, kind="dealer", record_id=int(did))
                )
                seen_phones.add(p10)
        for lid, phone in all_leads:
            p10 = normalize_in_mobile(phone or "")
            if p10 and p10 not in seen_phones:
                contacts.append(
                    CrmContact(name="", city="", phone10=p10, kind="lead", record_id=int(lid))
                )
                seen_phones.add(p10)

    return contacts, len(dealers), len(leads)


def annotate_crm_status(
    row: dict[str, Any], contacts: list[CrmContact]
) -> dict[str, Any]:
    is_existing, reason = crm_match_reason(row, contacts)
    out = dict(row)
    if is_existing:
        kind = "dealer" if reason.startswith("dealer") or ":dealer:" in reason else "lead"
        if reason.startswith("phone:dealer") or "name_city:dealer" in reason or "name:dealer" in reason:
            kind = "dealer"
        elif reason.startswith("phone:lead") or "name_city:lead" in reason or "name:lead" in reason:
            kind = "lead"
        # Parse kind from reason: phone:dealer:12
        parts = reason.split(":")
        if len(parts) >= 2 and parts[1] in {"dealer", "lead"}:
            kind = parts[1]
        out["crm_status"] = kind
        out["crm_match"] = reason
        out["eligible_import"] = False
    else:
        out["crm_status"] = "new"
        out["crm_match"] = ""
        out["eligible_import"] = True
    return out
