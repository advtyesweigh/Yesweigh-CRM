from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adms_employee_display_order import staff_display_rank
from app.adms_employee_exclusions import is_excluded_employee_code
from app.auth import get_current_user
from app.config import get_settings
from app.crypto_util import decrypt_secret, encrypt_secret
from app.db import get_db
from app.evolution_service import EvolutionService
from app.media_store import save_bytes, save_data_url
from app.models import (
    Employee,
    EvolutionIntegration,
    EvolutionWebhookEvent,
    User,
    WhatsAppConversation,
    WhatsAppInstance,
    WhatsAppInstanceActivityLog,
    WhatsAppInstanceEmployee,
    WhatsAppMessage,
)
from app.whatsapp_chat_types import is_group_jid, is_inbox_excluded_jid
from app.whatsapp_employee_exclusions import is_whatsapp_excluded_employee_code, normalize_code

ORG = "yesweigh"
router = APIRouter(tags=["evolution"])
Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


async def primary_owner_employee(db: AsyncSession, instance_id: int) -> Employee | None:
    """WhatsApp instance → linked employee (primary owner, else first active assignee)."""
    owner = (
        await db.execute(
            select(Employee)
            .join(WhatsAppInstanceEmployee, WhatsAppInstanceEmployee.employee_id == Employee.id)
            .where(
                WhatsAppInstanceEmployee.whatsapp_instance_id == instance_id,
                WhatsAppInstanceEmployee.organization_id == ORG,
                WhatsAppInstanceEmployee.is_active.is_(True),
                WhatsAppInstanceEmployee.revoked_at.is_(None),
                WhatsAppInstanceEmployee.is_primary_owner.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if owner:
        return owner
    return (
        await db.execute(
            select(Employee)
            .join(WhatsAppInstanceEmployee, WhatsAppInstanceEmployee.employee_id == Employee.id)
            .where(
                WhatsAppInstanceEmployee.whatsapp_instance_id == instance_id,
                WhatsAppInstanceEmployee.organization_id == ORG,
                WhatsAppInstanceEmployee.is_active.is_(True),
                WhatsAppInstanceEmployee.revoked_at.is_(None),
            )
            .order_by(WhatsAppInstanceEmployee.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def assign_conversation_to_instance_owner(
    db: AsyncSession, conv: WhatsAppConversation, instance_id: int, *, force: bool = False
) -> Employee | None:
    """Default conversation owner = WhatsApp-associated employee. No-op if already assigned unless force."""
    if conv.assigned_employee_id and not force:
        return None
    emp = await primary_owner_employee(db, instance_id)
    if not emp:
        return None
    conv.assigned_employee_id = emp.id
    if emp.user_id:
        conv.assigned_user_id = emp.user_id
    return emp


def clear_conversation_unread(conv: WhatsAppConversation) -> bool:
    """Clear CRM unread + green New badge state. Returns True if anything changed."""
    changed = False
    if (conv.unread_count or 0) != 0:
        conv.unread_count = 0
        changed = True
    if conv.status == "New":
        conv.status = "Open"
        changed = True
    if (conv.tag or "").strip().lower() == "new":
        conv.tag = ""
        changed = True
    return changed


def _require_admin(user: User) -> None:
    if user.role.lower() not in {"admin", "super_admin", "super admin", "management"}:
        raise HTTPException(403, "Administrator access required")


def _delete_password_ok(provided: str, expected: str) -> bool:
    a = (provided or "").strip()
    b = (expected or "").strip()
    if not a or not b or len(a) != len(b):
        return False
    return secrets.compare_digest(a, b)


def _evolution_logged_in(payload: Any) -> bool:
    """True only when WhatsApp is paired (QR scanned), not merely socket-up.

    Evolution GO often returns ``Connected: true, LoggedIn: false`` while a QR is
    still pending. Treating ``Connected`` alone as paired skipped QR display.
    """
    if not isinstance(payload, dict):
        return False
    if "LoggedIn" in payload or "loggedIn" in payload:
        return bool(payload.get("LoggedIn") or payload.get("loggedIn"))
    status = str(payload.get("status") or payload.get("state") or "").strip().lower()
    if status in {"open", "paired", "authenticated"}:
        return True
    jid = str(
        payload.get("jid")
        or payload.get("Jid")
        or payload.get("phone")
        or payload.get("Phone")
        or ""
    )
    has_device = bool(jid and ("@" in jid or jid.lstrip("+").isdigit()))
    if has_device and bool(payload.get("connected") or payload.get("Connected")):
        return True
    return False


def _evolution_phone(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    raw = str(
        payload.get("jid")
        or payload.get("Jid")
        or payload.get("phone")
        or payload.get("Phone")
        or ""
    )
    if not raw:
        return ""
    return raw.split("@")[0].split(":")[0]


def _webhook_callback(public_id: str) -> str:
    base = get_settings().cors_origins.split(",")[0].strip() or "https://crm.yesweigh.in"
    if "localhost" in base or not base.startswith("http"):
        base = "https://crm.yesweigh.in"
    return f"{base.rstrip('/')}/api/webhooks/evolution/{public_id}"


async def _get_integration(db: AsyncSession) -> EvolutionIntegration | None:
    return (
        await db.execute(
            select(EvolutionIntegration)
            .where(EvolutionIntegration.organization_id == ORG)
            .order_by(EvolutionIntegration.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _svc(db: AsyncSession) -> tuple[EvolutionIntegration, EvolutionService]:
    integ = await _get_integration(db)
    if not integ or not integ.enabled:
        raise HTTPException(400, "Evolution integration is not configured or disabled")
    key = decrypt_secret(integ.encrypted_api_key)
    if not key:
        # fall back to env key for bootstrap
        key = get_settings().evolution_api_key
    if not key:
        raise HTTPException(400, "Evolution API key missing")
    url = integ.server_url or get_settings().evolution_base_url
    return integ, EvolutionService(url, key)


async def _owned_instance(db: AsyncSession, public_id: str) -> WhatsAppInstance:
    inst = (
        await db.execute(
            select(WhatsAppInstance).where(
                WhatsAppInstance.public_id == public_id,
                WhatsAppInstance.organization_id == ORG,
                WhatsAppInstance.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not inst:
        raise HTTPException(404, "Instance not found")
    return inst


async def _log(db: AsyncSession, instance_id: int, action: str, details: str = "", user: User | None = None) -> None:
    db.add(
        WhatsAppInstanceActivityLog(
            organization_id=ORG,
            whatsapp_instance_id=instance_id,
            action=action,
            event_type=action,
            event_details=details,
        )
    )


class EvolutionConfigIn(BaseModel):
    server_url: str
    api_key: str = ""
    api_version: str = "evolution-go"
    enabled: bool = True


class EvolutionConfigOut(BaseModel):
    server_url: str
    api_version: str
    enabled: bool
    connection_status: str
    last_health_check: datetime | None = None
    webhook_callback_url: str
    api_key_set: bool


class InstanceCreateIn(BaseModel):
    """Create a YesWeigh WhatsApp line linked to one ADMS employee.

    Prefer primary_employee_id (display name derived from employee).
    display_name remains optional for legacy callers.
    """

    display_name: str = ""
    expected_phone_number: str = ""
    branch: str = ""
    department: str = ""
    purpose: str = "General Communication"
    description: str = ""
    primary_employee_id: int | None = None
    additional_employee_ids: list[int] = Field(default_factory=list)
    integration_type: str = "WhatsApp Web / Baileys"


class InstanceUpdateIn(BaseModel):
    display_name: str | None = None
    expected_phone_number: str | None = None
    branch: str | None = None
    department: str | None = None
    purpose: str | None = None
    description: str | None = None


class AssignIn(BaseModel):
    employee_id: int
    is_primary_owner: bool = False
    permissions: list[str] = Field(default_factory=lambda: ["view", "send", "receive"])


_ACTIVE_EMP_STATUSES = {"Active", "Probation", "active", "probation"}


def _enrollment_code(emp: Employee) -> str:
    return normalize_code(emp.adms_employee_code or emp.code or "")


def _instance_out(inst: WhatsAppInstance, assignees: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": inst.public_id,
        "display_name": inst.display_name,
        "employee_code": getattr(inst, "employee_code", "") or "",
        "employee_name": getattr(inst, "employee_name", "") or "",
        "department": inst.department,
        "branch": inst.branch,
        "purpose": inst.purpose,
        "description": inst.description,
        "expected_phone_number": inst.expected_phone_number,
        "connected_phone_number": inst.connected_phone_number,
        "profile_name": inst.profile_name,
        "profile_photo": inst.profile_photo,
        "connection_status": inst.connection_status,
        "registration_status": inst.registration_status,
        "integration_type": inst.integration_type,
        "provider": getattr(inst, "provider", None) or "evolution",
        "last_connected_at": inst.last_connected_at,
        "history_sync_status": getattr(inst, "history_sync_status", "") or "",
        "history_sync_at": getattr(inst, "history_sync_at", None),
        "history_sync_error": getattr(inst, "history_sync_error", "") or "",
        "created_at": inst.created_at,
        "assignees": assignees or [],
    }


def _maybe_schedule_history_sync(inst: WhatsAppInstance, *, trigger: str, force: bool = False) -> None:
    """Kick off instance-scoped history sync after LoggedIn (non-blocking)."""
    if (getattr(inst, "provider", None) or "").strip().lower() == "meta_cloud":
        return
    try:
        from app.whatsapp_history_sync import schedule_history_sync

        schedule_history_sync(inst.id, trigger=trigger, force=force)
    except Exception as exc:  # noqa: BLE001
        print(f"history sync schedule failed: {exc}")


async def _active_instance_employee_ids(db: AsyncSession) -> set[int]:
    rows = (
        await db.execute(
            select(WhatsAppInstanceEmployee.employee_id)
            .join(WhatsAppInstance, WhatsAppInstance.id == WhatsAppInstanceEmployee.whatsapp_instance_id)
            .where(
                WhatsAppInstanceEmployee.is_active.is_(True),
                WhatsAppInstanceEmployee.revoked_at.is_(None),
                WhatsAppInstance.organization_id == ORG,
                WhatsAppInstance.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    return {int(eid) for eid in rows if eid}


async def _resolve_create_employee(db: AsyncSession, employee_id: int) -> Employee:
    emp = await db.get(Employee, employee_id)
    if not emp or emp.status not in _ACTIVE_EMP_STATUSES:
        raise HTTPException(400, "Select an active employee")
    code = _enrollment_code(emp)
    if not code:
        raise HTTPException(400, "Employee has no ADMS enrollment code")
    if is_excluded_employee_code(code) or is_whatsapp_excluded_employee_code(code):
        raise HTTPException(400, "This employee is not eligible for a WhatsApp instance")
    linked = await _active_instance_employee_ids(db)
    if emp.id in linked:
        raise HTTPException(400, f"{emp.name} already has a WhatsApp instance")
    return emp


@router.get("/api/settings/evolution", response_model=EvolutionConfigOut)
async def get_evolution_config(db: Db, user: CurrentUser):
    _require_admin(user)
    integ = await _get_integration(db)
    settings = get_settings()
    if not integ:
        return EvolutionConfigOut(
            server_url=settings.evolution_base_url,
            api_version="evolution-go",
            enabled=False,
            connection_status="Disconnected",
            last_health_check=None,
            webhook_callback_url="https://crm.yesweigh.in/api/webhooks/evolution/{instanceId}",
            api_key_set=bool(settings.evolution_api_key),
        )
    return EvolutionConfigOut(
        server_url=integ.server_url,
        api_version=integ.api_version,
        enabled=integ.enabled,
        connection_status=integ.connection_status,
        last_health_check=integ.last_health_check,
        webhook_callback_url="https://crm.yesweigh.in/api/webhooks/evolution/{instanceId}",
        api_key_set=bool(integ.encrypted_api_key or settings.evolution_api_key),
    )


@router.post("/api/settings/evolution", response_model=EvolutionConfigOut)
async def save_evolution_config(body: EvolutionConfigIn, db: Db, user: CurrentUser):
    _require_admin(user)
    integ = await _get_integration(db)
    if not integ:
        integ = EvolutionIntegration(organization_id=ORG)
        db.add(integ)
    integ.server_url = body.server_url.strip()
    integ.api_version = body.api_version or "evolution-go"
    integ.enabled = body.enabled
    if body.api_key.strip():
        integ.encrypted_api_key = encrypt_secret(body.api_key.strip())
    integ.webhook_base_url = "https://crm.yesweigh.in/api/webhooks/evolution"
    await db.commit()
    await db.refresh(integ)
    return await get_evolution_config(db, user)


@router.post("/api/settings/evolution/test")
async def test_evolution(db: Db, user: CurrentUser):
    _require_admin(user)
    integ = await _get_integration(db)
    settings = get_settings()
    url = (integ.server_url if integ else None) or settings.evolution_base_url
    key = decrypt_secret(integ.encrypted_api_key) if integ else ""
    key = key or settings.evolution_api_key
    if not key:
        raise HTTPException(400, "API key not configured")
    result = await EvolutionService(url, key).test_connection()
    if integ:
        integ.connection_status = result["status"]
        integ.last_health_check = datetime.now(timezone.utc)
        await db.commit()
    return result


@router.get("/api/whatsapp/instances")
async def list_instances(db: Db, user: CurrentUser):
    _require_admin(user)
    rows = (
        await db.execute(
            select(WhatsAppInstance)
            .where(WhatsAppInstance.organization_id == ORG, WhatsAppInstance.deleted_at.is_(None))
            .order_by(WhatsAppInstance.id.desc())
        )
    ).scalars().all()
    items = []
    for inst in rows:
        assigns = (
            await db.execute(
                select(WhatsAppInstanceEmployee, Employee)
                .join(Employee, Employee.id == WhatsAppInstanceEmployee.employee_id)
                .where(
                    WhatsAppInstanceEmployee.whatsapp_instance_id == inst.id,
                    WhatsAppInstanceEmployee.is_active.is_(True),
                )
            )
        ).all()
        assignees = [
            {
                "employee_id": emp.id,
                "name": emp.name,
                "code": emp.code,
                "is_primary_owner": link.is_primary_owner,
            }
            for link, emp in assigns
        ]
        items.append(_instance_out(inst, assignees))

    total = len(items)
    connected = sum(1 for i in items if i["connection_status"] == "Connected")
    disconnected = sum(1 for i in items if i["connection_status"] != "Connected")
    unassigned = sum(1 for i in items if not i["assignees"])
    return {
        "items": items,
        "stats": {
            "total": total,
            "connected": connected,
            "disconnected": disconnected,
            "unassigned": unassigned,
        },
    }


@router.get("/api/whatsapp/instances/eligible-employees")
async def list_eligible_employees(db: Db, user: CurrentUser):
    """Active ADMS staff eligible for a personal WhatsApp instance."""
    _require_admin(user)
    linked_ids = await _active_instance_employee_ids(db)
    rows = (
        await db.execute(
            select(Employee).where(Employee.status.in_(tuple(_ACTIVE_EMP_STATUSES)))
        )
    ).scalars().all()

    items: list[dict[str, Any]] = []
    for emp in rows:
        code = _enrollment_code(emp)
        if not code:
            continue
        if is_excluded_employee_code(code) or is_whatsapp_excluded_employee_code(code):
            continue
        already = emp.id in linked_ids
        items.append(
            {
                "employee_id": emp.id,
                "name": emp.name,
                "employee_code": code,
                "adms_employee_code": emp.adms_employee_code or "",
                "department": emp.department or "",
                "branch": emp.branch or "",
                "photo_url": emp.photo_url or "",
                "already_linked": already,
                "selectable": not already,
            }
        )

    items.sort(
        key=lambda r: (
            0 if r["selectable"] else 1,
            staff_display_rank(r["employee_code"]),
            (r["name"] or "").lower(),
        )
    )
    return {"items": items}


@router.post("/api/whatsapp/instances")
async def create_instance(body: InstanceCreateIn, db: Db, user: CurrentUser):
    _require_admin(user)
    if not body.primary_employee_id:
        raise HTTPException(400, "Select an employee for this WhatsApp instance")

    primary = await _resolve_create_employee(db, body.primary_employee_id)
    emp_code = _enrollment_code(primary)
    display_name = (body.display_name or "").strip() or primary.name.strip()
    if len(display_name) < 2:
        raise HTTPException(400, "Employee name is required")

    integ, svc = await _svc(db)
    public_id = str(uuid.uuid4())
    suffix = secrets.token_hex(3)
    purpose_slug = "".join(c for c in body.purpose.lower().replace(" ", "_") if c.isalnum() or c == "_")[:20] or "general"
    evo_name = f"yw_{purpose_slug}_{suffix}"
    evo_token = str(uuid.uuid4())
    evo_id = str(uuid.uuid4())

    inst = WhatsAppInstance(
        public_id=public_id,
        organization_id=ORG,
        integration_id=integ.id,
        evolution_instance_id=evo_id,
        evolution_instance_name=evo_name,
        encrypted_instance_token=encrypt_secret(evo_token),
        display_name=display_name,
        employee_code=emp_code,
        employee_name=primary.name.strip(),
        expected_phone_number=body.expected_phone_number.strip(),
        department=body.department or primary.department or "",
        branch=body.branch or primary.branch or "",
        purpose=body.purpose,
        description=body.description,
        integration_type=body.integration_type,
        connection_status="Connecting",
        registration_status="pending",
        created_by_user_id=user.id,
    )
    db.add(inst)
    await db.flush()

    try:
        remote = await svc.create_instance(name=evo_name, token=evo_token, instance_id=evo_id)
        remote_data = remote.get("data") if isinstance(remote, dict) else None
        if isinstance(remote_data, dict):
            if remote_data.get("id"):
                inst.evolution_instance_id = str(remote_data["id"])
            if remote_data.get("token"):
                inst.encrypted_instance_token = encrypt_secret(str(remote_data["token"]))
                evo_token = str(remote_data["token"])
        webhook = _webhook_callback(public_id)
        try:
            await svc.connect(evo_token, webhook)
        except Exception:  # noqa: BLE001
            pass
        inst.registration_status = "registered"
        inst.connection_status = "QR Required"
        await _log(db, inst.id, "instance_created", f"Created {evo_name} for {primary.name} ({emp_code})")
    except Exception as exc:  # noqa: BLE001
        inst.registration_status = "remote_failed"
        inst.connection_status = "Error"
        await _log(db, inst.id, "instance_create_failed", str(exc))
        await db.commit()
        raise HTTPException(502, f"Evolution create failed: {exc}") from exc

    db.add(
        WhatsAppInstanceEmployee(
            organization_id=ORG,
            whatsapp_instance_id=inst.id,
            employee_id=primary.id,
            is_primary_owner=True,
            permissions=json.dumps(["view", "send", "receive"]),
            assigned_by=user.id,
        )
    )
    for eid in body.additional_employee_ids:
        if eid == primary.id:
            continue
        emp = await db.get(Employee, eid)
        if emp and emp.status in _ACTIVE_EMP_STATUSES:
            db.add(
                WhatsAppInstanceEmployee(
                    organization_id=ORG,
                    whatsapp_instance_id=inst.id,
                    employee_id=eid,
                    is_primary_owner=False,
                    permissions=json.dumps(["view", "send", "receive"]),
                    assigned_by=user.id,
                )
            )

    await db.commit()
    await db.refresh(inst)
    return _instance_out(
        inst,
        [
            {
                "employee_id": primary.id,
                "name": primary.name,
                "code": emp_code,
                "is_primary_owner": True,
            }
        ],
    )


@router.get("/api/whatsapp/instances/{public_id}")
async def get_instance(public_id: str, db: Db, user: CurrentUser):
    _require_admin(user)
    inst = await _owned_instance(db, public_id)
    return _instance_out(inst)


@router.patch("/api/whatsapp/instances/{public_id}")
async def update_instance(public_id: str, body: InstanceUpdateIn, db: Db, user: CurrentUser):
    _require_admin(user)
    inst = await _owned_instance(db, public_id)
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(inst, k, v)
    await _log(db, inst.id, "instance_updated", json.dumps(data))
    await db.commit()
    await db.refresh(inst)
    return _instance_out(inst)


@router.post("/api/whatsapp/instances/{public_id}/connect")
async def connect_instance(public_id: str, db: Db, user: CurrentUser):
    _require_admin(user)
    inst = await _owned_instance(db, public_id)
    if (inst.provider or "").strip().lower() == "meta_cloud":
        raise HTTPException(400, "Care Cloud WhatsApp does not use Evolution QR connect")
    _, svc = await _svc(db)
    token = decrypt_secret(inst.encrypted_instance_token)
    await svc.connect(token, _webhook_callback(public_id))
    inst.connection_status = "QR Required"
    await _log(db, inst.id, "connect_requested")
    await db.commit()
    return {"ok": True, "status": inst.connection_status}


@router.get("/api/whatsapp/instances/{public_id}/qrcode")
async def instance_qr(public_id: str, db: Db, user: CurrentUser):
    _require_admin(user)
    inst = await _owned_instance(db, public_id)
    if (inst.provider or "").strip().lower() == "meta_cloud":
        raise HTTPException(400, "Care Cloud WhatsApp has no QR code")
    _, svc = await _svc(db)
    token = decrypt_secret(inst.encrypted_instance_token)
    if not token:
        raise HTTPException(400, "Instance token missing")
    try:
        # Only skip QR when WhatsApp is actually paired (LoggedIn), not socket Connected.
        try:
            remote = await svc.status(token)
            payload = remote.get("data") if isinstance(remote, dict) else remote
            if _evolution_logged_in(payload):
                was_connected = (inst.connection_status or "").lower() == "connected"
                inst.connection_status = "Connected"
                inst.last_connected_at = datetime.now(timezone.utc)
                phone = _evolution_phone(payload)
                if phone:
                    inst.connected_phone_number = phone
                if isinstance(payload, dict):
                    profile_name = str(payload.get("Name") or payload.get("name") or "")
                    if profile_name:
                        inst.profile_name = profile_name
                if not inst.connected_phone_number:
                    try:
                        info = await svc.info(inst.evolution_instance_id, token)
                        info_data = info.get("data") if isinstance(info, dict) else info
                        phone = _evolution_phone(info_data)
                        if phone:
                            inst.connected_phone_number = phone
                    except Exception:  # noqa: BLE001
                        pass
                await db.commit()
                if not was_connected:
                    _maybe_schedule_history_sync(inst, trigger="qr_logged_in")
                return {
                    "instance_id": public_id,
                    "display_name": inst.display_name,
                    "connected": True,
                    "connection_status": "Connected",
                    "phone": inst.connected_phone_number or inst.profile_name,
                    "qr": None,
                }
        except Exception:  # noqa: BLE001
            pass

        # Ensure session is connecting so Evolution can mint a QR
        try:
            await svc.connect(token, _webhook_callback(public_id))
        except Exception:  # noqa: BLE001
            pass
        data = await svc.qr(token, retries=15, delay_s=1.0)
        # Normalize to a single browser-usable image string for the UI
        code = ""
        if isinstance(data, dict):
            code = str(data.get("qrcode") or "")
            if not code and isinstance(data.get("raw"), dict):
                raw = data["raw"]
                inner = raw.get("data") if isinstance(raw.get("data"), dict) else raw
                if isinstance(inner, dict):
                    code = str(inner.get("qrcode") or inner.get("base64") or inner.get("code") or "")
        inst.connection_status = "QR Required"
        await db.commit()
        return {
            "instance_id": public_id,
            "display_name": inst.display_name,
            "connected": False,
            "connection_status": "QR Required",
            "qr": data,
            "qrcode": code,
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(409, str(exc)) from exc


class PairingCodeBody(BaseModel):
    phone: str = Field(..., min_length=8, max_length=32)


@router.post("/api/whatsapp/instances/{public_id}/pairing-code")
async def instance_pairing_code(public_id: str, body: PairingCodeBody, db: Db, user: CurrentUser):
    """Mint a WhatsApp pairing code (link with phone number) via Evolution GO."""
    _require_admin(user)
    inst = await _owned_instance(db, public_id)
    if (inst.provider or "").strip().lower() == "meta_cloud":
        raise HTTPException(400, "Care Cloud WhatsApp does not use pairing codes")
    _, svc = await _svc(db)
    token = decrypt_secret(inst.encrypted_instance_token)
    if not token:
        raise HTTPException(400, "Instance token missing")
    try:
        remote = await svc.status(token)
        payload = remote.get("data") if isinstance(remote, dict) else remote
        if _evolution_logged_in(payload):
            was_connected = (inst.connection_status or "").lower() == "connected"
            inst.connection_status = "Connected"
            inst.last_connected_at = datetime.now(timezone.utc)
            phone = _evolution_phone(payload)
            if phone:
                inst.connected_phone_number = phone
            await db.commit()
            if not was_connected:
                _maybe_schedule_history_sync(inst, trigger="pair_already_logged_in")
            return {
                "instance_id": public_id,
                "display_name": inst.display_name,
                "connected": True,
                "connection_status": "Connected",
                "phone": inst.connected_phone_number or inst.profile_name,
                "pairing_code": None,
            }
    except Exception:  # noqa: BLE001
        pass

    # Evolution GO manager: connect first, brief wait, then /instance/pair
    try:
        await svc.connect(token, _webhook_callback(public_id))
    except Exception:  # noqa: BLE001
        pass
    await asyncio.sleep(1.0)
    try:
        result = await svc.pair(token, body.phone)
        inst.connection_status = "QR Required"
        if not (inst.expected_phone_number or "").strip():
            digits = "".join(c for c in body.phone if c.isdigit())
            if digits:
                inst.expected_phone_number = digits
        await _log(db, inst.id, "pairing_code_requested", result.get("phone") or body.phone)
        await db.commit()
        return {
            "instance_id": public_id,
            "display_name": inst.display_name,
            "connected": False,
            "connection_status": "QR Required",
            "phone": result.get("phone") or body.phone,
            "pairing_code": result.get("pairing_code"),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(409, str(exc)) from exc


@router.get("/api/whatsapp/instances/{public_id}/status")
async def instance_status(public_id: str, db: Db, user: CurrentUser):
    _require_admin(user)
    inst = await _owned_instance(db, public_id)
    _, svc = await _svc(db)
    token = decrypt_secret(inst.encrypted_instance_token)
    try:
        remote = await svc.status(token)
        payload = remote.get("data") if isinstance(remote, dict) else remote
        logged_in = _evolution_logged_in(payload)
        phone = _evolution_phone(payload)
        if isinstance(payload, dict):
            profile_name = str(payload.get("Name") or payload.get("name") or "")
            if profile_name:
                inst.profile_name = profile_name
        # Fallback: instance info when status shape is incomplete
        if not logged_in:
            try:
                info = await svc.info(inst.evolution_instance_id, token)
                info_data = info.get("data") if isinstance(info, dict) else info
                logged_in = _evolution_logged_in(info_data)
                if not phone:
                    phone = _evolution_phone(info_data)
            except Exception:  # noqa: BLE001
                pass
        if logged_in:
            was_connected = (inst.connection_status or "").lower() == "connected"
            inst.connection_status = "Connected"
            inst.last_connected_at = datetime.now(timezone.utc)
            if phone:
                inst.connected_phone_number = phone
            await db.commit()
            if not was_connected:
                _maybe_schedule_history_sync(inst, trigger="status_logged_in")
        elif inst.connection_status not in {"Error", "Logged Out"}:
            inst.connection_status = "QR Required"
            await db.commit()
        else:
            await db.commit()
        return {"instance_id": public_id, "connection_status": inst.connection_status, "remote": remote}
    except Exception as exc:  # noqa: BLE001
        inst.connection_status = "Error"
        await db.commit()
        return {"instance_id": public_id, "connection_status": "Error", "error": str(exc)}


@router.post("/api/whatsapp/instances/{public_id}/restart")
async def restart_instance(public_id: str, db: Db, user: CurrentUser):
    _require_admin(user)
    inst = await _owned_instance(db, public_id)
    _, svc = await _svc(db)
    token = decrypt_secret(inst.encrypted_instance_token)
    await svc.restart(inst.evolution_instance_id, token)
    await _log(db, inst.id, "restarted")
    await db.commit()
    return {"ok": True}


@router.post("/api/whatsapp/instances/{public_id}/sync-history")
async def sync_instance_history(public_id: str, db: Db, user: CurrentUser):
    """Manually trigger Evolution contacts + on-demand history sync for one instance."""
    _require_admin(user)
    inst = await _owned_instance(db, public_id)
    if (inst.connection_status or "").lower() != "connected":
        # Still allow attempt — contacts/history need LoggedIn; surface soft warning
        pass
    _maybe_schedule_history_sync(inst, trigger="manual", force=True)
    await _log(db, inst.id, "history_sync_requested", "manual")
    await db.commit()
    return {
        "ok": True,
        "instance_id": public_id,
        "history_sync_status": "running",
        "note": "Sync started in background. Messages arrive via webhooks as WhatsApp delivers history.",
    }


@router.post("/api/whatsapp/instances/{public_id}/disconnect")
async def disconnect_instance(public_id: str, db: Db, user: CurrentUser):
    _require_admin(user)
    inst = await _owned_instance(db, public_id)
    _, svc = await _svc(db)
    token = decrypt_secret(inst.encrypted_instance_token)
    await svc.disconnect(token)
    inst.connection_status = "Disconnected"
    await _log(db, inst.id, "disconnected")
    await db.commit()
    return {"ok": True}


class DeleteInstanceBody(BaseModel):
    password: str = ""


@router.delete("/api/whatsapp/instances/{public_id}")
async def delete_instance(
    public_id: str,
    db: Db,
    user: CurrentUser,
    body: Annotated[DeleteInstanceBody | None, Body()] = None,
    x_whatsapp_delete_password: Annotated[str | None, Header()] = None,
):
    _require_admin(user)
    expected = get_settings().whatsapp_instance_delete_password or ""
    provided = (body.password if body else "") or (x_whatsapp_delete_password or "")
    if not _delete_password_ok(provided, expected):
        raise HTTPException(403, "Invalid delete password")
    inst = await _owned_instance(db, public_id)
    _, svc = await _svc(db)
    try:
        await svc.delete(inst.evolution_instance_id)
    except Exception as exc:  # noqa: BLE001
        # still soft-delete locally if remote missing
        await _log(db, inst.id, "remote_delete_failed", str(exc))
    inst.deleted_at = datetime.now(timezone.utc)
    inst.connection_status = "Logged Out"
    await _log(db, inst.id, "deleted")
    await db.commit()
    return {"ok": True}


@router.get("/api/whatsapp/instances/{public_id}/employees")
async def list_assignees(public_id: str, db: Db, user: CurrentUser):
    _require_admin(user)
    inst = await _owned_instance(db, public_id)
    rows = (
        await db.execute(
            select(WhatsAppInstanceEmployee, Employee)
            .join(Employee, Employee.id == WhatsAppInstanceEmployee.employee_id)
            .where(
                WhatsAppInstanceEmployee.whatsapp_instance_id == inst.id,
                WhatsAppInstanceEmployee.is_active.is_(True),
            )
        )
    ).all()
    return [
        {
            "employee_id": emp.id,
            "name": emp.name,
            "code": emp.code,
            "department": emp.department,
            "designation": emp.designation,
            "is_primary_owner": link.is_primary_owner,
            "permissions": json.loads(link.permissions or "[]"),
        }
        for link, emp in rows
    ]


@router.post("/api/whatsapp/instances/{public_id}/employees")
async def assign_employee(public_id: str, body: AssignIn, db: Db, user: CurrentUser):
    _require_admin(user)
    inst = await _owned_instance(db, public_id)
    emp = await db.get(Employee, body.employee_id)
    if not emp:
        raise HTTPException(404, "Employee not found")
    existing = (
        await db.execute(
            select(WhatsAppInstanceEmployee).where(
                WhatsAppInstanceEmployee.whatsapp_instance_id == inst.id,
                WhatsAppInstanceEmployee.employee_id == body.employee_id,
                WhatsAppInstanceEmployee.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.is_primary_owner = body.is_primary_owner
        existing.permissions = json.dumps(body.permissions)
    else:
        db.add(
            WhatsAppInstanceEmployee(
                organization_id=ORG,
                whatsapp_instance_id=inst.id,
                employee_id=body.employee_id,
                is_primary_owner=body.is_primary_owner,
                permissions=json.dumps(body.permissions),
                assigned_by=user.id,
            )
        )
    await _log(db, inst.id, "employee_assigned", f"employee={body.employee_id}")
    await db.commit()
    return {"ok": True}


@router.delete("/api/whatsapp/instances/{public_id}/employees/{employee_id}")
async def revoke_employee(public_id: str, employee_id: int, db: Db, user: CurrentUser):
    _require_admin(user)
    inst = await _owned_instance(db, public_id)
    link = (
        await db.execute(
            select(WhatsAppInstanceEmployee).where(
                WhatsAppInstanceEmployee.whatsapp_instance_id == inst.id,
                WhatsAppInstanceEmployee.employee_id == employee_id,
                WhatsAppInstanceEmployee.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if not link:
        raise HTTPException(404, "Assignment not found")
    link.is_active = False
    link.revoked_at = datetime.now(timezone.utc)
    await _log(db, inst.id, "employee_revoked", f"employee={employee_id}")
    await db.commit()
    return {"ok": True}


@router.post("/api/webhooks/evolution/{public_id}")
async def evolution_webhook(
    public_id: str,
    request: Request,
    db: Db,
    x_api_key: Annotated[str | None, Header()] = None,
    apikey: Annotated[str | None, Header()] = None,
):
    inst = (
        await db.execute(
            select(WhatsAppInstance).where(
                WhatsAppInstance.public_id == public_id,
                WhatsAppInstance.organization_id == ORG,
                WhatsAppInstance.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not inst:
        raise HTTPException(404, "Unknown instance")

    # Prefer instance token match; also accept org integration key
    provided = x_api_key or apikey or ""
    expected = decrypt_secret(inst.encrypted_instance_token)
    integ = await _get_integration(db)
    org_key = decrypt_secret(integ.encrypted_api_key) if integ else ""
    org_key = org_key or get_settings().evolution_api_key
    if provided and provided not in {expected, org_key}:
        raise HTTPException(401, "Unauthorized webhook")

    body = await request.body()
    try:
        payload = json.loads(body.decode() or "{}")
    except json.JSONDecodeError:
        payload = {"raw": body.decode(errors="ignore")}

    event_type = str(payload.get("event") or payload.get("type") or "unknown")
    external_id = str(payload.get("id") or payload.get("eventId") or "")
    if external_id:
        exists = (
            await db.execute(
                select(func.count())
                .select_from(EvolutionWebhookEvent)
                .where(EvolutionWebhookEvent.external_event_id == external_id)
            )
        ).scalar() or 0
        if exists:
            return {"ok": True, "duplicate": True}

    # Store a sanitized copy (strip huge base64) so event log stays usable
    db.add(
        EvolutionWebhookEvent(
            organization_id=ORG,
            whatsapp_instance_id=inst.id,
            external_event_id=external_id,
            event_type=event_type,
            processing_status="received",
            payload=json.dumps(_sanitize_webhook_payload(payload))[:50000],
        )
    )
    lower = event_type.lower()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    logged_in = _evolution_logged_in(data)
    if isinstance(data, dict):
        phone = _evolution_phone(data)
        if phone:
            inst.connected_phone_number = phone
    # Pairing confirmed: LoggedIn / open with device — not socket Connected alone
    became_connected = False
    if logged_in or ("connection" in lower and "open" in lower and bool(_evolution_phone(data))):
        was_connected = (inst.connection_status or "").lower() == "connected"
        inst.connection_status = "Connected"
        inst.last_connected_at = datetime.now(timezone.utc)
        became_connected = not was_connected
    if "qr" in lower:
        inst.connection_status = "QR Required"
    if "disconnect" in lower or "logout" in lower:
        inst.connection_status = "Disconnected"

    # Persist inbound / outbound chat messages into the CRM inbox
    # Skip PushName-only noise; Message / SendMessage carry the body.
    intel_conv_id: int | None = None
    post_commit_ai: list[tuple[str, int]] = []
    if event_type in {"Message", "SendMessage"} or (
        any(x in lower for x in ("message", "messages", "send_message", "receive"))
        and "receipt" not in lower
        and "pushname" not in lower
    ):
        try:
            intel_conv_id, post_commit_ai = await _ingest_webhook_message(
                db, inst, payload, event_type
            )
        except Exception as exc:  # noqa: BLE001
            print(f"webhook ingest error: {exc}")

    # Delivery / read receipts → update ticks
    if any(x in lower for x in ("receipt", "read", "ack", "status")):
        try:
            await _apply_message_receipt(db, inst, payload, event_type)
        except Exception:  # noqa: BLE001
            pass

    await db.commit()
    # Schedule STT/translate only AFTER commit — otherwise the background task
    # often hits not-found and leaves voice bubbles with an empty Transcribe link.
    if post_commit_ai:
        try:
            from app.sarvam_transcribe import schedule_text_translation, schedule_transcription

            for kind, mid in post_commit_ai:
                if kind == "stt":
                    schedule_transcription(int(mid))
                elif kind == "translate":
                    schedule_text_translation(int(mid))
        except Exception as exc:  # noqa: BLE001
            print(f"post-commit AI schedule skipped: {exc}")
    if intel_conv_id:
        try:
            from app.intel.jobs import schedule_thread_intel

            schedule_thread_intel(int(intel_conv_id))
        except Exception as exc:  # noqa: BLE001
            print(f"intel enqueue skipped: {exc}")
    try:
        from app.whatsapp_new_messenger import schedule_new_messenger

        for kind, mid in post_commit_ai:
            if kind == "new_messenger":
                schedule_new_messenger(message_id=int(mid))
    except Exception as exc:  # noqa: BLE001
        print(f"new_messenger schedule skipped: {exc}")
    if became_connected:
        _maybe_schedule_history_sync(inst, trigger="webhook_logged_in")
    return {"ok": True}


async def _apply_message_receipt(
    db: AsyncSession, inst: WhatsAppInstance, payload: dict[str, Any], event_type: str
) -> None:
    """Apply Evolution GO Receipt (READ_RECEIPT) → gray/blue ticks on outbound messages."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return

    ids: list[str] = []
    # Evolution GO: data.MessageIDs + root state (Read / ReadSelf / Delivered)
    for key in ("MessageIDs", "messageIDs", "messageIds", "ids"):
        val = data.get(key)
        if isinstance(val, list):
            ids.extend(str(x) for x in val if x)
        elif val:
            ids.append(str(val))
    for key in ("id", "messageId", "msgId", "ID"):
        val = data.get(key)
        if isinstance(val, list):
            ids.extend(str(x) for x in val if x)
        elif val:
            ids.append(str(val))
    key = data.get("key") if isinstance(data.get("key"), dict) else {}
    if key.get("id"):
        ids.append(str(key["id"]))
    info = data.get("Info") if isinstance(data.get("Info"), dict) else {}
    if info.get("ID"):
        ids.append(str(info["ID"]))
    ids = list(dict.fromkeys(ids))
    if not ids:
        return

    state = str(payload.get("state") or data.get("state") or "").lower()
    status_raw = str(data.get("Type") or data.get("type") or data.get("status") or data.get("receiptType") or "").lower()
    lower = f"{event_type} {state} {status_raw}".lower()

    new_status = "delivered"
    if any(x in lower for x in ("read", "played", "blue")):
        new_status = "read"
    elif any(x in lower for x in ("deliver", "server", "device", "sender")):
        new_status = "delivered"

    for mid in ids:
        msg = (
            await db.execute(
                select(WhatsAppMessage).where(
                    WhatsAppMessage.whatsapp_instance_id == inst.id,
                    or_(
                        WhatsAppMessage.external_message_id == mid,
                        WhatsAppMessage.client_message_id == mid,
                    ),
                )
            )
        ).scalar_one_or_none()
        if not msg or msg.direction != "out":
            continue
        order = {"sending": 0, "sent": 1, "delivered": 2, "read": 3, "failed": -1}
        if order.get(new_status, 0) >= order.get(msg.status, 0):
            msg.status = new_status


def _sanitize_webhook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop embedded base64 so Postgres event rows stay small; media is saved to disk separately."""
    try:
        raw = json.dumps(payload)
        clone = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"event": payload.get("event"), "note": "payload_omitted"}
    data = clone.get("data")
    if isinstance(data, dict):
        msg = data.get("Message")
        if isinstance(msg, dict) and isinstance(msg.get("base64"), str) and len(msg["base64"]) > 200:
            msg["base64"] = f"[omitted:{len(msg['base64'])}chars]"
        if isinstance(msg, dict) and isinstance(msg.get("mediaUrl"), str) and msg["mediaUrl"].startswith("data:"):
            msg["mediaUrl"] = "[omitted-data-url]"
    return clone


def _message_node(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None
    msg = data.get("Message") if isinstance(data.get("Message"), dict) else None
    if msg is None and isinstance(data.get("message"), dict):
        inner = data["message"]
        msg = inner.get("message") if isinstance(inner.get("message"), dict) else inner
    if not isinstance(msg, dict):
        return None
    # Helpers are defined later in this module; resolve at call time.
    unwrapped = _unwrap_message_body(msg) or msg
    if unwrapped is msg:
        return msg
    # Keep Evolution GO download helpers that sit on the outer envelope.
    merged = dict(unwrapped)
    for k in ("base64", "Base64", "mediaUrl", "MediaUrl"):
        if k in msg and k not in merged:
            merged[k] = msg[k]
    return merged


def _detect_media(msg: dict[str, Any]) -> tuple[str, dict[str, Any], str, str]:
    """Return (media_type, media_node, filename, mimetype)."""
    msg = _unwrap_message_body(msg) if isinstance(msg, dict) else msg
    if not isinstance(msg, dict):
        return "", {}, "", ""

    # documentWithCaptionMessage nests the real documentMessage
    nested_doc = msg.get("documentWithCaptionMessage")
    if isinstance(nested_doc, dict):
        inner = nested_doc.get("message") if isinstance(nested_doc.get("message"), dict) else nested_doc
        if isinstance(inner, dict) and isinstance(inner.get("documentMessage"), dict):
            msg = {**msg, "documentMessage": inner["documentMessage"]}

    # Hydrated template header media (image/video/document title)
    tm = msg.get("templateMessage")
    if isinstance(tm, dict):
        ht = tm.get("hydratedTemplate") or tm.get("hydratedFourRowTemplate") or {}
        title = ht.get("Title") if isinstance(ht, dict) else None
        if isinstance(title, dict):
            msg = {**msg, **{k: title[k] for k in ("imageMessage", "videoMessage", "documentMessage") if k in title}}

    mapping = [
        ("image", "imageMessage", "photo.jpg"),
        ("video", "videoMessage", "video.mp4"),
        ("audio", "audioMessage", "voice.ogg"),
        ("document", "documentMessage", "file"),
        ("sticker", "stickerMessage", "sticker.webp"),
    ]
    for mtype, key, default_name in mapping:
        node = msg.get(key)
        if isinstance(node, dict):
            mime = str(node.get("mimetype") or node.get("Mimetype") or "")
            name = str(node.get("fileName") or node.get("filename") or default_name)
            if mtype == "document" and name == "file" and mime:
                ext = mime.split("/")[-1].split(";")[0]
                name = f"file.{ext}" if ext else name
            return mtype, node, name, mime
    return "", {}, "", ""


def _normalize_media_type(raw: str) -> str:
    """Map Evolution MediaType / aliases onto CRM media_type values."""
    mt = (raw or "").strip().lower()
    if mt in {"ptt", "voice", "audiomessage", "pttmessage"}:
        return "audio"
    if mt in {"gif", "ptv", "gifmessage"}:
        return "video"
    if mt in {"image", "video", "audio", "document", "sticker"}:
        return mt
    if mt in {"imagemessage", "videomessage", "documentmessage", "stickermessage"}:
        return mt.replace("message", "")
    return ""


def _outer_message_node(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Raw Evolution Message envelope (before unwrap) — needed for downloadmedia."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None
    msg = data.get("Message") if isinstance(data.get("Message"), dict) else None
    if msg is None and isinstance(data.get("message"), dict):
        inner = data["message"]
        msg = inner.get("message") if isinstance(inner.get("message"), dict) else inner
    return msg if isinstance(msg, dict) else None


def _thumbnail_b64(node: dict[str, Any] | None) -> str:
    if not isinstance(node, dict):
        return ""
    for key in ("JPEGThumbnail", "jpegThumbnail", "thumbnail", "Thumbnail"):
        val = node.get(key)
        if isinstance(val, str) and len(val) > 80 and not val.startswith("[omitted"):
            return val
    return ""


async def _persist_message_media(
    svc: EvolutionService,
    token: str,
    payload: dict[str, Any],
) -> dict[str, str]:
    """
    Download inbound or outbound media to VPS disk.
    Prefers webhook base64/mediaUrl; falls back to Evolution /message/downloadmedia;
    then JPEGThumbnail so channel/GIF replies still render a preview.
    """
    import asyncio

    msg = _message_node(payload)
    if not msg:
        return {}
    media_type, node, filename, mime = _detect_media(msg)
    if not media_type:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        info = data.get("Info") if isinstance(data.get("Info"), dict) else {}
        media_type = _normalize_media_type(str(info.get("MediaType") or ""))
        if not media_type and str(info.get("Type") or "").lower() == "media":
            if isinstance(msg.get("videoMessage"), dict):
                media_type = "video"
            elif isinstance(msg.get("imageMessage"), dict):
                media_type = "image"
            else:
                media_type = "document"
        if not media_type:
            return {}
        filename = {
            "image": "photo.jpg",
            "video": "video.mp4",
            "audio": "voice.ogg",
            "document": "file",
            "sticker": "sticker.webp",
        }.get(media_type, "file")

    if not isinstance(node, dict) or not node:
        for key in ("imageMessage", "videoMessage", "audioMessage", "documentMessage", "stickerMessage"):
            if isinstance(msg.get(key), dict):
                node = msg[key]
                break

    if isinstance(node, dict) and (
        node.get("gifPlayback")
        or str(node.get("mimetype") or node.get("Mimetype") or "").lower().startswith("video/")
    ):
        if media_type in {"", "document"}:
            media_type = "video"
            if not filename or filename == "file":
                filename = "video.mp4"

    b64 = msg.get("base64") or msg.get("Base64") or ""
    media_url = msg.get("mediaUrl") or msg.get("MediaUrl") or ""
    public = ""
    try:
        if isinstance(b64, str) and len(b64) > 80 and not b64.startswith("[omitted"):
            if b64.startswith("data:"):
                public, _, _ = save_data_url(b64, media_type=media_type or "document", filename=filename)
            else:
                prefix = f"data:{mime or 'application/octet-stream'};base64,"
                public, _, _ = save_data_url(prefix + b64, media_type=media_type or "document", filename=filename)
        elif isinstance(media_url, str) and media_url.startswith(("http://", "https://")):
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(media_url)
                r.raise_for_status()
                public, _ = save_bytes(r.content, media_type=media_type or "document", filename=filename)
        elif isinstance(media_url, str) and media_url.startswith("data:"):
            public, _, _ = save_data_url(media_url, media_type=media_type or "document", filename=filename)
        else:
            has_decrypt_key = bool(
                isinstance(node, dict)
                and (
                    node.get("mediaKey")
                    or node.get("MediaKey")
                    or node.get("fileEncSHA256")
                    or node.get("FileEncSHA256")
                )
            )
            thumb_early = _thumbnail_b64(node) or _thumbnail_b64(msg)
            # Channel GIF/replies often omit mediaKey; Evolution downloadmedia can hang.
            # Prefer thumbnail immediately when we already have one and no decrypt key.
            if has_decrypt_key or not thumb_early:
                outer = _outer_message_node(payload) or msg
                media_key = {
                    "image": "imageMessage",
                    "video": "videoMessage",
                    "audio": "audioMessage",
                    "document": "documentMessage",
                    "sticker": "stickerMessage",
                }.get(media_type or "")
                minimal = {media_key: node} if media_key and isinstance(node, dict) and node else None
                candidates: list[dict[str, Any]] = []
                for cand in (outer, msg, minimal):
                    if not isinstance(cand, dict):
                        continue
                    download_msg = {k: v for k, v in cand.items() if k.lower() != "base64"}
                    if isinstance(download_msg.get("base64"), str) and str(download_msg.get("base64")).startswith(
                        "[omitted"
                    ):
                        download_msg.pop("base64", None)
                    if download_msg:
                        candidates.append(download_msg)
                for download_msg in candidates:
                    try:
                        remote = await asyncio.wait_for(svc.download_media(token, download_msg), timeout=20.0)
                    except Exception as exc:  # noqa: BLE001
                        print(f"media download attempt failed: {exc}")
                        continue
                    data = remote.get("data") if isinstance(remote.get("data"), dict) else remote
                    raw = ""
                    if isinstance(data, dict):
                        raw = str(data.get("base64") or data.get("data") or "")
                    elif isinstance(data, str):
                        raw = data
                    if not raw and isinstance(remote.get("base64"), str):
                        raw = remote["base64"]
                    if raw and not raw.startswith("[omitted") and len(raw) > 80:
                        if not raw.startswith("data:"):
                            raw = f"data:{mime or 'application/octet-stream'};base64,{raw}"
                        public, _, _ = save_data_url(raw, media_type=media_type or "document", filename=filename)
                        break
    except Exception as exc:  # noqa: BLE001
        print(f"media persist failed: {exc}")
        public = ""

    # Always attempt thumbnail so inbox never keeps empty Photo/Attachment/file bubbles.
    if not public:
        thumb = _thumbnail_b64(node) or _thumbnail_b64(msg)
        if thumb:
            try:
                thumb_name = (
                    filename
                    if (filename or "").lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                    else "preview.jpg"
                )
                public, _, _ = save_data_url(
                    thumb if thumb.startswith("data:") else f"data:image/jpeg;base64,{thumb}",
                    media_type="image",
                    filename=thumb_name,
                )
                media_type = "image"
                filename = thumb_name
            except Exception as exc:  # noqa: BLE001
                print(f"thumbnail persist failed: {exc}")

    return {
        "media_type": media_type,
        "media_name": filename,
        "media_url": public,
    }



# Back-compat for older scripts
_persist_inbound_media = _persist_message_media


def _digits(value: str) -> str:
    return "".join(c for c in (value or "") if c.isdigit())


def _jid_phone(jid: str) -> str:
    """Extract phone digits from PN jid; empty for LID-only jids."""
    raw = (jid or "").strip()
    if not raw:
        return ""
    if raw.endswith("@lid") or "@lid" in raw:
        return ""
    user = raw.split("@")[0].split(":")[0]
    return _digits(user)


def _jid_lid(jid: str) -> str:
    """Extract LID digits from an @lid jid; empty otherwise."""
    raw = (jid or "").strip()
    if not raw or "@lid" not in raw:
        return ""
    return _digits(raw.split("@")[0].split(":")[0])


def _is_lid_phone(phone: str, *, lid: str = "") -> bool:
    """Heuristic: WhatsApp LIDs are long numeric ids, not E.164 mobiles.

    Observed LIDs are often 14–15+ digits; our CRM phones are typically 10–13
    (IN 91… = 12). When ``lid`` is known, matching digits are always LID.
    """
    d = _digits(phone)
    if not d:
        return False
    lid_d = _digits(lid)
    if lid_d and d == lid_d:
        return True
    return len(d) >= 14


def _remember_lid(conv: WhatsAppConversation, lid: str) -> None:
    lid = _digits(lid)
    if not lid:
        return
    if not (conv.customer_lid or "").strip():
        conv.customer_lid = lid


def _lid_mention_digits(text: str) -> list[str]:
    """Long numeric @mentions are WhatsApp LIDs (not E.164 mobiles)."""
    import re

    return [m for m in re.findall(r"@(\d{14,})", text or "")]


async def humanize_lid_mentions(
    db: AsyncSession,
    *,
    instance_id: int,
    text: str,
) -> str:
    """Replace ``@<LID>`` mention tokens with ``@Name`` when CRM knows the contact.

    Prefer same-instance matches, then fall back to any org conversation that
    already resolved that LID (mentions often reference people on other lines).
    """
    lids = _lid_mention_digits(text)
    if not lids or not (text or "").strip():
        return text or ""
    lid_filter = or_(
        WhatsAppConversation.customer_lid.in_(lids),
        WhatsAppConversation.customer_phone.in_(lids),
        WhatsAppConversation.customer_jid.in_([f"{x}@lid" for x in lids]),
    )
    rows = (
        await db.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.organization_id == ORG,
                lid_filter,
            )
        )
    ).scalars().all()
    # Prefer names from the same WhatsApp instance when both exist.
    name_by_lid: dict[str, str] = {}
    same_inst: dict[str, str] = {}
    for c in rows:
        name = (c.customer_name or "").strip()
        if not name or name == (c.customer_phone or "").strip():
            continue
        if name.startswith("@") and _digits(name) == name.lstrip("@"):
            continue
        keys = {
            _digits(c.customer_lid or ""),
            _digits(c.customer_phone or "") if _is_lid_phone(c.customer_phone or "") else "",
            _jid_lid(c.customer_jid or ""),
        }
        for key in keys:
            if not key or key not in lids:
                continue
            if c.whatsapp_instance_id == instance_id:
                same_inst.setdefault(key, name)
            else:
                name_by_lid.setdefault(key, name)
    name_by_lid.update(same_inst)
    out = text
    for lid, name in name_by_lid.items():
        out = out.replace(f"@{lid}", f"@{name}")
    return out


def _prefer_canonical_conversation(rows: list[WhatsAppConversation]) -> WhatsAppConversation | None:
    """Prefer PN (@s.whatsapp.net) over orphan @lid shells when several rows match."""
    if not rows:
        return None

    def score(c: WhatsAppConversation) -> tuple:
        jid = (c.customer_jid or "").strip()
        phone = _digits(c.customer_phone)
        pn = 0 if jid.endswith("@s.whatsapp.net") or (phone and not _is_lid_phone(phone)) else 1
        ts = c.last_message_at.timestamp() if c.last_message_at else 0.0
        return (pn, -ts, c.id)

    return sorted(rows, key=score)[0]


async def merge_conversation_into(
    db: AsyncSession,
    *,
    keep: WhatsAppConversation,
    absorb: WhatsAppConversation,
) -> None:
    """Move messages from absorb → keep, learn LID, delete absorb. Same-instance only."""
    if keep.id == absorb.id:
        return
    if keep.whatsapp_instance_id != absorb.whatsapp_instance_id:
        return

    keep_ext = {
        m
        for m in (
            await db.execute(
                select(WhatsAppMessage.external_message_id).where(
                    WhatsAppMessage.conversation_id == keep.id,
                    WhatsAppMessage.external_message_id != "",
                )
            )
        ).scalars().all()
        if m
    }
    absorb_msgs = (
        await db.execute(select(WhatsAppMessage).where(WhatsAppMessage.conversation_id == absorb.id))
    ).scalars().all()
    for m in absorb_msgs:
        ext = (m.external_message_id or "").strip()
        if ext and ext in keep_ext:
            await db.delete(m)
            continue
        m.conversation_id = keep.id
        if ext:
            keep_ext.add(ext)

    absorb_lid = (absorb.customer_lid or "").strip() or _jid_lid(absorb.customer_jid or "")
    if absorb_lid:
        _remember_lid(keep, absorb_lid)
    if _is_lid_phone(absorb.customer_phone or ""):
        _remember_lid(keep, absorb.customer_phone)

    # Keep PN identity on canonical row
    keep_phone = _digits(keep.customer_phone)
    absorb_phone = _digits(absorb.customer_phone)
    if absorb_phone and not _is_lid_phone(absorb_phone):
        if not keep_phone or _is_lid_phone(keep_phone):
            keep.customer_phone = absorb_phone
    keep_jid = (keep.customer_jid or "").strip()
    absorb_jid = (absorb.customer_jid or "").strip()
    if absorb_jid.endswith("@s.whatsapp.net") and not keep_jid.endswith("@s.whatsapp.net"):
        keep.customer_jid = absorb_jid
    if absorb.customer_name and (not keep.customer_name or keep.customer_name == keep.customer_phone):
        keep.customer_name = absorb.customer_name
    if absorb.customer_photo and not keep.customer_photo:
        keep.customer_photo = absorb.customer_photo
    if absorb.dealer_id and not keep.dealer_id:
        keep.dealer_id = absorb.dealer_id

    a_at = absorb.last_message_at
    k_at = keep.last_message_at
    if a_at is not None and a_at.tzinfo is None:
        a_at = a_at.replace(tzinfo=timezone.utc)
    if k_at is not None and k_at.tzinfo is None:
        k_at = k_at.replace(tzinfo=timezone.utc)
    if a_at and (k_at is None or a_at >= k_at):
        keep.last_message_at = absorb.last_message_at
        keep.last_message_preview = absorb.last_message_preview or keep.last_message_preview
    keep.unread_count = (keep.unread_count or 0) + (absorb.unread_count or 0)
    if (keep.tag or "").lower() == "synced" and (absorb.tag or "").lower() not in {"synced", ""}:
        keep.tag = absorb.tag
    elif (keep.tag or "").lower() == "synced" and keep.status == "Open":
        keep.tag = ""

    # ai_usage_logs.conversation_id is NO ACTION — reassign before delete
    try:
        from app.models import AiUsageLog

        usage_rows = (
            await db.execute(select(AiUsageLog).where(AiUsageLog.conversation_id == absorb.id))
        ).scalars().all()
        for u in usage_rows:
            u.conversation_id = keep.id
    except Exception:  # noqa: BLE001
        pass

    await db.delete(absorb)
    await db.flush()


async def _find_conversations_for_peer(
    db: AsyncSession,
    inst_id: int,
    *,
    phone: str = "",
    jid: str = "",
    lid: str = "",
) -> list[WhatsAppConversation]:
    phone = _digits(phone)
    lid = _digits(lid) or _jid_lid(jid)
    clauses: list[Any] = []
    if phone and not _is_lid_phone(phone):
        clauses.append(WhatsAppConversation.customer_phone == phone)
        clauses.append(WhatsAppConversation.customer_jid == f"{phone}@s.whatsapp.net")
    if lid:
        clauses.append(WhatsAppConversation.customer_lid == lid)
        clauses.append(WhatsAppConversation.customer_jid == f"{lid}@lid")
        clauses.append(WhatsAppConversation.customer_phone == lid)
    if jid:
        clauses.append(WhatsAppConversation.customer_jid == jid)
        # bare device jid variants (user:device@server)
        user = jid.split("@")[0].split(":")[0]
        server = jid.split("@")[1] if "@" in jid else ""
        if user and server:
            clauses.append(WhatsAppConversation.customer_jid == f"{user}@{server}")
        # Classic "{creator}-{ts}@g.us" may have been stored digit-stripped earlier
        if server.lower() == "g.us":
            g_digits = _digits(user)
            if g_digits and g_digits != user:
                clauses.append(WhatsAppConversation.customer_jid == f"{g_digits}@g.us")
                clauses.append(WhatsAppConversation.customer_phone == g_digits)
    if phone and _is_lid_phone(phone):
        clauses.append(WhatsAppConversation.customer_phone == phone)
        clauses.append(WhatsAppConversation.customer_lid == phone)
        clauses.append(WhatsAppConversation.customer_jid == f"{phone}@lid")
    if not clauses:
        return []
    return list(
        (
            await db.execute(
                select(WhatsAppConversation).where(
                    WhatsAppConversation.organization_id == ORG,
                    WhatsAppConversation.whatsapp_instance_id == inst_id,
                    or_(*clauses),
                )
            )
        ).scalars().all()
    )


_VISIBLE_CONTENT_KEYS = (
    "conversation",
    "extendedTextMessage",
    "imageMessage",
    "videoMessage",
    "audioMessage",
    "documentMessage",
    "documentWithCaptionMessage",
    "stickerMessage",
    "templateMessage",
    "buttonsMessage",
    "listMessage",
    "interactiveMessage",
    "highlyStructuredMessage",
)

_SHELL_MESSAGE_KEYS = (
    "ephemeralMessage",
    "viewOnceMessage",
    "viewOnceMessageV2",
    "viewOnceMessageV2Extension",
    "documentWithCaptionMessage",
    "questionMessage",
    "questionReplyMessage",
)


def _unwrap_message_body(body_src: dict[str, Any]) -> dict[str, Any]:
    """Unwrap view-once / ephemeral / poll-reply shells to the inner protobuf message."""
    msg: dict[str, Any] = body_src
    for _ in range(8):
        if not isinstance(msg, dict):
            return {}
        if any(k in msg for k in _VISIBLE_CONTENT_KEYS):
            return msg
        advanced = False
        for shell in _SHELL_MESSAGE_KEYS:
            node = msg.get(shell)
            if not isinstance(node, dict):
                continue
            inner = node.get("message") if isinstance(node.get("message"), dict) else None
            if isinstance(inner, dict):
                msg = inner
                advanced = True
                break
        if not advanced:
            return msg
    return msg if isinstance(msg, dict) else {}


def _button_label(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    for key in ("displayText", "DisplayText", "text", "Text", "buttonText", "ButtonText"):
        val = str(node.get(key) or "").strip()
        if val:
            return val
    for nested in node.values():
        if isinstance(nested, dict):
            label = _button_label(nested)
            if label:
                return label
    return ""


def _extract_template_text(tm: dict[str, Any]) -> tuple[str, str]:
    """Return (body_text, media_hint) from Evolution/whatsmeow templateMessage."""
    ht = tm.get("hydratedTemplate") or tm.get("hydratedFourRowTemplate") or tm.get("fourRowTemplate")
    if not isinstance(ht, dict):
        ht = {}
    parts: list[str] = []
    media_hint = ""

    title = ht.get("Title") if "Title" in ht else ht.get("title")
    if isinstance(title, dict):
        title_text = str(
            title.get("HydratedTitleText")
            or title.get("hydratedTitleText")
            or title.get("text")
            or ""
        ).strip()
        if title_text:
            parts.append(title_text)
        for key, hint in (
            ("imageMessage", "image"),
            ("videoMessage", "video"),
            ("documentMessage", "document"),
            ("locationMessage", ""),
        ):
            if isinstance(title.get(key), dict):
                media_hint = media_hint or hint
                break
    elif isinstance(title, str) and title.strip():
        parts.append(title.strip())

    content = str(ht.get("hydratedContentText") or ht.get("HydratedContentText") or "").strip()
    if content:
        parts.append(content)
    footer = str(ht.get("hydratedFooterText") or ht.get("HydratedFooterText") or "").strip()
    if footer:
        parts.append(footer)

    buttons = ht.get("hydratedButtons") or ht.get("HydratedButtons") or []
    labels: list[str] = []
    if isinstance(buttons, list):
        for raw in buttons:
            label = _button_label(raw)
            if label:
                labels.append(label)
    if labels:
        parts.append("[" + " | ".join(labels) + "]")

    # Fallback: highly structured / non-hydrated template bodies
    if not parts:
        for key in ("content", "text", "hydratedContentText", "message"):
            val = tm.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
                break
            if isinstance(val, dict):
                nested = str(val.get("text") or val.get("conversation") or "").strip()
                if nested:
                    parts.append(nested)
                    break

    return "\n\n".join(parts).strip(), media_hint


def _extract_buttons_or_list_text(body_src: dict[str, Any]) -> str:
    if isinstance(body_src.get("buttonsMessage"), dict):
        bm = body_src["buttonsMessage"]
        parts = [
            str(bm.get("contentText") or bm.get("text") or "").strip(),
            str(bm.get("footerText") or "").strip(),
        ]
        labels = []
        for raw in bm.get("buttons") or []:
            label = _button_label(raw)
            if label:
                labels.append(label)
        if labels:
            parts.append("[" + " | ".join(labels) + "]")
        return "\n\n".join(p for p in parts if p)

    if isinstance(body_src.get("listMessage"), dict):
        lm = body_src["listMessage"]
        parts = [
            str(lm.get("title") or "").strip(),
            str(lm.get("description") or lm.get("body") or "").strip(),
            str(lm.get("footerText") or "").strip(),
            str(lm.get("buttonText") or "").strip(),
        ]
        return "\n\n".join(p for p in parts if p)

    if isinstance(body_src.get("interactiveMessage"), dict):
        im = body_src["interactiveMessage"]
        parts: list[str] = []
        body = im.get("body") if isinstance(im.get("body"), dict) else {}
        header = im.get("header") if isinstance(im.get("header"), dict) else {}
        footer = im.get("footer") if isinstance(im.get("footer"), dict) else {}
        for node in (header, body, footer):
            val = str((node or {}).get("text") or (node or {}).get("title") or "").strip()
            if val:
                parts.append(val)
        return "\n\n".join(parts)

    return ""


def _extract_message_fields(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Parse Evolution GO webhook shapes (Info/Message) and Baileys-style key payloads."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None

    # Evolution GO: data.Info + data.Message
    info = data.get("Info") if isinstance(data.get("Info"), dict) else {}
    evo_msg = data.get("Message") if isinstance(data.get("Message"), dict) else None

    # Baileys-style fallback
    msg = data.get("message") if isinstance(data.get("message"), dict) else data
    key = msg.get("key") if isinstance(msg.get("key"), dict) else {}

    remote_jid = str(
        info.get("Chat")
        or key.get("remoteJid")
        or data.get("from")
        or data.get("chat")
        or data.get("jid")
        or msg.get("chat")
        or ""
    )
    # Channels / status / broadcast stay out of CRM (history sync already skips these).
    # Real @g.us groups are allowed — name/icon filled via Evolution group APIs.
    if is_inbox_excluded_jid(remote_jid):
        return None
    is_group = is_group_jid(remote_jid) or bool(info.get("IsGroup"))

    # Participant identity (groups): Sender / SenderAlt — NEVER use as conversation jid.
    sender_raw = str(info.get("Sender") or key.get("participant") or data.get("sender") or "")
    sender_alt = str(
        info.get("SenderAlt") or key.get("participantAlt") or data.get("senderAlt") or ""
    )
    participant_phone = _jid_phone(sender_raw) or _jid_phone(sender_alt) or ""
    participant_lid = _jid_lid(sender_alt) or _jid_lid(sender_raw) or ""

    if is_group:
        # Conversation identity is the group Chat jid — keep @g.us.
        group_user = remote_jid.split("@")[0].split(":")[0] if remote_jid else ""
        phone = _digits(group_user) or _jid_phone(remote_jid)
        lid = ""  # do not attach participant LID to the group row
        if not phone:
            return None
    else:
        alt_candidates = [
            info.get("SenderAlt"),
            info.get("RecipientAlt"),
            data.get("JIDAlt"),
            key.get("remoteJidAlt"),
            key.get("participantAlt"),
        ]
        phone = _jid_phone(remote_jid)
        lid = _jid_lid(remote_jid)
        for alt in alt_candidates:
            alt_s = str(alt or "")
            if not phone:
                phone = _jid_phone(alt_s)
            if not lid:
                lid = _jid_lid(alt_s)
        # last resort: digits from remote (may be LID — still store so chat is visible)
        if not phone:
            phone = _digits(remote_jid.split("@")[0].split(":")[0])
        if not phone and not lid:
            return None
        if not phone:
            phone = lid

    from_me = bool(
        info.get("IsFromMe")
        if "IsFromMe" in info
        else (key.get("fromMe") or data.get("fromMe") or False)
    )

    text = ""
    media_hint = ""
    body_src = evo_msg if isinstance(evo_msg, dict) else None
    if body_src is None and isinstance(msg.get("message"), dict):
        body_src = msg["message"]
    if body_src is None and isinstance(msg, dict):
        body_src = msg

    if isinstance(body_src, dict):
        raw_body = body_src
        body_src = _unwrap_message_body(body_src)
        # Skip protocol / reaction stubs with no user-visible content
        visible_keys = _VISIBLE_CONTENT_KEYS
        if isinstance(raw_body.get("protocolMessage"), dict) and not any(
            k in raw_body or k in body_src for k in visible_keys
        ):
            return None
        if "reactionMessage" in raw_body and not any(k in raw_body or k in body_src for k in visible_keys):
            return None
        text = str(body_src.get("conversation") or "").strip()
        if not text and isinstance(body_src.get("extendedTextMessage"), dict):
            text = str(body_src["extendedTextMessage"].get("text") or "").strip()
        if isinstance(body_src.get("imageMessage"), dict):
            media_hint = media_hint or "image"
            if not text:
                text = str(body_src["imageMessage"].get("caption") or "").strip()
        if isinstance(body_src.get("videoMessage"), dict):
            media_hint = media_hint or "video"
            if not text:
                vm = body_src["videoMessage"]
                text = str(
                    vm.get("caption")
                    or vm.get("accessibilityLabel")
                    or ""
                ).strip()
        if isinstance(body_src.get("audioMessage"), dict):
            media_hint = media_hint or "audio"
        if isinstance(body_src.get("documentMessage"), dict):
            media_hint = media_hint or "document"
            if not text:
                text = str(
                    body_src["documentMessage"].get("fileName")
                    or body_src["documentMessage"].get("title")
                    or ""
                ).strip()
        if isinstance(body_src.get("documentWithCaptionMessage"), dict):
            media_hint = media_hint or "document"
            nested = body_src["documentWithCaptionMessage"].get("message")
            if isinstance(nested, dict) and isinstance(nested.get("documentMessage"), dict):
                doc = nested["documentMessage"]
                if not text:
                    text = str(doc.get("caption") or doc.get("fileName") or "").strip()
        if isinstance(body_src.get("stickerMessage"), dict):
            media_hint = media_hint or "sticker"
        if isinstance(body_src.get("templateMessage"), dict):
            tmpl_text, tmpl_media = _extract_template_text(body_src["templateMessage"])
            if tmpl_text and not text:
                text = tmpl_text
            media_hint = media_hint or tmpl_media
        if not text:
            rich = _extract_buttons_or_list_text(body_src)
            if rich:
                text = rich

    if not media_hint:
        media_hint = _normalize_media_type(str(info.get("MediaType") or ""))
        if not media_hint and str(info.get("Type") or "").lower() == "media":
            if isinstance(body_src, dict) and isinstance(body_src.get("videoMessage"), dict):
                media_hint = "video"
            elif isinstance(body_src, dict) and isinstance(body_src.get("imageMessage"), dict):
                media_hint = "image"
            else:
                media_hint = "document"

    if not text:
        raw = data.get("text") or data.get("body") or ""
        if isinstance(raw, dict):
            text = str(raw.get("text") or raw.get("conversation") or "").strip()
        else:
            text = str(raw or "").strip()

    push_name = str(
        info.get("PushName")
        or data.get("pushName")
        or data.get("notifyName")
        or data.get("NewPushName")
        or msg.get("pushName")
        or ""
    )
    ext_id = str(
        info.get("ID")
        or key.get("id")
        or data.get("id")
        or data.get("messageId")
        or ""
    )
    if not text and not ext_id and not media_hint:
        return None

    # Prefer WhatsApp message time (history sync) over ingest wall-clock
    msg_ts = _parse_wa_timestamp(
        info.get("Timestamp")
        or info.get("timestamp")
        or data.get("timestamp")
        or data.get("messageTimestamp")
        or msg.get("messageTimestamp")
        or key.get("timestamp")
    )

    # Prefer PN jid when we learned a real phone from alt; keep @lid when LID-only.
    # Groups must keep the Chat @g.us jid — never rewrite to participant PN/LID.
    if is_group:
        if is_group_jid(remote_jid):
            g_user = remote_jid.split("@")[0].split(":")[0]
            jid = f"{g_user}@g.us"
        else:
            jid = f"{phone}@g.us"
    elif phone and not _is_lid_phone(phone, lid=lid):
        jid = f"{phone}@s.whatsapp.net"
    elif lid:
        jid = f"{lid}@lid"
    else:
        jid = remote_jid if "@" in remote_jid else f"{phone}@s.whatsapp.net"
    # Never store raw "[media]" — always a human label (UI also guards this).
    # Only use "Attachment" when we know it is media but lack a concrete type.
    media_labels = {
        "image": "📷 Photo",
        "video": "🎥 Video",
        "audio": "🎤 Voice message",
        "document": "📄 Document",
        "sticker": "Sticker",
    }
    if text:
        preview = text
    elif media_hint:
        preview = media_labels.get(media_hint, "Attachment")
    else:
        # Non-media protobuf we could not decode — keep chat row but avoid fake media label.
        preview = "Unsupported message"
    return {
        "phone": phone,
        "jid": jid,
        "lid": lid,
        "from_me": from_me,
        "text": preview,
        "push_name": push_name,
        "external_id": ext_id,
        "media_type": media_hint,
        "timestamp": msg_ts,
        "is_group": is_group,
        "participant_phone": participant_phone,
        "participant_lid": participant_lid,
    }


def _parse_wa_timestamp(raw: Any) -> datetime | None:
    """Parse Evolution/whatsmeow timestamps (unix sec/ms, ISO, or time.Time string)."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val > 1e12:  # ms
            val /= 1000.0
        elif val > 1e10:  # µs unlikely but guard
            val /= 1_000_000.0
        try:
            return datetime.fromtimestamp(val, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        return _parse_wa_timestamp(int(text))
    try:
        # RFC3339 / ISO
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _ingest_webhook_message(
    db: AsyncSession, inst: WhatsAppInstance, payload: dict[str, Any], event_type: str
) -> tuple[int | None, list[tuple[str, int]]]:
    """Ingest one Evolution message. Returns ``(intel_conv_id, post_commit_ai_jobs)``.

    AI jobs are ``("stt"|"translate", message_id)`` and MUST be scheduled only after
    the webhook handler commits — otherwise fire-and-forget STT often sees not-found.
    """
    fields = _extract_message_fields(payload)
    if not fields:
        return None, []
    if is_inbox_excluded_jid(fields.get("jid") or ""):
        return None, []
    is_group = bool(fields.get("is_group")) or is_group_jid(fields.get("jid") or "")
    # Participant LID must never drive group conversation matching / merge.
    lid_digits = (
        ""
        if is_group
        else (_digits(fields.get("lid") or "") or _jid_lid(fields.get("jid") or ""))
    )
    async def _absorb_outbound_echo(exists: WhatsAppMessage) -> None:
        # Upgrade tick if this is our outbound echo — never insert a WhatsApp twin.
        if fields["from_me"] and exists.direction == "out" and exists.status in {
            "sending",
            "sent",
            "delivered",
        }:
            if exists.status in {"sending", "sent"}:
                exists.status = "delivered"
        if fields["external_id"] and not (exists.external_message_id or "").strip():
            exists.external_message_id = fields["external_id"]
        # Keep CRM agent name; webhook would otherwise relabel as "WhatsApp".
        if fields["from_me"] and (exists.sender_name or "").strip() in {"", "WhatsApp"}:
            # Prefer leaving employee name if already set; only fill blank.
            if not (exists.sender_name or "").strip():
                exists.sender_name = "WhatsApp"
        # Learn LID on the conversation that already owns this message (CRM send path).
        # Skip for groups — participant LID is not the chat identity.
        owned = await db.get(WhatsAppConversation, exists.conversation_id)
        if owned and lid_digits and not is_group_jid(owned.customer_jid or ""):
            _remember_lid(owned, lid_digits)

    if fields["external_id"]:
        exists = (
            await db.execute(
                select(WhatsAppMessage).where(
                    WhatsAppMessage.whatsapp_instance_id == inst.id,
                    or_(
                        WhatsAppMessage.external_message_id == fields["external_id"],
                        # CRM send uses client_message_id as Evolution msg id; webhook may
                        # arrive before external_message_id is written (race → LID dupe).
                        WhatsAppMessage.client_message_id == fields["external_id"],
                    ),
                )
            )
        ).scalar_one_or_none()
        if exists:
            await _absorb_outbound_echo(exists)
            return None, []

    # Groups: match only by @g.us jid / group id. DMs: PN + LID peer matching.
    peers = await _find_conversations_for_peer(
        db,
        inst.id,
        phone=fields.get("phone") or "",
        jid=fields.get("jid") or "",
        lid="" if is_group else lid_digits,
    )
    if is_group:
        # Prefer an existing @g.us row; never fold into a participant LID/PN DM.
        group_peers = [p for p in peers if is_group_jid(p.customer_jid or "")]
        conv = _prefer_canonical_conversation(group_peers) or None
        if conv is None:
            # Exact jid match only (ignore LID-like phone collisions from peer helper)
            for p in peers:
                if (p.customer_jid or "").strip().lower() == (fields.get("jid") or "").strip().lower():
                    conv = p
                    break
    else:
        conv = _prefer_canonical_conversation(peers)
        # Fold any LID orphan shells matched in the same peer set into the canonical row.
        if conv and len(peers) > 1:
            for other in peers:
                if other.id == conv.id:
                    continue
                other_jid = (other.customer_jid or "").strip()
                other_phone = _digits(other.customer_phone)
                if other_jid.endswith("@lid") or _is_lid_phone(other_phone) or (other.customer_lid or "") == lid_digits:
                    await merge_conversation_into(db, keep=conv, absorb=other)

    now = datetime.now(timezone.utc)
    event_at = fields.get("timestamp") or now
    # Prefer PN as customer_phone / jid; never overwrite PN with LID.
    # Groups keep @g.us — never rewrite to participant @lid / @s.whatsapp.net.
    # Classic group JIDs are "{creator}-{timestamp}@g.us"; digit-stripping the
    # user part destroys the hyphen and Evolution group/info can no longer resolve.
    store_phone = fields["phone"]
    store_jid = fields["jid"]
    if is_group or is_group_jid(store_jid):
        g_user = (store_jid.split("@")[0].split(":")[0] if store_jid else "") or store_phone
        store_phone = _digits(g_user) or store_phone
        store_jid = f"{g_user}@g.us" if g_user else store_jid
    elif lid_digits and (
        not store_phone
        or store_phone == lid_digits
        or _is_lid_phone(store_phone, lid=lid_digits)
    ):
        store_phone = lid_digits
        store_jid = f"{lid_digits}@lid"
    elif store_phone and not _is_lid_phone(store_phone, lid=lid_digits):
        store_jid = f"{store_phone}@s.whatsapp.net"

    display_text = await humanize_lid_mentions(db, instance_id=inst.id, text=fields["text"])
    preview_text = display_text
    if is_group and not fields["from_me"] and (fields.get("push_name") or "").strip():
        preview_text = f"{fields['push_name'].strip()}: {display_text}"

    # Early echo merge when we already have a conversation (CRM send path).
    # Avoids media download + inserting a WhatsApp-labeled twin of the agent row.
    if fields["from_me"] and conv and (display_text or "").strip():
        try:
            from app.ai.whatsapp_send_guards import find_recent_outbound_echo_twin

            twin = await find_recent_outbound_echo_twin(
                db,
                instance_id=inst.id,
                conversation_id=conv.id,
                body=display_text,
                within_seconds=900,
            )
            if twin is not None:
                await _absorb_outbound_echo(twin)
                return None, []
        except Exception as exc:  # noqa: BLE001
            print(f"outbound echo dedupe skipped: {exc}")

    if not conv:
        conv = WhatsAppConversation(
            public_id=str(uuid.uuid4()),
            organization_id=ORG,
            whatsapp_instance_id=inst.id,
            customer_phone=store_phone,
            customer_name=("" if is_group or is_group_jid(store_jid) else fields["push_name"]),
            customer_jid=store_jid,
            customer_lid="" if is_group else (lid_digits if lid_digits else ""),
            status="New" if not fields["from_me"] else "Open",
            tag="New" if not fields["from_me"] else "",
            last_message_preview=preview_text[:255],
            last_message_at=event_at,
            unread_count=(
                0
                if fields["from_me"] or event_at < now - timedelta(minutes=5)
                else 1
            ),
        )
        db.add(conv)
        await db.flush()
        await assign_conversation_to_instance_owner(db, conv, inst.id)
    else:
        if not conv.assigned_employee_id:
            await assign_conversation_to_instance_owner(db, conv, inst.id)
        if (
            not is_group
            and not is_group_jid(store_jid)
            and fields["push_name"]
            and (not conv.customer_name or conv.customer_name == conv.customer_phone)
        ):
            conv.customer_name = fields["push_name"]
        if lid_digits and not is_group and not is_group_jid(conv.customer_jid or ""):
            _remember_lid(conv, lid_digits)
        # Upgrade LID-as-phone → real PN when we learn it (DMs only)
        if (
            not is_group
            and store_phone
            and not _is_lid_phone(store_phone, lid=lid_digits)
        ):
            if not conv.customer_phone or _is_lid_phone(conv.customer_phone, lid=lid_digits):
                conv.customer_phone = store_phone
            if not (conv.customer_jid or "").endswith("@s.whatsapp.net"):
                conv.customer_jid = f"{store_phone}@s.whatsapp.net"
        # Ensure group rows keep @g.us even if an older bug wrote something else.
        # Also upgrade digit-stripped classic JIDs back to hyphenated form.
        if is_group and store_jid and is_group_jid(store_jid):
            cur_jid = (conv.customer_jid or "").strip()
            if not is_group_jid(cur_jid):
                conv.customer_jid = store_jid
                conv.customer_phone = store_phone
                conv.customer_lid = ""
            elif (
                "-" in store_jid
                and "-" not in cur_jid
                and _digits(cur_jid.split("@")[0]) == _digits(store_jid.split("@")[0])
            ):
                conv.customer_jid = store_jid
                conv.customer_phone = store_phone or conv.customer_phone
                conv.customer_lid = ""
        # Only bump preview/unread for messages newer than (or equal to) last known
        last_at = conv.last_message_at
        if last_at is not None and last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        is_newer = last_at is None or event_at >= last_at
        if is_newer:
            conv.last_message_preview = preview_text[:255]
            conv.last_message_at = event_at
            if fields["from_me"]:
                # Awaiting/unread cleared when the outbound row is persisted (below).
                if conv.status in {"Pending Customer Reply", "Resolved"}:
                    conv.status = "Open"
            else:
                # History backfill of old inbound should not inflate unread
                if event_at >= now - timedelta(minutes=5):
                    if not getattr(conv, "is_spam", False):
                        conv.unread_count = (conv.unread_count or 0) + 1
                    if conv.status == "Resolved":
                        conv.status = "Open"
                        conv.tag = "Reopened"
                    elif conv.status != "New":
                        conv.status = "Open"

    # Group subject + icon only (newsletters already skipped by is_inbox_excluded_jid)
    if is_group or is_group_jid(conv.customer_jid or store_jid or ""):
        try:
            from app.whatsapp_chat_meta import enrich_conversation_meta

            need_meta = (
                not (conv.customer_name or "").strip()
                or conv.customer_name == conv.customer_phone
                or not (conv.customer_photo or "").strip()
            )
            if need_meta:
                _, svc_meta = await _svc(db)
                token_meta = decrypt_secret(inst.encrypted_instance_token)
                await enrich_conversation_meta(svc=svc_meta, token=token_meta, conv=conv, force=False)
        except Exception as exc:  # noqa: BLE001
            print(f"group meta skipped: {exc}")

    media_labels = {
        "image": "📷 Photo",
        "video": "🎥 Video",
        "audio": "🎤 Voice message",
        "document": "📄 Document",
        "sticker": "Sticker",
    }
    media_meta: dict[str, str] = {
        "media_type": fields.get("media_type") or "",
        "media_name": {
            "image": "photo.jpg",
            "video": "video.mp4",
            "audio": "voice.ogg",
            "document": "file",
            "sticker": "sticker.webp",
        }.get(fields.get("media_type") or "", ""),
        "media_url": "",
    }
    body_text = display_text
    # Persist media for inbound AND outbound (phone-sent / history-sync) messages
    if fields.get("media_type") or _message_node(payload):
        try:
            _, svc = await _svc(db)
            token = decrypt_secret(inst.encrypted_instance_token)
            saved = await _persist_message_media(svc, token, payload)
            if saved:
                media_meta = {**media_meta, **{k: v for k, v in saved.items() if v}}
                if media_meta.get("media_type") and body_text in {"", "[media]", "media", "Attachment", *media_labels.values()}:
                    body_text = media_labels.get(media_meta["media_type"], "Attachment")
        except Exception as exc:  # noqa: BLE001
            print(f"media download skipped: {exc}")
    if media_meta.get("media_type") and body_text in {"", "[media]", "media", "Attachment"}:
        body_text = media_labels.get(media_meta["media_type"], "Attachment")
    if body_text.strip().lower() in {"[media]", "media"}:
        body_text = "Attachment"

    # Second-chance echo merge after media caption normalization (new conv / media outs).
    if fields["from_me"] and conv and (body_text or "").strip():
        try:
            from app.ai.whatsapp_send_guards import find_recent_outbound_echo_twin

            twin = await find_recent_outbound_echo_twin(
                db,
                instance_id=inst.id,
                conversation_id=conv.id,
                body=body_text,
                within_seconds=900,
            )
            if twin is not None:
                await _absorb_outbound_echo(twin)
                return None, []
        except Exception as exc:  # noqa: BLE001
            print(f"outbound echo dedupe skipped: {exc}")

    sender_label = "WhatsApp" if fields["from_me"] else (
        fields["push_name"]
        or fields.get("participant_phone")
        or fields["phone"]
    )
    post_commit_ai: list[tuple[str, int]] = []
    media_type_norm = (media_meta.get("media_type") or fields.get("media_type") or "").lower()
    media_url_norm = media_meta.get("media_url") or ""
    pending_stt = media_type_norm in {"audio", "ptt", "voice", "video"} and media_url_norm.startswith(
        "/api/whatsapp/media/"
    )
    msg = WhatsAppMessage(
        public_id=str(uuid.uuid4()),
        organization_id=ORG,
        conversation_id=conv.id,
        whatsapp_instance_id=inst.id,
        direction="out" if fields["from_me"] else "in",
        body=body_text,
        media_url=media_url_norm,
        media_type=media_meta.get("media_type") or fields.get("media_type") or "",
        media_name=media_meta.get("media_name") or "",
        external_message_id=fields["external_id"],
        status="sent" if fields["from_me"] else "delivered",
        sender_name=sender_label,
        created_at=event_at,
        # Visible "Transcribing…" until post-commit worker finishes (or fails).
        transcription_status="pending" if pending_stt else "",
    )
    db.add(msg)
    await db.flush()

    if not is_group:
        if msg.direction == "in":
            from app.whatsapp_reply_needed import apply_inbound_reply_state

            await apply_inbound_reply_state(db, conv, inbound_text=body_text, inbound_msg=msg)
        else:
            from app.whatsapp_reply_latency import complete_staff_reply

            await complete_staff_reply(
                db,
                conv,
                replied_at=event_at,
                source="phone_sync",
                inst=inst,
                msg=msg,
            )

    # Queue Sarvam work for AFTER webhook commit (see handler). Never schedule here.
    if pending_stt:
        post_commit_ai.append(("stt", int(msg.id)))
    else:
        try:
            from app.sarvam_transcribe import is_text_translatable_message

            if is_text_translatable_message(msg):
                msg.transcription_status = "pending"
                post_commit_ai.append(("translate", int(msg.id)))
        except Exception as exc:  # noqa: BLE001
            print(f"text translation queue skipped: {exc}")

    # New-messenger onboarding — inbound DMs only; runs after commit.
    if not is_group and msg.direction == "in" and not fields.get("from_me"):
        post_commit_ai.append(("new_messenger", int(msg.id)))

    # Live notify agents (WhatsApp-style)
    try:
        from app.inbox_realtime import publish

        await publish(
            {
                "type": "message",
                "direction": msg.direction,
                "conversation_id": conv.public_id,
                "message_id": msg.public_id,
                "preview": (msg.body or msg.media_name or "[media]")[:120],
                "customer_name": conv.customer_name or conv.customer_phone,
                "customer_phone": conv.customer_phone,
                "customer_photo": (conv.customer_photo or "")[:500],
                "unread_count": conv.unread_count,
                "media_type": msg.media_type,
                "from_me": bool(fields["from_me"]),
            }
        )
    except Exception:  # noqa: BLE001
        pass
    from app.intel.extract import conversation_is_intel_subject

    jid = conv.customer_jid or fields.get("jid") or ""
    if is_group or not conversation_is_intel_subject(jid):
        return None, post_commit_ai
    return int(conv.id), post_commit_ai
