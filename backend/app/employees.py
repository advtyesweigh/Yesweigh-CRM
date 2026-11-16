from __future__ import annotations

import re
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adms_employee_display_order import STAFF_DISPLAY_ORDER, UNKNOWN_RANK
from app.auth import get_current_user, hash_password
from app.db import get_db
from app.models import Employee, EmployeeActivityLog, EmployeeDocument, EmployeeTarget, User
from app.schemas import (
    EmployeeActivityOut,
    EmployeeDocumentOut,
    EmployeeIn,
    EmployeeListOut,
    EmployeeOut,
    EmployeePageOut,
    EmployeeStatsOut,
    EmployeeStatusIn,
    EmployeeTargetIn,
    EmployeeTargetOut,
    ResetPasswordIn,
)

router = APIRouter(prefix="/api/employees", tags=["employees"])
Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

DOCS_ROOT = Path("/data/employee-docs")
ALLOWED_DOC_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
}
MAX_DOC_BYTES = 10 * 1024 * 1024

SENSITIVE_ROLES = {"admin", "hr", "management", "hr manager"}


def _can_see_sensitive(user: User) -> bool:
    return user.role.lower() in SENSITIVE_ROLES or user.role.lower() in {"admin"}


def _normalize_status(value: str) -> str:
    mapping = {
        "active": "Active",
        "inactive": "Inactive",
        "on leave": "On Leave",
        "on_leave": "On Leave",
        "probation": "Probation",
        "suspended": "Suspended",
        "resigned": "Resigned",
        "terminated": "Terminated",
    }
    key = (value or "Active").strip().lower()
    return mapping.get(key, value.strip() or "Active")


def _active_statuses() -> tuple[str, ...]:
    return ("Active", "Probation", "active")


async def _next_employee_code(db: AsyncSession) -> str:
    rows = (await db.execute(select(Employee.code))).scalars().all()
    max_n = 0
    for code in rows:
        m = re.search(r"(\d+)$", code or "")
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"YW-{max_n + 1:04d}"


def _gen_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def _log(db: AsyncSession, employee_id: int, activity_type: str, summary: str) -> None:
    db.add(
        EmployeeActivityLog(
            employee_id=employee_id,
            activity_type=activity_type,
            summary=summary,
        )
    )


def _to_list(emp: Employee) -> EmployeeListOut:
    return EmployeeListOut.model_validate(emp)


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _normalize_aadhaar(value: str) -> str:
    digits = _digits_only(value)
    if not digits:
        return ""
    if len(digits) != 12:
        raise HTTPException(400, "Aadhaar must be 12 digits")
    return digits


def _resolve_login_password(body: EmployeeIn, aadhaar: str = "") -> str:
    raw = (body.password or body.temporary_password or "").strip() or (aadhaar or "").strip()
    if len(raw) < 12:
        raise HTTPException(400, "Password must be at least 12 characters")
    return raw


def _to_detail(emp: Employee, user: User) -> EmployeeOut:
    data = EmployeeOut.model_validate(emp)
    if not _can_see_sensitive(user):
        data.salary = None
        data.pan = ""
        # Non-HR roles only see last 4 digits
        if data.aadhaar and len(data.aadhaar) >= 4:
            data.aadhaar = data.aadhaar[-4:]
        else:
            data.aadhaar = ""
    return data


@router.get("", response_model=EmployeePageOut)
async def list_employees(
    db: Db,
    user: CurrentUser,
    q: str = "",
    department: str = "",
    branch: str = "",
    status: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort: str = "id",
    order: str = "desc",
):
    stmt = select(Employee)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Employee.name.ilike(like),
                Employee.code.ilike(like),
                Employee.phone.ilike(like),
                Employee.email.ilike(like),
            )
        )
    if department:
        stmt = stmt.where(Employee.department == department)
    if branch:
        stmt = stmt.where(Employee.branch == branch)
    if status:
        stmt = stmt.where(Employee.status == _normalize_status(status))

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0

    sort_key = (sort or "id").strip().lower()
    # Default / display_order: fixed roster (Biju…Ashima), then name
    if sort_key in {"id", "display_order", "roster"}:
        whens: list[tuple] = []
        for code, rank in STAFF_DISPLAY_ORDER.items():
            whens.append((Employee.adms_employee_code == code, rank))
            whens.append((Employee.code == code, rank))
        rank_expr = case(*whens, else_=UNKNOWN_RANK)
        if order.lower() == "desc" and sort_key == "display_order":
            stmt = stmt.order_by(rank_expr.desc(), Employee.name.desc(), Employee.id.desc())
        else:
            # Fixed office order (also replaces legacy default sort=id)
            stmt = stmt.order_by(rank_expr.asc(), Employee.name.asc(), Employee.id.asc())
    else:
        sort_map = {
            "name": Employee.name,
            "code": Employee.code,
            "department": Employee.department,
            "branch": Employee.branch,
            "join_date": Employee.join_date,
            "status": Employee.status,
        }
        col = sort_map.get(sort_key, Employee.name)
        stmt = stmt.order_by(col.desc() if order.lower() == "desc" else col.asc())
    rows = (await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))).scalars().all()

    active = (
        await db.execute(
            select(func.count()).select_from(Employee).where(Employee.status.in_(_active_statuses()))
        )
    ).scalar() or 0
    on_leave = (
        await db.execute(
            select(func.count())
            .select_from(Employee)
            .where(Employee.status.in_(["On Leave", "on leave", "On leave"]))
        )
    ).scalar() or 0
    all_total = (await db.execute(select(func.count()).select_from(Employee))).scalar() or 0

    # Present/absent placeholders until ADMS daily sync lands in UI
    present_today = 0
    absent_today = max(0, active - present_today)

    return EmployeePageOut(
        items=[_to_list(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        stats=EmployeeStatsOut(
            total=all_total,
            active=active,
            present_today=present_today,
            absent_today=absent_today,
            on_leave=on_leave,
        ),
    )


@router.post("", response_model=EmployeeOut)
async def create_employee(body: EmployeeIn, db: Db, user: CurrentUser):
    code = (body.code or "").strip() or await _next_employee_code(db)
    exists = (await db.execute(select(Employee).where(Employee.code == code))).scalar_one_or_none()
    if exists:
        raise HTTPException(400, "Employee ID already exists")

    payload = body.model_dump(
        exclude={"enable_crm_login", "login_email", "temporary_password", "password", "code"}
    )
    payload["code"] = code
    payload["status"] = _normalize_status(payload.get("status") or "Active")
    if not _can_see_sensitive(user):
        payload["salary"] = None
        payload["pan"] = ""
    aadhaar = _normalize_aadhaar(payload.get("aadhaar") or "")
    if not aadhaar:
        raise HTTPException(400, "Aadhaar is required (exactly 12 digits)")
    payload["aadhaar"] = aadhaar
    payload["crm_role"] = (body.crm_role or "Employee").strip() or "Employee"

    emp = Employee(**payload)
    db.add(emp)
    await db.flush()

    # Login username = full Aadhaar; password hashed into users.password_hash
    login_id = aadhaar
    if (await db.execute(select(User).where(User.email == login_id))).scalar_one_or_none():
        raise HTTPException(400, "Login username already exists")
    temp = _resolve_login_password(body, aadhaar=aadhaar)
    u = User(
        email=login_id,
        name=body.name,
        password_hash=hash_password(temp),
        role=payload["crm_role"].lower().replace(" ", "_"),
        is_active=True,
        must_change_password=False,
    )
    db.add(u)
    await db.flush()
    emp.user_id = u.id
    emp.account_status = "Enabled"

    await _log(db, emp.id, "employee_created", f"Employee {emp.code} created by {user.email}")
    await db.commit()
    await db.refresh(emp)
    return _to_detail(emp, user)


@router.get("/meta/options")
async def employee_options(user: CurrentUser):
    return {
        "departments": [
            "Management",
            "Sales",
            "Marketing",
            "Service",
            "Software Development",
            "Accounts",
            "HR",
            "Production",
            "Warehouse",
            "Administration",
        ],
        "branches": ["Head Office", "Kochi", "Chennai", "Bengaluru"],
        "statuses": [
            "Active",
            "Probation",
            "On Leave",
            "Suspended",
            "Resigned",
            "Terminated",
            "Inactive",
        ],
        "employment_types": ["Permanent", "Temporary", "Contract", "Intern", "Part-time"],
        "blood_groups": ["A+", "A−", "B+", "B−", "AB+", "AB−", "O+", "O−", "Unknown"],
        "roles": [
            "Super Admin",
            "Management",
            "Sales Manager",
            "Sales Executive",
            "Marketing Manager",
            "HR Manager",
            "Service Manager",
            "Service Engineer",
            "Accounts",
            "Employee",
        ],
        "languages": [
            "English",
            "Hindi",
            "Malayalam",
            "Tamil",
            "Kannada",
            "Telugu",
            "Marathi",
            "Gujarati",
            "Bengali",
        ],
        "document_types": [
            "Profile photograph",
            "Aadhaar or alternative identity proof",
            "PAN card",
            "Address proof",
            "Educational certificates",
            "Experience certificates",
            "Resume / CV",
            "Appointment letter",
            "Employment agreement",
            "Driving licence",
            "Bank account proof",
            "Other documents",
        ],
    }


@router.get("/{employee_id}", response_model=EmployeeOut)
async def get_employee(employee_id: int, db: Db, user: CurrentUser):
    emp = await db.get(Employee, employee_id)
    if not emp:
        raise HTTPException(404, "Employee not found")
    return _to_detail(emp, user)


@router.patch("/{employee_id}", response_model=EmployeeOut)
async def update_employee(employee_id: int, body: EmployeeIn, db: Db, user: CurrentUser):
    emp = await db.get(Employee, employee_id)
    if not emp:
        raise HTTPException(404, "Employee not found")

    payload = body.model_dump(exclude={"enable_crm_login", "login_email", "temporary_password", "password"})
    if payload.get("code") and payload["code"] != emp.code:
        clash = (
            await db.execute(select(Employee).where(Employee.code == payload["code"], Employee.id != emp.id))
        ).scalar_one_or_none()
        if clash:
            raise HTTPException(400, "Employee ID already exists")
    payload["status"] = _normalize_status(payload.get("status") or emp.status)
    if not _can_see_sensitive(user):
        payload.pop("salary", None)
        payload.pop("pan", None)
    aadhaar = _normalize_aadhaar(payload.get("aadhaar") or emp.aadhaar or "")
    if not aadhaar:
        raise HTTPException(400, "Aadhaar is required (exactly 12 digits)")
    payload["aadhaar"] = aadhaar
    if body.crm_role:
        payload["crm_role"] = body.crm_role.strip() or emp.crm_role or "Employee"

    for k, v in payload.items():
        if k == "code" and not v:
            continue
        setattr(emp, k, v)

    role_value = (emp.crm_role or "Employee").lower().replace(" ", "_")
    pwd_raw = (body.password or body.temporary_password or "").strip()

    if emp.user_id:
        u = await db.get(User, emp.user_id)
        if u:
            # Keep login id in sync with Aadhaar when it changes
            if aadhaar and u.email != aadhaar:
                clash = (
                    await db.execute(select(User).where(User.email == aadhaar, User.id != u.id))
                ).scalar_one_or_none()
                if clash:
                    raise HTTPException(400, "Login username already exists")
                u.email = aadhaar
            u.role = role_value
            u.name = emp.name or u.name
            if pwd_raw:
                if len(pwd_raw) < 12:
                    raise HTTPException(400, "Password must be at least 12 characters")
                u.password_hash = hash_password(pwd_raw)
                u.must_change_password = False
            emp.account_status = emp.account_status or "Enabled"
    elif body.enable_crm_login or aadhaar:
        login_id = aadhaar
        if (await db.execute(select(User).where(User.email == login_id))).scalar_one_or_none():
            raise HTTPException(400, "Login username already exists")
        temp = _resolve_login_password(body, aadhaar=aadhaar) if (pwd_raw or aadhaar) else _gen_password()
        if len(temp) < 12:
            raise HTTPException(400, "Password must be at least 12 characters")
        u = User(
            email=login_id,
            name=emp.name,
            password_hash=hash_password(temp),
            role=role_value,
            is_active=True,
            must_change_password=False,
        )
        db.add(u)
        await db.flush()
        emp.user_id = u.id
        emp.account_status = "Enabled"

    await _log(db, emp.id, "employee_updated", f"Employee {emp.code} updated by {user.email}")
    await db.commit()
    await db.refresh(emp)
    return _to_detail(emp, user)


@router.patch("/{employee_id}/status", response_model=EmployeeOut)
async def update_status(employee_id: int, body: EmployeeStatusIn, db: Db, user: CurrentUser):
    emp = await db.get(Employee, employee_id)
    if not emp:
        raise HTTPException(404, "Employee not found")
    emp.status = _normalize_status(body.status)
    if emp.status in {"Inactive", "Terminated", "Resigned", "Suspended"}:
        emp.deactivated_at = datetime.now(timezone.utc)
        emp.account_status = "Disabled"
        if emp.user_id:
            u = await db.get(User, emp.user_id)
            if u:
                u.is_active = False
    else:
        emp.deactivated_at = None
    await _log(db, emp.id, "status_changed", f"Status set to {emp.status}")
    await db.commit()
    await db.refresh(emp)
    return _to_detail(emp, user)


@router.post("/{employee_id}/reset-password")
async def reset_password(employee_id: int, body: ResetPasswordIn, db: Db, user: CurrentUser):
    emp = await db.get(Employee, employee_id)
    if not emp or not emp.user_id:
        raise HTTPException(400, "Employee has no CRM login account")
    u = await db.get(User, emp.user_id)
    if not u:
        raise HTTPException(404, "Linked user not found")
    temp = (body.temporary_password or "").strip() or _gen_password()
    if len(temp) < 12:
        raise HTTPException(400, "Password must be at least 12 characters")
    u.password_hash = hash_password(temp)
    u.must_change_password = True
    u.is_active = True
    emp.account_status = "Enabled"
    await _log(db, emp.id, "password_reset", f"Password reset by {user.email}")
    await db.commit()
    return {"ok": True, "temporary_password": temp, "must_change_password": True}


@router.get("/{employee_id}/documents", response_model=list[EmployeeDocumentOut])
async def list_documents(employee_id: int, db: Db, user: CurrentUser):
    emp = await db.get(Employee, employee_id)
    if not emp:
        raise HTTPException(404, "Employee not found")
    rows = (
        await db.execute(
            select(EmployeeDocument)
            .where(EmployeeDocument.employee_id == employee_id)
            .order_by(EmployeeDocument.id.desc())
        )
    ).scalars().all()
    return rows


@router.post("/{employee_id}/documents", response_model=EmployeeDocumentOut)
async def upload_document(
    employee_id: int,
    db: Db,
    user: CurrentUser,
    doc_type: str = Form(...),
    file: UploadFile = File(...),
):
    emp = await db.get(Employee, employee_id)
    if not emp:
        raise HTTPException(404, "Employee not found")
    content = await file.read()
    if len(content) > MAX_DOC_BYTES:
        raise HTTPException(400, "File too large (max 10MB)")
    mime = file.content_type or ""
    if mime not in ALLOWED_DOC_TYPES:
        raise HTTPException(400, "Only PDF, JPG and PNG allowed")
    DOCS_ROOT.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", file.filename or "document")
    dest = DOCS_ROOT / f"{employee_id}_{int(datetime.now().timestamp())}_{safe_name}"
    dest.write_bytes(content)
    doc = EmployeeDocument(
        employee_id=employee_id,
        doc_type=doc_type,
        file_name=file.filename or safe_name,
        stored_path=str(dest),
        mime_type=mime,
        file_size=len(content),
        verification_status="Pending",
        uploaded_by=user.id,
    )
    db.add(doc)
    await _log(db, employee_id, "document_uploaded", f"{doc_type}: {file.filename}")
    await db.commit()
    await db.refresh(doc)
    return doc


@router.get("/{employee_id}/documents/{document_id}/download")
async def download_document(employee_id: int, document_id: int, db: Db, user: CurrentUser):
    doc = await db.get(EmployeeDocument, document_id)
    if not doc or doc.employee_id != employee_id:
        raise HTTPException(404, "Document not found")
    path = Path(doc.stored_path)
    if not path.exists():
        raise HTTPException(404, "File missing on server")
    await _log(db, employee_id, "document_downloaded", f"Downloaded {doc.file_name} by {user.email}")
    await db.commit()
    return FileResponse(path, filename=doc.file_name, media_type=doc.mime_type or "application/octet-stream")


@router.delete("/{employee_id}/documents/{document_id}", status_code=204)
async def delete_document(employee_id: int, document_id: int, db: Db, user: CurrentUser):
    doc = await db.get(EmployeeDocument, document_id)
    if not doc or doc.employee_id != employee_id:
        raise HTTPException(404, "Document not found")
    path = Path(doc.stored_path)
    if path.exists():
        path.unlink()
    await db.delete(doc)
    await _log(db, employee_id, "document_deleted", f"Deleted {doc.file_name}")
    await db.commit()


@router.get("/{employee_id}/targets", response_model=list[EmployeeTargetOut])
async def list_targets(employee_id: int, db: Db, user: CurrentUser):
    return (
        await db.execute(
            select(EmployeeTarget)
            .where(EmployeeTarget.employee_id == employee_id)
            .order_by(EmployeeTarget.id.desc())
        )
    ).scalars().all()


@router.patch("/{employee_id}/targets", response_model=EmployeeTargetOut)
async def upsert_target(employee_id: int, body: EmployeeTargetIn, db: Db, user: CurrentUser):
    emp = await db.get(Employee, employee_id)
    if not emp:
        raise HTTPException(404, "Employee not found")
    row = EmployeeTarget(employee_id=employee_id, **body.model_dump())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/{employee_id}/activity", response_model=list[EmployeeActivityOut])
async def list_activity(employee_id: int, db: Db, user: CurrentUser):
    return (
        await db.execute(
            select(EmployeeActivityLog)
            .where(EmployeeActivityLog.employee_id == employee_id)
            .order_by(EmployeeActivityLog.id.desc())
            .limit(100)
        )
    ).scalars().all()


@router.get("/{employee_id}/attendance")
async def employee_attendance(employee_id: int, db: Db, user: CurrentUser):
    emp = await db.get(Employee, employee_id)
    if not emp:
        raise HTTPException(404, "Employee not found")
    return {
        "employee_id": emp.id,
        "adms_employee_code": emp.adms_employee_code,
        "adms_device": emp.adms_device,
        "today": {"check_in": None, "check_out": None, "hours": None},
        "month": {"present": 0, "absent": 0, "late": 0, "leave": 0, "missing_punches": 0},
        "note": "Mapped to ADMS via employee code; daily punch sync expands in Phase 6.",
    }


@router.get("/{employee_id}/performance")
async def employee_performance(employee_id: int, db: Db, user: CurrentUser):
    emp = await db.get(Employee, employee_id)
    if not emp:
        raise HTTPException(404, "Employee not found")
    is_sales = "sales" in (emp.department or "").lower() or "sales" in (emp.designation or "").lower()
    return {
        "employee_id": emp.id,
        "department": emp.department,
        "is_sales": is_sales,
        "sales": {
            "assigned_leads": 0,
            "daily_call_target": 0,
            "verified_calls": 0,
            "followups_completed": 0,
            "missed_followups": 0,
            "quotations": 0,
            "orders": 0,
            "revenue": 0,
            "conversion_rate": 0,
        }
        if is_sales
        else None,
        "work": {"assigned": 0, "completed": 0, "pending": 0, "overdue": 0},
    }
