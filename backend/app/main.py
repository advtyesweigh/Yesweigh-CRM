from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import adms_employees, adms_report, adms_sync
from app.auth import create_access_token, get_current_user, verify_password
from app.config import get_settings
from app.db import get_db
from app.employees import router as employees_router
from app.evolution import router as evolution_router
from app.inbox import router as inbox_router
from app.cloud_whatsapp import router as cloud_whatsapp_router
from app.kerala_district_order import UNKNOWN_RANK, district_rank_whens
from app.models import AdmsAttendanceLog, Employee, Lead, User, WorkItem
from app.schemas import DashboardOut, LeadIn, LeadOut, LeadPage, LeadUpdate, TokenOut, UserOut, WorkIn, WorkOut
from app.vps_health import router as vps_health_router
from app.catalog import router as catalog_router
from app.dealers import router as dealers_router
from app.sales_products import router as sales_products_router
from app.sales_stats import router as sales_stats_router
from app.pincode import router as pincode_router
from app.zoho import router as zoho_router
from app.ai import router as ai_router

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    adms_sync.start_background_sync()
    # Orphaned AI runs stay "running" forever after API restart — fail them so /work stops polling.
    try:
        from app.ai.service import recover_stale_runs_on_startup

        await recover_stale_runs_on_startup()
    except Exception:  # noqa: BLE001
        pass
    yield
    await adms_sync.stop_background_sync()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()] or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(employees_router)
app.include_router(evolution_router)
app.include_router(inbox_router)
app.include_router(cloud_whatsapp_router)
app.include_router(vps_health_router)
app.include_router(zoho_router)
app.include_router(dealers_router)
app.include_router(catalog_router)
app.include_router(sales_products_router)
app.include_router(sales_stats_router)
app.include_router(pincode_router)
app.include_router(ai_router)

Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@app.get("/api/health")
async def health():
    return {"ok": True, "app": settings.app_name}


@app.post("/api/auth/login", response_model=TokenOut)
async def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], db: Db):
    username = (form.username or "").strip()
    # Allow Aadhaar (digits) or email as login id
    if username.isdigit():
        username = "".join(ch for ch in username if ch.isdigit())
    user = (await db.execute(select(User).where(User.email == username))).scalar_one_or_none()
    if not user and username.isdigit():
        emp = (
            await db.execute(select(Employee).where(Employee.aadhaar == username))
        ).scalar_one_or_none()
        if emp and emp.user_id:
            user = await db.get(User, emp.user_id)
    if not user or not verify_password(form.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    token = create_access_token(user.email)
    return TokenOut(access_token=token, name=user.name, email=user.email, role=user.role)


@app.get("/api/me", response_model=UserOut)
async def me(user: CurrentUser):
    return user


@app.get("/api/dashboard", response_model=DashboardOut)
async def dashboard(db: Db, user: CurrentUser):
    leads_total = (await db.execute(select(func.count()).select_from(Lead))).scalar() or 0
    leads_new = (await db.execute(select(func.count()).select_from(Lead).where(Lead.status == "new"))).scalar() or 0
    work_open = (
        await db.execute(
            select(func.count()).select_from(WorkItem).where(WorkItem.status.in_(["todo", "doing", "blocked"]))
        )
    ).scalar() or 0
    employees_active = (
        await db.execute(
            select(func.count())
            .select_from(Employee)
            .where(Employee.status.in_(["Active", "Probation", "active"]))
        )
    ).scalar() or 0
    return DashboardOut(
        leads_total=leads_total,
        leads_new=leads_new,
        work_open=work_open,
        employees_active=employees_active,
        modules=["leads", "whatsapp", "adms", "employees", "work"],
    )


# Known spelling variants so UI "Kozhikode" still matches LMOMS "Kozhikkode", etc.
_DISTRICT_ALIASES: dict[str, list[str]] = {
    "kozhikode": ["Kozhikode", "Kozhikkode"],
    "kozhikkode": ["Kozhikode", "Kozhikkode"],
    "kasaragod": ["Kasaragod", "Kasargod", "Kasargode"],
    "kasargod": ["Kasaragod", "Kasargod", "Kasargode"],
    "kasargode": ["Kasaragod", "Kasargod", "Kasargode"],
    "pathanamthitta": ["Pathanamthitta", "Pattanamtitta"],
    "pattanamtitta": ["Pathanamthitta", "Pattanamtitta"],
    "thiruvananthapuram": ["Thiruvananthapuram", "Thiruvanathapuram", "Trivandrum"],
    "thiruvanathapuram": ["Thiruvananthapuram", "Thiruvanathapuram", "Trivandrum"],
    "trivandrum": ["Thiruvananthapuram", "Thiruvanathapuram", "Trivandrum"],
}
_STATE_ALIASES: dict[str, list[str]] = {
    "uttarakhand": ["Uttarakhand", "Uttaranchal"],
    "uttaranchal": ["Uttarakhand", "Uttaranchal"],
    "andaman and nicobar": ["Andaman and Nicobar", "Andaman and Nicobar Islands"],
    "andaman and nicobar islands": ["Andaman and Nicobar", "Andaman and Nicobar Islands"],
}


def _alias_values(raw: str, table: dict[str, list[str]]) -> list[str]:
    term = (raw or "").strip()
    if not term:
        return []
    keyed = "".join(ch for ch in term.lower() if ch.isalnum() or ch.isspace()).strip()
    # also try compacted key without spaces for lookup
    compact = "".join(ch for ch in term.lower() if ch.isalnum())
    vals = table.get(term.lower()) or table.get(keyed) or table.get(compact) or [term]
    out: list[str] = []
    for v in [term, *vals]:
        if v and v not in out:
            out.append(v)
    return out


@app.get("/api/leads", response_model=LeadPage)
async def list_leads(
    db: Db,
    user: CurrentUser,
    page: int = 1,
    page_size: int = 50,
    limit: int | None = None,
    q: str = "",
    status: str = "",
    state: str = "",
    district: str = "",
):
    """Paginated leads with text search, status, and cascading state/district filters."""
    size = limit if limit is not None else page_size
    size = max(1, min(size, 200))
    page = max(1, page)

    def apply_filters(stmt, *, include_status: bool):
        term = (q or "").strip()
        if term:
            pattern = f"%{term}%"
            stmt = stmt.where(
                or_(
                    Lead.name.ilike(pattern),
                    Lead.company.ilike(pattern),
                    Lead.phone.ilike(pattern),
                    Lead.city.ilike(pattern),
                    Lead.district.ilike(pattern),
                    Lead.state.ilike(pattern),
                    Lead.pin.ilike(pattern),
                )
            )
        state_vals = _alias_values(state, _STATE_ALIASES)
        if state_vals:
            stmt = stmt.where(or_(*[Lead.state.ilike(v) for v in state_vals]))
        district_vals = _alias_values(district, _DISTRICT_ALIASES)
        if district_vals:
            stmt = stmt.where(or_(*[Lead.district.ilike(v) for v in district_vals]))
        if include_status:
            st = (status or "").strip().lower()
            if st and st != "all":
                stmt = stmt.where(func.lower(Lead.status) == st)
        return stmt

    # Tile counts honor search + geo, but not the active status tile.
    counts_base = apply_filters(
        select(Lead.status, func.count()).group_by(Lead.status),
        include_status=False,
    )
    count_rows = (await db.execute(counts_base)).all()
    status_counts = {str(s or ""): int(n) for s, n in count_rows}
    for key in ("new", "contacted", "qualified", "won", "lost"):
        status_counts.setdefault(key, 0)

    query = apply_filters(select(Lead), include_status=True)
    count_query = apply_filters(select(func.count()).select_from(Lead), include_status=True)

    total = (await db.execute(count_query)).scalar() or 0
    offset = (page - 1) * size
    # Default: Kerala north→south by district (aliases normalized), then name.
    district_rank = case(*district_rank_whens(Lead.district), else_=UNKNOWN_RANK)
    rows = (
        await db.execute(
            query.order_by(district_rank.asc(), Lead.name.asc(), Lead.id.asc())
            .offset(offset)
            .limit(size)
        )
    ).scalars().all()
    return LeadPage(
        items=rows,
        total=total,
        page=page,
        page_size=size,
        status_counts=status_counts,
    )


@app.post("/api/leads", response_model=LeadOut)
async def create_lead(body: LeadIn, db: Db, user: CurrentUser):
    data = body.model_dump()
    if not (data.get("name") or "").strip() and not (data.get("company") or "").strip():
        raise HTTPException(400, "Name or company is required")
    lead = Lead(**data, owner_id=user.id)
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    return lead


@app.post("/api/leads/import", response_model=dict)
async def import_leads(body: list[LeadIn], db: Db, user: CurrentUser):
    """Bulk create leads with phone → email → name+company dedupe against existing rows."""
    existing = (await db.execute(select(Lead))).scalars().all()

    def _phone(v: str) -> str:
        digits = "".join(c for c in (v or "") if c.isdigit())
        return digits[-10:] if len(digits) >= 10 else digits

    def _email(v: str) -> str:
        return (v or "").strip().lower()

    def _nameco(name: str, company: str) -> str:
        def part(s: str) -> str:
            return "".join(c for c in (s or "").upper() if c.isalnum())

        return f"{part(name)}|{part(company)}"

    def _key(phone: str, email: str, name: str, company: str) -> str:
        p = _phone(phone)
        if p:
            return f"phone:{p}"
        e = _email(email)
        if e:
            return f"email:{e}"
        return f"nameco:{_nameco(name, company)}"

    seen = {_key(r.phone, r.email, r.name, r.company) for r in existing}
    inserted = 0
    skipped = 0
    for item in body:
        key = _key(item.phone, item.email, item.name, item.company)
        if key in seen:
            skipped += 1
            continue
        db.add(Lead(**item.model_dump(), owner_id=user.id))
        seen.add(key)
        inserted += 1
    await db.commit()
    return {"inserted": inserted, "skipped_duplicates": skipped, "received": len(body)}


@app.patch("/api/leads/{lead_id}", response_model=LeadOut)
async def update_lead(lead_id: int, body: LeadUpdate, db: Db, user: CurrentUser):
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    data = body.model_dump(exclude_unset=True)
    if "name" in data or "company" in data:
        name = data.get("name", lead.name)
        company = data.get("company", lead.company)
        if not (name or "").strip() and not (company or "").strip():
            raise HTTPException(400, "Name or company is required")
    if "pin" in data and data["pin"] is not None:
        data["pin"] = "".join(c for c in str(data["pin"]) if c.isdigit())[:20]
    for k, v in data.items():
        setattr(lead, k, v if v is not None else "")
    await db.commit()
    await db.refresh(lead)
    return lead


@app.delete("/api/leads/{lead_id}", status_code=204)
async def delete_lead(lead_id: int, db: Db, user: CurrentUser):
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    await db.delete(lead)
    await db.commit()
    return None


@app.get("/api/work", response_model=list[WorkOut])
async def list_work(db: Db, user: CurrentUser):
    return (await db.execute(select(WorkItem).order_by(WorkItem.id.desc()).limit(200))).scalars().all()


@app.post("/api/work", response_model=WorkOut)
async def create_work(body: WorkIn, db: Db, user: CurrentUser):
    item = WorkItem(**body.model_dump(), assignee_id=body.assignee_id or user.id)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


@app.patch("/api/work/{work_id}", response_model=WorkOut)
async def update_work(work_id: int, body: WorkIn, db: Db, user: CurrentUser):
    item = await db.get(WorkItem, work_id)
    if not item:
        raise HTTPException(404, "Work item not found")
    for k, v in body.model_dump().items():
        setattr(item, k, v)
    await db.commit()
    await db.refresh(item)
    return item


def _adms_auth() -> httpx.BasicAuth | None:
    if settings.adms_username and settings.adms_password:
        return httpx.BasicAuth(settings.adms_username, settings.adms_password)
    return None


_LOCAL_IPS = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


def _is_local_or_test_device(d: dict) -> bool:
    """Exclude simulator / loopback devices from CRM ADMS views."""
    serial = str(d.get("serial_number") or d.get("serial") or "").strip().lower()
    name = str(d.get("name") or "").strip().lower()
    ip = str(d.get("last_ip") or "").strip().lower()
    if serial in {"test", "simulator", "demo", "local"}:
        return True
    if name in {"test", "simulator", "demo", "local"}:
        return True
    if serial.startswith("test") or name.startswith("test"):
        return True
    if ip in _LOCAL_IPS or ip.startswith("127."):
        return True
    return False


def _filter_real_devices(rows: list[dict]) -> list[dict]:
    return [d for d in rows if not _is_local_or_test_device(d)]


def _adms_connection_status(d: dict) -> str:
    """Normalize ADMS Online / Unstable / Offline (prefer explicit connection_status)."""
    raw = str(d.get("connection_status") or "").strip()
    if raw in ("Online", "Unstable", "Offline"):
        return raw
    if d.get("unstable"):
        return "Unstable"
    if d.get("online"):
        return "Online"
    return "Offline"


def _device_counts(rows: list[dict]) -> dict:
    return {
        "total": len(rows),
        "online": sum(1 for d in rows if _adms_connection_status(d) == "Online"),
        "unstable": sum(1 for d in rows if _adms_connection_status(d) == "Unstable"),
        "offline": sum(
            1
            for d in rows
            if _adms_connection_status(d) == "Offline" and d.get("status") != "disabled"
        ),
        "pending_approval": sum(1 for d in rows if d.get("status") == "pending"),
        "approved": sum(1 for d in rows if d.get("status") == "approved"),
    }


def _parse_heartbeat_utc(raw: str | None):
    """Parse ADMS last_heartbeat_at ISO string to timezone-aware UTC datetime."""
    from datetime import datetime, timezone

    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    except ValueError:
        return None


def _format_ist_last_online(raw: str | None) -> str | None:
    """dd/mm/yyyy HH:mm in Asia/Kolkata from ADMS heartbeat timestamp."""
    from datetime import timezone
    from zoneinfo import ZoneInfo

    ts = _parse_heartbeat_utc(raw)
    if not ts:
        return None
    ist = ts.astimezone(ZoneInfo("Asia/Kolkata"))
    return ist.strftime("%d/%m/%Y %H:%M")


def _pick_primary_device(active: list[dict]) -> dict | None:
    """Prefer Online, then Unstable, else most recent heartbeat."""
    if not active:
        return None

    def _rank(d: dict) -> tuple:
        st = _adms_connection_status(d)
        tier = 0 if st == "Online" else (1 if st == "Unstable" else 2)
        ts = _parse_heartbeat_utc(d.get("last_heartbeat_at") or "")
        return (tier, -(ts.timestamp() if ts else 0.0))

    return min(active, key=_rank)


@app.get("/api/integrations/adms/health")
async def adms_health(user: CurrentUser):
    """Online only when ADMS gateway is up AND at least one real device is Online.

    ADMS thresholds (Delay=15): Online ≤45s, Unstable 45–120s, Offline >120s.
    Unstable is reported separately — do not treat it as binary Offline in the UI.
    Gateway /health alone is not enough — the ZK unit can be offline while the
    listener still answers.
    """
    gateway_ok = False
    status_code: int | None = None
    devices_online = 0
    devices_unstable = 0
    devices_total = 0
    primary: dict | None = None
    last_online_at = ""
    last_online_ist: str | None = None
    connection_status = "Offline"
    error: str | None = None
    thresholds: dict | None = None

    try:
        async with httpx.AsyncClient(timeout=4.0, auth=_adms_auth()) as client:
            r = await client.get(f"{settings.adms_base_url}/health")
            gateway_ok = r.is_success
            status_code = r.status_code
            if gateway_ok:
                try:
                    h = r.json()
                    if isinstance(h, dict):
                        thresholds = h.get("thresholds") if isinstance(h.get("thresholds"), dict) else {
                            "online_seconds": h.get("device_online_seconds"),
                            "offline_seconds": h.get("device_offline_seconds"),
                            "unstable_band": f"{h.get('device_online_seconds')}-{h.get('device_offline_seconds')}s",
                        }
                except Exception:  # noqa: BLE001
                    thresholds = None

            if gateway_ok:
                dr = await client.get(f"{settings.adms_base_url}/api/devices")
                if dr.is_success:
                    raw = dr.json()
                    rows = _filter_real_devices(raw if isinstance(raw, list) else [])
                    # Prefer approved / non-disabled; still count pending with heartbeat
                    active = [d for d in rows if d.get("status") != "disabled"]
                    devices_total = len(active)
                    online_devs = [d for d in active if _adms_connection_status(d) == "Online"]
                    unstable_devs = [d for d in active if _adms_connection_status(d) == "Unstable"]
                    devices_online = len(online_devs)
                    devices_unstable = len(unstable_devs)
                    pick = _pick_primary_device(active)
                    if pick:
                        hb = str(pick.get("last_heartbeat_at") or pick.get("last_seen") or "")
                        last_online_at = hb
                        last_online_ist = _format_ist_last_online(hb)
                        connection_status = _adms_connection_status(pick)
                        primary = {
                            "serial": pick.get("serial_number") or "",
                            "name": pick.get("name") or pick.get("serial_number") or "",
                            "online": connection_status == "Online",
                            "unstable": connection_status == "Unstable",
                            "connection_status": connection_status,
                            "last_heartbeat_at": hb,
                            "last_online_ist": last_online_ist or "",
                            "status": pick.get("status") or "",
                            "seconds_since_last_seen": pick.get("seconds_since_last_seen"),
                        }
                else:
                    error = f"devices status {dr.status_code}"
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        gateway_ok = False

    device_online = devices_online > 0
    device_unstable = (not device_online) and devices_unstable > 0
    if device_online:
        connection_status = "Online"
    elif device_unstable:
        connection_status = "Unstable"
    elif not primary:
        connection_status = "Offline"
    ok = gateway_ok and device_online
    offline_for_seconds: int | None = None
    needs_onsite = False
    recovery_hint = ""
    if connection_status == "Unstable" and last_online_at:
        ts = _parse_heartbeat_utc(last_online_at)
        if ts is not None:
            from datetime import datetime, timezone

            offline_for_seconds = int((datetime.now(timezone.utc) - ts).total_seconds())
        recovery_hint = (
            "Device heartbeat is stale (Unstable 45–120s). Waiting for next getrequest / "
            "ErrorDelay retry — not yet Offline."
        )
    elif not ok and last_online_at:
        ts = _parse_heartbeat_utc(last_online_at)
        if ts is not None:
            from datetime import datetime, timezone

            offline_for_seconds = int((datetime.now(timezone.utc) - ts).total_seconds())
            if offline_for_seconds >= int(settings.adms_offline_alert_seconds):
                needs_onsite = True
                recovery_hint = (
                    "Device stopped heartbeating. Toggle Cloud/ADMS on the biometric panel "
                    "or power-cycle it; confirm it can reach http://103.176.85.102:8081."
                )
            else:
                recovery_hint = (
                    "Gateway is up; waiting for the device ErrorDelay retry / next getrequest "
                    "(usually within ~30–120s after a blip)."
                )
    elif not gateway_ok:
        recovery_hint = "ADMS gateway unreachable — check adms.service on the VPS."
    return {
        "ok": ok,
        "gateway_ok": gateway_ok,
        "device_online": device_online,
        "device_unstable": device_unstable,
        "connection_status": connection_status,
        "devices_online": devices_online,
        "devices_unstable": devices_unstable,
        "devices_total": devices_total,
        "primary": primary,
        "last_online_at": last_online_at,
        "last_online_ist": last_online_ist,
        "offline_for_seconds": offline_for_seconds,
        "needs_onsite": needs_onsite,
        "recovery_hint": recovery_hint,
        "thresholds": thresholds,
        "status_code": status_code,
        "url": settings.adms_base_url,
        **({"error": error} if error else {}),
    }


@app.get("/api/integrations/adms/summary")
async def adms_summary(user: CurrentUser, db: Db):
    gateway: dict = {"ok": False, "url": settings.adms_base_url}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{settings.adms_base_url}/health")
            gateway = {
                "ok": r.is_success,
                "adms": r.json() if r.is_success else None,
                "url": settings.adms_base_url,
            }
    except Exception as exc:  # noqa: BLE001
        gateway = {"ok": False, "error": str(exc), "url": settings.adms_base_url}

    pg_total = (await db.execute(select(func.count()).select_from(AdmsAttendanceLog))).scalar() or 0
    pg_pending = (
        await db.execute(
            select(func.count()).select_from(AdmsAttendanceLog).where(AdmsAttendanceLog.firebase_status == "pending")
        )
    ).scalar() or 0
    adms_body = gateway.get("adms") if isinstance(gateway.get("adms"), dict) else {}
    sync = (adms_body or {}).get("sync") if isinstance(adms_body, dict) else {}
    devices = None
    device_list: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=5.0, auth=_adms_auth()) as client:
            dr = await client.get(f"{settings.adms_base_url}/api/devices")
            if dr.is_success:
                raw = dr.json()
                if isinstance(raw, list):
                    device_list = _filter_real_devices(raw)
                    devices = _device_counts(device_list)
            if devices is None:
                sr = await client.get(f"{settings.adms_base_url}/api/summary")
                if sr.is_success:
                    devices = sr.json().get("devices")
    except Exception:  # noqa: BLE001
        pass

    return {
        **gateway,
        "dashboard_url": "http://103.176.85.102:8081/",
        "pg": {"total": pg_total, "pending_firebase": pg_pending},
        "devices": devices,
        "device_list": [
            {
                "serial": d.get("serial_number") or "",
                "name": d.get("name") or d.get("serial_number") or "",
                "status": d.get("status") or "",
                "last_ip": d.get("last_ip") or "",
                "heartbeat": d.get("last_heartbeat_at") or "",
                "records": d.get("total_records") or 0,
                "online": bool(d.get("online")),
            }
            for d in device_list
        ],
        "sync": sync,
    }


@app.get("/api/integrations/adms/daily")
async def adms_daily(
    db: Db,
    user: CurrentUser,
    date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 50,
    q: str = "",
):
    """Daily attendance report: first punch = check-in, last = check-out."""
    return await adms_report.daily_report(
        db,
        auth=_adms_auth(),
        date_from=date_from or date,
        date_to=date_to or date_from or date,
        page=page,
        page_size=page_size,
        q=q,
    )


@app.get("/api/integrations/adms/logs")
async def adms_logs(
    db: Db,
    user: CurrentUser,
    page: int = 1,
    page_size: int = 50,
    employee: str = "",
    device: str = "",
    firebase_status: str = "",
    q: str = "",
):
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    clauses = []
    if employee.strip():
        clauses.append(AdmsAttendanceLog.employee_code == employee.strip())
    if device.strip():
        clauses.append(AdmsAttendanceLog.device_serial == device.strip())
    if firebase_status.strip():
        clauses.append(AdmsAttendanceLog.firebase_status == firebase_status.strip())
    if q.strip():
        like = f"%{q.strip()}%"
        clauses.append(
            or_(
                AdmsAttendanceLog.employee_code.ilike(like),
                AdmsAttendanceLog.employee_name.ilike(like),
                AdmsAttendanceLog.device_serial.ilike(like),
                AdmsAttendanceLog.device_name.ilike(like),
            )
        )
    # Hide simulator / local test device punches from CRM list
    clauses.append(AdmsAttendanceLog.device_serial.notin_(["TEST", "test", "SIMULATOR", "DEMO"]))
    clauses.append(~AdmsAttendanceLog.device_serial.ilike("test%"))
    filt = and_(*clauses) if clauses else None
    count_q = select(func.count()).select_from(AdmsAttendanceLog)
    list_q = select(AdmsAttendanceLog)
    if filt is not None:
        count_q = count_q.where(filt)
        list_q = list_q.where(filt)
    total = (await db.execute(count_q)).scalar() or 0
    rows = (
        await db.execute(
            list_q.order_by(AdmsAttendanceLog.punch_time.desc(), AdmsAttendanceLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return {
        "ok": True,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": max(1, (total + page_size - 1) // page_size),
        "items": [
            {
                "id": r.id,
                "adms_id": r.adms_id,
                "event_id": r.event_id,
                "time": r.punch_time,
                "employee": r.employee_code,
                "employee_name": r.employee_name,
                "device": r.device_serial,
                "device_name": r.device_name,
                "punch": r.punch_label or r.punch_status,
                "verify": r.verify_label or r.verify_method,
                "firebase": r.firebase_status,
                "punch_date": r.punch_date,
            }
            for r in rows
        ],
    }


def _adms_code_sort_key(code: str) -> tuple:
    s = (code or "").strip()
    if s.isdigit():
        return (0, int(s), s)
    return (1, 0, s.lower())


@app.get("/api/integrations/adms/available-employee-ids")
async def adms_available_employee_ids(user: CurrentUser, db: Db):
    """Distinct ADMS Emp Numbers (attendance + device users) minus CRM-linked IDs."""
    users = await adms_employees.collect_adms_users(db, _adms_auth())
    linked = await adms_employees.linked_adms_codes(db)
    used = sorted(
        {str(emp.adms_employee_code or "").strip() for emp in linked.values() if emp.adms_employee_code},
        key=_adms_code_sort_key,
    )
    available_users = [
        {
            "employee_code": u["employee_code"],
            "name": (u.get("name") or "").strip(),
            "card": (u.get("card") or "").strip(),
        }
        for u in users
        if u["employee_code"].lower() not in linked
    ]
    available = [u["employee_code"] for u in available_users]
    return {
        "available": available,
        "available_users": available_users,
        "used": used,
        "source_count": len(users),
        "used_count": len(used),
        "named_count": sum(1 for u in available_users if u["name"]),
    }


@app.get("/api/integrations/adms/device-employees")
async def adms_device_employees(user: CurrentUser, db: Db):
    """List ADMS/ZK device users (id + name) with CRM link status."""
    users = await adms_employees.collect_adms_users(db, _adms_auth())
    linked = await adms_employees.linked_adms_codes(db)
    items = []
    for u in users:
        emp = linked.get(u["employee_code"].lower())
        items.append(
            {
                **u,
                "linked": emp is not None,
                "crm_employee_id": emp.id if emp else None,
                "crm_name": emp.name if emp else None,
            }
        )
    return {
        "count": len(items),
        "named_count": sum(1 for i in items if (i.get("name") or "").strip()),
        "linked_count": sum(1 for i in items if i["linked"]),
        "employees": items,
    }


@app.get("/api/integrations/adms/import-preview")
async def adms_import_preview(
    user: CurrentUser,
    db: Db,
    device_serial: str | None = None,
):
    """Device picker + preview rows for Import from Device UI."""
    return await adms_employees.import_preview(
        db,
        _adms_auth(),
        device_serial=device_serial,
    )


@app.post("/api/integrations/adms/import-employees")
async def adms_import_employees(
    user: CurrentUser,
    db: Db,
    request: Request,
    named_only: bool = False,
    update_names: bool = False,
    dry_run: bool = False,
    confirm_overwrite: bool = False,
    device_serial: str | None = None,
):
    """Import ADMS/ZK users as CRM employee stubs (enrollment ID + Name).

    Mapping key: company + device serial + enrollment ID. Does not overwrite
    manually maintained CRM names unless confirm_overwrite=true.
    """
    body: dict = {}
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            body = raw
    except Exception:  # noqa: BLE001
        body = {}
    serial = str(body.get("device_serial") or device_serial or "").strip() or None
    return await adms_employees.import_adms_employees(
        db,
        _adms_auth(),
        device_serial=serial,
        company=str(body.get("company") or "YesWeigh"),
        named_only=bool(body.get("named_only", named_only)),
        update_names=bool(body.get("update_names", update_names)),
        confirm_overwrite=bool(body.get("confirm_overwrite", confirm_overwrite)),
        dry_run=bool(body.get("dry_run", dry_run)),
    )


@app.post("/api/integrations/adms/sync-device-users")
async def adms_sync_device_users(user: CurrentUser, request: Request):
    """Ask online ZK devices to upload USERINFO (PIN + Name) into ADMS."""
    body: dict = {}
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            body = raw
    except Exception:  # noqa: BLE001
        body = {}
    pins = body.get("pins")
    pin_list = [str(p).strip() for p in pins if str(p).strip()] if isinstance(pins, list) else None
    return await adms_employees.sync_device_users(
        _adms_auth(),
        device_serial=str(body.get("device_serial") or "").strip() or None,
        pins=pin_list,
    )


@app.post("/api/integrations/adms/map-employee")
async def adms_map_employee(user: CurrentUser, db: Db, request: Request):
    """Map a device enrollment ID to an existing CRM employee."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON body required")
    return await adms_employees.map_enrollment_to_employee(
        db,
        employee_id=int(body.get("employee_id") or 0),
        enrollment_id=str(body.get("enrollment_id") or body.get("adms_employee_code") or ""),
        device_serial=str(body.get("device_serial") or ""),
        overwrite_name=(str(body["overwrite_name"]) if body.get("overwrite_name") is not None else None),
        confirm_overwrite=bool(body.get("confirm_overwrite")),
    )


@app.post("/api/integrations/adms/import-employees-csv")
async def adms_import_employees_csv(user: CurrentUser, db: Db, request: Request):
    """CSV fallback when device USERINFO is unsupported or names are missing.

    Body: JSON `{ csv, device_serial?, confirm_overwrite?, company? }`
    or multipart form with `file` (+ optional `device_serial`, `confirm_overwrite`).
    """
    device_serial = str(request.query_params.get("device_serial") or "")
    confirm_overwrite = request.query_params.get("confirm_overwrite", "").lower() in {
        "1",
        "true",
        "yes",
    }
    company = "YesWeigh"
    content = ""
    ctype = (request.headers.get("content-type") or "").lower()

    if "multipart/form-data" in ctype:
        form = await request.form()
        device_serial = str(form.get("device_serial") or device_serial)
        confirm_overwrite = str(form.get("confirm_overwrite") or "").lower() in {
            "1",
            "true",
            "yes",
        } or confirm_overwrite
        company = str(form.get("company") or company)
        upload = form.get("file")
        if upload is not None and hasattr(upload, "read"):
            raw_bytes = await upload.read()  # type: ignore[misc]
            content = raw_bytes.decode("utf-8-sig", errors="replace")
        elif form.get("csv"):
            content = str(form.get("csv") or "")
    else:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if isinstance(body, dict):
            content = str(body.get("csv") or body.get("content") or "")
            device_serial = str(body.get("device_serial") or device_serial)
            confirm_overwrite = bool(body.get("confirm_overwrite", confirm_overwrite))
            company = str(body.get("company") or company)

    if not content.strip():
        raise HTTPException(400, "CSV content required (JSON {csv} or multipart file)")
    return await adms_employees.import_employees_csv(
        db,
        content,
        device_serial=device_serial,
        company=company,
        confirm_overwrite=confirm_overwrite,
    )


@app.get("/api/integrations/adms/employee/{employee_code}")
async def adms_employee_lookup(employee_code: str, user: CurrentUser):
    """Lookup a single ADMS employee by code for CRM Add Employee prefills."""
    code = (employee_code or "").strip()
    if not code:
        return {"found": False, "message": "ADMS ID is required"}
    try:
        async with httpx.AsyncClient(timeout=10.0, auth=_adms_auth()) as client:
            r = await client.get(
                f"{settings.adms_base_url}/api/employees",
                params={"limit": 5000},
            )
            if not r.is_success:
                return {"found": False, "message": f"ADMS lookup failed ({r.status_code})"}
            employees = r.json().get("employees") or []
    except Exception as exc:  # noqa: BLE001
        return {"found": False, "message": str(exc) or "ADMS unreachable"}

    match = None
    code_l = code.lower()
    for e in employees:
        if not isinstance(e, dict):
            continue
        emp_code = str(e.get("employee_code") or e.get("code") or "").strip()
        if emp_code.lower() == code_l:
            match = e
            break
    if not match:
        return {"found": False, "message": "No ADMS employee found for that ID"}

    phone = str(match.get("phone") or match.get("mobile") or match.get("phone_number") or "").strip()
    whatsapp = str(match.get("whatsapp") or match.get("alt_phone") or match.get("alternate_phone") or "").strip()
    aadhaar = str(match.get("aadhaar") or match.get("aadhar") or match.get("aadhaar_last4") or "").strip()
    if aadhaar:
        aadhaar = "".join(ch for ch in aadhaar if ch.isdigit())
    dob = match.get("dob") or match.get("date_of_birth") or match.get("birth_date") or None
    if dob is not None:
        dob = str(dob)[:10]

    return {
        "found": True,
        "employee_code": str(match.get("employee_code") or match.get("code") or code).strip(),
        "name": str(match.get("name") or match.get("employee_name") or "").strip(),
        "phone": phone,
        "email": str(match.get("email") or "").strip(),
        "gender": str(match.get("gender") or "").strip(),
        "dob": dob,
        "aadhaar": aadhaar,
        "whatsapp": whatsapp,
    }


@app.post("/api/integrations/adms/sync")
async def adms_sync_now(user: CurrentUser):
    return await adms_sync.sync_once(max_batches=80)


@app.get("/api/integrations/adms/overview")
async def adms_overview(
    user: CurrentUser,
    date: str | None = None,
    date_to: str | None = None,
    branch: str = "",
    device: str = "",
    status: str = "",
    q: str = "",
):
    params = {
        k: v
        for k, v in {
            "date": date,
            "date_to": date_to,
            "branch": branch or None,
            "device": device or None,
            "status": status or None,
            "q": q or None,
        }.items()
        if v
    }
    try:
        async with httpx.AsyncClient(timeout=20.0, auth=_adms_auth()) as client:
            r = await client.get(f"{settings.adms_base_url}/api/overview", params=params)
            if not r.is_success:
                return {"ok": False, "error": r.text[:300], "url": settings.adms_base_url}
            data = r.json()
            data["ok"] = True
            data["url"] = settings.adms_base_url
            data["dashboard_url"] = "http://103.176.85.102:8081/"
            return data
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "url": settings.adms_base_url}


@app.get("/api/integrations/evolution/status")
async def evolution_status(user: CurrentUser):
    headers = {}
    if settings.evolution_api_key:
        headers["apikey"] = settings.evolution_api_key
    try:
        async with httpx.AsyncClient(timeout=5.0, headers=headers) as client:
            r = await client.get(f"{settings.evolution_base_url}/server/ok")
            return {"ok": r.is_success, "status_code": r.status_code, "url": settings.evolution_base_url}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "url": settings.evolution_base_url}
