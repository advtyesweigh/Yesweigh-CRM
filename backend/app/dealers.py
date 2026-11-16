"""CRM dealers API — list Zoho-imported dealer master records."""
from __future__ import annotations

import json
from calendar import monthrange
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.dealer_exclusions import dealer_not_excluded_clause
from app.dealer_merges import remapped_customer_id_expr, sales_customer_ids_for_dealer
from app.models import Dealer, User, ZohoDealer, ZohoSale
from app.sales_amount import sales_amount_expr

router = APIRouter(prefix="/api/dealers", tags=["dealers"])

Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

# Lifetime sales threshold for High Value Dealers (₹10 Lakh).
HIGH_VALUE_SALES = Decimal("1000000")
# Active / Inactive KPI cohort window (purchase recency, not dealers.status).
ACTIVE_SALES_MONTHS = 6


def _add_months(d: date, months: int) -> date:
    """Calendar-month offset (clamps day to end of target month)."""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def _sale_cutoff_iso() -> str:
    """ISO date: sales on/after this count as Active (last N months)."""
    return _add_months(date.today(), -ACTIVE_SALES_MONTHS).isoformat()


def _active_sales_clause(sales_sub: Any, cutoff: str) -> Any:
    """Has a last purchase on/after cutoff (MAX(zoho_sales.date) >= cutoff)."""
    last = sales_sub.c.last_sale_date
    return and_(last.is_not(None), last != "", last >= cutoff)


def _inactive_sales_clause(sales_sub: Any, cutoff: str) -> Any:
    """No sales, or last purchase before cutoff — complementary to Active."""
    last = sales_sub.c.last_sale_date
    return or_(last.is_(None), last == "", last < cutoff)


class DealerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str = ""
    company: str = ""
    phone: str = ""
    email: str = ""
    city: str = ""
    state: str = ""
    gstin: str = ""
    zoho_contact_id: str = ""
    source: str = ""
    status: str = ""
    assigned_salesman: str | None = None
    sales_total: float | None = None
    last_sale_date: str | None = None


class DealerPageOut(BaseModel):
    items: list[DealerOut]
    total: int
    page: int
    page_size: int
    stats: dict[str, int]


class DealerZohoDetailOut(BaseModel):
    dealer_id: int
    zoho_contact_id: str
    source: Literal["sql", "cached"]
    contact: dict[str, Any] = Field(default_factory=dict)
    fallback_reason: str | None = None


def _phone_digits_sql(col: Any):
    """Digits-only phone string (mirrors phone_util.strip)."""
    return func.regexp_replace(func.coalesce(col, ""), r"[^0-9]", "", "g")


def _normalized_phone_sql(col: Any):
    """SQL equivalent of phone_util.normalize_in_mobile — 10 digits or ''."""
    digits = _phone_digits_sql(col)
    after_91 = case(
        (
            (func.length(digits) > 10) & (func.left(digits, 2) == "91"),
            func.substr(digits, 3),
        ),
        else_=digits,
    )
    after_0 = case(
        (
            (func.length(after_91) == 11) & (func.left(after_91, 1) == "0"),
            func.substr(after_91, 2),
        ),
        else_=after_91,
    )
    return case(
        (func.length(after_0) >= 10, func.right(after_0, 10)),
        else_=literal(""),
    )


def _no_whatsapp_clause(phone_col: Any) -> Any:
    """Blank or not a usable 10-digit Indian mobile."""
    return _normalized_phone_sql(phone_col) == ""


def _sales_by_customer_subq():
    """Lifetime sales per dealer, folding merge sources into their survivor id."""
    cid = remapped_customer_id_expr(ZohoSale.customer_id).label("customer_id")
    return (
        select(
            cid,
            func.coalesce(func.sum(sales_amount_expr()), 0).label("sales_total"),
            func.max(ZohoSale.date).label("last_sale_date"),
        )
        .where(ZohoSale.customer_id != "")
        .group_by(cid)
        .subquery()
    )


def _visible_dealers_clause() -> Any:
    return dealer_not_excluded_clause(Dealer.name, Dealer.company, Dealer.zoho_contact_id)


def _apply_list_scope(
    stmt: Any,
    *,
    q: str = "",
    state: str = "",
    city: str = "",
    district: str = "",
    kam: str = "",
    sales_sub: Any = None,
    sales_filter: str = "",
    cohort: str = "",
    sale_cutoff: str = "",
) -> Any:
    """Apply Advanced Filter + search scope shared by list and KPI strip stats."""
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Dealer.name.ilike(like),
                Dealer.company.ilike(like),
                Dealer.phone.ilike(like),
                Dealer.email.ilike(like),
                Dealer.city.ilike(like),
                Dealer.state.ilike(like),
                Dealer.zoho_contact_id.ilike(like),
                Dealer.gstin.ilike(like),
            )
        )
    state_key = state.strip()
    if state_key:
        stmt = stmt.where(Dealer.state == state_key)
    # UI labels district; DB only has city — accept either param.
    city_key = (city or district or "").strip()
    if city_key:
        stmt = stmt.where(Dealer.city == city_key)
    kam_key = kam.strip()
    if kam_key:
        stmt = stmt.where(Dealer.assigned_salesman == kam_key)
    sf = (sales_filter or "").strip().lower()
    if sales_sub is not None and sf in ("with_sales", "zero"):
        sales_amt = func.coalesce(sales_sub.c.sales_total, 0)
        if sf == "with_sales":
            stmt = stmt.where(sales_amt > 0)
        else:
            stmt = stmt.where(sales_amt == 0)
    cohort_key = (cohort or "").strip().lower()
    if sales_sub is not None and sale_cutoff and cohort_key == "active":
        stmt = stmt.where(_active_sales_clause(sales_sub, sale_cutoff))
    elif sales_sub is not None and sale_cutoff and cohort_key == "inactive":
        stmt = stmt.where(_inactive_sales_clause(sales_sub, sale_cutoff))
    return stmt


class SalesByStateRow(BaseModel):
    state: str
    sales_total: float
    invoice_count: int = 0


class SalesByStateOut(BaseModel):
    year: int
    items: list[SalesByStateRow]


_STATE_CANON: dict[str, str] = {
    "orissa": "Odisha",
    "odisha": "Odisha",
    "uttaranchal": "Uttarakhand",
    "uttarakhand": "Uttarakhand",
    "pondicherry": "Puducherry",
    "puducherry": "Puducherry",
    "nct of delhi": "Delhi",
    "delhi": "Delhi",
    "jammu & kashmir": "Jammu and Kashmir",
    "andaman & nicobar islands": "Andaman and Nicobar",
    "andaman and nicobar islands": "Andaman and Nicobar",
}


def _canon_state(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    key = " ".join(s.lower().replace("&", " and ").split())
    return _STATE_CANON.get(key) or s


@router.get("/sales-by-state", response_model=SalesByStateOut)
async def sales_by_state(
    db: Db,
    user: CurrentUser,
    year: int | None = Query(None, description="Calendar year (default: current year)"),
):
    """Sum zoho_sales totals by dealer state for a calendar year (invoice date)."""
    _ = user
    y = year or date.today().year
    y0 = f"{y:04d}-01-01"
    y1 = f"{y + 1:04d}-01-01"
    visible = _visible_dealers_clause()
    remapped = remapped_customer_id_expr(ZohoSale.customer_id)
    rows = (
        await db.execute(
            select(
                Dealer.state,
                func.coalesce(func.sum(sales_amount_expr()), 0).label("sales_total"),
                func.count().label("invoice_count"),
            )
            .select_from(ZohoSale)
            .join(Dealer, Dealer.zoho_contact_id == remapped)
            .where(visible)
            .where(ZohoSale.customer_id != "")
            .where(ZohoSale.date >= y0)
            .where(ZohoSale.date < y1)
            .where(Dealer.state.is_not(None))
            .where(Dealer.state != "")
            .group_by(Dealer.state)
        )
    ).all()

    merged: dict[str, tuple[float, int]] = {}
    for state_raw, sales_total, invoice_count in rows:
        state = _canon_state(str(state_raw or ""))
        if not state:
            continue
        prev_sales, prev_n = merged.get(state, (0.0, 0))
        merged[state] = (prev_sales + float(sales_total or 0), prev_n + int(invoice_count or 0))

    items = [
        SalesByStateRow(state=st, sales_total=round(sales, 2), invoice_count=n)
        for st, (sales, n) in merged.items()
        if sales > 0
    ]
    items.sort(key=lambda r: r.sales_total, reverse=True)
    return SalesByStateOut(year=y, items=items)


@router.get("/states", response_model=list[str])
async def list_dealer_states(db: Db, user: CurrentUser):
    """Distinct non-empty dealer states (sorted) for the toolbar filter."""
    _ = user
    visible = _visible_dealers_clause()
    rows = (
        await db.execute(
            select(func.distinct(Dealer.state))
            .where(visible)
            .where(Dealer.state.is_not(None))
            .where(Dealer.state != "")
            .order_by(Dealer.state.asc())
        )
    ).scalars().all()
    return [str(s).strip() for s in rows if str(s or "").strip()]


@router.get("/cities", response_model=list[str])
async def list_dealer_cities(
    db: Db,
    user: CurrentUser,
    state: str = Query("", description="Required: filter cities/districts for this state"),
):
    """Distinct non-empty cities for a state (used as District options in UI)."""
    _ = user
    state_key = state.strip()
    if not state_key:
        return []
    visible = _visible_dealers_clause()
    rows = (
        await db.execute(
            select(func.distinct(Dealer.city))
            .where(visible)
            .where(Dealer.state == state_key)
            .where(Dealer.city.is_not(None))
            .where(Dealer.city != "")
            .order_by(Dealer.city.asc())
        )
    ).scalars().all()
    return [str(c).strip() for c in rows if str(c or "").strip()]


@router.get("/kams", response_model=list[str])
async def list_dealer_kams(db: Db, user: CurrentUser):
    """Distinct non-empty assigned_salesman values for Advanced Filter KAM."""
    _ = user
    visible = _visible_dealers_clause()
    rows = (
        await db.execute(
            select(func.distinct(Dealer.assigned_salesman))
            .where(visible)
            .where(Dealer.assigned_salesman.is_not(None))
            .where(Dealer.assigned_salesman != "")
            .order_by(Dealer.assigned_salesman.asc())
        )
    ).scalars().all()
    return [str(k).strip() for k in rows if str(k or "").strip()]


@router.get("", response_model=DealerPageOut)
async def list_dealers(
    db: Db,
    user: CurrentUser,
    q: str = "",
    state: str = Query("", description="Exact match on dealers.state"),
    city: str = Query("", description="Exact match on dealers.city (district UI)"),
    district: str = Query(
        "",
        description="Alias for city — dealers have no district column",
    ),
    kam: str = Query("", description="Exact match on dealers.assigned_salesman"),
    sales_filter: str = Query(
        "",
        description="Sales amount filter: ''|all|with_sales|zero",
    ),
    cohort: str = Query(
        "",
        description="Advanced Filter status cohort: ''|all|active|inactive (sales recency)",
    ),
    kpi: str = Query(
        "",
        description="KPI strip filter: total|active|inactive|no_whatsapp|high_value|states",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=200),
    sort: str = "sales_total",
    order: str = "desc",
):
    _ = user
    visible = _visible_dealers_clause()
    sales_sub = _sales_by_customer_subq()
    sale_cutoff = _sale_cutoff_iso()
    cohort_key = (cohort or "").strip().lower()
    if cohort_key == "all":
        cohort_key = ""
    sf_key = (sales_filter or "").strip().lower()
    if sf_key == "all":
        sf_key = ""
    # LEFT JOIN so we can ORDER BY SUM(excl-GST sales) across all pages.
    stmt = (
        select(Dealer)
        .outerjoin(sales_sub, Dealer.zoho_contact_id == sales_sub.c.customer_id)
        .where(visible)
    )
    # Coalesce so dealers with no sales (NULL) sort with 0 — last when DESC.
    sales_total_col = func.coalesce(sales_sub.c.sales_total, 0)

    scope_kwargs = dict(
        q=q,
        state=state,
        city=city,
        district=district,
        kam=kam,
        sales_sub=sales_sub,
        sales_filter=sf_key,
        cohort=cohort_key,
        sale_cutoff=sale_cutoff,
    )
    stmt = _apply_list_scope(stmt, **scope_kwargs)

    kpi_key = (kpi or "").strip().lower()
    # KPI cohort composes with Advanced Filter (intersection). Skip duplicate
    # active/inactive when Advanced cohort already applies the same rule.
    if kpi_key == "active" and cohort_key != "active":
        stmt = stmt.where(_active_sales_clause(sales_sub, sale_cutoff))
    elif kpi_key == "inactive" and cohort_key != "inactive":
        stmt = stmt.where(_inactive_sales_clause(sales_sub, sale_cutoff))
    elif kpi_key == "no_whatsapp":
        stmt = stmt.where(_no_whatsapp_clause(Dealer.phone))
    elif kpi_key == "high_value":
        stmt = stmt.where(sales_total_col >= HIGH_VALUE_SALES)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
    sort_key = (sort or "sales_total").strip().lower()
    sort_map = {
        "id": Dealer.id,
        "name": Dealer.name,
        "company": Dealer.company,
        "phone": Dealer.phone,
        "email": Dealer.email,
        "city": Dealer.city,
        "state": Dealer.state,
        "status": Dealer.status,
        "zoho_contact_id": Dealer.zoho_contact_id,
        "sales_total": sales_total_col,
        "total_sales": sales_total_col,
        "last_sale_date": sales_sub.c.last_sale_date,
    }
    col = sort_map.get(sort_key, sales_total_col)
    descending = (order or "desc").strip().lower() != "asc"
    if descending:
        stmt = stmt.order_by(col.desc().nullslast(), Dealer.name.asc())
    else:
        stmt = stmt.order_by(col.asc().nullslast(), Dealer.name.asc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()

    # Include merge-source contact ids so page totals match remapped sales_sub.
    zoho_ids: list[str] = []
    seen_z: set[str] = set()
    for r in rows:
        for zid in sales_customer_ids_for_dealer(r.zoho_contact_id or ""):
            if zid not in seen_z:
                seen_z.add(zid)
                zoho_ids.append(zid)
    sales_map: dict[str, tuple[float, str | None]] = {}
    if zoho_ids:
        remapped = remapped_customer_id_expr(ZohoSale.customer_id).label("customer_id")
        sales_rows = (
            await db.execute(
                select(
                    remapped,
                    func.coalesce(func.sum(sales_amount_expr()), 0),
                    func.max(ZohoSale.date),
                )
                .where(ZohoSale.customer_id.in_(zoho_ids))
                .group_by(remapped)
            )
        ).all()
        for cid, sales_total, last_sale in sales_rows:
            last = (str(last_sale).strip() if last_sale else "") or None
            sales_map[str(cid)] = (float(sales_total or 0), last)

    items: list[DealerOut] = []
    for r in rows:
        out = DealerOut.model_validate(r)
        zid = (r.zoho_contact_id or "").strip()
        if zid and zid in sales_map:
            # Do not reuse `total` — that shadows the pagination dealer count.
            sales_amt, last_sale = sales_map[zid]
            out.sales_total = sales_amt
            out.last_sale_date = last_sale
        items.append(out)

    # KPI strip counts follow Advanced Filter + search scope (not the KPI tile).
    # Cohort/sales_filter need the sales join — always outer-join for stats base.
    def _stats_base(*, sales_join: str = "outer"):
        s = select(func.count()).select_from(Dealer)
        if sales_join == "inner":
            s = s.join(sales_sub, Dealer.zoho_contact_id == sales_sub.c.customer_id)
        else:
            s = s.outerjoin(sales_sub, Dealer.zoho_contact_id == sales_sub.c.customer_id)
        s = s.where(visible)
        return _apply_list_scope(s, **scope_kwargs)

    stats_total = (await db.execute(_stats_base())).scalar() or 0
    active = (
        await db.execute(
            _stats_base().where(_active_sales_clause(sales_sub, sale_cutoff))
        )
    ).scalar() or 0
    inactive = (
        await db.execute(
            _stats_base().where(_inactive_sales_clause(sales_sub, sale_cutoff))
        )
    ).scalar() or 0
    no_whatsapp = (
        await db.execute(_stats_base().where(_no_whatsapp_clause(Dealer.phone)))
    ).scalar() or 0

    high_value = (
        await db.execute(
            _stats_base(sales_join="inner").where(sales_sub.c.sales_total >= HIGH_VALUE_SALES)
        )
    ).scalar() or 0

    states_covered = (
        await db.execute(
            _apply_list_scope(
                select(func.count(func.distinct(Dealer.state)))
                .select_from(Dealer)
                .outerjoin(sales_sub, Dealer.zoho_contact_id == sales_sub.c.customer_id)
                .where(visible)
                .where(Dealer.state != "")
                .where(Dealer.state.is_not(None)),
                **scope_kwargs,
            )
        )
    ).scalar() or 0

    return DealerPageOut(
        items=items,
        total=int(total),
        page=page,
        page_size=page_size,
        stats={
            "total": int(stats_total),
            "active": int(active),
            "inactive": int(inactive),
            "no_whatsapp": int(no_whatsapp),
            "high_value": int(high_value),
            "states_covered": int(states_covered),
            # Exposed so UI copy always matches the live threshold (₹10 L = 1_000_000).
            "high_value_threshold": int(HIGH_VALUE_SALES),
        },
    )


def _cached_contact_from_staging(staging: ZohoDealer | None) -> dict[str, Any] | None:
    if not staging or not (staging.raw_json or "").strip():
        return None
    try:
        raw = json.loads(staging.raw_json)
    except json.JSONDecodeError:
        return None
    return raw if isinstance(raw, dict) else None


@router.get("/{dealer_id}/zoho", response_model=DealerZohoDetailOut)
async def dealer_zoho_detail(dealer_id: int, db: Db, user: CurrentUser):
    """Read-only Zoho contact detail from SQL only (no live Zoho API call)."""
    _ = user
    dealer = (
        await db.execute(select(Dealer).where(Dealer.id == dealer_id))
    ).scalar_one_or_none()
    if not dealer:
        raise HTTPException(404, "Dealer not found")

    zoho_id = (dealer.zoho_contact_id or "").strip()
    if not zoho_id:
        raise HTTPException(404, "Dealer has no Zoho contact id")

    staging = (
        await db.execute(select(ZohoDealer).where(ZohoDealer.zoho_contact_id == zoho_id))
    ).scalar_one_or_none()
    cached = _cached_contact_from_staging(staging)

    if cached:
        return DealerZohoDetailOut(
            dealer_id=dealer.id,
            zoho_contact_id=zoho_id,
            source="sql",
            contact=cached,
            fallback_reason=None,
        )

    minimal: dict[str, Any] = {
        "contact_id": zoho_id,
        "contact_name": dealer.name or "",
        "company_name": dealer.company or "",
        "email": dealer.email or "",
        "phone": dealer.phone or "",
        "gst_no": dealer.gstin or "",
        "status": dealer.status or "",
        "billing_address": {
            "city": dealer.city or "",
            "state": dealer.state or "",
        },
    }
    if staging:
        if staging.mobile:
            minimal["mobile"] = staging.mobile
        if staging.email and not minimal["email"]:
            minimal["email"] = staging.email
        if staging.phone and not minimal["phone"]:
            minimal["phone"] = staging.phone
        if staging.gst_no and not minimal["gst_no"]:
            minimal["gst_no"] = staging.gst_no
        if staging.contact_name and not minimal["contact_name"]:
            minimal["contact_name"] = staging.contact_name
        if staging.company_name and not minimal["company_name"]:
            minimal["company_name"] = staging.company_name
        if staging.status:
            minimal["status"] = staging.status
        if staging.billing_city and not minimal["billing_address"]["city"]:
            minimal["billing_address"]["city"] = staging.billing_city
        if staging.billing_state and not minimal["billing_address"]["state"]:
            minimal["billing_address"]["state"] = staging.billing_state

    return DealerZohoDetailOut(
        dealer_id=dealer.id,
        zoho_contact_id=zoho_id,
        source="sql",
        contact=minimal,
        fallback_reason="No full Zoho payload stored yet — run contact detail sync",
    )
