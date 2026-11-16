"""Sync YesWeigh Firebase catalog → CRM Postgres (+ optional local images).

Auth: synthetic Aadhaar email `{digits}@yesweigh.auth` via Identity Toolkit REST
(same as service.yesweigh.in login). Catalog Firestore + Storage `catalog/` are
publicly readable (`allow read: if true`); Auth is still used when credentials
are provided.

Exclude: GENERIC SPARE PARTS, STAMPING GJ, STAMPING KL, INACTIVE
(category + products) — see catalog_exclusions. SOFTWARE KEYS are included.
Also skip non-active products (status != active), matching YesOne browse.

Env (never commit secrets):
  FIREBASE_EMAIL / FIREBASE_PASSWORD  — or FIREBASE_LOGIN_ID (Aadhaar/phone) + password
  FIREBASE_API_KEY                    — defaults to yesweigh-service web key
  FIREBASE_PROJECT_ID                 — default yesweigh-service
  DATABASE_URL                        — CRM Postgres (asyncpg URL ok)
  CATALOG_IMAGE_DIR                   — default /data/catalog-images
  DOWNLOAD_IMAGES=0                   — download image bytes (default 0; URLs stored always)
  DRY_RUN=1                           — fetch only, no DB writes

Run:
  docker exec -e FIREBASE_LOGIN_ID=... -e FIREBASE_PASSWORD=... \\
    yesweigh-crm-api python -m app.sync_firebase_catalog
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from sqlalchemy import delete, select

from app.db import SessionLocal, engine
from app import models as _models  # noqa: F401
from app.catalog_exclusions import (
    excluded_category_display_names,
    is_excluded_category,
)
from app.models import CatalogCategory, CatalogProduct
from app.db import Base

# yesweighservice src/firebase.ts web client key (public)
DEFAULT_API_KEY = "AIzaSyBiLI--JR640YlXb2uiuBrLJ83XtFMPncE"
DEFAULT_PROJECT = "yesweigh-service"
AUTH_EMAIL_DOMAIN = "yesweigh.auth"

CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "weighing import": "Imported weighing scales & precision equipment",
    "weighing india": "Made-in-India weighing solutions",
    "printing scales": "Label printing & receipt weighing scales",
    "analytical scales": "High-precision lab & analytical balances",
    "indicators": "Digital weight indicators & displays",
    "industrial scales": "Heavy-duty industrial weighing systems",
    "software key": "License keys & software activation products",
}


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def auth_email_from_login(login_id: str) -> str:
    """Mirror yesweighservice loginAuth.authEmailForLoginId."""
    raw = login_id.strip()
    if "@" in raw:
        return raw.lower()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12:
        return f"{digits}@{AUTH_EMAIL_DOMAIN}"
    if len(digits) == 10:
        return f"p{digits}@{AUTH_EMAIL_DOMAIN}"
    return f"u{raw.lower()}@{AUTH_EMAIL_DOMAIN}"


def resolve_firebase_email() -> str:
    email = _env("FIREBASE_EMAIL")
    if email:
        return email
    login_id = _env("FIREBASE_LOGIN_ID")
    if login_id:
        return auth_email_from_login(login_id)
    raise SystemExit("Set FIREBASE_EMAIL or FIREBASE_LOGIN_ID (+ FIREBASE_PASSWORD)")


def category_description(name: str) -> str:
    key = name.strip().lower()
    if key in CATEGORY_DESCRIPTIONS:
        return CATEGORY_DESCRIPTIONS[key]
    for partial, desc in CATEGORY_DESCRIPTIONS.items():
        if partial in key:
            return desc
    readable = " ".join(w.capitalize() for w in name.lower().split())
    return f"Browse {readable} products"


def display_order_value(raw: Any) -> int:
    """Match YesOne mapProduct: finite displayOrder, else 999."""
    try:
        if raw is None or raw == "":
            return 999
        n = int(raw)
        return n
    except (TypeError, ValueError):
        try:
            f = float(raw)
            if f != f:  # NaN
                return 999
            return int(f)
        except (TypeError, ValueError):
            return 999


def decode_value(node: Any) -> Any:
    if node is None or not isinstance(node, dict):
        return None
    if "stringValue" in node:
        return node["stringValue"]
    if "integerValue" in node:
        return int(node["integerValue"])
    if "doubleValue" in node:
        return float(node["doubleValue"])
    if "booleanValue" in node:
        return bool(node["booleanValue"])
    if "nullValue" in node:
        return None
    if "timestampValue" in node:
        return node["timestampValue"]
    if "arrayValue" in node:
        vals = (node["arrayValue"] or {}).get("values") or []
        return [decode_value(v) for v in vals]
    if "mapValue" in node:
        fields = (node["mapValue"] or {}).get("fields") or {}
        return {k: decode_value(v) for k, v in fields.items()}
    return None


def decode_doc(doc: dict[str, Any]) -> dict[str, Any]:
    doc_id = str(doc.get("name") or "").rstrip("/").split("/")[-1]
    fields = doc.get("fields") or {}
    out = {k: decode_value(v) for k, v in fields.items()}
    out["_id"] = doc_id
    return out


def to_decimal(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def image_ext(url: str) -> str:
    path = urlparse(url).path.lower()
    for ext in (".png", ".webp", ".gif", ".jpeg", ".jpg"):
        if ext in path:
            return ext if ext != ".jpeg" else ".jpg"
    return ".jpg"


async def sign_in(client: httpx.AsyncClient, api_key: str, email: str, password: str) -> str:
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}"
    r = await client.post(url, json={"email": email, "password": password, "returnSecureToken": True})
    data = r.json()
    if not r.is_success or not data.get("idToken"):
        msg = (data.get("error") or {}).get("message") or r.text[:200]
        raise SystemExit(f"Firebase Auth failed: {msg}")
    return str(data["idToken"])


async def list_collection(
    client: httpx.AsyncClient,
    *,
    project: str,
    collection: str,
    api_key: str,
    id_token: str | None,
) -> list[dict[str, Any]]:
    base = f"https://firestore.googleapis.com/v1/projects/{project}/databases/(default)/documents/{collection}"
    headers = {"Authorization": f"Bearer {id_token}"} if id_token else {}
    docs: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, str] = {"pageSize": "300", "key": api_key}
        if page_token:
            params["pageToken"] = page_token
        r = await client.get(base, params=params, headers=headers)
        if not r.is_success:
            raise SystemExit(f"Firestore {collection} failed: {r.status_code} {r.text[:240]}")
        body = r.json()
        for doc in body.get("documents") or []:
            docs.append(decode_doc(doc))
        page_token = body.get("nextPageToken") or ""
        if not page_token:
            break
    return docs


async def download_image(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = await client.get(url, follow_redirects=True)
    if not r.is_success:
        return ""
    dest.write_bytes(r.content)
    return str(dest)


def public_image_url(local_path: str, remote_url: str) -> str:
    if local_path:
        # Served by /api/catalog/images/...
        name = Path(local_path).name
        parent = Path(local_path).parent.name
        grand = Path(local_path).parent.parent.name if Path(local_path).parent.parent else ""
        if grand == "products" or parent == "products":
            # .../products/{id}/0.jpg or .../categories/{id}.jpg
            parts = Path(local_path).parts
            try:
                idx = parts.index("catalog-images")
                rel = "/".join(parts[idx + 1 :])
                return f"/api/catalog/images/{quote(rel)}"
            except ValueError:
                pass
            try:
                # /data/catalog-images/...
                idx = next(i for i, p in enumerate(parts) if p.endswith("catalog-images") or p == "catalog-images")
                rel = "/".join(parts[idx + 1 :])
                return f"/api/catalog/images/{quote(rel)}"
            except StopIteration:
                return remote_url
        return remote_url
    return remote_url


def normalize_image_urls(raw: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    primary = str(raw.get("imageUrl") or "").strip()
    arr = raw.get("imageUrls")
    if isinstance(arr, list):
        for u in arr:
            s = str(u or "").strip()
            if s and s not in urls:
                urls.append(s)
    if primary and primary not in urls:
        urls.insert(0, primary)
    docs = raw.get("imageDocs")
    if isinstance(docs, list):
        for d in docs:
            if isinstance(d, dict):
                s = str(d.get("url") or "").strip()
                if s and s not in urls:
                    urls.append(s)
    return urls


async def main() -> int:
    api_key = _env("FIREBASE_API_KEY", DEFAULT_API_KEY)
    project = _env("FIREBASE_PROJECT_ID", DEFAULT_PROJECT)
    password = _env("FIREBASE_PASSWORD")
    if not password:
        raise SystemExit("Set FIREBASE_PASSWORD")
    email = resolve_firebase_email()
    image_dir = Path(_env("CATALOG_IMAGE_DIR", "/data/catalog-images"))
    # Default off: downloading ~1k images OOMs the 512m API container (exit 137).
    download = _env("DOWNLOAD_IMAGES", "0") in {"1", "true", "yes"}
    dry_run = _env("DRY_RUN", "0") in {"1", "true", "yes"}

    async with httpx.AsyncClient(timeout=60.0) as client:
        print(f"Signing in as {email} …")
        token = await sign_in(client, api_key, email, password)
        print("Auth OK")

        categories_raw = await list_collection(
            client, project=project, collection="catalogCategories", api_key=api_key, id_token=token
        )
        products_raw = await list_collection(
            client, project=project, collection="catalogProducts", api_key=api_key, id_token=token
        )
        print(f"Fetched {len(categories_raw)} categories, {len(products_raw)} products")

        excluded_ids: set[str] = set()
        categories: list[dict[str, Any]] = []
        for c in categories_raw:
            name = str(c.get("name") or "").strip()
            if is_excluded_category(name):
                excluded_ids.add(str(c["_id"]))
                print(f"  exclude category: {name}")
                continue
            categories.append(c)

        products: list[dict[str, Any]] = []
        for p in products_raw:
            cat_name = str(p.get("categoryName") or "").strip()
            cat_id = str(p.get("categoryId") or "").strip()
            if is_excluded_category(cat_name) or cat_id in excluded_ids:
                continue
            if p.get("hiddenFromCatalog") is True:
                continue
            # YesOne browse loads status == 'active' only
            status = str(p.get("status") or "active").strip().lower()
            if status != "active":
                continue
            products.append(p)

        # Recount active products per category after exclusions / hidden
        counts: dict[str, int] = {}
        for p in products:
            cid = str(p.get("categoryId") or "").strip()
            if cid:
                counts[cid] = counts.get(cid, 0) + 1

        print(f"Keeping {len(categories)} categories, {len(products)} active products")

        if dry_run:
            for c in sorted(
                categories,
                key=lambda x: (display_order_value(x.get("displayOrder")), str(x.get("name") or "")),
            ):
                cid = str(c["_id"])
                print(f"  {c.get('name')} → {counts.get(cid, 0)}")
            return 0

        # Ensure tables exist
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        image_dir.mkdir(parents=True, exist_ok=True)

        async with SessionLocal() as db:
            # Upsert categories
            existing_cats = {
                r.firebase_id: r
                for r in (await db.execute(select(CatalogCategory))).scalars().all()
            }
            keep_cat_ids: set[str] = set()
            now = datetime.now(timezone.utc)

            for c in categories:
                fid = str(c["_id"])
                keep_cat_ids.add(fid)
                name = str(c.get("name") or "").strip()
                thumb = str(c.get("thumbnailUrl") or "").strip()
                local_path = ""
                if download and thumb:
                    dest = image_dir / "categories" / f"{fid}{image_ext(thumb)}"
                    try:
                        local_path = await download_image(client, thumb, dest)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  cat image fail {fid}: {exc}")

                row = existing_cats.get(fid)
                if not row:
                    row = CatalogCategory(firebase_id=fid)
                    db.add(row)
                row.name = name
                row.description = category_description(name)
                row.display_order = display_order_value(c.get("displayOrder"))
                row.product_count = int(counts.get(fid, 0))
                row.thumbnail_url = thumb
                if local_path:
                    row.local_image_path = local_path
                row.is_weighing_scale = bool(c.get("isWeighingScale") or False)
                row.synced_at = now

            # Upsert products
            existing_prods = {
                r.firebase_id: r
                for r in (await db.execute(select(CatalogProduct))).scalars().all()
            }
            keep_prod_ids: set[str] = set()

            for i, p in enumerate(products):
                fid = str(p["_id"])
                keep_prod_ids.add(fid)
                urls = normalize_image_urls(p)
                primary = urls[0] if urls else ""
                local_path = ""
                if download and primary:
                    dest = image_dir / "products" / fid / f"0{image_ext(primary)}"
                    try:
                        local_path = await download_image(client, primary, dest)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  product image fail {fid}: {exc}")
                # secondary gallery (cap 4 downloads)
                if download:
                    for gi, u in enumerate(urls[1:4], start=1):
                        dest = image_dir / "products" / fid / f"{gi}{image_ext(u)}"
                        try:
                            await download_image(client, u, dest)
                        except Exception:  # noqa: BLE001
                            pass

                row = existing_prods.get(fid)
                if not row:
                    row = CatalogProduct(firebase_id=fid)
                    db.add(row)
                row.name = str(p.get("name") or "").strip()
                row.sku = str(p.get("sku") or "").strip()
                row.description = str(p.get("description") or "").strip()
                row.unit = str(p.get("unit") or "pcs").strip() or "pcs"
                row.rate = to_decimal(p.get("rate"))
                row.stock = to_decimal(p.get("stock"))
                row.stock_status = str(p.get("stockStatus") or "").strip()
                row.image_url = primary
                row.image_urls_json = json.dumps(urls)
                if local_path:
                    row.local_image_path = local_path
                row.category_id = str(p.get("categoryId") or "").strip()
                row.category_name = str(p.get("categoryName") or "").strip()
                row.status = str(p.get("status") or "active").strip() or "active"
                row.hsn = str(p.get("hsn") or "").strip()
                row.tax_name = str(p.get("taxName") or "").strip()
                row.tax_percentage = to_decimal(p.get("taxPercentage"))
                row.display_order = display_order_value(p.get("displayOrder"))
                row.hidden_from_catalog = bool(p.get("hiddenFromCatalog") or False)
                row.model_number = str(p.get("modelNumber") or "").strip()
                row.discontinued_soon = bool(p.get("discontinuedSoon") or False)
                row.new_arrival = bool(p.get("newArrival") or False)
                row.synced_at = now

                if (i + 1) % 50 == 0:
                    print(f"  upserted {i + 1}/{len(products)} products…")
                    await db.flush()

            # Remove stale rows (including excluded spares left from prior sync)
            for fid, row in existing_cats.items():
                if fid not in keep_cat_ids:
                    await db.delete(row)
            for fid, row in existing_prods.items():
                if fid not in keep_prod_ids:
                    await db.delete(row)

            # Also wipe excluded by name if somehow kept (never re-insert)
            for excl_name in excluded_category_display_names():
                await db.execute(
                    delete(CatalogCategory).where(
                        CatalogCategory.name.ilike(excl_name)
                    )
                )
                await db.execute(
                    delete(CatalogProduct).where(
                        CatalogProduct.category_name.ilike(excl_name)
                    )
                )

            await db.commit()

        print("Sync complete")
        # Summary
        async with SessionLocal() as db:
            cats = (
                await db.execute(
                    select(CatalogCategory).order_by(
                        CatalogCategory.display_order, CatalogCategory.name
                    )
                )
            ).scalars().all()
            prod_n = len(
                (await db.execute(select(CatalogProduct))).scalars().all()
            )
            print(f"DB: {len(cats)} categories, {prod_n} products")
            for c in cats:
                print(f"  {c.name} ({c.product_count})")

    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
