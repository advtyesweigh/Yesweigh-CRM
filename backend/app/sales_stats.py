"""Dashboard sales overview — MTD + last-6-months totals from zoho_sales."""
from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.dealer_exclusions import dealer_not_excluded_clause
from app.dealer_merges import remapped_customer_id_expr
from app.models import Dealer, User, ZohoSale
from app.product_categories import is_skipped_invoice_status
from app.sales_amount import sales_amount_expr

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-sales"])

Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

# Hide junk / non-salesperson buckets from "Sales by Salesman" (exact name match).
EXCLUDED_SALESPERSON_NAMES: frozenset[str] = frozenset(
    {
        "Others",
        "Safna",
        "Safna(Directors)",
        "GATC SELF",
        "Eldho - Spare",
    }
)

# Merge Zoho name variants into one display bucket before grouping.
SALESPERSON_NAME_ALIASES: dict[str, str] = {
    "Visakh - Inside kerala": "Visakh B",
    "Visakh - Spares": "Visakh B",
    "Shibin - Spares": "Shibin",
}

# Inclusive chart cutoff: keep this person, drop everyone ranked below.
SALESMAN_CHART_CUTOFF_NAME = "Sarita Solanki"


class MonthSalesPoint(BaseModel):
    year: int
    month: int
    label: str
    total: float = 0
    total_lakh: float = 0
    invoice_count: int = 0


class SalesOverviewOut(BaseModel):
    as_of: str
    this_month_total: float = 0
    this_month_invoice_count: int = 0
    this_year_total: float = 0
    this_year_invoice_count: int = 0
    fy_label: str = ""
    fy_from: str = ""
    fy_to: str = ""
    prior_fy_total: float = 0
    prior_fy_invoice_count: int = 0
    prior_fy_label: str = ""
    yoy_pct: float | None = None
    last_month_till_date_total: float = 0
    last_month_till_date_invoice_count: int = 0
    compare_from: str = ""
    compare_to: str = ""
    target: float | None = None
    target_pct: float | None = None
    months: list[MonthSalesPoint] = Field(default_factory=list)


class SalesmanSalesItem(BaseModel):
    name: str
    value: float = 0
    invoice_count: int = 0
    value_pct: float = 0


class SalesBySalesmanOut(BaseModel):
    year: int
    fy_label: str = ""
    fy_from: str = ""
    fy_to: str = ""
    total_value: float = 0
    items: list[SalesmanSalesItem] = Field(default_factory=list)


class TopDealerYtdItem(BaseModel):
    dealer_id: int
    name: str
    amount: float = 0
    invoice_count: int = 0


class TopDealersYtdOut(BaseModel):
    year: int
    fy_label: str = ""
    as_of: str
    from_date: str
    items: list[TopDealerYtdItem] = Field(default_factory=list)


def _f(v: Decimal | float | int | None) -> float:
    if v is None:
        return 0.0
    return float(v)


def _indian_fy_start_year(d: date) -> int:
    """FY starts 1 Apr; return the calendar year of that April."""
    return d.year if d.month >= 4 else d.year - 1


def _indian_fy_bounds(
    today: date | None = None,
    *,
    fy_start_year: int | None = None,
) -> tuple[date, date, str, int]:
    """Indian FY Apr 1 → Mar 31. Returns (start, end, 'YYYY-YY', start_year)."""
    d = today or date.today()
    start_y = fy_start_year if fy_start_year is not None else _indian_fy_start_year(d)
    fy_start = date(start_y, 4, 1)
    fy_end = date(start_y + 1, 3, 31)
    label = f"{start_y}-{str(start_y + 1)[-2:]}"
    return fy_start, fy_end, label, start_y


def _indian_fy_ytd_range(
    today: date | None = None,
    *,
    fy_start_year: int | None = None,
) -> tuple[str, str, str, int]:
    """FY start ISO → min(today, FY end) ISO, label, start year."""
    d = today or date.today()
    fy_start, fy_end, label, start_y = _indian_fy_bounds(d, fy_start_year=fy_start_year)
    end = min(d, fy_end)
    return fy_start.isoformat(), end.isoformat(), label, start_y


def _month_bounds(y: int, m: int) -> tuple[str, str]:
    last = monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def _shift_month(y: int, m: int, delta: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) + delta
    return idx // 12, idx % 12 + 1


def _month_label(y: int, m: int) -> str:
    # Match frontend en-GB short month + 2-digit year (e.g. Jun 26, Sept 26)
    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"]
    return f"{names[m - 1]} {y % 100:02d}"


@router.get("/sales-overview", response_model=SalesOverviewOut)
async def sales_overview(
    db: Db,
    user: CurrentUser,
    months_count: int = Query(
        default=6,
        ge=3,
        le=24,
        description="How many calendar months to include in `months` (newest first).",
    ),
):
    """Current MTD achieved, prior-month same-day window, FY YTD + prior FY, and month series."""
    _ = user
    today = date.today()
    y, m, d = today.year, today.month, today.day
    this_from, _this_to = _month_bounds(y, m)
    this_to_mtd = today.isoformat()

    ly, lm = _shift_month(y, m, -1)
    last_from, _ = _month_bounds(ly, lm)
    last_dom = min(d, monthrange(ly, lm)[1])
    last_to_mtd = f"{ly:04d}-{lm:02d}-{last_dom:02d}"

    async def sum_range(start: str, end: str) -> tuple[float, int]:
        row = (
            await db.execute(
                select(
                    func.coalesce(func.sum(sales_amount_expr()), 0),
                    func.count(),
                ).where(
                    ZohoSale.date != "",
                    ZohoSale.date >= start,
                    ZohoSale.date <= end,
                )
            )
        ).one()
        return round(_f(row[0]), 2), int(row[1] or 0)

    this_total, this_n = await sum_range(this_from, this_to_mtd)
    last_total, last_n = await sum_range(last_from, last_to_mtd)
    fy_from, fy_to, fy_label, fy_start_y = _indian_fy_ytd_range(today)
    year_total, year_n = await sum_range(fy_from, fy_to)

    # Prior FY same calendar YTD window (Apr 1 → same month/day last year).
    prior_start_y = fy_start_y - 1
    prior_from_d = date(prior_start_y, 4, 1)
    try:
        prior_to_d = date(today.year - 1, today.month, today.day)
    except ValueError:
        # Feb 29 → Feb 28
        prior_to_d = date(today.year - 1, today.month, 28)
    prior_fy_end = date(prior_start_y + 1, 3, 31)
    if prior_to_d > prior_fy_end:
        prior_to_d = prior_fy_end
    if prior_to_d < prior_from_d:
        prior_to_d = prior_from_d
    prior_from = prior_from_d.isoformat()
    prior_to = prior_to_d.isoformat()
    prior_label = f"{prior_start_y}-{str(prior_start_y + 1)[-2:]}"
    prior_total, prior_n = await sum_range(prior_from, prior_to)
    yoy_pct: float | None = None
    if prior_total > 0:
        yoy_pct = round(100.0 * (year_total - prior_total) / prior_total, 1)

    # Newest month first
    months: list[MonthSalesPoint] = []
    for i in range(0, months_count):
        my, mm = _shift_month(y, m, -i)
        start, end = _month_bounds(my, mm)
        # Current month is MTD so chart matches "this month achieved"
        if my == y and mm == m:
            end = this_to_mtd
        total, n = await sum_range(start, end)
        months.append(
            MonthSalesPoint(
                year=my,
                month=mm,
                label=_month_label(my, mm),
                total=total,
                total_lakh=round(total / 100_000.0, 2),
                invoice_count=n,
            )
        )

    # No company sales target table yet — leave null (UI shows — / hides %).
    return SalesOverviewOut(
        as_of=this_to_mtd,
        this_month_total=this_total,
        this_month_invoice_count=this_n,
        this_year_total=year_total,
        this_year_invoice_count=year_n,
        fy_label=fy_label,
        fy_from=fy_from,
        fy_to=fy_to,
        prior_fy_total=prior_total,
        prior_fy_invoice_count=prior_n,
        prior_fy_label=prior_label,
        yoy_pct=yoy_pct,
        last_month_till_date_total=last_total,
        last_month_till_date_invoice_count=last_n,
        compare_from=last_from,
        compare_to=last_to_mtd,
        target=None,
        target_pct=None,
        months=months,
    )


@router.get("/sales-by-salesman", response_model=SalesBySalesmanOut)
async def sales_by_salesman(
    db: Db,
    user: CurrentUser,
    year: int | None = Query(
        default=None,
        ge=2000,
        le=2100,
        description="Indian FY start year (Apr). Default: current FY.",
    ),
    limit: int = Query(default=12, ge=1, le=50),
):
    """Indian FY YTD sales (excl. GST) grouped by Zoho salesperson_name."""
    _ = user
    today = date.today()
    start, end, fy_label, y = _indian_fy_ytd_range(today, fy_start_year=year)

    rows = (
        await db.execute(
            select(
                ZohoSale.salesperson_name,
                ZohoSale.status,
                func.coalesce(func.sum(sales_amount_expr()), 0),
                func.count(),
            )
            .where(
                ZohoSale.date != "",
                ZohoSale.date >= start,
                ZohoSale.date <= end,
            )
            .group_by(ZohoSale.salesperson_name, ZohoSale.status)
        )
    ).all()

    # Aggregate in Python so we can skip void/draft and normalize blank names.
    buckets: dict[str, list[float | int]] = {}
    for name_raw, status, total, n in rows:
        st = str(status or "").strip().lower()
        if is_skipped_invoice_status(st) or st == "deleted":
            continue
        name = (name_raw or "").strip() or "Unassigned"
        name = SALESPERSON_NAME_ALIASES.get(name, name)
        if name in EXCLUDED_SALESPERSON_NAMES:
            continue
        prev = buckets.get(name)
        if prev is None:
            buckets[name] = [float(total or 0), int(n or 0)]
        else:
            prev[0] = float(prev[0]) + float(total or 0)
            prev[1] = int(prev[1]) + int(n or 0)

    ranked = sorted(buckets.items(), key=lambda kv: kv[1][0], reverse=True)
    # total_value + value_pct are based on remaining people only (bars vs top remaining).
    grand = sum(float(v[0]) for _, v in ranked)

    cutoff_key = SALESMAN_CHART_CUTOFF_NAME.casefold()
    cutoff_idx = next(
        (i for i, (name, _) in enumerate(ranked) if name.strip().casefold() == cutoff_key),
        None,
    )
    if cutoff_idx is not None:
        # Stop at Sarita Solanki (inclusive); still honor a smaller limit above her.
        top = ranked[: min(limit, cutoff_idx + 1)]
    else:
        top = ranked[:limit]

    max_val = float(top[0][1][0]) if top else 0.0

    items: list[SalesmanSalesItem] = []
    for name, (val, n) in top:
        pct = round((float(val) / max_val) * 100.0, 1) if max_val > 0 else 0.0
        items.append(
            SalesmanSalesItem(
                name=name,
                value=round(float(val), 2),
                invoice_count=int(n),
                value_pct=pct,
            )
        )

    return SalesBySalesmanOut(
        year=y,
        fy_label=fy_label,
        fy_from=start,
        fy_to=end,
        total_value=round(grand, 2),
        items=items,
    )


@router.get("/top-dealers-ytd", response_model=TopDealersYtdOut)
async def top_dealers_ytd(
    db: Db,
    user: CurrentUser,
    year: int | None = Query(
        default=None,
        ge=2000,
        le=2100,
        description="Indian FY start year (Apr). Default: current FY.",
    ),
    limit: int = Query(default=6, ge=1, le=25),
):
    """Top dealers by Indian FY YTD sales (excl. GST / sub_total), ranked by amount."""
    _ = user
    today = date.today()
    year_from, year_to, fy_label, y = _indian_fy_ytd_range(today, fy_start_year=year)
    visible = dealer_not_excluded_clause(Dealer.name, Dealer.company, Dealer.zoho_contact_id)
    remapped = remapped_customer_id_expr(ZohoSale.customer_id)

    rows = (
        await db.execute(
            select(
                Dealer.id,
                Dealer.company,
                Dealer.name,
                ZohoSale.status,
                func.coalesce(func.sum(sales_amount_expr()), 0),
                func.count(),
            )
            .select_from(ZohoSale)
            .join(Dealer, Dealer.zoho_contact_id == remapped)
            .where(visible)
            .where(ZohoSale.customer_id != "")
            .where(ZohoSale.date != "")
            .where(ZohoSale.date >= year_from)
            .where(ZohoSale.date <= year_to)
            .group_by(Dealer.id, Dealer.company, Dealer.name, ZohoSale.status)
        )
    ).all()

    buckets: dict[int, list[object]] = {}
    for dealer_id, company, name, status, total, n in rows:
        st = str(status or "").strip().lower()
        if is_skipped_invoice_status(st) or st == "deleted":
            continue
        label = (company or "").strip() or (name or "").strip() or "Unknown"
        prev = buckets.get(int(dealer_id))
        if prev is None:
            buckets[int(dealer_id)] = [label, float(total or 0), int(n or 0)]
        else:
            prev[1] = float(prev[1]) + float(total or 0)
            prev[2] = int(prev[2]) + int(n or 0)

    ranked = sorted(buckets.items(), key=lambda kv: float(kv[1][1]), reverse=True)[:limit]
    items = [
        TopDealerYtdItem(
            dealer_id=did,
            name=str(vals[0]),
            amount=round(float(vals[1]), 2),
            invoice_count=int(vals[2]),
        )
        for did, vals in ranked
    ]
    return TopDealersYtdOut(
        year=y,
        fy_label=fy_label,
        as_of=year_to,
        from_date=year_from,
        items=items,
    )


# Backward-compatible alias (same YTD behavior).
@router.get("/top-dealers-mtd", response_model=TopDealersYtdOut, include_in_schema=False)
async def top_dealers_mtd_alias(
    db: Db,
    user: CurrentUser,
    year: int | None = Query(default=None, ge=2000, le=2100),
    limit: int = Query(default=6, ge=1, le=25),
):
    return await top_dealers_ytd(db=db, user=user, year=year, limit=limit)
