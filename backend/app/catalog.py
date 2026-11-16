"""CRM product catalog API — categories + products synced from YesWeigh Firebase."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.catalog_exclusions import is_excluded_category
from app.config import get_settings
from app.db import get_db
from app.models import CatalogCategory, CatalogProduct, User

router = APIRouter(prefix="/api/catalog", tags=["catalog"])

Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

# Match YesOne fetchCatalogPayloadFromFirestore: status == 'active'
_ACTIVE_PRODUCT = and_(
    CatalogProduct.hidden_from_catalog.is_(False),
    CatalogProduct.status.ilike("active"),
)


def _catalog_image_root() -> Path:
    return Path(get_settings().catalog_image_dir)


def _image_public_url(local_path: str, remote_url: str) -> str:
    if local_path:
        root = _catalog_image_root().resolve()
        try:
            rel = Path(local_path).resolve().relative_to(root)
            return f"/api/catalog/images/{rel.as_posix()}"
        except (ValueError, OSError):
            pass
    return remote_url or ""


def _money(v: Decimal | None) -> float | None:
    if v is None:
        return None
    return float(v)


def _qty(v: Decimal | None) -> float | None:
    if v is None:
        return None
    return float(v)


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str = ""
    display_order: int = 0
    product_count: int = 0
    image_url: str = ""
    is_weighing_scale: bool = False


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    sku: str = ""
    description: str = ""
    unit: str = "pcs"
    rate: float | None = None
    stock: float | None = None
    stock_status: str = ""
    image_url: str = ""
    image_urls: list[str] = Field(default_factory=list)
    category_id: str = ""
    category_name: str = ""
    status: str = ""
    model_number: str = ""
    discontinued_soon: bool = False
    new_arrival: bool = False
    display_order: int = 0


class CatalogBrowseOut(BaseModel):
    categories: list[CategoryOut]
    products: list[ProductOut]
    total_products: int
    synced_at: str | None = None


def _cat_out(row: CatalogCategory) -> CategoryOut:
    return CategoryOut(
        id=row.firebase_id,
        name=row.name,
        description=row.description or "",
        display_order=row.display_order or 0,
        product_count=row.product_count or 0,
        image_url=_image_public_url(row.local_image_path or "", row.thumbnail_url or ""),
        is_weighing_scale=bool(row.is_weighing_scale),
    )


def _prod_out(row: CatalogProduct) -> ProductOut:
    urls: list[str] = []
    try:
        parsed = json.loads(row.image_urls_json or "[]")
        if isinstance(parsed, list):
            urls = [str(u) for u in parsed if u]
    except json.JSONDecodeError:
        urls = []
    primary = _image_public_url(row.local_image_path or "", row.image_url or "")
    # Prefer local primary; keep remotes for gallery extras
    gallery = [primary] if primary else []
    for u in urls:
        pub = u
        if pub and pub not in gallery:
            gallery.append(pub)
    return ProductOut(
        id=row.firebase_id,
        name=row.name,
        sku=row.sku or "",
        description=row.description or "",
        unit=row.unit or "pcs",
        rate=_money(row.rate),
        stock=_qty(row.stock),
        stock_status=row.stock_status or "",
        image_url=primary or (urls[0] if urls else ""),
        image_urls=gallery,
        category_id=row.category_id or "",
        category_name=row.category_name or "",
        status=row.status or "",
        model_number=row.model_number or "",
        discontinued_soon=bool(row.discontinued_soon),
        new_arrival=bool(row.new_arrival),
        display_order=row.display_order or 0,
    )


async def _active_counts_by_category(db: AsyncSession) -> dict[str, int]:
    rows = (
        await db.execute(
            select(CatalogProduct.category_id, func.count())
            .where(_ACTIVE_PRODUCT)
            .group_by(CatalogProduct.category_id)
        )
    ).all()
    return {str(cid): int(n) for cid, n in rows if cid}


@router.get("/browse", response_model=CatalogBrowseOut)
async def browse_catalog(
    db: Db,
    user: CurrentUser,
    category_id: str = "",
    q: str = "",
):
    cats = (
        await db.execute(
            select(CatalogCategory).order_by(
                CatalogCategory.display_order, CatalogCategory.name
            )
        )
    ).scalars().all()
    active_counts = await _active_counts_by_category(db)
    cat_outs: list[CategoryOut] = []
    for c in cats:
        if is_excluded_category(c.name):
            continue
        n = active_counts.get(c.firebase_id, 0)
        if n <= 0:
            continue
        out = _cat_out(c)
        out.product_count = n
        cat_outs.append(out)

    stmt = select(CatalogProduct).where(_ACTIVE_PRODUCT)
    cid = (category_id or "").strip()
    if cid:
        stmt = stmt.where(CatalogProduct.category_id == cid)
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        stmt = stmt.where(
            or_(
                CatalogProduct.name.ilike(like),
                CatalogProduct.sku.ilike(like),
                CatalogProduct.category_name.ilike(like),
                CatalogProduct.model_number.ilike(like),
            )
        )
    # YesOne compareCatalogProductsInCategory: displayOrder then name
    stmt = stmt.order_by(
        CatalogProduct.display_order, CatalogProduct.name
    )
    products = (await db.execute(stmt.limit(2000))).scalars().all()
    prod_outs = [
        _prod_out(p) for p in products if not is_excluded_category(p.category_name or "")
    ]

    synced = None
    if cats:
        times = [c.synced_at for c in cats if c.synced_at]
        if times:
            synced = max(times).isoformat()

    total_active = (
        await db.execute(
            select(func.count()).select_from(CatalogProduct).where(_ACTIVE_PRODUCT)
        )
    ).scalar() or 0

    return CatalogBrowseOut(
        categories=cat_outs,
        products=prod_outs if cid or term else [],
        total_products=int(total_active),
        synced_at=synced,
    )


@router.get("/categories", response_model=list[CategoryOut])
async def list_categories(db: Db, user: CurrentUser):
    rows = (
        await db.execute(
            select(CatalogCategory).order_by(
                CatalogCategory.display_order, CatalogCategory.name
            )
        )
    ).scalars().all()
    active_counts = await _active_counts_by_category(db)
    out: list[CategoryOut] = []
    for c in rows:
        if is_excluded_category(c.name):
            continue
        n = active_counts.get(c.firebase_id, 0)
        if n <= 0:
            continue
        item = _cat_out(c)
        item.product_count = n
        out.append(item)
    return out


@router.get("/products", response_model=list[ProductOut])
async def list_products(
    db: Db,
    user: CurrentUser,
    category_id: str = "",
    q: str = "",
    limit: int = Query(500, ge=1, le=2000),
):
    stmt = select(CatalogProduct).where(_ACTIVE_PRODUCT)
    if category_id.strip():
        stmt = stmt.where(CatalogProduct.category_id == category_id.strip())
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                CatalogProduct.name.ilike(like),
                CatalogProduct.sku.ilike(like),
                CatalogProduct.category_name.ilike(like),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(CatalogProduct.display_order, CatalogProduct.name).limit(limit)
        )
    ).scalars().all()
    return [_prod_out(r) for r in rows if not is_excluded_category(r.category_name or "")]


@router.get("/products/{product_id}", response_model=ProductOut)
async def get_product(product_id: str, db: Db, user: CurrentUser):
    row = (
        await db.execute(
            select(CatalogProduct).where(CatalogProduct.firebase_id == product_id)
        )
    ).scalar_one_or_none()
    if not row or row.hidden_from_catalog:
        raise HTTPException(404, "Product not found")
    return _prod_out(row)


@router.get("/images/{path:path}")
async def catalog_image(path: str):
    """Serve locally cached catalog images (no auth — public product photos)."""
    root = _catalog_image_root().resolve()
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(404, "Not found") from exc
    if not target.is_file():
        raise HTTPException(404, "Not found")
    media = "image/jpeg"
    lower = target.suffix.lower()
    if lower == ".png":
        media = "image/png"
    elif lower == ".webp":
        media = "image/webp"
    elif lower == ".gif":
        media = "image/gif"
    return FileResponse(target, media_type=media)
