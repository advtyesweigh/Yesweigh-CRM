"""Pull ZK/ADMS device users into CRM employee stubs + CSV fallback."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adms_employee_display_order import staff_display_rank
from app.adms_employee_exclusions import is_excluded_employee_code
from app.config import get_settings
from app.models import AdmsAttendanceLog, AdmsSyncState, Employee

DEFAULT_COMPANY = "YesWeigh"
SYNC_KEY_PREFIX = "employee_import_last_"


def _code_sort_key(code: str) -> tuple:
    s = (code or "").strip()
    rank = staff_display_rank(s)
    if s.isdigit():
        return (rank, 0, int(s), s)
    return (rank, 1, 0, s.lower())


def _placeholder_name(code: str) -> str:
    return f"Employee {code}"


def _is_placeholder_name(name: str, code: str) -> bool:
    n = (name or "").strip().lower()
    if not n:
        return True
    c = (code or "").strip().lower()
    return n in {f"employee {c}", f"emp {c}", c, f"user {c}"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _link_key(device_serial: str, enrollment_id: str) -> str:
    return f"{(device_serial or '').strip().lower()}::{(enrollment_id or '').strip().lower()}"


async def fetch_adms_devices(auth: httpx.BasicAuth | None) -> list[dict[str, Any]]:
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=15.0, auth=auth) as client:
            r = await client.get(f"{settings.adms_base_url}/api/devices")
            if not r.is_success:
                return []
            data = r.json()
            return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


async def fetch_adms_employee_rows(
    auth: httpx.BasicAuth | None,
    *,
    device_serial: str | None = None,
) -> list[dict[str, Any]]:
    """Return ADMS /api/employees rows (employee_code, name, card, branch, device_serial…)."""
    settings = get_settings()
    params: dict[str, Any] = {"limit": 5000}
    if device_serial:
        params["device_serial"] = device_serial
    try:
        async with httpx.AsyncClient(timeout=15.0, auth=auth) as client:
            r = await client.get(f"{settings.adms_base_url}/api/employees", params=params)
            if not r.is_success:
                return []
            rows = r.json().get("employees") or []
            return [e for e in rows if isinstance(e, dict)]
    except Exception:  # noqa: BLE001
        return []


async def fetch_userinfo_status(auth: httpx.BasicAuth | None, serial: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=12.0, auth=auth) as client:
            r = await client.get(f"{settings.adms_base_url}/api/devices/{serial}/userinfo-status")
            if r.is_success:
                return r.json()
    except Exception:  # noqa: BLE001
        pass
    return {}


async def collect_adms_users(
    db: AsyncSession,
    auth: httpx.BasicAuth | None,
    *,
    device_serial: str | None = None,
) -> list[dict[str, str]]:
    """Union of ADMS device users + attendance codes, with best-known name/card."""
    by_code: dict[str, dict[str, str]] = {}
    serial_filter = (device_serial or "").strip()

    for e in await fetch_adms_employee_rows(auth, device_serial=serial_filter or None):
        code = str(e.get("employee_code") or e.get("code") or "").strip()
        if not code or is_excluded_employee_code(code):
            continue
        serial = str(e.get("device_serial") or serial_filter or "").strip()
        if serial_filter and serial and serial != serial_filter:
            continue
        name = str(e.get("name") or e.get("employee_name") or "").strip()
        card = str(e.get("card") or "").strip()
        branch = str(e.get("branch") or "").strip()
        phone = str(e.get("phone") or e.get("mobile") or "").strip()
        key = code.lower()
        prev = by_code.get(key)
        if prev and (prev.get("name") or "").strip() and not name:
            continue
        by_code[key] = {
            "employee_code": code,
            "name": name,
            "card": card,
            "branch": branch,
            "phone": phone,
            "device_serial": serial or serial_filter,
            "source": "adms",
        }

    q = select(AdmsAttendanceLog.employee_code, AdmsAttendanceLog.device_serial).where(
        AdmsAttendanceLog.employee_code != ""
    )
    if serial_filter:
        q = q.where(AdmsAttendanceLog.device_serial == serial_filter)
    log_rows = (await db.execute(q.distinct())).all()
    for code, serial in log_rows:
        c = str(code or "").strip()
        if not c or is_excluded_employee_code(c):
            continue
        s = str(serial or serial_filter or "").strip()
        key = c.lower()
        if key not in by_code:
            by_code[key] = {
                "employee_code": c,
                "name": "",
                "card": "",
                "branch": "",
                "phone": "",
                "device_serial": s,
                "source": "attendance",
            }
        elif not by_code[key].get("device_serial") and s:
            by_code[key]["device_serial"] = s

    return sorted(by_code.values(), key=lambda r: _code_sort_key(r["employee_code"]))


async def linked_adms_codes(db: AsyncSession) -> dict[str, Employee]:
    """Map lower(adms_employee_code) → Employee for linked rows."""
    rows = (
        await db.execute(
            select(Employee).where(
                Employee.adms_employee_code != "",
                Employee.adms_employee_code.is_not(None),
            )
        )
    ).scalars().all()
    out: dict[str, Employee] = {}
    for emp in rows:
        key = str(emp.adms_employee_code or "").strip().lower()
        if key:
            out[key] = emp
    return out


async def linked_by_device(db: AsyncSession) -> dict[str, Employee]:
    """Map device_serial::enrollment_id → Employee."""
    rows = (
        await db.execute(
            select(Employee).where(
                Employee.adms_employee_code != "",
                Employee.adms_employee_code.is_not(None),
            )
        )
    ).scalars().all()
    out: dict[str, Employee] = {}
    for emp in rows:
        code = str(emp.adms_employee_code or "").strip()
        if not code:
            continue
        serial = str(emp.adms_device or "").strip()
        out[_link_key(serial, code)] = emp
        # Also index bare code for legacy links without device
        bare = _link_key("", code)
        out.setdefault(bare, emp)
    return out


async def codes_in_use(db: AsyncSession) -> set[str]:
    rows = (await db.execute(select(Employee.code))).scalars().all()
    return {str(c or "").strip().lower() for c in rows if str(c or "").strip()}


async def get_sync_meta(db: AsyncSession, key: str) -> str:
    row = (
        await db.execute(select(AdmsSyncState).where(AdmsSyncState.key == key))
    ).scalar_one_or_none()
    return str(row.value) if row else ""


async def set_sync_meta(db: AsyncSession, key: str, value: str) -> None:
    row = (
        await db.execute(select(AdmsSyncState).where(AdmsSyncState.key == key))
    ).scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(AdmsSyncState(key=key, value=value))


async def import_adms_employees(
    db: AsyncSession,
    auth: httpx.BasicAuth | None,
    *,
    device_serial: str | None = None,
    company: str = DEFAULT_COMPANY,
    named_only: bool = False,
    update_names: bool = False,
    confirm_overwrite: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Create CRM employee stubs from ADMS/ZK users.

    Mapping key: company + device_serial + enrollment_id (adms_employee_code).
    Does not overwrite manually maintained names without confirm_overwrite.
    Does not create CRM login / aadhaar.
    """
    serial = (device_serial or "").strip()
    company_name = (company or DEFAULT_COMPANY).strip() or DEFAULT_COMPANY
    users = await collect_adms_users(db, auth, device_serial=serial or None)
    by_device = await linked_by_device(db)
    linked = await linked_adms_codes(db)
    used_codes = await codes_in_use(db)

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    needs_confirm: list[dict[str, str]] = []

    named_count = sum(1 for u in users if (u.get("name") or "").strip())
    unnamed_count = len(users) - named_count

    for u in users:
        code = u["employee_code"]
        name = (u.get("name") or "").strip()
        dev = (u.get("device_serial") or serial or "").strip()
        if is_excluded_employee_code(code):
            skipped.append({"employee_code": code, "reason": "excluded_left", "device_serial": dev})
            continue
        if named_only and not name:
            skipped.append({"employee_code": code, "reason": "unnamed", "device_serial": dev})
            continue

        existing = by_device.get(_link_key(dev, code)) or linked.get(code.lower())
        if existing:
            # Same company+device+enrollment → skip duplicates; optional name refresh
            if str(existing.company or "").strip() and str(existing.company).strip() != company_name:
                # Different company — still treat as linked by enrollment to avoid cross-company clobber
                skipped.append({"employee_code": code, "reason": "already_linked_other", "device_serial": dev})
                continue

            if not (existing.adms_device or "").strip() and dev and not dry_run:
                existing.adms_device = dev

            richer = not _is_placeholder_name(existing.name, code)
            if update_names and name and richer and not confirm_overwrite:
                needs_confirm.append(
                    {
                        "employee_code": code,
                        "device_serial": dev,
                        "crm_id": str(existing.id),
                        "crm_name": existing.name,
                        "device_name": name,
                        "reason": "overwrite_requires_confirmation",
                    }
                )
                skipped.append({"employee_code": code, "reason": "needs_overwrite_confirm", "device_serial": dev})
                continue

            if update_names and name and (
                confirm_overwrite
                or _is_placeholder_name(existing.name, code)
            ):
                if not dry_run:
                    existing.name = name
                    if u.get("phone") and not (existing.phone or "").strip():
                        existing.phone = u["phone"]
                    if u.get("branch") and not (existing.branch or "").strip():
                        existing.branch = u["branch"]
                    if dev:
                        existing.adms_device = dev
                updated.append(
                    {
                        "employee_code": code,
                        "name": name,
                        "id": str(existing.id),
                        "device_serial": dev,
                    }
                )
            else:
                skipped.append({"employee_code": code, "reason": "already_linked", "device_serial": dev})
            continue

        crm_code = code
        if code.lower() in used_codes:
            base = f"ADMS-{code}"
            crm_code = base
            n = 2
            while crm_code.lower() in used_codes:
                crm_code = f"{base}-{n}"
                n += 1

        display = name or _placeholder_name(code)
        if dry_run:
            created.append(
                {
                    "employee_code": code,
                    "name": display,
                    "code": crm_code,
                    "device_serial": dev,
                }
            )
            used_codes.add(crm_code.lower())
            continue

        try:
            emp = Employee(
                code=crm_code,
                name=display,
                phone=(u.get("phone") or "").strip(),
                branch=(u.get("branch") or "").strip(),
                adms_employee_code=code,
                adms_device=dev,
                status="Active",
                company=company_name,
                employment_type="Permanent",
                shift="General shift",
                languages="English",
                crm_role="Employee",
                account_status="",
                notes=f"Imported from ADMS/ZK device {dev or 'unknown'}",
            )
            db.add(emp)
            await db.flush()
            used_codes.add(crm_code.lower())
            linked[code.lower()] = emp
            by_device[_link_key(dev, code)] = emp
            created.append(
                {
                    "employee_code": code,
                    "name": display,
                    "code": crm_code,
                    "id": str(emp.id),
                    "device_serial": dev,
                }
            )
        except Exception as exc:  # noqa: BLE001
            failed.append({"employee_code": code, "reason": str(exc), "device_serial": dev})

    now = _utcnow()
    if not dry_run:
        key = f"{SYNC_KEY_PREFIX}{serial or 'all'}"
        await set_sync_meta(db, key, now)
        if created or updated:
            await db.commit()
        else:
            await db.commit()  # persist sync meta

    return {
        "ok": True,
        "dry_run": dry_run,
        "device_serial": serial,
        "company": company_name,
        "source_count": len(users),
        "named_count": named_count,
        "unnamed_count": unnamed_count,
        "created_count": len(created),
        "updated_count": len(updated),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "needs_confirm_count": len(needs_confirm),
        "created": created,
        "updated": updated,
        "failed": failed,
        "needs_confirm": needs_confirm,
        "skipped": skipped[:80],
        "last_synchronization_time": now,
        "note": (
            "Names missing from ADMS — use CSV fallback or wait for device USERINFO. "
            "Auto-sync stays off until X305 Pro USERINFO is verified."
            if unnamed_count and not named_count
            else (
                "Some CRM names need overwrite confirmation."
                if needs_confirm
                else ""
            )
        ),
    }


async def map_enrollment_to_employee(
    db: AsyncSession,
    *,
    employee_id: int,
    enrollment_id: str,
    device_serial: str = "",
    overwrite_name: str | None = None,
    confirm_overwrite: bool = False,
) -> dict[str, Any]:
    """Link an existing CRM employee to a device enrollment PIN."""
    emp = (
        await db.execute(select(Employee).where(Employee.id == employee_id))
    ).scalar_one_or_none()
    if not emp:
        return {"ok": False, "error": "Employee not found"}
    code = (enrollment_id or "").strip()
    if not code:
        return {"ok": False, "error": "enrollment_id required"}
    if is_excluded_employee_code(code):
        return {"ok": False, "error": f"Enrollment {code} is retired/left and cannot be mapped"}

    # Prevent duplicate enrollment mapping on same company+device
    clash = (
        await db.execute(
            select(Employee).where(
                Employee.adms_employee_code == code,
                Employee.id != employee_id,
            )
        )
    ).scalars().all()
    serial = (device_serial or emp.adms_device or "").strip()
    for other in clash:
        other_serial = (other.adms_device or "").strip()
        if other_serial == serial or not serial or not other_serial:
            return {
                "ok": False,
                "error": f"Enrollment {code} already linked to {other.name} ({other.code})",
            }

    emp.adms_employee_code = code
    if serial:
        emp.adms_device = serial
    if overwrite_name and overwrite_name.strip():
        if not _is_placeholder_name(emp.name, code) and not confirm_overwrite:
            return {
                "ok": False,
                "needs_confirm": True,
                "crm_name": emp.name,
                "proposed_name": overwrite_name.strip(),
                "error": "Overwrite requires confirmation",
            }
        emp.name = overwrite_name.strip()
    await db.commit()
    return {
        "ok": True,
        "employee_id": emp.id,
        "adms_employee_code": emp.adms_employee_code,
        "adms_device": emp.adms_device,
        "name": emp.name,
    }


async def import_employees_csv(
    db: AsyncSession,
    content: str,
    *,
    device_serial: str = "",
    company: str = DEFAULT_COMPANY,
    confirm_overwrite: bool = False,
) -> dict[str, Any]:
    """
    CSV fallback when ADMS USERINFO is unsupported.

    Expected columns (header, case-insensitive):
      enrollment_id | pin | employee_code | adms_id  (required)
      name | employee_name
      device_serial | device (optional)
      phone, branch (optional)
    """
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        return {"ok": False, "error": "CSV has no header row"}

    def col(*names: str) -> str | None:
        lower = { (h or "").strip().lower(): h for h in (reader.fieldnames or []) }
        for n in names:
            if n in lower:
                return lower[n]
        return None

    c_code = col("enrollment_id", "pin", "employee_code", "adms_id", "adms_employee_code", "emp_number")
    c_name = col("name", "employee_name", "emp_name")
    c_device = col("device_serial", "device", "serial")
    c_phone = col("phone", "mobile")
    c_branch = col("branch")
    if not c_code:
        return {
            "ok": False,
            "error": "CSV must include enrollment_id / pin / employee_code column",
        }

    users: list[dict[str, str]] = []
    for row in reader:
        code = str(row.get(c_code) or "").strip()
        if not code:
            continue
        users.append(
            {
                "employee_code": code,
                "name": str(row.get(c_name) or "").strip() if c_name else "",
                "phone": str(row.get(c_phone) or "").strip() if c_phone else "",
                "branch": str(row.get(c_branch) or "").strip() if c_branch else "",
                "device_serial": (
                    str(row.get(c_device) or "").strip()
                    if c_device
                    else (device_serial or "")
                )
                or device_serial,
                "source": "csv",
            }
        )

    # Reuse import logic against in-memory list by temporarily writing through same path
    # Inline minimal import (avoid ADMS fetch)
    by_device = await linked_by_device(db)
    linked = await linked_adms_codes(db)
    used_codes = await codes_in_use(db)
    company_name = (company or DEFAULT_COMPANY).strip() or DEFAULT_COMPANY

    created: list[dict[str, str]] = []
    updated: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    needs_confirm: list[dict[str, str]] = []

    for u in users:
        code = u["employee_code"]
        name = (u.get("name") or "").strip()
        dev = (u.get("device_serial") or device_serial or "").strip()
        if is_excluded_employee_code(code):
            skipped.append({"employee_code": code, "reason": "excluded_left"})
            continue
        existing = by_device.get(_link_key(dev, code)) or linked.get(code.lower())
        if existing:
            richer = not _is_placeholder_name(existing.name, code)
            if name and richer and name != existing.name and not confirm_overwrite:
                needs_confirm.append(
                    {
                        "employee_code": code,
                        "crm_name": existing.name,
                        "device_name": name,
                        "crm_id": str(existing.id),
                    }
                )
                skipped.append({"employee_code": code, "reason": "needs_overwrite_confirm"})
                continue
            if name and (confirm_overwrite or _is_placeholder_name(existing.name, code)):
                existing.name = name
                if u.get("phone") and not (existing.phone or "").strip():
                    existing.phone = u["phone"]
                if u.get("branch") and not (existing.branch or "").strip():
                    existing.branch = u["branch"]
                if dev:
                    existing.adms_device = dev
                updated.append({"employee_code": code, "name": name, "id": str(existing.id)})
            else:
                skipped.append({"employee_code": code, "reason": "already_linked"})
            continue

        crm_code = code
        if code.lower() in used_codes:
            base = f"ADMS-{code}"
            crm_code = base
            n = 2
            while crm_code.lower() in used_codes:
                crm_code = f"{base}-{n}"
                n += 1
        display = name or _placeholder_name(code)
        try:
            emp = Employee(
                code=crm_code,
                name=display,
                phone=(u.get("phone") or "").strip(),
                branch=(u.get("branch") or "").strip(),
                adms_employee_code=code,
                adms_device=dev,
                status="Active",
                company=company_name,
                employment_type="Permanent",
                shift="General shift",
                languages="English",
                crm_role="Employee",
                account_status="",
                notes=f"Imported from CSV (device {dev or 'n/a'})",
            )
            db.add(emp)
            await db.flush()
            used_codes.add(crm_code.lower())
            linked[code.lower()] = emp
            by_device[_link_key(dev, code)] = emp
            created.append(
                {
                    "employee_code": code,
                    "name": display,
                    "code": crm_code,
                    "id": str(emp.id),
                    "device_serial": dev,
                }
            )
        except Exception as exc:  # noqa: BLE001
            failed.append({"employee_code": code, "reason": str(exc)})

    now = _utcnow()
    await set_sync_meta(db, f"{SYNC_KEY_PREFIX}csv_{device_serial or 'all'}", now)
    await db.commit()
    return {
        "ok": True,
        "source": "csv",
        "device_serial": device_serial,
        "source_count": len(users),
        "created_count": len(created),
        "updated_count": len(updated),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "needs_confirm_count": len(needs_confirm),
        "created": created,
        "updated": updated,
        "failed": failed,
        "needs_confirm": needs_confirm,
        "skipped": skipped[:80],
        "last_synchronization_time": now,
    }


async def sync_device_users(
    auth: httpx.BasicAuth | None,
    *,
    device_serial: str | None = None,
    pins: list[str] | None = None,
) -> dict[str, Any]:
    """Queue DATA QUERY USERINFO on approved non-test ADMS device(s)."""
    settings = get_settings()
    queued: list[dict[str, Any]] = []
    errors: list[str] = []
    target = (device_serial or "").strip()
    try:
        async with httpx.AsyncClient(timeout=20.0, auth=auth) as client:
            dr = await client.get(f"{settings.adms_base_url}/api/devices")
            if not dr.is_success:
                return {"ok": False, "error": f"devices list failed ({dr.status_code})"}
            devices = dr.json() if isinstance(dr.json(), list) else []
            for d in devices:
                if not isinstance(d, dict):
                    continue
                serial = str(d.get("serial_number") or "").strip()
                if not serial or d.get("status") != "approved":
                    continue
                if target and serial != target:
                    continue
                serial_l = serial.lower()
                if serial_l in {"test", "simulator", "demo"} or serial_l.startswith("test"):
                    continue
                ip = str(d.get("last_ip") or "")
                if ip in {"127.0.0.1", "::1", "localhost"} or ip.startswith("127."):
                    continue
                body: dict[str, Any]
                if pins:
                    body = {"pins": [str(p).strip() for p in pins if str(p).strip()]}
                else:
                    body = {"all_known": True}
                qr = await client.post(
                    f"{settings.adms_base_url}/api/devices/{serial}/query-users",
                    json=body,
                )
                if qr.is_success:
                    queued.append({"serial": serial, **qr.json()})
                else:
                    errors.append(f"{serial}: {qr.status_code} {qr.text[:120]}")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "queued": queued,
        "errors": errors,
        "message": (
            "Queued USERINFO query. When the biometric device is online it will upload "
            "PIN + Name via /iclock/cdata; then run Import from Device again. "
            "If names never arrive, use CSV fallback."
        ),
    }


async def import_preview(
    db: AsyncSession,
    auth: httpx.BasicAuth | None,
    *,
    device_serial: str | None = None,
) -> dict[str, Any]:
    """Device picker payload: devices + preview rows + last sync."""
    devices = await fetch_adms_devices(auth)
    serial = (device_serial or "").strip()
    real_devices = []
    for d in devices:
        sn = str(d.get("serial_number") or "").strip()
        if not sn:
            continue
        sn_l = sn.lower()
        if sn_l in {"test", "simulator", "demo"} or sn_l.startswith("test"):
            continue
        real_devices.append(d)

    users = await collect_adms_users(db, auth, device_serial=serial or None) if serial else []
    linked = await linked_adms_codes(db)
    by_device = await linked_by_device(db)
    items = []
    for u in users:
        code = u["employee_code"]
        dev = (u.get("device_serial") or serial or "").strip()
        emp = by_device.get(_link_key(dev, code)) or linked.get(code.lower())
        items.append(
            {
                **u,
                "linked": emp is not None,
                "crm_employee_id": emp.id if emp else None,
                "crm_name": emp.name if emp else None,
                "crm_code": emp.code if emp else None,
                "is_placeholder": _is_placeholder_name(emp.name, code) if emp else True,
            }
        )

    last_sync = await get_sync_meta(db, f"{SYNC_KEY_PREFIX}{serial or 'all'}")
    status = await fetch_userinfo_status(auth, serial) if serial else {}

    crm_emps = (
        await db.execute(
            select(Employee.id, Employee.code, Employee.name, Employee.adms_employee_code, Employee.adms_device)
            .where(Employee.status.in_(["Active", "Probation", "active"]))
            .limit(2000)
        )
    ).all()
    crm_emps = sorted(
        crm_emps,
        key=lambda r: (staff_display_rank(str(r[3] or r[1] or "")), (r[2] or "").lower()),
    )

    return {
        "ok": True,
        "devices": real_devices,
        "selected_device": serial,
        "last_synchronization_time": last_sync or status.get("last_userinfo_at") or "",
        "userinfo_status": status,
        "count": len(items),
        "named_count": sum(1 for i in items if (i.get("name") or "").strip()),
        "linked_count": sum(1 for i in items if i["linked"]),
        "employees": items,
        "crm_employees": [
            {
                "id": r[0],
                "code": r[1],
                "name": r[2],
                "adms_employee_code": r[3] or "",
                "adms_device": r[4] or "",
            }
            for r in crm_emps
        ],
        "csv_fallback_recommended": bool(serial) and (
            not items or sum(1 for i in items if (i.get("name") or "").strip()) == 0
        ),
        "auto_sync_users": bool(status.get("auto_sync_users")) if status else False,
    }
