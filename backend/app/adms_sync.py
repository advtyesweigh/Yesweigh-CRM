"""Pull ADMS SQLite punches into CRM Postgres (Firebase not required)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.adms_employee_exclusions import is_excluded_employee_code
from app.config import get_settings
from app.db import SessionLocal
from app.models import AdmsAttendanceLog, AdmsSyncState

logger = logging.getLogger("crm.adms_sync")

CURSOR_KEY = "attendance_after_id"
_stop = asyncio.Event()
_task: asyncio.Task | None = None
_lock = asyncio.Lock()


def _auth() -> httpx.BasicAuth | None:
    settings = get_settings()
    if settings.adms_username and settings.adms_password:
        return httpx.BasicAuth(settings.adms_username, settings.adms_password)
    return None


async def _get_cursor(db) -> int:
    row = (await db.execute(select(AdmsSyncState).where(AdmsSyncState.key == CURSOR_KEY))).scalar_one_or_none()
    if not row:
        return 0
    try:
        return int(row.value or "0")
    except ValueError:
        return 0


async def _set_cursor(db, after_id: int) -> None:
    existing = (await db.execute(select(AdmsSyncState).where(AdmsSyncState.key == CURSOR_KEY))).scalar_one_or_none()
    if existing:
        existing.value = str(after_id)
        existing.updated_at = datetime.now(timezone.utc)
    else:
        db.add(AdmsSyncState(key=CURSOR_KEY, value=str(after_id)))


def _row_values(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "adms_id": int(item.get("id") or 0),
        "event_id": str(item.get("event_id") or ""),
        "employee_code": str(item.get("employee_code") or ""),
        "employee_name": str(item.get("employee_name") or ""),
        "device_serial": str(item.get("device_serial") or ""),
        "device_name": str(item.get("device_name") or ""),
        "branch": str(item.get("branch") or ""),
        "punch_time": str(item.get("punch_time") or ""),
        "punch_date": str(item.get("punch_date") or ""),
        "punch_status": str(item.get("punch_status") or ""),
        "punch_label": str(item.get("punch_label") or ""),
        "verify_method": str(item.get("verify_method") or ""),
        "verify_label": str(item.get("verify_label") or ""),
        "work_code": str(item.get("work_code") or ""),
        "firebase_status": str(item.get("firebase_status") or "pending"),
        "received_at": str(item.get("received_at") or ""),
        "synced_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


async def _upsert_items(db, items: list[dict[str, Any]]) -> int:
    if not items:
        return 0
    rows = [
        _row_values(i)
        for i in items
        if i.get("event_id") and not is_excluded_employee_code(str(i.get("employee_code") or ""))
    ]
    if not rows:
        return 0
    stmt = insert(AdmsAttendanceLog).values(rows)
    update_cols = {
        c.name: getattr(stmt.excluded, c.name)
        for c in AdmsAttendanceLog.__table__.columns
        if c.name not in ("id", "event_id")
    }
    stmt = stmt.on_conflict_do_update(index_elements=["event_id"], set_=update_cols)
    await db.execute(stmt)
    return len(rows)


async def sync_once(*, max_batches: int = 50) -> dict[str, Any]:
    """Pull new punches from ADMS into Postgres. Safe to call concurrently (locked)."""
    settings = get_settings()
    async with _lock:
        inserted = 0
        batches = 0
        after_id = 0
        total_remote = None
        error: str | None = None
        try:
            async with SessionLocal() as db:
                after_id = await _get_cursor(db)
                async with httpx.AsyncClient(timeout=30.0, auth=_auth()) as client:
                    for _ in range(max_batches):
                        r = await client.get(
                            f"{settings.adms_base_url}/api/attendance/export",
                            params={"after_id": after_id, "limit": settings.adms_sync_batch_size},
                        )
                        if not r.is_success:
                            error = r.text[:300]
                            break
                        payload = r.json()
                        items = payload.get("items") or []
                        total_remote = payload.get("total")
                        if not items:
                            break
                        n = await _upsert_items(db, items)
                        inserted += n
                        after_id = int(payload.get("next_after_id") or items[-1].get("id") or after_id)
                        await _set_cursor(db, after_id)
                        await db.commit()
                        batches += 1
                        if not payload.get("has_more"):
                            break
                    # Refresh firebase_status for recent rows without advancing cursor
                    try:
                        rr = await client.get(
                            f"{settings.adms_base_url}/api/attendance",
                            params={"limit": 200},
                        )
                        if rr.is_success:
                            recent = rr.json()
                            if isinstance(recent, list) and recent:
                                await _upsert_items(db, recent)
                                await db.commit()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("ADMS recent refresh failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            logger.exception("ADMS sync failed")

        pg_total = 0
        try:
            async with SessionLocal() as db:
                pg_total = (await db.execute(select(func.count()).select_from(AdmsAttendanceLog))).scalar() or 0
        except Exception:  # noqa: BLE001
            pass

        return {
            "ok": error is None,
            "error": error,
            "upserted": inserted,
            "batches": batches,
            "after_id": after_id,
            "remote_total": total_remote,
            "pg_total": pg_total,
        }


_offline_alerted: set[str] = set()


async def _check_offline_alert() -> None:
    """Log / webhook once when gateway is up but device stays offline past threshold."""
    settings = get_settings()
    threshold = max(60, int(settings.adms_offline_alert_seconds))
    try:
        async with httpx.AsyncClient(timeout=5.0, auth=_auth()) as client:
            hr = await client.get(f"{settings.adms_base_url}/health")
            if not hr.is_success:
                return
            dr = await client.get(f"{settings.adms_base_url}/api/devices")
            if not dr.is_success:
                return
            body = dr.json()
            rows = body if isinstance(body, list) else []
    except Exception as exc:  # noqa: BLE001
        logger.debug("ADMS offline check skipped: %s", exc)
        return

    now = datetime.now(timezone.utc)
    for d in rows:
        serial = str(d.get("serial_number") or "")
        if not serial or serial.upper() == "TEST" or d.get("status") == "disabled":
            continue
        if d.get("online"):
            _offline_alerted.discard(serial)
            continue
        hb = d.get("last_heartbeat_at") or ""
        age_s = d.get("seconds_since_last_seen")
        if age_s is None and hb:
            try:
                ts = datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_s = int((now - ts).total_seconds())
            except ValueError:
                continue
        if age_s is None or int(age_s) < threshold:
            continue
        if serial in _offline_alerted:
            continue
        _offline_alerted.add(serial)
        hint = str(d.get("recovery_hint") or "").strip() or (
            "Toggle Cloud/ADMS on panel or power-cycle; confirm http://103.176.85.102:8081"
        )
        logger.warning(
            "ADMS_DEVICE_SILENT serial=%s offline_for=%ss needs_onsite=%s — %s",
            serial,
            age_s,
            bool(d.get("needs_onsite")),
            hint,
        )
        url = (settings.adms_offline_alert_webhook_url or "").strip()
        if not url:
            continue
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                await client.post(
                    url,
                    json={
                        "event": "crm.adms_device_silent",
                        "serial": serial,
                        "age_seconds": age_s,
                        "last_heartbeat_at": hb,
                        "recovery": hint,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ADMS offline webhook failed: %s", exc)


async def _loop() -> None:
    settings = get_settings()
    # Drain backlog quickly on boot
    while not _stop.is_set():
        result = await sync_once(max_batches=40)
        if result.get("error"):
            logger.warning("ADMS sync: %s", result["error"])
        elif result.get("upserted"):
            logger.info(
                "ADMS sync upserted=%s after_id=%s pg_total=%s",
                result["upserted"],
                result["after_id"],
                result["pg_total"],
            )
        # Offline watch shares the sync cadence (no extra process)
        try:
            await _check_offline_alert()
        except Exception as exc:  # noqa: BLE001
            logger.debug("ADMS offline watch error: %s", exc)
        # If still catching up, loop immediately
        remote = result.get("remote_total")
        pg = result.get("pg_total") or 0
        if remote is not None and pg < int(remote) and not result.get("error"):
            await asyncio.sleep(0.5)
            continue
        try:
            await asyncio.wait_for(_stop.wait(), timeout=max(5, settings.adms_sync_interval_seconds))
        except asyncio.TimeoutError:
            pass


def start_background_sync() -> None:
    global _task
    settings = get_settings()
    if not settings.adms_sync_enabled:
        logger.info("ADMS sync disabled")
        return
    if _task and not _task.done():
        return
    _stop.clear()
    _task = asyncio.create_task(_loop())
    logger.info("ADMS → Postgres sync started (interval=%ss)", settings.adms_sync_interval_seconds)


async def stop_background_sync() -> None:
    global _task
    _stop.set()
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
