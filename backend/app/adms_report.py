"""Daily attendance report from Postgres punches (Asia/Kolkata)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AdmsAttendanceLog, Employee

IST = ZoneInfo("Asia/Kolkata")

# Default office schedule (wall clock, Asia/Kolkata)
# Mon–Fri 09:30–17:30 · Sat 09:30–17:00 · Sun off
WEEKDAY_START = (9, 30)
WEEKDAY_END = (17, 30)
SATURDAY_START = (9, 30)
SATURDAY_END = (17, 0)


def today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def shift_for_date(iso_date: str) -> dict[str, Any]:
    """Return shift window for a calendar day. Sunday = off."""
    try:
        d = date.fromisoformat(iso_date[:10])
    except ValueError:
        d = datetime.now(IST).date()
    wd = d.weekday()  # Mon=0 … Sun=6
    if wd == 6:
        return {
            "name": "Off",
            "label": "Off",
            "hours_label": "Off",
            "start": None,
            "end": None,
            "off": True,
        }
    if wd == 5:
        return {
            "name": "Saturday",
            "label": "9:30 to 5:00",
            "hours_label": "9:30 to 5:00",
            "start": SATURDAY_START,
            "end": SATURDAY_END,
            "off": False,
        }
    return {
        "name": "Weekday",
        "label": "9:30 to 5:30",
        "hours_label": "9:30 to 5:30",
        "start": WEEKDAY_START,
        "end": WEEKDAY_END,
        "off": False,
    }


def resolve_working_day(iso_date: str, *, max_lookback: int = 366) -> tuple[str, bool]:
    """If iso_date is Off, walk back to the most recent working day.

    Returns (effective_iso_date, rolled_back).
    """
    try:
        d = date.fromisoformat(iso_date[:10])
    except ValueError:
        d = datetime.now(IST).date()
    for _ in range(max(1, max_lookback)):
        iso = d.isoformat()
        if not shift_for_date(iso).get("off"):
            return iso, iso != iso_date[:10]
        d -= timedelta(days=1)
    # Fallback: never leave caller without a day
    return d.isoformat(), True


def _punch_clock(punch_time: str | None) -> tuple[int, int] | None:
    if not punch_time or len(punch_time) < 16:
        return None
    try:
        return int(punch_time[11:13]), int(punch_time[14:16])
    except ValueError:
        return None


def _minutes_between(a: tuple[int, int], b: tuple[int, int]) -> int:
    return a[0] * 60 + a[1] - (b[0] * 60 + b[1])


def format_duration(minutes: int) -> str:
    if minutes <= 0:
        return "—"
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def format_working_hours(minutes: int | None) -> str:
    """HH:MM worked span (mockup style)."""
    if minutes is None or minutes < 0:
        return "—"
    h, m = divmod(minutes, 60)
    return f"{h:02d}:{m:02d}"


def format_date_ddmmyyyy(iso_date: str) -> str:
    try:
        return date.fromisoformat(iso_date[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return iso_date


def format_clock_ampm(punch_time: str | None) -> str:
    clock = _punch_clock(punch_time)
    if not clock:
        return "—"
    h, m = clock
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12:02d}:{m:02d} {suffix}"


def format_clock_24(punch_time: str | None) -> str:
    if not punch_time or len(punch_time) < 16:
        return "—"
    return punch_time[11:16]


def _is_placeholder_display_name(name: str, code: str) -> bool:
    n = (name or "").strip().lower()
    if not n:
        return True
    c = (code or "").strip().lower()
    return n in {f"employee {c}", f"emp {c}", c, f"user {c}"}


async def _name_maps(db: AsyncSession, auth: httpx.BasicAuth | None) -> dict[str, str]:
    names: dict[str, str] = {}
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=8.0, auth=auth) as client:
            r = await client.get(f"{settings.adms_base_url}/api/employees", params={"limit": 5000})
            if r.is_success:
                for e in (r.json().get("employees") or []):
                    code = str(e.get("employee_code") or "").strip()
                    name = str(e.get("name") or "").strip()
                    if code and name and not _is_placeholder_display_name(name, code):
                        names[code] = name
    except Exception:  # noqa: BLE001
        pass

    rows = (
        await db.execute(
            select(Employee.adms_employee_code, Employee.name, Employee.code).where(
                or_(Employee.adms_employee_code != "", Employee.code != "")
            )
        )
    ).all()
    for adms_code, name, code in rows:
        n = (name or "").strip()
        if not n:
            continue
        keys = [k for k in (str(adms_code or "").strip(), str(code or "").strip()) if k]
        placeholder = any(_is_placeholder_display_name(n, k) for k in keys) or _is_placeholder_display_name(
            n, str(adms_code or code or "").strip()
        )
        for key in keys:
            existing = names.get(key)
            if placeholder:
                # Never let "Employee N" wipe a real ADMS/device name
                if not existing:
                    names[key] = n
                continue
            names[key] = n
    return names


async def _known_employee_codes(db: AsyncSession, auth: httpx.BasicAuth | None) -> set[str]:
    """Union of CRM-mapped + ADMS employee codes for absent/leave roster."""
    from app.adms_employee_exclusions import is_excluded_employee_code

    codes: set[str] = set()
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=8.0, auth=auth) as client:
            r = await client.get(f"{settings.adms_base_url}/api/employees", params={"limit": 5000})
            if r.is_success:
                for e in (r.json().get("employees") or []):
                    c = str(e.get("employee_code") or "").strip()
                    if c and not is_excluded_employee_code(c):
                        codes.add(c)
    except Exception:  # noqa: BLE001
        pass
    rows = (
        await db.execute(
            select(Employee.adms_employee_code, Employee.code, Employee.status).where(
                Employee.status.in_(["Active", "Probation", "active"])
            )
        )
    ).all()
    for adms_code, code, _status in rows:
        if adms_code and str(adms_code).strip():
            c = str(adms_code).strip()
            if not is_excluded_employee_code(c):
                codes.add(c)
        elif code and str(code).strip():
            c = str(code).strip()
            if not is_excluded_employee_code(c):
                codes.add(c)
    return codes


def _row_metrics(
    *,
    cin: str,
    cout_display: str | None,
    shift: dict[str, Any],
) -> dict[str, Any]:
    cin_clock = _punch_clock(cin)
    cout_clock = _punch_clock(cout_display) if cout_display else None

    if shift.get("off"):
        status = "On Leave" if cin_clock else "Absent"
        return {
            "late_minutes": 0,
            "early_minutes": 0,
            "working_minutes": None,
            "status": status,
        }

    start = shift["start"]
    end = shift["end"]
    assert start and end

    if not cin_clock:
        return {
            "late_minutes": 0,
            "early_minutes": 0,
            "working_minutes": None,
            "status": "Absent",
        }

    late_min = max(0, _minutes_between(cin_clock, start))
    early_min = max(0, _minutes_between(end, cout_clock)) if cout_clock else 0
    working = None
    if cin_clock and cout_clock:
        working = max(0, _minutes_between(cout_clock, cin_clock))

    status = "Late" if late_min > 0 else "On Time"
    return {
        "late_minutes": late_min,
        "early_minutes": early_min,
        "working_minutes": working,
        "status": status,
    }


async def daily_report(
    db: AsyncSession,
    *,
    auth: httpx.BasicAuth | None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 50,
    q: str = "",
) -> dict[str, Any]:
    requested_from = (date_from or today_ist()).strip()[:10]
    requested_to = (date_to or requested_from).strip()[:10]
    day = requested_from
    day_to = requested_to
    showing_last_working_day = False
    # Single-day Off view → load last working day so KPIs/rows aren't empty Sunday dashes
    if day == day_to and shift_for_date(day).get("off"):
        day, showing_last_working_day = resolve_working_day(day)
        day_to = day
    page = max(1, page)
    page_size = max(1, min(page_size, 200))

    exclude = and_(
        AdmsAttendanceLog.device_serial.notin_(["TEST", "test", "SIMULATOR", "DEMO"]),
        ~AdmsAttendanceLog.device_serial.ilike("test%"),
        AdmsAttendanceLog.punch_date >= day,
        AdmsAttendanceLog.punch_date <= day_to,
    )

    agg = (
        select(
            AdmsAttendanceLog.employee_code.label("employee_code"),
            AdmsAttendanceLog.punch_date.label("punch_date"),
            func.min(AdmsAttendanceLog.punch_time).label("check_in"),
            func.max(AdmsAttendanceLog.punch_time).label("check_out"),
            func.max(AdmsAttendanceLog.employee_name).label("employee_name"),
            func.count().label("punches"),
        )
        .where(exclude)
        .group_by(AdmsAttendanceLog.employee_code, AdmsAttendanceLog.punch_date)
    )
    sub = agg.subquery()
    rows = (await db.execute(select(sub).order_by(sub.c.punch_date.desc(), sub.c.employee_code.asc()))).all()

    name_map = await _name_maps(db, auth)
    known = await _known_employee_codes(db, auth)
    qterm = q.strip().lower()

    # Index present punches by date
    present_by_day: dict[str, set[str]] = {}
    items: list[dict[str, Any]] = []

    from app.adms_employee_exclusions import is_excluded_employee_code

    for r in rows:
        code = str(r.employee_code or "")
        if is_excluded_employee_code(code):
            continue
        punch_date = str(r.punch_date)
        present_by_day.setdefault(punch_date, set()).add(code)
        pname = str(r.employee_name or "").strip()
        name = name_map.get(code) or pname or f"Employee {code}"
        if qterm and qterm not in name.lower() and qterm not in code.lower():
            continue

        cin = str(r.check_in or "")
        cout = str(r.check_out or "")
        same = cin == cout
        cout_display = None if same else cout
        shift = shift_for_date(punch_date)
        m = _row_metrics(cin=cin, cout_display=cout_display, shift=shift)

        items.append(
            {
                "employee_name": name,
                "employee_number": code,
                "date": format_date_ddmmyyyy(punch_date),
                "date_iso": punch_date,
                "shift": shift["label"],
                "shift_name": shift["name"],
                "check_in": format_clock_24(cin),
                "check_in_display": format_clock_ampm(cin),
                "check_out": format_clock_24(cout_display) if cout_display else "—",
                "check_out_display": format_clock_ampm(cout_display) if cout_display else "—",
                "late": format_duration(m["late_minutes"]),
                "late_minutes": m["late_minutes"],
                "early": format_duration(m["early_minutes"]),
                "early_minutes": m["early_minutes"],
                "total_late": format_duration(m["late_minutes"] + m["early_minutes"]),
                "total_late_minutes": m["late_minutes"] + m["early_minutes"],
                "working_hours": format_working_hours(m["working_minutes"]),
                "working_minutes": m["working_minutes"],
                "status": m["status"],
                "punches": int(r.punches or 0),
            }
        )

    # For single-day views, append Absent rows for known employees with no punch
    if day == day_to and known:
        shift = shift_for_date(day)
        present = present_by_day.get(day, set())
        for code in sorted(known, key=lambda c: (len(c), c)):
            if code in present:
                continue
            name = name_map.get(code) or f"Employee {code}"
            if qterm and qterm not in name.lower() and qterm not in code.lower():
                continue
            items.append(
                {
                    "employee_name": name,
                    "employee_number": code,
                    "date": format_date_ddmmyyyy(day),
                    "date_iso": day,
                    "shift": shift["label"],
                    "shift_name": shift["name"],
                    "check_in": "—",
                    "check_in_display": "—",
                    "check_out": "—",
                    "check_out_display": "—",
                    "late": "—",
                    "late_minutes": 0,
                    "early": "—",
                    "early_minutes": 0,
                    "total_late": "—",
                    "total_late_minutes": 0,
                    "working_hours": "—",
                    "working_minutes": None,
                    "status": "On Leave" if shift.get("off") else "Absent",
                    "punches": 0,
                }
            )

    # KPI for primary day (date_to)
    kpi_day = day_to
    day_items = [i for i in items if i["date_iso"] == kpi_day]
    # Prefer present-only for check-ins count
    present_n = sum(1 for i in day_items if i["status"] in ("On Time", "Late"))
    on_time_n = sum(1 for i in day_items if i["status"] == "On Time")
    late_n = sum(1 for i in day_items if i["status"] == "Late")
    leave_n = sum(1 for i in day_items if i["status"] == "On Leave")
    absent_n = sum(1 for i in day_items if i["status"] == "Absent")
    total_emp = len(day_items) if day_items else max(len(known), present_n)
    if total_emp == 0:
        total_emp = present_n

    def pct(n: int) -> float:
        return round((n / total_emp) * 100, 1) if total_emp else 0.0

    # Check-ins = punch events that day (from raw logs)
    checkins = (
        await db.execute(
            select(func.count())
            .select_from(AdmsAttendanceLog)
            .where(
                and_(
                    AdmsAttendanceLog.punch_date == kpi_day,
                    AdmsAttendanceLog.device_serial.notin_(["TEST", "test", "SIMULATOR", "DEMO"]),
                    ~AdmsAttendanceLog.device_serial.ilike("test%"),
                )
            )
        )
    ).scalar() or 0

    # Fixed roster order (Biju → … → Ashima); unknown codes after; name as tiebreak
    from app.adms_employee_display_order import staff_display_rank

    items.sort(
        key=lambda i: (
            i["date_iso"],
            staff_display_rank(i.get("employee_number")),
            (i.get("employee_name") or "").lower(),
        )
    )

    total = len(items)
    start_i = (page - 1) * page_size
    page_items = items[start_i : start_i + page_size]
    shift_today = shift_for_date(kpi_day)
    display_note = None
    if showing_last_working_day:
        display_note = (
            f"Showing {format_date_ddmmyyyy(day)} (last working day)"
            f" — selected {format_date_ddmmyyyy(requested_from)} was Off"
        )

    return {
        "ok": True,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": max(1, (total + page_size - 1) // page_size) if total else 1,
        "date_from": day,
        "date_to": day_to,
        "requested_date_from": requested_from,
        "requested_date_to": requested_to,
        "showing_last_working_day": showing_last_working_day,
        "display_note": display_note,
        "shift": {
            "weekday": "09:30–17:30",
            "saturday": "09:30–17:00",
            "sunday": "off",
            "timezone": "Asia/Kolkata",
            "today_label": shift_today["label"],
            "note": "Late = check-in after shift start; Early = check-out before shift end; Total late = late + early.",
        },
        "kpis": {
            "total_employees": total_emp,
            "present": present_n,
            "present_pct": pct(present_n),
            "on_time": on_time_n,
            "on_time_pct": pct(on_time_n),
            "late": late_n,
            "late_pct": pct(late_n),
            "on_leave": leave_n,
            "on_leave_pct": pct(leave_n),
            "absent": absent_n,
            "absent_pct": pct(absent_n),
            "checkins": int(checkins),
        },
        "items": page_items,
    }
