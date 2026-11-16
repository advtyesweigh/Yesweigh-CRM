"""Zoho Books integration: OAuth, org select, webhook, manual sync."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import get_settings
from app.crypto_util import decrypt_secret, encrypt_secret
from app.db import get_db
from app.models import User, ZohoOAuthState, ZohoWebhookEvent
from app import zoho_service as zs

router = APIRouter(tags=["zoho"])
Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

ZOHO_DC_PRESETS: dict[str, tuple[str, str]] = {
    "in": ("https://accounts.zoho.in", "https://www.zohoapis.in"),
    "com": ("https://accounts.zoho.com", "https://www.zohoapis.com"),
    "eu": ("https://accounts.zoho.eu", "https://www.zohoapis.eu"),
    "au": ("https://accounts.zoho.com.au", "https://www.zohoapis.com.au"),
    "jp": ("https://accounts.zoho.jp", "https://www.zohoapis.jp"),
}


def _require_admin(user: User) -> None:
    if user.role.lower() not in {"admin", "super_admin", "super admin", "management"}:
        raise HTTPException(403, "Administrator access required")


def _frontend_settings_url(*, query: str = "tab=zoho") -> str:
    return f"{zs.public_base_url()}/settings?{query}"


def _popup_oauth_html(*, ok: bool, error: str = "") -> HTMLResponse:
    """Close OAuth popup and notify opener via postMessage."""
    origin = zs.public_base_url()
    payload = json.dumps({"type": "zoho-oauth", "ok": ok, "error": error[:200]})
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Zoho</title>
<style>
  body{{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;
    background:#f5f7fb;color:#152238}}
  .card{{background:#fff;border:1px solid #e5eaf1;border-radius:12px;padding:28px 32px;text-align:center;max-width:360px}}
  .ok{{color:#087a56}} .err{{color:#bd2438}}
</style></head>
<body><div class="card">
  <p class="{'ok' if ok else 'err'}">{'Signed in with Zoho. You can close this window.' if ok else f'Sign-in failed: {error or "unknown error"}'}</p>
</div>
<script>
(function(){{
  var payload = {payload};
  var origin = {json.dumps(origin)};
  try {{
    if (window.opener && !window.opener.closed) {{
      window.opener.postMessage(payload, origin);
    }}
  }} catch (e) {{}}
  setTimeout(function(){{ try {{ window.close(); }} catch (e) {{}} }}, 400);
}})();
</script></body></html>"""
    return HTMLResponse(html, status_code=200)


class ZohoOrgOut(BaseModel):
    organization_id: str
    name: str
    contact_name: str = ""
    plan_name: str = ""


class ZohoStatusOut(BaseModel):
    connected: bool
    credentials_configured: bool
    credentials_source: str = "none"  # env | db | none
    credentials_locked_by_env: bool = False
    client_id: str = ""
    client_id_hint: str = ""
    connection_status: str
    api_product: str = "books"
    zoho_org_id: str = ""
    zoho_org_name: str = ""
    zoho_org_number: str = ""
    accounts_url: str = ""
    api_base_url: str = ""
    data_center: str = "in"
    oauth_callback_url: str
    webhook_url: str
    webhook_secret_set: bool
    webhook_secret_hint: str = ""
    last_sync_at: datetime | None = None
    last_sync_status: str = ""
    last_sync_error: str = ""
    last_error: str = ""
    pending_organizations: list[ZohoOrgOut] = Field(default_factory=list)
    setup_hint: str = ""


class SelectOrgIn(BaseModel):
    organization_id: str
    name: str = ""
    organization_number: str = ""


class CredentialsIn(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    data_center: str = "in"
    accounts_url: str = ""
    api_base_url: str = ""


class SyncOut(BaseModel):
    ok: bool
    invoices: int = 0
    credit_notes: int = 0
    dealers: int = 0
    cities_filled: int = 0
    date_from: str = ""
    date_to: str = ""
    last_sync_at: datetime | None = None
    message: str = ""


class WebhookSecretOut(BaseModel):
    webhook_secret: str
    webhook_url: str


def _detect_dc(accounts: str) -> str:
    a = (accounts or "").lower()
    if "zoho.eu" in a:
        return "eu"
    if "zoho.com.au" in a or "zohoapis.com.au" in a:
        return "au"
    if "zoho.jp" in a:
        return "jp"
    if "zoho.in" in a or "zohoapis.in" in a:
        return "in"
    if "zoho.com" in a or "zohoapis.com" in a:
        return "com"
    return "in"


async def _status_payload(db: AsyncSession, *, include_pending_orgs: bool = False) -> ZohoStatusOut:
    settings = get_settings()
    integ = await zs.ensure_integration(db)
    # Ensure webhook secret exists so webhook can work before OAuth.
    secret = decrypt_secret(integ.encrypted_webhook_secret)
    if not secret:
        secret = secrets.token_urlsafe(24)
        integ.encrypted_webhook_secret = encrypt_secret(secret)
        await db.commit()
        await db.refresh(integ)

    pending: list[ZohoOrgOut] = []
    if include_pending_orgs and decrypt_secret(integ.encrypted_refresh_token) and not integ.zoho_org_id:
        try:
            orgs = await zs.list_organizations(db, integ)
            pending = [
                ZohoOrgOut(
                    organization_id=o["organization_id"],
                    name=o.get("name") or "",
                    contact_name=o.get("contact_name") or "",
                    plan_name=o.get("plan_name") or "",
                )
                for o in orgs
                if o.get("organization_id")
            ]
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            integ.last_error = str(exc)
            await db.commit()

    client_id, _, source = zs.resolve_client_credentials(settings, integ)
    creds = bool(client_id and source != "none")
    env_locked = source == "env"
    hint = ""
    if not creds:
        hint = "Zoho app not configured on server"

    accounts = zs.accounts_url(settings, integ=integ)
    api_base = zs.api_base_url(settings, integ=integ)
    connected = bool(
        integ.zoho_org_id
        and decrypt_secret(integ.encrypted_refresh_token)
        and integ.connection_status.lower() in {"connected", "ok"}
    )
    cid_hint = ""
    if client_id:
        cid_hint = client_id[:6] + "…" + client_id[-4:] if len(client_id) > 12 else client_id

    return ZohoStatusOut(
        connected=connected,
        credentials_configured=creds,
        credentials_source=source,
        credentials_locked_by_env=env_locked,
        client_id="" if env_locked else (integ.client_id or ""),
        client_id_hint=cid_hint,
        connection_status=integ.connection_status,
        api_product=integ.api_product or "books",
        zoho_org_id=integ.zoho_org_id,
        zoho_org_name=integ.zoho_org_name,
        zoho_org_number=integ.zoho_org_number or integ.zoho_org_id,
        accounts_url=accounts,
        api_base_url=api_base,
        data_center=_detect_dc(accounts),
        oauth_callback_url=zs.zoho_callback_url(settings),
        webhook_url=zs.zoho_webhook_url(settings),
        webhook_secret_set=bool(decrypt_secret(integ.encrypted_webhook_secret)),
        webhook_secret_hint=(secret[:4] + "…" + secret[-4:]) if secret and len(secret) > 8 else "••••",
        last_sync_at=integ.last_sync_at,
        last_sync_status=integ.last_sync_status,
        last_sync_error=integ.last_sync_error,
        last_error=integ.last_error,
        pending_organizations=pending,
        setup_hint=hint,
    )


@router.get("/api/integrations/zoho", response_model=ZohoStatusOut)
@router.get("/api/settings/zoho", response_model=ZohoStatusOut)
async def zoho_status(db: Db, user: CurrentUser):
    _require_admin(user)
    return await _status_payload(db, include_pending_orgs=True)


@router.post("/api/integrations/zoho/credentials", response_model=ZohoStatusOut)
async def save_credentials(body: CredentialsIn, db: Db, user: CurrentUser):
    """Credentials are env-only — UI paste is disabled. Optional DC URL updates still allowed."""
    _require_admin(user)
    integ = await zs.ensure_integration(db)

    if body.client_id.strip() or body.client_secret.strip():
        raise HTTPException(
            400,
            "Zoho Client ID/Secret are configured via ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET on the server",
        )

    dc = (body.data_center or "in").strip().lower()
    if body.accounts_url.strip() and body.api_base_url.strip():
        integ.accounts_url = body.accounts_url.strip().rstrip("/")
        integ.api_base_url = body.api_base_url.strip().rstrip("/")
    elif dc in ZOHO_DC_PRESETS:
        accounts, api_base = ZOHO_DC_PRESETS[dc]
        integ.accounts_url = accounts
        integ.api_base_url = api_base

    await db.commit()
    return await _status_payload(db, include_pending_orgs=True)


@router.get("/api/integrations/zoho/connect")
async def zoho_connect(
    db: Db,
    user: CurrentUser,
    popup: Annotated[bool, Query()] = True,
):
    _require_admin(user)
    settings = get_settings()
    integ = await zs.ensure_integration(db)
    if not zs.credentials_configured(settings, integ):
        raise HTTPException(
            400,
            "Zoho app not configured on server (set ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET)",
        )
    state = secrets.token_urlsafe(24)
    db.add(
        ZohoOAuthState(
            state=state,
            user_id=user.id,
            popup=popup,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=20),
        )
    )
    await db.commit()
    return {"auth_url": zs.build_auth_url(state, settings, integ=integ), "popup": popup}


@router.get("/api/integrations/zoho/callback")
async def zoho_callback(
    request: Request,
    db: Db,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Public OAuth redirect target — no JWT required. Popup mode closes via postMessage."""

    async def _fail(msg: str, *, as_popup: bool = False):
        if as_popup:
            return _popup_oauth_html(ok=False, error=msg)
        return RedirectResponse(_frontend_settings_url(query=f"tab=zoho&error={quote(msg)}"), status_code=302)

    popup = False
    row = None
    if state:
        row = (
            await db.execute(select(ZohoOAuthState).where(ZohoOAuthState.state == state))
        ).scalar_one_or_none()
        if row:
            popup = bool(row.popup)

    if error:
        if row:
            await db.execute(delete(ZohoOAuthState).where(ZohoOAuthState.state == state))
            await db.commit()
        return await _fail(error, as_popup=popup)
    if not code or not state:
        return await _fail("missing_code", as_popup=popup)
    if not row:
        return await _fail("invalid_state", as_popup=False)

    now = datetime.now(timezone.utc)
    exp = row.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    user_id = row.user_id
    await db.execute(delete(ZohoOAuthState).where(ZohoOAuthState.state == state))
    if exp < now:
        await db.commit()
        return await _fail("state_expired", as_popup=popup)

    integ = await zs.ensure_integration(db)
    try:
        tokens = await zs.exchange_code_for_tokens(code, integ=integ)
        zs.apply_tokens(integ, tokens)
        integ.connected_by_user_id = user_id
        integ.connected_at = now
        integ.zoho_org_id = ""
        integ.zoho_org_name = ""
        integ.zoho_org_number = ""
        integ.connection_status = "pending_org"
        integ.last_error = ""
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        integ.connection_status = "error"
        integ.last_error = str(exc)
        await db.commit()
        return await _fail(str(exc)[:120], as_popup=popup)

    if popup:
        return _popup_oauth_html(ok=True)
    return RedirectResponse(_frontend_settings_url(query="tab=zoho&oauth=1"), status_code=302)


@router.post("/api/integrations/zoho/select-org", response_model=ZohoStatusOut)
async def select_org(body: SelectOrgIn, db: Db, user: CurrentUser):
    _require_admin(user)
    integ = await zs.ensure_integration(db)
    if not decrypt_secret(integ.encrypted_refresh_token):
        raise HTTPException(400, "Connect Zoho before selecting an organization")
    org_id = body.organization_id.strip()
    if not org_id:
        raise HTTPException(400, "organization_id required")

    # Validate against Zoho list when possible
    name = body.name.strip()
    number = body.organization_number.strip() or org_id
    try:
        orgs = await zs.list_organizations(db, integ)
        match = next((o for o in orgs if o["organization_id"] == org_id), None)
        if match:
            name = match.get("name") or name
            number = org_id
        elif orgs:
            raise HTTPException(400, "Organization not found for this Zoho account")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        # If list fails but user supplied id, still allow save (Books sometimes rate-limits).
        integ.last_error = str(exc)

    integ.zoho_org_id = org_id
    integ.zoho_org_name = name or f"Org {org_id}"
    integ.zoho_org_number = number
    integ.connection_status = "connected"
    integ.connected_at = datetime.now(timezone.utc)
    integ.connected_by_user_id = user.id
    integ.last_error = ""
    await db.commit()
    return await _status_payload(db)


@router.post("/api/integrations/zoho/disconnect", response_model=ZohoStatusOut)
async def disconnect(db: Db, user: CurrentUser):
    _require_admin(user)
    integ = await zs.ensure_integration(db)
    # Keep webhook secret so push endpoint keeps working.
    integ.encrypted_access_token = ""
    integ.encrypted_refresh_token = ""
    integ.token_expires_at = None
    integ.zoho_org_id = ""
    integ.zoho_org_name = ""
    integ.zoho_org_number = ""
    integ.connection_status = "disconnected"
    integ.last_error = ""
    integ.last_sync_status = ""
    integ.last_sync_error = ""
    await db.commit()
    return await _status_payload(db)


@router.post("/api/integrations/zoho/sync", response_model=SyncOut)
async def sync_now(
    db: Db,
    user: CurrentUser,
    full_customers: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
):
    _require_admin(user)
    integ = await zs.ensure_integration(db)
    if not decrypt_secret(integ.encrypted_refresh_token) or not integ.zoho_org_id:
        raise HTTPException(400, "Connect Zoho and select an organization first")
    try:
        counts = await zs.sync_recent(
            db,
            integ,
            full_customers=full_customers,
            date_from=date_from,
            date_to=date_to,
        )
        integ.last_sync_at = datetime.now(timezone.utc)
        integ.last_sync_status = "ok"
        integ.last_sync_error = ""
        integ.connection_status = "connected"
        await db.commit()
        range_bit = ""
        if counts.get("date_from"):
            range_bit = f" ({counts['date_from']} → {counts.get('date_to') or ''})"
        return SyncOut(
            ok=True,
            invoices=counts["invoices"],
            credit_notes=counts["credit_notes"],
            dealers=counts["dealers"],
            date_from=str(counts.get("date_from") or ""),
            date_to=str(counts.get("date_to") or ""),
            last_sync_at=integ.last_sync_at,
            message=(
                f"Synced {counts['invoices']} invoices, "
                f"{counts['credit_notes']} credit notes, "
                f"{counts['dealers']} dealers"
                + range_bit
                + (f" ({counts.get('customer_pages', 0)} customer pages)" if full_customers else "")
            ),
        )
    except Exception as exc:  # noqa: BLE001
        integ.last_sync_at = datetime.now(timezone.utc)
        integ.last_sync_status = "error"
        integ.last_sync_error = str(exc)
        integ.last_error = str(exc)
        await db.commit()
        raise HTTPException(502, str(exc)) from exc


@router.post("/api/integrations/zoho/sync/customers", response_model=SyncOut)
async def sync_customers(db: Db, user: CurrentUser):
    """Full import of all Zoho Books customers into dealers / zoho_dealers."""
    _require_admin(user)
    integ = await zs.ensure_integration(db)
    if not decrypt_secret(integ.encrypted_refresh_token) or not integ.zoho_org_id:
        raise HTTPException(400, "Connect Zoho and select an organization first")
    try:
        counts = await zs.sync_all_customers(db, integ)
        integ.last_sync_at = datetime.now(timezone.utc)
        integ.last_sync_status = "ok"
        integ.last_sync_error = ""
        integ.connection_status = "connected"
        await db.commit()
        return SyncOut(
            ok=True,
            dealers=counts["dealers"],
            last_sync_at=integ.last_sync_at,
            message=f"Imported {counts['dealers']} customers across {counts['pages']} pages",
        )
    except Exception as exc:  # noqa: BLE001
        integ.last_sync_at = datetime.now(timezone.utc)
        integ.last_sync_status = "error"
        integ.last_sync_error = str(exc)
        integ.last_error = str(exc)
        await db.commit()
        raise HTTPException(502, str(exc)) from exc


@router.post("/api/integrations/zoho/sync/dealer-cities", response_model=SyncOut)
@router.post("/api/integrations/zoho/sync/contact-details", response_model=SyncOut)
async def sync_dealer_contact_details(
    db: Db,
    user: CurrentUser,
    limit: int | None = Query(None, ge=1, le=5000),
    force_all: bool = Query(False),
):
    """Fetch full Zoho Books contacts into SQL (raw_json + dealers city/state/phone)."""
    _require_admin(user)
    integ = await zs.ensure_integration(db)
    if not decrypt_secret(integ.encrypted_refresh_token) or not integ.zoho_org_id:
        raise HTTPException(400, "Connect Zoho and select an organization first")
    try:
        stats = await zs.enrich_dealer_cities(db, integ, limit=limit, force_all=force_all)
        integ.last_sync_at = datetime.now(timezone.utc)
        integ.last_sync_status = "ok"
        integ.last_sync_error = ""
        integ.connection_status = "connected"
        await db.commit()
        return SyncOut(
            ok=True,
            dealers=stats.get("fetched", 0),
            cities_filled=stats.get("cities_filled", 0),
            last_sync_at=integ.last_sync_at,
            message=(
                f"Fetched {stats.get('fetched', 0)} full contacts "
                f"(candidates {stats.get('candidates', 0)}); "
                f"filled {stats.get('cities_filled', 0)} cities "
                f"({stats.get('errors', 0)} errors, "
                f"{stats.get('skipped_no_city', 0)} still blank)"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        integ.last_sync_at = datetime.now(timezone.utc)
        integ.last_sync_status = "error"
        integ.last_sync_error = str(exc)
        integ.last_error = str(exc)
        await db.commit()
        raise HTTPException(502, str(exc)) from exc


@router.post("/api/integrations/zoho/sync/sales", response_model=SyncOut)
async def sync_sales(
    db: Db,
    user: CurrentUser,
    date_from: str = "2025-04-01",
    date_to: str | None = None,
):
    """Historical invoices + credit notes import (no sales orders)."""
    _require_admin(user)
    integ = await zs.ensure_integration(db)
    if not decrypt_secret(integ.encrypted_refresh_token) or not integ.zoho_org_id:
        raise HTTPException(400, "Connect Zoho and select an organization first")
    try:
        counts = await zs.sync_sales_range(
            db,
            integ,
            date_from=date_from,
            date_to=date_to,
        )
        integ.last_sync_at = datetime.now(timezone.utc)
        integ.last_sync_status = "ok"
        integ.last_sync_error = ""
        integ.connection_status = "connected"
        await db.commit()
        return SyncOut(
            ok=True,
            invoices=counts["invoices"],
            credit_notes=counts["credit_notes"],
            date_from=counts["date_from"],
            date_to=counts["date_to"],
            last_sync_at=integ.last_sync_at,
            message=(
                f"Imported {counts['invoices']} invoices + {counts['credit_notes']} credit notes "
                f"({counts['date_from']} → {counts['date_to']})"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        integ.last_sync_at = datetime.now(timezone.utc)
        integ.last_sync_status = "error"
        integ.last_sync_error = str(exc)
        integ.last_error = str(exc)
        await db.commit()
        raise HTTPException(502, str(exc)) from exc


@router.post("/api/integrations/zoho/webhook-secret/rotate", response_model=WebhookSecretOut)
async def rotate_webhook_secret(db: Db, user: CurrentUser):
    _require_admin(user)
    integ = await zs.ensure_integration(db)
    secret = secrets.token_urlsafe(24)
    integ.encrypted_webhook_secret = encrypt_secret(secret)
    await db.commit()
    return WebhookSecretOut(webhook_secret=secret, webhook_url=zs.zoho_webhook_url())


@router.get("/api/integrations/zoho/webhook-secret", response_model=WebhookSecretOut)
async def reveal_webhook_secret(db: Db, user: CurrentUser):
    _require_admin(user)
    integ = await zs.ensure_integration(db)
    secret = decrypt_secret(integ.encrypted_webhook_secret)
    if not secret:
        secret = secrets.token_urlsafe(24)
        integ.encrypted_webhook_secret = encrypt_secret(secret)
        await db.commit()
    return WebhookSecretOut(webhook_secret=secret, webhook_url=zs.zoho_webhook_url())


@router.get("/api/integrations/zoho/webhook")
async def zoho_webhook_probe():
    """Zoho / uptime probes sometimes GET the URL before saving."""
    return {"ok": True, "service": "yesweigh-crm-zoho-webhook"}


@router.post("/api/integrations/zoho/webhook")
async def zoho_webhook(
    request: Request,
    db: Db,
    x_zoho_webhook_secret: Annotated[str | None, Header()] = None,
    x_webhook_secret: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
):
    """
    Public Zoho → CRM push endpoint.
    Verify with header X-Zoho-Webhook-Secret (or X-Webhook-Secret / Bearer).
    Works even before OAuth connect, as long as the shared secret matches.
    """
    integ = await zs.ensure_integration(db)
    # Persist Interweighing org id when known from GATC / Books session.
    if not (integ.zoho_org_id or "").strip():
        integ.zoho_org_id = "60001225303"
        integ.zoho_org_name = integ.zoho_org_name or "INTERWEIGHING PRIVATE LIMITED"
        integ.zoho_org_number = integ.zoho_org_number or "60001225303"
        await db.flush()

    expected = decrypt_secret(integ.encrypted_webhook_secret) or get_settings().zoho_webhook_secret.strip()
    provided = (x_zoho_webhook_secret or x_webhook_secret or "").strip()
    if not provided and authorization:
        auth = authorization.strip()
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        else:
            provided = auth

    raw_body = await request.body()
    payload: Any
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}") if raw_body else {}
    except Exception:  # noqa: BLE001
        payload = {"raw": raw_body.decode("utf-8", errors="replace")} if raw_body else {}
    if not isinstance(payload, dict):
        payload = {"data": payload}

    # Zoho Books URL validation often POSTs without custom headers / with empty body.
    # Accept those probes with 200 so the URL can be saved; real events must auth.
    is_probe = (not raw_body) or payload == {} or str(payload.get("event_type") or "").lower() in {
        "ping",
        "verify",
        "test",
        "url_validation",
    }
    secret_ok = False
    if not expected:
        if provided:
            integ.encrypted_webhook_secret = encrypt_secret(provided)
            expected = provided
            await db.flush()
            secret_ok = True
        elif is_probe:
            return {"ok": True, "verified": True}
        else:
            raise HTTPException(401, "Webhook secret not configured")
    else:
        secret_ok = bool(provided) and secrets.compare_digest(provided, expected)
        if not secret_ok:
            if is_probe:
                return {"ok": True, "verified": True}
            raise HTTPException(401, "Invalid webhook secret")

    module, event_type, record_id, record = zs.normalize_webhook_payload(payload)
    external_id = str(
        payload.get("event_id")
        or payload.get("notification_id")
        or f"{module}:{record_id}:{event_type}"
    )

    # Idempotent event log
    if external_id and external_id != f"{module}::{event_type}":
        existing = (
            await db.execute(
                select(ZohoWebhookEvent).where(ZohoWebhookEvent.external_event_id == external_id)
            )
        ).scalar_one_or_none()
        if existing and existing.processing_status == "processed":
            return {"ok": True, "duplicate": True}

    ev = ZohoWebhookEvent(
        external_event_id=external_id[:160],
        event_type=event_type[:80],
        module=module[:80],
        zoho_record_id=(record_id or "")[:80],
        processing_status="received",
        payload=json.dumps(payload, default=str),
    )
    db.add(ev)
    await db.flush()

    try:
        result = await zs.process_webhook_event(
            db,
            integ,
            module=module,
            event_type=event_type,
            record_id=record_id,
            record=record,
        )
        ev.processing_status = "processed"
        ev.processed_at = datetime.now(timezone.utc)
    except Exception as exc:  # noqa: BLE001
        ev.processing_status = "error"
        ev.error_message = str(exc)
        await db.commit()
        raise HTTPException(500, f"Webhook processing failed: {exc}") from exc

    await db.commit()
    return {"ok": True, "module": module, "record_id": record_id, **result}
