"""Dashboard product sales APIs — top SKUs by value + qty for a calendar year."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.catalog import _image_public_url
from app.catalog_exclusions import is_excluded_category, is_sales_qty_category
from app.db import get_db
from app.models import CatalogProduct, User, ZohoInvoiceLineItem
from app.product_categories import PRODUCT_CATEGORIES

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-sales"])

Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

# Non-product invoice lines (services, fees, freight, blanks, accessories noise).
_NON_PRODUCT_HINTS = (
    "freight",
    "shipping",
    "transport",
    "courier",
    "delivery charge",
    "packing charge",
    "handling charge",
    "calibration",
    "stamping",
    "gatc",
    " spare",
    "spares",
    "spare part",
    "fee",
    "fees",
    "installation charge",
    "service charge",
    "amc ",
    " amc",
    "warranty charge",
    "round off",
    "discount",
    "on/off switch",
    "on off switch",
    "dust cover",
    "stamping plate",
    "stamping fee",
)

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)
# Model-ish tokens that help fuzzy catalog matching (Q8, Qz2, YSA204E, …).
_MODEL_RE = re.compile(r"(?:q\d+\w*|qz\d+|ys[a-z]?\d+\w*|[a-z]{2,}\d{2,}[a-z]*)", re.I)

# Merge Zoho SKU / name variants into one dashboard display row (before ranking).
# Keys are `_norm_name` forms; value is the UI label.
PRODUCT_NAME_ALIASES: dict[str, str] = {
    "bench scale qz1": "Bench Scale",
    "bench scale qz2": "Bench Scale",
    "benchscale qz1": "Bench Scale",
    "benchscale qz2": "Bench Scale",
    "bench scale qz 1": "Bench Scale",
    "bench scale qz 2": "Bench Scale",
    # Same Zoho items often invoice under the older "PLATFORM SCALE -BENCH (G-QZ*)" title.
    "platform scale -bench (g-qz1)": "Bench Scale",
    "platform scale -bench (g-qz2)": "Bench Scale",
    "platform scale - bench (g-qz1)": "Bench Scale",
    "platform scale - bench (g-qz2)": "Bench Scale",
    "platform scale-bench (g-qz1)": "Bench Scale",
    "platform scale-bench (g-qz2)": "Bench Scale",
}
# Known Zoho product_ids for Qz1 / Qz2 (covers every invoice title for that id).
PRODUCT_ID_ALIASES: dict[str, str] = {
    "99381000024199057": "Bench Scale",  # Qz1
    "99381000027664507": "Bench Scale",  # Qz2
}
# Catch spacing / case drift: "BENCH Scale Qz1", "PLATFORM SCALE -BENCH (G-QZ-2)", …
_PRODUCT_ALIAS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^bench\s*scale\s*qz[\s\-]*[12]\b", re.I), "Bench Scale"),
    (
        re.compile(r"^platform\s*scale\s*-?\s*bench\s*\(?\s*g[\s\-]*qz[\s\-]*[12]\s*\)?\s*$", re.I),
        "Bench Scale",
    ),
)


class ProductSalesItemOut(BaseModel):
    name: str
    sku: str = ""
    value: float = 0
    qty: float = 0
    value_pct: float = 0
    qty_pct: float = 0
    image_url: str = ""


class ProductSalesOut(BaseModel):
    year: int
    total_value: float = 0
    total_qty: float = 0
    by_value: list[ProductSalesItemOut] = Field(default_factory=list)
    by_qty: list[ProductSalesItemOut] = Field(default_factory=list)
    # Legacy alias kept so older clients still render something.
    categories: list[ProductSalesItemOut] = Field(default_factory=list)
    sort: str = "value"


class CategorySalesItemOut(BaseModel):
    category: str
    value: float = 0
    qty: float = 0
    value_pct: float = 0
    qty_pct: float = 0


class CategorySalesOut(BaseModel):
    year: int
    total_value: float = 0
    total_qty: float = 0
    items: list[CategorySalesItemOut] = Field(default_factory=list)
    sort: str = "value"


def _f(v: Decimal | float | int | None) -> float:
    if v is None:
        return 0.0
    return float(v)


def _is_non_product(name: str) -> bool:
    n = (name or "").strip().lower()
    if not n:
        return True
    return any(h in n for h in _NON_PRODUCT_HINTS)


def _norm_name(name: str) -> str:
    return " ".join((name or "").lower().split())


def _compact(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _aliased_display_name(item_name: str, product_id: str = "") -> str | None:
    """Return canonical display name when this Zoho line should merge with siblings."""
    pid = (product_id or "").strip()
    if pid and pid in PRODUCT_ID_ALIASES:
        return PRODUCT_ID_ALIASES[pid]
    n = _norm_name(item_name)
    if not n:
        return None
    hit = PRODUCT_NAME_ALIASES.get(n)
    if hit:
        return hit
    # Also try compact-key lookup for spacing-only drift already in the dict.
    compact = _compact(item_name)
    for raw, label in PRODUCT_NAME_ALIASES.items():
        if _compact(raw) == compact:
            return label
    for pat, label in _PRODUCT_ALIAS_PATTERNS:
        if pat.search(n):
            return label
    return None


def _resolve_alias(names: list[str], product_id: str = "", display_name: str = "") -> str | None:
    """Alias if product_id or any known Zoho title for the bucket matches."""
    pid = (product_id or "").strip()
    if pid and pid in PRODUCT_ID_ALIASES:
        return PRODUCT_ID_ALIASES[pid]
    for candidate in [display_name, *names]:
        hit = _aliased_display_name(candidate, "")
        if hit:
            return hit
    return None


def _tokens(name: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(name or "") if len(t) > 1}


def _models(name: str) -> set[str]:
    return {m.lower() for m in _MODEL_RE.findall(name or "")}


class _CatRow:
    __slots__ = ("name", "sku", "norm", "compact", "tokens", "models", "image_url")

    def __init__(self, name: str, sku: str, image_url: str):
        self.name = name
        self.sku = sku
        self.norm = _norm_name(name)
        self.compact = _compact(name)
        self.tokens = _tokens(name)
        self.models = _models(name) | _models(sku)
        self.image_url = image_url


async def _load_catalog_rows(db: AsyncSession) -> list[_CatRow]:
    """Active hardware catalog rows used for Sales-by-Items-Qty matching.

    Includes Products-page hardware categories only (scales, POS, printers, …).
    Excludes SOFTWARE KEYS, spare/stamping/inactive categories, and blank-category
    orphan rows that otherwise inflate unit totals via fuzzy name match.
    """
    rows = (
        await db.execute(
            select(
                CatalogProduct.name,
                CatalogProduct.sku,
                CatalogProduct.category_name,
                CatalogProduct.image_url,
                CatalogProduct.local_image_path,
            ).where(
                and_(
                    CatalogProduct.hidden_from_catalog.is_(False),
                    CatalogProduct.status.ilike("active"),
                )
            )
        )
    ).all()
    out: list[_CatRow] = []
    for name, sku, cat_name, image_url, local_path in rows:
        cat = cat_name or ""
        if is_excluded_category(cat):
            continue
        if not is_sales_qty_category(cat):
            continue
        url = _image_public_url(local_path or "", image_url or "")
        out.append(_CatRow(name=name or "", sku=sku or "", image_url=url))
    return out


def _catalog_indexes(
    rows: list[_CatRow],
) -> tuple[dict[str, _CatRow], dict[str, _CatRow], list[_CatRow]]:
    by_name: dict[str, _CatRow] = {}
    by_sku: dict[str, _CatRow] = {}
    with_image: list[_CatRow] = []
    for row in rows:
        if row.norm and row.norm not in by_name:
            by_name[row.norm] = row
        sk = (row.sku or "").strip().lower()
        if sk and sk not in by_sku:
            by_sku[sk] = row
        if row.image_url:
            with_image.append(row)
    return by_name, by_sku, with_image


def _best_catalog_match(
    names: list[str],
    product_id: str,
    by_name: dict[str, _CatRow],
    by_sku: dict[str, _CatRow],
    candidates: list[_CatRow],
) -> _CatRow | None:
    """Match Zoho item → catalog product (exact / sku / fuzzy)."""
    sk = (product_id or "").strip().lower()
    if sk and sk in by_sku:
        return by_sku[sk]

    for name in names:
        key = _norm_name(name)
        if key and key in by_name:
            return by_name[key]

    # Compact equality (spacing / punctuation drift).
    for name in names:
        c = _compact(name)
        if len(c) < 6:
            continue
        for row in candidates:
            if row.compact == c:
                return row

    best: _CatRow | None = None
    best_score = 0.0
    for name in names:
        n_norm = _norm_name(name)
        n_tok = _tokens(name)
        n_mod = _models(name)
        if not n_norm:
            continue
        for row in candidates:
            score = 0.0
            # Substring either way (min length guards wrong thumbs).
            if len(row.norm) >= 10 and (row.norm in n_norm or n_norm in row.norm):
                score += 4.0
            elif len(n_norm) >= 12 and len(row.norm) >= 12:
                # Prefix / shared stem
                if n_norm[:14] == row.norm[:14]:
                    score += 3.0

            shared_models = n_mod & row.models
            if shared_models:
                score += 3.5 * len(shared_models)

            if n_tok and row.tokens:
                inter = n_tok & row.tokens
                # Drop ultra-common noise tokens
                inter -= {"scale", "scales", "with", "and", "for", "the", "ss", "ms", "pc"}
                if inter:
                    score += 1.2 * len(inter)
                    score += 2.0 * len(inter) / max(len(n_tok), len(row.tokens))

            # Prefer rows that look like finished goods (shorter accessory names lose).
            if "cover" in row.tokens or "board" in row.tokens or "sticker" in row.tokens:
                score *= 0.35
            if "load" in row.tokens and "cell" in row.tokens:
                score *= 0.4

            if score > best_score:
                best_score = score
                best = row

    # Threshold: avoid random weak matches.
    if best and best_score >= 4.0:
        return best
    return None


def _match_image(
    names: list[str],
    product_id: str,
    by_name: dict[str, _CatRow],
    by_sku: dict[str, _CatRow],
    with_image: list[_CatRow],
) -> str:
    hit = _best_catalog_match(names, product_id, by_name, by_sku, with_image)
    return hit.image_url if hit else ""


def _in_catalog(
    names: list[str],
    product_id: str,
    by_name: dict[str, _CatRow],
    by_sku: dict[str, _CatRow],
    all_rows: list[_CatRow],
) -> bool:
    return _best_catalog_match(names, product_id, by_name, by_sku, all_rows) is not None


def _sku_key(product_id: str, item_name: str) -> str:
    """Primary bucket: same Zoho product_id (all invoice titles for that id)."""
    pid = (product_id or "").strip()
    if pid:
        return f"id:{pid}"
    return f"name:{_norm_name(item_name)}"


def _accumulate_bucket(
    merged: dict[str, dict],
    *,
    key: str,
    display_name: str,
    raw_names: list[str],
    sku: str,
    value_f: float,
    qty_f: float,
    aliased: bool,
) -> None:
    cur = merged.get(key)
    if not cur:
        merged[key] = {
            "name": display_name,
            "names": list(raw_names),
            "sku": sku,
            "value": value_f,
            "qty": qty_f,
            "name_value": value_f,
            "aliased": aliased,
        }
        return
    cur["value"] += value_f
    cur["qty"] += qty_f
    for n in raw_names:
        if n and n not in cur["names"]:
            cur["names"].append(n)
    if aliased:
        cur["name"] = display_name
        cur["aliased"] = True
    elif not cur.get("aliased") and value_f > cur["name_value"]:
        cur["name"] = display_name
        cur["name_value"] = value_f
    if sku and not cur["sku"]:
        cur["sku"] = sku


@router.get("/sales-by-product", response_model=ProductSalesOut)
async def sales_by_product(
    db: Db,
    user: CurrentUser,
    year: int | None = Query(None, description="Calendar year (default: current)"),
    sort: str = Query("value", pattern="^(value|qty)$"),
    limit: int = Query(10, ge=1, le=500, description="Top N products per ranking"),
):
    """Top invoice line-item products (by Zoho product_id / item name) for the calendar year.

    `by_qty` / `total_qty` match hardware Products-page catalog categories only
    (excludes SOFTWARE KEYS, spare/stamping categories, blank-category orphans).
    `by_value` stays broader (revenue SKUs) but still drops obvious service/fee lines.
    """
    y = year or date.today().year
    start = f"{y}-01-01"
    end = f"{y}-12-31"

    rows = (
        await db.execute(
            select(
                ZohoInvoiceLineItem.item_name,
                ZohoInvoiceLineItem.product_id,
                func.coalesce(func.sum(ZohoInvoiceLineItem.item_total), 0).label("value"),
                func.coalesce(func.sum(ZohoInvoiceLineItem.quantity), 0).label("qty"),
            )
            .where(
                ZohoInvoiceLineItem.invoice_date >= start,
                ZohoInvoiceLineItem.invoice_date <= end,
            )
            .group_by(ZohoInvoiceLineItem.item_name, ZohoInvoiceLineItem.product_id)
        )
    ).all()

    # Pass 1: coalesce every invoice title that shares a Zoho product_id.
    by_id: dict[str, dict] = {}
    for item_name, product_id, value, qty in rows:
        raw_name = (item_name or "").strip()
        if _is_non_product(raw_name):
            continue
        pid = (product_id or "").strip()
        key = _sku_key(pid, raw_name)
        _accumulate_bucket(
            by_id,
            key=key,
            display_name=raw_name,
            raw_names=[raw_name] if raw_name else [],
            sku=pid,
            value_f=_f(value),
            qty_f=_f(qty),
            aliased=False,
        )

    # Pass 2: merge Qz1/Qz2 (and name/id aliases) into one display row before ranking.
    merged: dict[str, dict] = {}
    for it in by_id.values():
        names = it.get("names") or [it["name"]]
        alias = _resolve_alias(names, it.get("sku") or "", it.get("name") or "")
        display = alias or it["name"]
        key = f"alias:{_norm_name(alias)}" if alias else f"row:{id(it)}"
        _accumulate_bucket(
            merged,
            key=key,
            display_name=display,
            raw_names=names,
            sku=it.get("sku") or "",
            value_f=float(it["value"]),
            qty_f=float(it["qty"]),
            aliased=bool(alias),
        )

    catalog_rows = await _load_catalog_rows(db)
    by_name, by_sku, with_image = _catalog_indexes(catalog_rows)

    all_items = list(merged.values())
    for it in all_items:
        names = it.get("names") or [it["name"]]
        it["in_catalog"] = _in_catalog(names, it["sku"], by_name, by_sku, catalog_rows)
        it["image_url"] = _match_image(names, it["sku"], by_name, by_sku, with_image)

    value_items = all_items
    qty_items = [it for it in all_items if it["in_catalog"]]

    total_value = sum(v["value"] for v in value_items)
    total_qty = sum(v["qty"] for v in qty_items)

    def _to_out(items: list[dict], *, pct_mode: str) -> list[ProductSalesItemOut]:
        out: list[ProductSalesItemOut] = []
        for it in items:
            value = it["value"]
            qty = it["qty"]
            out.append(
                ProductSalesItemOut(
                    name=it["name"],
                    sku=it["sku"],
                    value=round(value, 2),
                    qty=round(qty, 3),
                    value_pct=round((100.0 * value / total_value) if total_value else 0.0, 1),
                    qty_pct=round((100.0 * qty / total_qty) if total_qty else 0.0, 1),
                    image_url=it.get("image_url") or "",
                )
            )
        return out

    by_value = _to_out(
        sorted(value_items, key=lambda x: (-x["value"], x["name"]))[:limit],
        pct_mode="value",
    )
    by_qty = _to_out(
        sorted(qty_items, key=lambda x: (-x["qty"], x["name"]))[:limit],
        pct_mode="qty",
    )
    primary = by_value if sort == "value" else by_qty

    return ProductSalesOut(
        year=y,
        total_value=round(total_value, 2),
        total_qty=round(total_qty, 3),
        by_value=by_value,
        by_qty=by_qty,
        categories=primary,
        sort=sort,
    )


@router.get("/sales-by-category", response_model=CategorySalesOut)
async def sales_by_category(
    db: Db,
    user: CurrentUser,
    year: int | None = Query(None, description="Calendar year (default: current)"),
    sort: str = Query("value", pattern="^(value|qty)$"),
):
    """Aggregate invoice line items by CRM product_category buckets for a calendar year."""
    _ = user
    y = year or date.today().year
    start = f"{y}-01-01"
    end = f"{y}-12-31"

    rows = (
        await db.execute(
            select(
                ZohoInvoiceLineItem.product_category,
                func.coalesce(func.sum(ZohoInvoiceLineItem.item_total), 0).label("value"),
                func.coalesce(func.sum(ZohoInvoiceLineItem.quantity), 0).label("qty"),
            )
            .where(
                ZohoInvoiceLineItem.invoice_date >= start,
                ZohoInvoiceLineItem.invoice_date <= end,
            )
            .group_by(ZohoInvoiceLineItem.product_category)
        )
    ).all()

    by_cat = {c: {"value": 0.0, "qty": 0.0} for c in PRODUCT_CATEGORIES}
    for cat, value, qty in rows:
        key = (cat or "").strip() or "Others"
        if key not in by_cat:
            key = "Others"
        by_cat[key]["value"] += _f(value)
        by_cat[key]["qty"] += _f(qty)

    total_value = sum(v["value"] for v in by_cat.values())
    total_qty = sum(v["qty"] for v in by_cat.values())

    items: list[CategorySalesItemOut] = []
    for cat, vals in by_cat.items():
        value = vals["value"]
        qty = vals["qty"]
        items.append(
            CategorySalesItemOut(
                category=cat,
                value=round(value, 2),
                qty=round(qty, 3),
                value_pct=round((100.0 * value / total_value) if total_value else 0.0, 1),
                qty_pct=round((100.0 * qty / total_qty) if total_qty else 0.0, 1),
            )
        )

    if sort == "qty":
        items.sort(key=lambda c: (-c.qty, c.category))
    else:
        items.sort(key=lambda c: (-c.value, c.category))

    return CategorySalesOut(
        year=y,
        total_value=round(total_value, 2),
        total_qty=round(total_qty, 3),
        items=items,
        sort=sort,
    )
