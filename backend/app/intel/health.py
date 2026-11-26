"""Dealer health snapshot. Band is a deterministic rule, not a model score.

attention — unreplied DM, open service thread, or open commitment
quiet — no countable sale in the last 6 months, and none of the above
steady — active in that window and none of the above

``open_balance`` is a display figure. It is not a collections workflow.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_merges import sales_customer_ids_for_dealer
from app.dealers import _add_months, _sale_cutoff_iso, _visible_dealers_clause
from app.intel.queries import _dm_scope, _unresolved_service_filters
from app.models import AiCommitment, AiDealerHealth, Dealer, WhatsAppConversation, ZohoSale
from app.sales_amount import sales_amount_expr, zoho_sale_countable_clause

ORG = "yesweigh"


def customer_ids_for_health(zoho_contact_id: str) -> list[str]:
    """Merge-aware Zoho ids. ELMASS and VENKATACHALAM fold only via dealer_merges."""
    return sales_customer_ids_for_dealer(zoho_contact_id)


def india_fy_bounds(today: date) -> tuple[str, str, str, str]:
    """Current FY start, today, prior FY start, prior FY end. India FY is April–March."""
    if today.month >= 4:
        start = date(today.year, 4, 1)
    else:
        start = date(today.year - 1, 4, 1)
    prior_start = date(start.year - 1, 4, 1)
    prior_end = date(start.year, 3, 31)
    return start.isoformat(), today.isoformat(), prior_start.isoformat(), prior_end.isoformat()


def health_band(
    *,
    active: bool,
    open_service_threads: int,
    unreplied_threads: int,
    open_commitments: int,
) -> str:
    if unreplied_threads or open_service_threads or open_commitments:
        return "attention"
    if not active:
        return "quiet"
    return "steady"


def health_why(
    *,
    active: bool,
    cutoff: str,
    open_service_threads: int,
    unreplied_threads: int,
    open_commitments: int,
    band: str,
) -> list[str]:
    reasons: list[str] = []
    if unreplied_threads:
        reasons.append(f"{unreplied_threads} unreplied DM thread(s) linked to this dealer.")
    if open_service_threads:
        reasons.append(f"{open_service_threads} unresolved service thread(s).")
    if open_commitments:
        reasons.append(f"{open_commitments} open commitment(s).")
    if active:
        reasons.append(f"Countable invoice on or after {cutoff} (6-month active rule).")
    else:
        reasons.append(f"No countable invoice on or after {cutoff} (6-month active rule).")
    reasons.append(f"Band {band} is this rule, not a score.")
    return reasons


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except Exception:
        return Decimal("0.00")


async def snapshot_for_dealer(db: AsyncSession, dealer_id: int) -> dict[str, Any] | None:
    dealer = await db.get(Dealer, dealer_id)
    if dealer is None:
        return None
    today = date.today()
    cutoff = _sale_cutoff_iso()
    fy_start, fy_end, prior_start, prior_end = india_fy_bounds(today)
    twelve = _add_months(today, -12).isoformat()
    ids = customer_ids_for_health(dealer.zoho_contact_id or "")
    sales_fy = Decimal("0")
    sales_prior = Decimal("0")
    last_sale = ""
    n12 = 0
    open_balance = Decimal("0")
    if ids:
        amount = sales_amount_expr()
        row = (
            await db.execute(
                select(
                    func.max(ZohoSale.date),
                    func.coalesce(
                        func.sum(
                            case(
                                (and_(ZohoSale.date >= fy_start, ZohoSale.date <= fy_end), amount),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (and_(ZohoSale.date >= prior_start, ZohoSale.date <= prior_end), amount),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(case((ZohoSale.date >= twelve, 1), else_=0)),
                        0,
                    ),
                    func.coalesce(
                        func.sum(case((ZohoSale.balance > 0, ZohoSale.balance), else_=0)),
                        0,
                    ),
                )
                .where(ZohoSale.customer_id.in_(ids))
                .where(*zoho_sale_countable_clause())
            )
        ).one()
        last_sale = str(row[0] or "")
        sales_fy = _dec(row[1])
        sales_prior = _dec(row[2])
        n12 = int(row[3] or 0)
        open_balance = _dec(row[4])
    active = bool(last_sale) and last_sale >= cutoff
    unreplied = int(
        (
            await db.execute(
                select(func.count())
                .select_from(WhatsAppConversation)
                .where(
                    *_dm_scope(),
                    WhatsAppConversation.dealer_id == dealer.id,
                    WhatsAppConversation.awaiting_staff_reply.is_(True),
                    WhatsAppConversation.status != "Resolved",
                )
            )
        ).scalar_one()
        or 0
    )
    last_in, last_out, service_filters = _unresolved_service_filters(None)
    service_n = int(
        (
            await db.execute(
                select(func.count())
                .select_from(WhatsAppConversation)
                .join(last_in, last_in.c.conversation_id == WhatsAppConversation.id)
                .outerjoin(last_out, last_out.c.conversation_id == WhatsAppConversation.id)
                .where(*service_filters, WhatsAppConversation.dealer_id == dealer.id)
            )
        ).scalar_one()
        or 0
    )
    open_commitments = int(
        (
            await db.execute(
                select(func.count())
                .select_from(AiCommitment)
                .where(AiCommitment.dealer_id == dealer.id, AiCommitment.status == "open")
            )
        ).scalar_one()
        or 0
    )
    band = health_band(
        active=active,
        open_service_threads=service_n,
        unreplied_threads=unreplied,
        open_commitments=open_commitments,
    )
    return {
        "dealer_id": dealer.id,
        "dealer_name": (dealer.company or dealer.name or "").strip(),
        "as_of": today.isoformat(),
        "sales_fy": float(sales_fy),
        "sales_prior_fy": float(sales_prior),
        "last_sale_date": last_sale or None,
        "invoice_count_12m": n12,
        "open_balance": float(open_balance),
        "open_balance_note": "Display only. Not a collections or dunning workflow.",
        "active": active,
        "active_rule": f"Countable invoice on or after {cutoff} (6 months).",
        "open_service_threads": service_n,
        "unreplied_threads": unreplied,
        "open_commitments": open_commitments,
        "health_band": band,
        "why": health_why(
            active=active,
            cutoff=cutoff,
            open_service_threads=service_n,
            unreplied_threads=unreplied,
            open_commitments=open_commitments,
            band=band,
        ),
        "sources": [
            {
                "type": "dealer",
                "id": str(dealer.id),
                "label": (dealer.company or dealer.name or "Dealer")[:160],
                "href": f"/dealers?q={quote((dealer.company or dealer.name or '').strip())}",
            }
        ],
        "zoho_customer_ids": ids,
    }


async def rebuild_dealer_health(db: AsyncSession) -> int:
    """Upsert today's band for visible dealers from grouped reads, not per-dealer queries."""
    from app.dealer_merges import remapped_customer_id_expr

    today = date.today()
    cutoff = _sale_cutoff_iso()
    fy_start, fy_end, prior_start, prior_end = india_fy_bounds(today)
    twelve = _add_months(today, -12).isoformat()
    amount = sales_amount_expr()
    cid = remapped_customer_id_expr(ZohoSale.customer_id)
    sales_rows = (
        await db.execute(
            select(
                cid.label("customer_id"),
                func.max(ZohoSale.date),
                func.coalesce(
                    func.sum(case((and_(ZohoSale.date >= fy_start, ZohoSale.date <= fy_end), amount), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (and_(ZohoSale.date >= prior_start, ZohoSale.date <= prior_end), amount),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(func.sum(case((ZohoSale.date >= twelve, 1), else_=0)), 0),
                func.coalesce(func.sum(case((ZohoSale.balance > 0, ZohoSale.balance), else_=0)), 0),
            )
            .where(ZohoSale.customer_id != "")
            .where(*zoho_sale_countable_clause())
            .group_by(cid)
        )
    ).all()
    by_zoho = {
        str(r[0]): {
            "last": str(r[1] or ""),
            "fy": _dec(r[2]),
            "prior": _dec(r[3]),
            "n12": int(r[4] or 0),
            "open": _dec(r[5]),
        }
        for r in sales_rows
        if r[0]
    }
    unreplied_rows = (
        await db.execute(
            select(WhatsAppConversation.dealer_id, func.count())
            .where(
                *_dm_scope(),
                WhatsAppConversation.dealer_id.is_not(None),
                WhatsAppConversation.awaiting_staff_reply.is_(True),
                WhatsAppConversation.status != "Resolved",
            )
            .group_by(WhatsAppConversation.dealer_id)
        )
    ).all()
    unreplied_by = {int(did): int(n or 0) for did, n in unreplied_rows if did}
    last_in, last_out, service_filters = _unresolved_service_filters(None)
    service_rows = (
        await db.execute(
            select(WhatsAppConversation.dealer_id, func.count())
            .join(last_in, last_in.c.conversation_id == WhatsAppConversation.id)
            .outerjoin(last_out, last_out.c.conversation_id == WhatsAppConversation.id)
            .where(*service_filters, WhatsAppConversation.dealer_id.is_not(None))
            .group_by(WhatsAppConversation.dealer_id)
        )
    ).all()
    service_by = {int(did): int(n or 0) for did, n in service_rows if did}
    commit_rows = (
        await db.execute(
            select(AiCommitment.dealer_id, func.count())
            .where(AiCommitment.status == "open", AiCommitment.dealer_id.is_not(None))
            .group_by(AiCommitment.dealer_id)
        )
    ).all()
    commit_by = {int(did): int(n or 0) for did, n in commit_rows if did}
    dealers = (await db.execute(select(Dealer).where(_visible_dealers_clause()))).scalars().all()
    existing_rows = (
        await db.execute(select(AiDealerHealth).where(AiDealerHealth.as_of == today))
    ).scalars().all()
    existing_by = {row.dealer_id: row for row in existing_rows}
    written = 0
    for dealer in dealers:
        metrics = by_zoho.get((dealer.zoho_contact_id or "").strip()) or {}
        last_sale = str(metrics.get("last") or "")
        active = bool(last_sale) and last_sale >= cutoff
        service_n = service_by.get(dealer.id, 0)
        unreplied = unreplied_by.get(dealer.id, 0)
        open_commitments = commit_by.get(dealer.id, 0)
        band = health_band(
            active=active,
            open_service_threads=service_n,
            unreplied_threads=unreplied,
            open_commitments=open_commitments,
        )
        row = existing_by.get(dealer.id)
        if row is None:
            row = AiDealerHealth(organization_id=ORG, dealer_id=dealer.id, as_of=today)
            db.add(row)
        row.sales_fy = metrics.get("fy") or Decimal("0")
        row.sales_prior_fy = metrics.get("prior") or Decimal("0")
        row.last_sale_date = last_sale
        row.invoice_count_12m = int(metrics.get("n12") or 0)
        row.open_balance = metrics.get("open") or Decimal("0")
        row.active = active
        row.open_service_threads = service_n
        row.unreplied_threads = unreplied
        row.open_commitments = open_commitments
        row.health_band = band
        written += 1
    await db.flush()
    return written
