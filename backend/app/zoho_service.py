"""Zoho Books OAuth + API helpers (India DC by default)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.crypto_util import decrypt_secret, encrypt_secret
from app.dealer_exclusions import is_excluded_contact
from app.models import Dealer, ZohoCreditNote, ZohoDealer, ZohoIntegration, ZohoSale
from app.phone_util import normalize_in_mobile

ORG = "yesweigh"


def public_base_url(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    base = (s.public_base_url or "").strip()
    if not base:
        origin = (s.cors_origins or "").split(",")[0].strip()
        base = origin if origin.startswith("http") else "https://crm.yesweigh.in"
    if "localhost" in base:
        base = "https://crm.yesweigh.in"
    return base.rstrip("/")


def zoho_callback_url(settings: Settings | None = None) -> str:
    return f"{public_base_url(settings)}/api/integrations/zoho/callback"


def zoho_webhook_url(settings: Settings | None = None) -> str:
    return f"{public_base_url(settings)}/api/integrations/zoho/webhook"


def resolve_client_credentials(
    settings: Settings | None = None,
    integ: ZohoIntegration | None = None,
) -> tuple[str, str, str]:
    """
    Return (client_id, client_secret, source) where source is env|none.

    OAuth app credentials come only from server env
    (ZOHO_CLIENT_ID + ZOHO_CLIENT_SECRET). DB/UI paste is not used.
    """
    del integ  # reserved for call-site compatibility; credentials are env-only
    s = settings or get_settings()
    env_id = (s.zoho_client_id or "").strip()
    env_secret = (s.zoho_client_secret or "").strip()
    if env_id and env_secret:
        return env_id, env_secret, "env"
    return "", "", "none"


def credentials_configured(
    settings: Settings | None = None,
    integ: ZohoIntegration | None = None,
) -> bool:
    cid, secret, _ = resolve_client_credentials(settings, integ)
    return bool(cid and secret)


def credentials_source(
    settings: Settings | None = None,
    integ: ZohoIntegration | None = None,
) -> str:
    return resolve_client_credentials(settings, integ)[2]


def accounts_url(settings: Settings | None = None, override: str = "", integ: ZohoIntegration | None = None) -> str:
    s = settings or get_settings()
    if override:
        return override.rstrip("/")
    if integ and (integ.accounts_url or "").strip():
        return integ.accounts_url.strip().rstrip("/")
    return (s.zoho_accounts_url or "https://accounts.zoho.in").rstrip("/")


def api_base_url(settings: Settings | None = None, override: str = "", integ: ZohoIntegration | None = None) -> str:
    s = settings or get_settings()
    if override:
        return override.rstrip("/")
    if integ and (integ.api_base_url or "").strip():
        return integ.api_base_url.strip().rstrip("/")
    return (s.zoho_api_base_url or "https://www.zohoapis.in").rstrip("/")


def build_auth_url(
    state: str,
    settings: Settings | None = None,
    integ: ZohoIntegration | None = None,
) -> str:
    s = settings or get_settings()
    client_id, _, _ = resolve_client_credentials(s, integ)
    params = {
        "scope": s.zoho_oauth_scopes or "ZohoBooks.fullaccess.all",
        "client_id": client_id,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "redirect_uri": zoho_callback_url(s),
        "state": state,
    }
    return f"{accounts_url(s, integ=integ)}/oauth/v2/auth?{urlencode(params)}"


async def exchange_code_for_tokens(
    code: str,
    settings: Settings | None = None,
    integ: ZohoIntegration | None = None,
) -> dict[str, Any]:
    s = settings or get_settings()
    client_id, client_secret, _ = resolve_client_credentials(s, integ)
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": zoho_callback_url(s),
        "code": code,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{accounts_url(s, integ=integ)}/oauth/v2/token", data=data)
        payload = r.json() if r.content else {}
        if r.status_code >= 400:
            raise RuntimeError(payload.get("error") or payload.get("message") or r.text or "Token exchange failed")
        return payload


async def refresh_access_token(
    refresh_token: str,
    settings: Settings | None = None,
    integ: ZohoIntegration | None = None,
) -> dict[str, Any]:
    s = settings or get_settings()
    client_id, client_secret, _ = resolve_client_credentials(s, integ)
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{accounts_url(s, integ=integ)}/oauth/v2/token", data=data)
        payload = r.json() if r.content else {}
        if r.status_code >= 400:
            raise RuntimeError(payload.get("error") or payload.get("message") or r.text or "Token refresh failed")
        return payload


def apply_tokens(integ: ZohoIntegration, token_payload: dict[str, Any], settings: Settings | None = None) -> None:
    s = settings or get_settings()
    access = str(token_payload.get("access_token") or "")
    refresh = str(token_payload.get("refresh_token") or "")
    expires_in = int(token_payload.get("expires_in") or 3600)
    if access:
        integ.encrypted_access_token = encrypt_secret(access)
        integ.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(60, expires_in - 60))
    if refresh:
        integ.encrypted_refresh_token = encrypt_secret(refresh)
    if not (integ.accounts_url or "").strip():
        integ.accounts_url = accounts_url(s, integ=integ)
    if not (integ.api_base_url or "").strip():
        integ.api_base_url = api_base_url(s, integ=integ)
    api_domain = str(token_payload.get("api_domain") or "").rstrip("/")
    if api_domain:
        integ.api_base_url = api_domain


async def get_integration(db: AsyncSession) -> ZohoIntegration | None:
    return (
        await db.execute(
            select(ZohoIntegration)
            .where(ZohoIntegration.organization_id == ORG)
            .order_by(ZohoIntegration.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def ensure_integration(db: AsyncSession) -> ZohoIntegration:
    integ = await get_integration(db)
    if integ:
        return integ
    settings = get_settings()
    integ = ZohoIntegration(
        organization_id=ORG,
        enabled=True,
        api_product="books",
        accounts_url=accounts_url(settings),
        api_base_url=api_base_url(settings),
        connection_status="disconnected",
    )
    # Prefer env webhook secret for bootstrap; otherwise generate later in router.
    if settings.zoho_webhook_secret.strip():
        integ.encrypted_webhook_secret = encrypt_secret(settings.zoho_webhook_secret.strip())
    db.add(integ)
    await db.flush()
    return integ


async def ensure_valid_access_token(db: AsyncSession, integ: ZohoIntegration) -> str:
    access = decrypt_secret(integ.encrypted_access_token)
    refresh = decrypt_secret(integ.encrypted_refresh_token)
    now = datetime.now(timezone.utc)
    expires = integ.token_expires_at
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if access and expires and expires > now + timedelta(seconds=30):
        return access
    if not refresh:
        raise RuntimeError("Zoho refresh token missing — reconnect Zoho")
    payload = await refresh_access_token(refresh, integ=integ)
    apply_tokens(integ, payload)
    await db.flush()
    access = decrypt_secret(integ.encrypted_access_token)
    if not access:
        raise RuntimeError("Zoho access token refresh failed")
    return access


async def books_get(
    db: AsyncSession,
    integ: ZohoIntegration,
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = await ensure_valid_access_token(db, integ)
    base = api_base_url(override=integ.api_base_url)
    q = dict(params or {})
    if integ.zoho_org_id:
        q.setdefault("organization_id", integ.zoho_org_id)
    async with httpx.AsyncClient(timeout=45.0) as client:
        r = await client.get(
            f"{base}{path}",
            params=q,
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
        )
        payload = r.json() if r.content else {}
        if r.status_code >= 400:
            msg = payload.get("message") or payload.get("error") or r.text or f"HTTP {r.status_code}"
            raise RuntimeError(str(msg))
        return payload


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _addr_field(contact: dict[str, Any], key: str) -> str:
    """Read city/state/etc from billing_address, then shipping_address."""
    for field in ("billing_address", "shipping_address"):
        addr = contact.get(field) or {}
        if isinstance(addr, dict):
            val = str(addr.get(key) or "").strip()
            if val:
                return val
    return ""


def _sale_ids(doc: dict[str, Any]) -> tuple[str, str]:
    """Return (zoho_record_id, document_type) for invoice / salesorder payloads."""
    if doc.get("salesorder_id") or doc.get("salesorder_number"):
        rid = str(doc.get("salesorder_id") or doc.get("salesorder_number") or "")
        return rid, "salesorder"
    rid = str(doc.get("invoice_id") or doc.get("invoice_number") or doc.get("id") or "")
    return rid, "invoice"


def _status_from_event(event_type: str, fallback: str = "") -> str:
    et = (event_type or "").lower()
    if "void" in et:
        return "void"
    if "cancel" in et:
        return "cancelled"
    if "delete" in et or et.endswith("_deleted"):
        return "deleted"
    return fallback


async def upsert_sale(
    db: AsyncSession,
    inv: dict[str, Any],
    source: str = "poll",
    *,
    event_type: str = "",
    document_type: str | None = None,
) -> ZohoSale:
    rid, inferred_type = _sale_ids(inv)
    if not rid:
        raise ValueError("sale document missing id")
    doc_type = document_type or inferred_type
    existing = (
        await db.execute(select(ZohoSale).where(ZohoSale.zoho_record_id == rid))
    ).scalar_one_or_none()
    row = existing or ZohoSale(zoho_record_id=rid)
    row.document_type = doc_type
    if doc_type == "salesorder":
        row.invoice_number = str(inv.get("salesorder_number") or inv.get("invoice_number") or "")
    else:
        row.invoice_number = str(inv.get("invoice_number") or inv.get("salesorder_number") or "")
    row.date = str(inv.get("date") or "")
    row.customer_id = str(inv.get("customer_id") or "")
    row.customer_name = str(inv.get("customer_name") or "")
    status = str(inv.get("status") or "")
    status = _status_from_event(event_type, status) or status
    row.status = status
    row.currency_code = str(inv.get("currency_code") or "INR")
    row.total = _dec(inv.get("total"))
    # Prefer Zoho tax-exclusive fields; fall back to total - tax_total when needed
    sub = _dec(inv.get("sub_total") or inv.get("bcy_sub_total"))
    if sub is None:
        tot = _dec(inv.get("total") or inv.get("bcy_total"))
        tax = _dec(inv.get("tax_total") or inv.get("bcy_tax_total"))
        if tot is not None and tax is not None:
            sub = tot - tax
    row.sub_total = sub
    sp = (
        inv.get("salesperson_name")
        or inv.get("sales_person")
        or inv.get("Sales person")
        or ""
    )
    if isinstance(sp, dict):
        sp = sp.get("name") or sp.get("salesperson_name") or ""
    row.salesperson_name = str(sp or "").strip()
    row.balance = _dec(inv.get("balance"))
    row.source = source
    row.raw_json = json.dumps(inv, default=str)
    if not existing:
        db.add(row)
    await db.flush()
    return row


async def mark_sale_status(
    db: AsyncSession,
    *,
    record_id: str,
    status: str,
    document_type: str = "invoice",
    source: str = "webhook",
    raw: dict[str, Any] | None = None,
) -> ZohoSale | None:
    rid = (record_id or "").strip()
    if not rid:
        return None
    existing = (
        await db.execute(select(ZohoSale).where(ZohoSale.zoho_record_id == rid))
    ).scalar_one_or_none()
    row = existing or ZohoSale(zoho_record_id=rid, document_type=document_type)
    row.document_type = document_type or row.document_type or "invoice"
    row.status = status
    row.source = source
    if raw is not None:
        row.raw_json = json.dumps(raw, default=str)
        if not row.invoice_number:
            row.invoice_number = str(
                raw.get("invoice_number") or raw.get("salesorder_number") or ""
            )
        if not row.customer_id:
            row.customer_id = str(raw.get("customer_id") or "")
        if not row.customer_name:
            row.customer_name = str(raw.get("customer_name") or "")
    if not existing:
        db.add(row)
    await db.flush()
    return row


async def upsert_credit_note(db: AsyncSession, cn: dict[str, Any], source: str = "poll") -> ZohoCreditNote:
    rid = str(cn.get("creditnote_id") or cn.get("creditnote_number") or "")
    if not rid:
        raise ValueError("credit note missing id")
    existing = (
        await db.execute(select(ZohoCreditNote).where(ZohoCreditNote.zoho_record_id == rid))
    ).scalar_one_or_none()
    row = existing or ZohoCreditNote(zoho_record_id=rid)
    row.creditnote_number = str(cn.get("creditnote_number") or "")
    row.date = str(cn.get("date") or "")
    row.customer_id = str(cn.get("customer_id") or "")
    row.customer_name = str(cn.get("customer_name") or "")
    row.status = str(cn.get("status") or "")
    row.currency_code = str(cn.get("currency_code") or "INR")
    row.total = _dec(cn.get("total"))
    row.balance = _dec(cn.get("balance"))
    row.reference_invoice_id = str(cn.get("invoice_id") or "")
    row.source = source
    row.raw_json = json.dumps(cn, default=str)
    if not existing:
        db.add(row)
    await db.flush()
    return row


async def upsert_dealer_from_contact(
    db: AsyncSession,
    contact: dict[str, Any],
    *,
    event_type: str = "",
) -> ZohoDealer:
    cid = str(contact.get("contact_id") or contact.get("customer_id") or "")
    if not cid:
        raise ValueError("contact missing id")
    if _is_delete_event(event_type):
        return await delete_dealer_from_contact(db, cid, raw=contact)

    contact_name = str(contact.get("contact_name") or contact.get("customer_name") or "")
    company_name = str(contact.get("company_name") or "")
    excluded = is_excluded_contact(contact, name=contact_name, company=company_name, zoho_id=cid)

    staging = (
        await db.execute(select(ZohoDealer).where(ZohoDealer.zoho_contact_id == cid))
    ).scalar_one_or_none()
    row = staging or ZohoDealer(zoho_contact_id=cid)
    row.contact_name = contact_name
    row.company_name = company_name
    row.email = str(contact.get("email") or "")
    row.phone = normalize_in_mobile(str(contact.get("phone") or ""))
    row.mobile = normalize_in_mobile(str(contact.get("mobile") or ""))
    row.gst_no = str(contact.get("gst_no") or contact.get("tax_reg_no") or "")
    row.billing_city = _addr_field(contact, "city")
    # List API often omits billing_address; place_of_contact*_formatted is the GST state.
    row.billing_state = (
        _addr_field(contact, "state")
        or str(contact.get("place_of_contact_formatted") or "").strip()
        or str(contact.get("place_of_contact") or "").strip()
    )
    row.status = "excluded" if excluded else str(contact.get("status") or contact.get("contact_type") or "active")
    row.raw_json = json.dumps(contact, default=str)
    if not staging:
        db.add(row)
    await db.flush()

    # Junk / internal Zoho contacts: never create or reactivate CRM dealers.
    if excluded:
        dealer = (
            await db.execute(select(Dealer).where(Dealer.zoho_contact_id == cid))
        ).scalar_one_or_none()
        if dealer:
            await db.delete(dealer)
            await db.flush()
            row.dealer_id = None
            await db.flush()
        return row

    dealer = (
        await db.execute(select(Dealer).where(Dealer.zoho_contact_id == cid))
    ).scalar_one_or_none()
    if not dealer and (row.phone or row.mobile or row.email):
        q = select(Dealer)
        if row.phone:
            dealer = (await db.execute(q.where(Dealer.phone == row.phone))).scalar_one_or_none()
        if not dealer and row.mobile:
            dealer = (await db.execute(q.where(Dealer.phone == row.mobile))).scalar_one_or_none()
        if not dealer and row.email:
            dealer = (await db.execute(q.where(Dealer.email == row.email))).scalar_one_or_none()
    if not dealer:
        dealer = Dealer(zoho_contact_id=cid, source="zoho")
        db.add(dealer)
    dealer.name = row.contact_name or row.company_name or dealer.name
    dealer.company = row.company_name or dealer.company
    dealer.phone = row.mobile or row.phone or normalize_in_mobile(dealer.phone)
    dealer.email = row.email or dealer.email
    dealer.city = row.billing_city or dealer.city
    dealer.state = row.billing_state or dealer.state
    dealer.gstin = row.gst_no or dealer.gstin
    dealer.zoho_contact_id = cid
    dealer.source = "zoho"
    dealer.status = "active" if (row.status or "").lower() not in {"inactive", "deleted", "excluded"} else "inactive"
    await db.flush()
    row.dealer_id = dealer.id
    await db.flush()
    return row


def _is_delete_event(event_type: str) -> bool:
    et = (event_type or "").lower()
    return "delete" in et or et.endswith("_deleted") or et in {"deleted", "contact_delete", "customer_delete"}


async def delete_dealer_from_contact(
    db: AsyncSession,
    contact_id: str,
    *,
    raw: dict[str, Any] | None = None,
) -> ZohoDealer:
    cid = (contact_id or "").strip()
    if not cid:
        raise ValueError("contact missing id")
    staging = (
        await db.execute(select(ZohoDealer).where(ZohoDealer.zoho_contact_id == cid))
    ).scalar_one_or_none()
    row = staging or ZohoDealer(zoho_contact_id=cid)
    row.status = "deleted"
    if raw is not None:
        row.raw_json = json.dumps(raw, default=str)
        if not row.contact_name:
            row.contact_name = str(raw.get("contact_name") or raw.get("customer_name") or "")
        if not row.company_name:
            row.company_name = str(raw.get("company_name") or "")
    if not staging:
        db.add(row)
    await db.flush()

    dealer = (
        await db.execute(select(Dealer).where(Dealer.zoho_contact_id == cid))
    ).scalar_one_or_none()
    if dealer:
        dealer.status = "inactive"
        dealer.source = "zoho"
        await db.flush()
        row.dealer_id = dealer.id
        await db.flush()
    return row


async def list_organizations(db: AsyncSession, integ: ZohoIntegration) -> list[dict[str, str]]:
    data = await books_get(db, integ, "/books/v3/organizations")
    orgs = data.get("organizations") or []
    out: list[dict[str, str]] = []
    for o in orgs:
        out.append(
            {
                "organization_id": str(o.get("organization_id") or ""),
                "name": str(o.get("name") or ""),
                "contact_name": str(o.get("contact_name") or ""),
                "plan_name": str(o.get("plan_name") or ""),
                "is_org_active": str(o.get("is_org_active") or ""),
            }
        )
    return out


def today_kolkata_iso() -> str:
    """Current calendar date in Asia/Kolkata as YYYY-MM-DD."""
    # IST is fixed UTC+5:30
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).date().isoformat()


async def sync_all_customers(
    db: AsyncSession,
    integ: ZohoIntegration,
    *,
    per_page: int = 200,
    max_pages: int = 500,
    enrich_missing_city: bool = False,
) -> dict[str, int]:
    """Paginate every Zoho Books customer contact into zoho_dealers + dealers.

    List endpoints often omit billing_address; when enrich_missing_city is True,
    follow up with GET /contacts/{id} for rows still missing city (slow).
    """
    if not integ.zoho_org_id:
        raise RuntimeError("Select a Zoho organization before syncing")
    counts = {"dealers": 0, "pages": 0, "cities_filled": 0, "enriched": 0, "enrich_errors": 0}
    for page in range(1, max_pages + 1):
        data = await books_get(
            db,
            integ,
            "/books/v3/contacts",
            {"page": page, "per_page": per_page, "contact_type": "customer"},
        )
        contacts = data.get("contacts") or []
        for c in contacts:
            await upsert_dealer_from_contact(db, c)
            counts["dealers"] += 1
        counts["pages"] = page
        await db.commit()
        if not (data.get("page_context") or {}).get("has_more_page"):
            break
        if not contacts:
            break
    if enrich_missing_city:
        filled = await enrich_dealer_cities(db, integ)
        counts["cities_filled"] = filled.get("cities_filled", 0)
        counts["enriched"] = filled.get("fetched", 0)
        counts["enrich_errors"] = filled.get("errors", 0)
    return counts


def _raw_has_detail_payload(raw_json: str) -> bool:
    """True when raw_json includes nested address detail from GET /contacts/{id}.

    Zoho list responses omit billing_address entirely; detail responses include it
    (often with city/address even when empty strings). Treat presence of a
    billing_address or shipping_address object as the detail marker.
    """
    if not (raw_json or "").strip():
        return False
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    ba = data.get("billing_address")
    sa = data.get("shipping_address")
    return isinstance(ba, dict) or isinstance(sa, dict)


async def enrich_dealer_cities(
    db: AsyncSession,
    integ: ZohoIntegration,
    *,
    limit: int | None = None,
    pause_s: float = 0.15,
    commit_every: int = 25,
    force_all: bool = False,
) -> dict[str, int]:
    """Fetch full Zoho contact details into zoho_dealers.raw_json + dealers columns.

    List sync omits nested addresses; GET /contacts/{id} has billing/shipping,
    GST, PAN, contact persons, etc. By default only rows missing a detail payload
    (or billing_city) are fetched; force_all re-fetches every non-excluded contact.
    """
    import asyncio

    if not integ.zoho_org_id:
        raise RuntimeError("Select a Zoho organization before syncing")

    stmt = (
        select(ZohoDealer)
        .where(ZohoDealer.zoho_contact_id != "")
        .where(ZohoDealer.status.notin_(["deleted", "excluded"]))
        .order_by(ZohoDealer.id.asc())
    )
    all_rows = (await db.execute(stmt)).scalars().all()
    rows: list[ZohoDealer] = []
    for row in all_rows:
        if is_excluded_contact(
            name=row.contact_name or "",
            company=row.company_name or "",
            zoho_id=row.zoho_contact_id or "",
        ):
            continue
        if force_all:
            rows.append(row)
            continue
        if (row.billing_city or "").strip() and _raw_has_detail_payload(row.raw_json or ""):
            continue
        rows.append(row)
    if limit is not None and limit > 0:
        rows = rows[:limit]

    # Copy city from staging → dealers when staging already has it.
    dealers_missing = (
        await db.execute(
            select(Dealer).where(Dealer.zoho_contact_id != "").where(
                or_(Dealer.city == "", Dealer.city.is_(None))
            )
        )
    ).scalars().all()
    staging_by_zid = {
        z.zoho_contact_id: z
        for z in (
            await db.execute(
                select(ZohoDealer).where(
                    ZohoDealer.zoho_contact_id.in_(
                        [d.zoho_contact_id for d in dealers_missing if d.zoho_contact_id]
                    )
                )
            )
        ).scalars().all()
    } if dealers_missing else {}

    copied = 0
    for d in dealers_missing:
        if is_excluded_contact(name=d.name or "", company=d.company or "", zoho_id=d.zoho_contact_id or ""):
            continue
        st = staging_by_zid.get(d.zoho_contact_id)
        if st and (st.billing_city or "").strip():
            d.city = st.billing_city.strip()[:80]
            if (st.billing_state or "").strip() and not (d.state or "").strip():
                d.state = st.billing_state.strip()[:80]
            copied += 1
    if copied:
        await db.commit()

    stats = {
        "candidates": len(rows),
        "fetched": 0,
        "cities_filled": copied,
        "errors": 0,
        "skipped_no_city": 0,
        "skipped_excluded": 0,
    }
    for i, row in enumerate(rows, start=1):
        cid = (row.zoho_contact_id or "").strip()
        if not cid:
            continue
        if is_excluded_contact(
            name=row.contact_name or "",
            company=row.company_name or "",
            zoho_id=cid,
        ):
            stats["skipped_excluded"] += 1
            continue
        try:
            data = await books_get(db, integ, f"/books/v3/contacts/{cid}")
            contact = data.get("contact") if isinstance(data, dict) else None
            if not isinstance(contact, dict):
                stats["errors"] += 1
                continue
            before = (row.billing_city or "").strip()
            await upsert_dealer_from_contact(db, contact)
            await db.refresh(row)
            stats["fetched"] += 1
            after = (row.billing_city or "").strip()
            if after and after != before:
                stats["cities_filled"] += 1
            elif not after:
                stats["skipped_no_city"] += 1
        except Exception:
            stats["errors"] += 1
        if i % commit_every == 0:
            await db.commit()
        if pause_s > 0:
            await asyncio.sleep(pause_s)
    await db.commit()
    return stats


async def sync_sales_range(
    db: AsyncSession,
    integ: ZohoIntegration,
    *,
    date_from: str,
    date_to: str | None = None,
    per_page: int = 200,
    max_pages: int = 500,
) -> dict[str, int]:
    """
    Import invoices + credit notes in [date_from, date_to] (YYYY-MM-DD, Asia/Kolkata dates).
    Does not import sales orders.
    """
    if not integ.zoho_org_id:
        raise RuntimeError("Select a Zoho organization before syncing")
    start = (date_from or "").strip()
    end = (date_to or "").strip() or today_kolkata_iso()
    if not start:
        raise ValueError("date_from is required")
    counts = {
        "invoices": 0,
        "credit_notes": 0,
        "invoice_pages": 0,
        "credit_note_pages": 0,
        "date_from": start,
        "date_to": end,
    }

    for page in range(1, max_pages + 1):
        data = await books_get(
            db,
            integ,
            "/books/v3/invoices",
            {
                "page": page,
                "per_page": per_page,
                "date_start": start,
                "date_end": end,
                "sort_column": "date",
                "sort_order": "A",
            },
        )
        invoices = data.get("invoices") or []
        for inv in invoices:
            await upsert_sale(db, inv, source="poll", document_type="invoice")
            counts["invoices"] += 1
        counts["invoice_pages"] = page
        await db.commit()
        if not (data.get("page_context") or {}).get("has_more_page"):
            break
        if not invoices:
            break

    for page in range(1, max_pages + 1):
        data = await books_get(
            db,
            integ,
            "/books/v3/creditnotes",
            {
                "page": page,
                "per_page": per_page,
                "date_start": start,
                "date_end": end,
                "sort_column": "date",
                "sort_order": "A",
            },
        )
        notes = data.get("creditnotes") or []
        for cn in notes:
            await upsert_credit_note(db, cn, source="poll")
            counts["credit_notes"] += 1
        counts["credit_note_pages"] = page
        await db.commit()
        if not (data.get("page_context") or {}).get("has_more_page"):
            break
        if not notes:
            break

    return counts


async def sync_recent(
    db: AsyncSession,
    integ: ZohoIntegration,
    *,
    pages: int = 3,
    per_page: int = 50,
    full_customers: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, int]:
    """Pull invoices, credit notes, and contacts from Zoho Books."""
    if not integ.zoho_org_id:
        raise RuntimeError("Select a Zoho organization before syncing")
    counts = {
        "invoices": 0,
        "credit_notes": 0,
        "dealers": 0,
        "customer_pages": 0,
        "invoice_pages": 0,
        "credit_note_pages": 0,
        "date_from": "",
        "date_to": "",
    }

    if date_from:
        sales = await sync_sales_range(
            db,
            integ,
            date_from=date_from,
            date_to=date_to,
            per_page=200,
        )
        counts["invoices"] = sales["invoices"]
        counts["credit_notes"] = sales["credit_notes"]
        counts["invoice_pages"] = sales["invoice_pages"]
        counts["credit_note_pages"] = sales["credit_note_pages"]
        counts["date_from"] = sales["date_from"]
        counts["date_to"] = sales["date_to"]
    else:
        for page in range(1, pages + 1):
            data = await books_get(
                db,
                integ,
                "/books/v3/invoices",
                {"page": page, "per_page": per_page, "sort_column": "created_time", "sort_order": "D"},
            )
            invoices = data.get("invoices") or []
            for inv in invoices:
                await upsert_sale(db, inv, source="poll", document_type="invoice")
                counts["invoices"] += 1
            counts["invoice_pages"] = page
            if not (data.get("page_context") or {}).get("has_more_page"):
                break

        for page in range(1, pages + 1):
            data = await books_get(
                db,
                integ,
                "/books/v3/creditnotes",
                {"page": page, "per_page": per_page, "sort_column": "created_time", "sort_order": "D"},
            )
            notes = data.get("creditnotes") or []
            for cn in notes:
                await upsert_credit_note(db, cn, source="poll")
                counts["credit_notes"] += 1
            counts["credit_note_pages"] = page
            if not (data.get("page_context") or {}).get("has_more_page"):
                break

    if full_customers:
        cust = await sync_all_customers(db, integ, per_page=200)
        counts["dealers"] = cust["dealers"]
        counts["customer_pages"] = cust["pages"]
    else:
        for page in range(1, pages + 1):
            data = await books_get(
                db,
                integ,
                "/books/v3/contacts",
                {"page": page, "per_page": per_page, "contact_type": "customer"},
            )
            contacts = data.get("contacts") or []
            for c in contacts:
                await upsert_dealer_from_contact(db, c)
                counts["dealers"] += 1
            counts["customer_pages"] = page
            if not (data.get("page_context") or {}).get("has_more_page"):
                break

    return counts


def _infer_module_from_event(event_type: str, module: str) -> str:
    et = (event_type or "").lower()
    mod = (module or "").lower()
    if mod in {"invoice", "invoices", "salesorder", "salesorders", "sales_order", "creditnote", "creditnotes", "contact", "contacts", "customer", "customers"}:
        if mod in {"invoices"}:
            return "invoice"
        if mod in {"salesorders", "sales_order"}:
            return "salesorder"
        if mod in {"creditnotes"}:
            return "creditnote"
        if mod in {"contacts", "customer", "customers"}:
            return "contact"
        return mod
    if "salesorder" in et or "sales_order" in et:
        return "salesorder"
    if "creditnote" in et or "credit_note" in et:
        return "creditnote"
    if "invoice" in et:
        return "invoice"
    if "contact" in et or "customer" in et:
        return "contact"
    return mod or "unknown"


def normalize_webhook_payload(payload: dict[str, Any]) -> tuple[str, str, str, dict[str, Any] | None]:
    """
    Return (module, event_type, zoho_record_id, record_dict).
    Accepts Zoho Books / Flow / workflow webhook style payloads.
    """
    event_type = str(
        payload.get("event_type")
        or payload.get("event")
        or payload.get("notification_type")
        or payload.get("action")
        or ""
    ).lower()
    module = str(payload.get("module") or payload.get("entity") or payload.get("resource") or "").lower()

    invoice = payload.get("invoice")
    if isinstance(invoice, dict):
        return "invoice", event_type or "invoice", str(invoice.get("invoice_id") or ""), invoice

    salesorder = payload.get("salesorder") or payload.get("sales_order")
    if isinstance(salesorder, dict):
        return (
            "salesorder",
            event_type or "salesorder",
            str(salesorder.get("salesorder_id") or ""),
            salesorder,
        )

    creditnote = payload.get("creditnote") or payload.get("credit_note")
    if isinstance(creditnote, dict):
        return (
            "creditnote",
            event_type or "creditnote",
            str(creditnote.get("creditnote_id") or ""),
            creditnote,
        )

    contact = payload.get("contact") or payload.get("customer")
    if isinstance(contact, dict):
        return (
            "contact",
            event_type or "contact",
            str(contact.get("contact_id") or contact.get("customer_id") or ""),
            contact,
        )

    data = payload.get("data")
    if isinstance(data, dict):
        nested = dict(data)
        if event_type and not (
            nested.get("event_type") or nested.get("event") or nested.get("notification_type") or nested.get("action")
        ):
            nested["event_type"] = event_type
        if module and not (nested.get("module") or nested.get("entity") or nested.get("resource")):
            nested["module"] = module
        return normalize_webhook_payload(nested)

    # Flat / minimal payloads (id + event only)
    if payload.get("salesorder_id"):
        return "salesorder", event_type or "salesorder", str(payload.get("salesorder_id")), payload
    if payload.get("invoice_id"):
        return "invoice", event_type or "invoice", str(payload.get("invoice_id")), payload
    if payload.get("creditnote_id"):
        return "creditnote", event_type or "creditnote", str(payload.get("creditnote_id")), payload
    if payload.get("contact_id") or payload.get("customer_id"):
        return (
            "contact",
            event_type or "contact",
            str(payload.get("contact_id") or payload.get("customer_id")),
            payload,
        )

    module = _infer_module_from_event(event_type, module)
    rid = str(payload.get("id") or payload.get("record_id") or "")
    return module or "unknown", event_type or "unknown", rid, None if not rid else payload


async def fetch_books_record(
    db: AsyncSession,
    integ: ZohoIntegration,
    module: str,
    record_id: str,
) -> dict[str, Any] | None:
    """Best-effort fetch of a Books record when webhook payload is sparse."""
    rid = (record_id or "").strip()
    if not rid or not integ.zoho_org_id:
        return None
    try:
        if module == "invoice":
            data = await books_get(db, integ, f"/books/v3/invoices/{rid}")
            rec = data.get("invoice")
            return rec if isinstance(rec, dict) else None
        if module == "salesorder":
            data = await books_get(db, integ, f"/books/v3/salesorders/{rid}")
            rec = data.get("salesorder")
            return rec if isinstance(rec, dict) else None
        if module == "creditnote":
            data = await books_get(db, integ, f"/books/v3/creditnotes/{rid}")
            rec = data.get("creditnote")
            return rec if isinstance(rec, dict) else None
        if module == "contact":
            data = await books_get(db, integ, f"/books/v3/contacts/{rid}")
            rec = data.get("contact")
            return rec if isinstance(rec, dict) else None
    except Exception:
        return None
    return None


async def process_webhook_event(
    db: AsyncSession,
    integ: ZohoIntegration,
    *,
    module: str,
    event_type: str,
    record_id: str,
    record: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Persist sales / customer webhook events into zoho_sales, zoho_credit_notes,
    zoho_dealers, and dealers.
    """
    mod = (module or "").lower()
    et = (event_type or "").lower()
    action = "noop"

    # Enrich sparse create/edit payloads when OAuth is connected.
    needs_body = record is None or (
        isinstance(record, dict)
        and not any(
            k in record
            for k in (
                "invoice_number",
                "salesorder_number",
                "creditnote_number",
                "contact_name",
                "customer_name",
                "total",
                "status",
                "email",
                "phone",
            )
        )
    )
    if needs_body and record_id and not _is_delete_event(et) and "void" not in et and "cancel" not in et:
        fetched = await fetch_books_record(db, integ, mod, record_id)
        if fetched:
            record = fetched

    # Sales Order webhooks are intentionally ignored — CRM sync is invoice + customer only.
    if mod in {"salesorder", "salesorders", "sales_order"}:
        action = "salesorder_ignored"
    elif mod == "invoice":
        doc_type = "invoice"
        status_override = _status_from_event(et)
        if status_override in {"void", "cancelled", "deleted"} and (not record or needs_body):
            await mark_sale_status(
                db,
                record_id=record_id,
                status=status_override,
                document_type=doc_type,
                source="webhook",
                raw=record,
            )
            action = f"sale_{status_override}"
        elif record:
            await upsert_sale(
                db,
                record,
                source="webhook",
                event_type=et,
                document_type=doc_type,
            )
            action = "sale_upsert"
        elif record_id and status_override:
            await mark_sale_status(
                db,
                record_id=record_id,
                status=status_override,
                document_type=doc_type,
                source="webhook",
            )
            action = f"sale_{status_override}"
        else:
            action = "sale_skipped"
    elif mod == "creditnote":
        if record:
            await upsert_credit_note(db, record, source="webhook")
            action = "creditnote_upsert"
        else:
            action = "creditnote_skipped"
    elif mod == "contact":
        if _is_delete_event(et) or (record and str(record.get("status") or "").lower() == "deleted"):
            await delete_dealer_from_contact(db, record_id or str((record or {}).get("contact_id") or ""), raw=record)
            action = "contact_deleted"
        elif record:
            await upsert_dealer_from_contact(db, record, event_type=et)
            action = "contact_upsert"
        elif record_id:
            # Minimal create/edit with id only — create stub, then try fetch.
            stub = {"contact_id": record_id}
            fetched = await fetch_books_record(db, integ, "contact", record_id)
            await upsert_dealer_from_contact(db, fetched or stub, event_type=et)
            action = "contact_upsert"
        else:
            action = "contact_skipped"
    else:
        action = "ignored"

    return {"module": mod, "event_type": et, "record_id": record_id, "action": action}
