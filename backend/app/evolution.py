from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
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
from app.whatsapp_employee_exclusions import is_whatsapp_excluded_employee_code, normalize_code

ORG = "yesweigh"
router = APIRouter(tags=["evolution"])
Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


def _require_admin(user: User) -> None:
    if user.role.lower() not in {"admin", "super_admin", "super admin", "management"}:
        raise HTTPException(403, "Administrator access required")


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
        "last_connected_at": inst.last_connected_at,
        "created_at": inst.created_at,
        "assignees": assignees or [],
    }


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
    _, svc = await _svc(db)
    token = decrypt_secret(inst.encrypted_instance_token)
    if not token:
        raise HTTPException(400, "Instance token missing")
    try:
        # If already connected, return status instead of a QR
        try:
            remote = await svc.status(token)
            payload = remote.get("data") if isinstance(remote, dict) else remote
            if isinstance(payload, dict) and (
                payload.get("connected") or payload.get("Connected") or payload.get("LoggedIn")
            ):
                inst.connection_status = "Connected"
                inst.last_connected_at = datetime.now(timezone.utc)
                phone = str(payload.get("jid") or payload.get("phone") or payload.get("Jid") or "")
                if phone and ("@" in phone or phone.replace("+", "").isdigit()):
                    inst.connected_phone_number = phone.split("@")[0].split(":")[0]
                profile_name = str(payload.get("Name") or payload.get("name") or "")
                if profile_name:
                    inst.profile_name = profile_name
                # Prefer jid from instance info when status only returns Name
                if not inst.connected_phone_number:
                    try:
                        info = await svc.info(inst.evolution_instance_id, token)
                        info_data = info.get("data") if isinstance(info, dict) else info
                        if isinstance(info_data, dict):
                            jid = str(info_data.get("jid") or info_data.get("phone") or "")
                            if jid:
                                inst.connected_phone_number = jid.split("@")[0].split(":")[0]
                    except Exception:  # noqa: BLE001
                        pass
                await db.commit()
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


@router.get("/api/whatsapp/instances/{public_id}/status")
async def instance_status(public_id: str, db: Db, user: CurrentUser):
    _require_admin(user)
    inst = await _owned_instance(db, public_id)
    _, svc = await _svc(db)
    token = decrypt_secret(inst.encrypted_instance_token)
    try:
        remote = await svc.status(token)
        payload = remote.get("data") if isinstance(remote, dict) else remote
        connected = False
        phone = ""
        if isinstance(payload, dict):
            # Evolution GO returns Connected/LoggedIn (PascalCase) and sometimes lowercase.
            connected = bool(
                payload.get("connected")
                or payload.get("Connected")
                or payload.get("LoggedIn")
                or payload.get("loggedIn")
                or payload.get("status") == "open"
            )
            phone = str(
                payload.get("phone")
                or payload.get("Phone")
                or payload.get("jid")
                or payload.get("Jid")
                or ""
            )
            profile_name = str(payload.get("Name") or payload.get("name") or "")
            if profile_name:
                inst.profile_name = profile_name
        # Fallback: instance info / all-list when status shape is incomplete
        if not connected:
            try:
                info = await svc.info(inst.evolution_instance_id, token)
                info_data = info.get("data") if isinstance(info, dict) else info
                if isinstance(info_data, dict):
                    connected = bool(info_data.get("connected") or info_data.get("Connected"))
                    if not phone:
                        phone = str(info_data.get("jid") or info_data.get("phone") or "")
            except Exception:  # noqa: BLE001
                pass
        if connected:
            inst.connection_status = "Connected"
            inst.last_connected_at = datetime.now(timezone.utc)
            if phone:
                inst.connected_phone_number = phone.split("@")[0].split(":")[0]
        elif inst.connection_status not in {"Error", "Logged Out"}:
            inst.connection_status = "QR Required"
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


@router.delete("/api/whatsapp/instances/{public_id}")
async def delete_instance(public_id: str, db: Db, user: CurrentUser):
    _require_admin(user)
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
    connected_flag = False
    if isinstance(data, dict):
        connected_flag = bool(data.get("connected") or data.get("Connected") or data.get("LoggedIn"))
        jid = str(data.get("jid") or data.get("Jid") or data.get("phone") or "")
        if jid and ("@" in jid or jid.isdigit()):
            inst.connected_phone_number = jid.split("@")[0].split(":")[0]
    if connected_flag or "connection" in lower and ("open" in lower or payload.get("connected") is True):
        inst.connection_status = "Connected"
        inst.last_connected_at = datetime.now(timezone.utc)
    elif "connect" in lower or payload.get("connected") is True:
        inst.connection_status = "Connected"
        inst.last_connected_at = datetime.now(timezone.utc)
    if "qr" in lower:
        inst.connection_status = "QR Required"
    if "disconnect" in lower or "logout" in lower:
        inst.connection_status = "Disconnected"

    # Persist inbound / outbound chat messages into the CRM inbox
    # Skip PushName-only noise; Message / SendMessage carry the body.
    if event_type in {"Message", "SendMessage"} or (
        any(x in lower for x in ("message", "messages", "send_message", "receive"))
        and "receipt" not in lower
        and "pushname" not in lower
    ):
        try:
            await _ingest_webhook_message(db, inst, payload, event_type)
        except Exception as exc:  # noqa: BLE001
            print(f"webhook ingest error: {exc}")

    # Delivery / read receipts → update ticks
    if any(x in lower for x in ("receipt", "read", "ack", "status")):
        try:
            await _apply_message_receipt(db, inst, payload, event_type)
        except Exception:  # noqa: BLE001
            pass

    await db.commit()
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
    return msg if isinstance(msg, dict) else None


def _detect_media(msg: dict[str, Any]) -> tuple[str, dict[str, Any], str, str]:
    """Return (media_type, media_node, filename, mimetype)."""
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
    info_type = ""
    return "", {}, "", info_type


async def _persist_inbound_media(
    svc: EvolutionService,
    token: str,
    payload: dict[str, Any],
) -> dict[str, str]:
    """
    Auto-download inbound media to VPS disk.
    Prefers webhook base64/mediaUrl; falls back to Evolution /message/downloadmedia.
    """
    msg = _message_node(payload)
    if not msg:
        return {}
    media_type, node, filename, mime = _detect_media(msg)
    if not media_type:
        # Info.MediaType fallback
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        info = data.get("Info") if isinstance(data.get("Info"), dict) else {}
        mt = str(info.get("MediaType") or info.get("Type") or "").lower()
        if mt in {"image", "video", "audio", "document", "sticker"}:
            media_type = mt
        else:
            return {}

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

            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.get(media_url)
                r.raise_for_status()
                public, _ = save_bytes(r.content, media_type=media_type or "document", filename=filename)
        elif isinstance(media_url, str) and media_url.startswith("data:"):
            public, _, _ = save_data_url(media_url, media_type=media_type or "document", filename=filename)
        else:
            # Ask Evolution to decrypt/download using the media node
            download_msg = {k: v for k, v in msg.items() if k.lower() != "base64"}
            remote = await svc.download_media(token, download_msg)
            data = remote.get("data") if isinstance(remote.get("data"), dict) else remote
            raw = ""
            if isinstance(data, dict):
                raw = str(data.get("base64") or data.get("data") or "")
            elif isinstance(data, str):
                raw = data
            if not raw and isinstance(remote.get("base64"), str):
                raw = remote["base64"]
            if raw:
                if not raw.startswith("data:"):
                    raw = f"data:{mime or 'application/octet-stream'};base64,{raw}"
                public, _, _ = save_data_url(raw, media_type=media_type or "document", filename=filename)
    except Exception as exc:  # noqa: BLE001
        print(f"media persist failed: {exc}")
        return {"media_type": media_type, "media_name": filename, "media_url": ""}

    return {
        "media_type": media_type,
        "media_name": filename,
        "media_url": public,
    }


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
    if "@g.us" in remote_jid or bool(info.get("IsGroup")):
        return None

    alt_candidates = [
        info.get("SenderAlt"),
        info.get("RecipientAlt"),
        data.get("JIDAlt"),
        key.get("remoteJidAlt"),
        key.get("participantAlt"),
    ]
    phone = _jid_phone(remote_jid)
    if not phone:
        for alt in alt_candidates:
            phone = _jid_phone(str(alt or ""))
            if phone:
                break
    # last resort: digits from remote (may be LID — still store so chat is visible)
    if not phone:
        phone = _digits(remote_jid.split("@")[0].split(":")[0])
    if not phone:
        return None

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
        text = str(body_src.get("conversation") or "").strip()
        if not text and isinstance(body_src.get("extendedTextMessage"), dict):
            text = str(body_src["extendedTextMessage"].get("text") or "").strip()
        if not text and isinstance(body_src.get("imageMessage"), dict):
            text = str(body_src["imageMessage"].get("caption") or "").strip()
            media_hint = "image"
        if not text and isinstance(body_src.get("videoMessage"), dict):
            text = str(body_src["videoMessage"].get("caption") or "").strip()
            media_hint = "video"
        if isinstance(body_src.get("audioMessage"), dict):
            media_hint = media_hint or "audio"
        if isinstance(body_src.get("documentMessage"), dict):
            media_hint = media_hint or "document"
            if not text:
                text = str(body_src["documentMessage"].get("fileName") or "").strip()
        if isinstance(body_src.get("stickerMessage"), dict):
            media_hint = media_hint or "sticker"

    if not media_hint:
        info_media = str(info.get("MediaType") or "").lower()
        if info_media in {"image", "video", "audio", "document", "sticker"}:
            media_hint = info_media
        elif str(info.get("Type") or "").lower() == "media":
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

    jid = remote_jid if "@" in remote_jid else f"{phone}@s.whatsapp.net"
    preview = text or ({"image": "📷 Photo", "video": "🎥 Video", "audio": "🎤 Voice message", "document": "📄 Document", "sticker": "Sticker"}.get(media_hint, "[media]"))
    return {
        "phone": phone,
        "jid": jid,
        "from_me": from_me,
        "text": preview,
        "push_name": push_name,
        "external_id": ext_id,
        "media_type": media_hint,
    }


async def _ingest_webhook_message(
    db: AsyncSession, inst: WhatsAppInstance, payload: dict[str, Any], event_type: str
) -> None:
    fields = _extract_message_fields(payload)
    if not fields:
        return
    if fields["external_id"]:
        exists = (
            await db.execute(
                select(WhatsAppMessage).where(
                    WhatsAppMessage.external_message_id == fields["external_id"],
                    WhatsAppMessage.whatsapp_instance_id == inst.id,
                )
            )
        ).scalar_one_or_none()
        if exists:
            # Upgrade tick if this is our outbound echo
            if fields["from_me"] and exists.direction == "out" and exists.status in {"sending", "sent"}:
                exists.status = "delivered"
            return

    # Match by phone, LID jid, or LID digits mistakenly stored as phone
    lid_digits = _digits(fields["jid"].split("@")[0]) if "@lid" in (fields["jid"] or "") else ""
    match_phones = [p for p in [fields["phone"], lid_digits] if p]
    match_clauses = [
        WhatsAppConversation.customer_phone.in_(match_phones) if match_phones else False,
        WhatsAppConversation.customer_jid == fields["jid"] if fields["jid"] else False,
    ]
    if lid_digits:
        match_clauses.append(WhatsAppConversation.customer_jid == f"{lid_digits}@lid")
        match_clauses.append(WhatsAppConversation.customer_phone == lid_digits)
    live_clauses = [c for c in match_clauses if c is not False]
    conv = (
        await db.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.organization_id == ORG,
                WhatsAppConversation.whatsapp_instance_id == inst.id,
                or_(*live_clauses) if live_clauses else False,
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    # Prefer PN as customer_phone when jid is LID
    store_phone = fields["phone"]
    store_jid = fields["jid"]
    if "@lid" in store_jid and store_phone == lid_digits:
        # only LID known — keep digits as phone but jid must stay @lid for send
        store_jid = f"{lid_digits}@lid"

    if not conv:
        conv = WhatsAppConversation(
            public_id=str(uuid.uuid4()),
            organization_id=ORG,
            whatsapp_instance_id=inst.id,
            customer_phone=store_phone,
            customer_name=fields["push_name"],
            customer_jid=store_jid,
            status="New" if not fields["from_me"] else "Open",
            tag="New" if not fields["from_me"] else "",
            last_message_preview=fields["text"][:255],
            last_message_at=now,
            unread_count=0 if fields["from_me"] else 1,
        )
        db.add(conv)
        await db.flush()
    else:
        if fields["push_name"] and (not conv.customer_name or conv.customer_name == conv.customer_phone):
            conv.customer_name = fields["push_name"]
        # Upgrade LID-as-phone → real PN when we learn it
        if store_phone and store_phone != lid_digits:
            if not conv.customer_phone or conv.customer_phone == lid_digits or len(conv.customer_phone) > 15:
                conv.customer_phone = store_phone
        if store_jid:
            if "@lid" in store_jid or not (conv.customer_jid or "").endswith("@lid"):
                conv.customer_jid = store_jid
        conv.last_message_preview = fields["text"][:255]
        conv.last_message_at = now
        if not fields["from_me"]:
            conv.unread_count = (conv.unread_count or 0) + 1
            if conv.status == "Resolved":
                conv.status = "Open"
                conv.tag = "Reopened"
            elif conv.status == "New":
                pass
            else:
                conv.status = "Open"

    media_meta: dict[str, str] = {
        "media_type": fields.get("media_type") or "",
        "media_name": "",
        "media_url": "",
    }
    body_text = fields["text"]
    # Auto-download inbound media (all types) onto VPS disk — uses full webhook body
    if not fields["from_me"]:
        try:
            _, svc = await _svc(db)
            token = decrypt_secret(inst.encrypted_instance_token)
            saved = await _persist_inbound_media(svc, token, payload)
            if saved:
                media_meta = {**media_meta, **{k: v for k, v in saved.items() if v}}
                labels = {
                    "image": "📷 Photo",
                    "video": "🎥 Video",
                    "audio": "🎤 Voice message",
                    "document": "📄 Document",
                    "sticker": "Sticker",
                }
                if media_meta.get("media_type") and body_text in {"", "[media]", *labels.values()}:
                    body_text = labels.get(media_meta["media_type"], "[media]")
        except Exception as exc:  # noqa: BLE001
            print(f"inbound media download skipped: {exc}")

    msg = WhatsAppMessage(
        public_id=str(uuid.uuid4()),
        organization_id=ORG,
        conversation_id=conv.id,
        whatsapp_instance_id=inst.id,
        direction="out" if fields["from_me"] else "in",
        body=body_text,
        media_url=media_meta.get("media_url") or "",
        media_type=media_meta.get("media_type") or fields.get("media_type") or "",
        media_name=media_meta.get("media_name") or "",
        external_message_id=fields["external_id"],
        status="sent" if fields["from_me"] else "delivered",
        sender_name="WhatsApp" if fields["from_me"] else (fields["push_name"] or fields["phone"]),
    )
    db.add(msg)
    await db.flush()

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
