from __future__ import annotations

import asyncio
import os
import platform
import time
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user
from app.models import User

router = APIRouter(prefix="/api/admin/vps-health", tags=["vps-health"])

CurrentUser = Annotated[User, Depends(get_current_user)]

HOST_AGENT_SOCK = os.environ.get("VPS_HOST_AGENT_SOCK", "/var/run/yesweigh-vps-agent.sock")
HOST_AGENT_URL = os.environ.get("VPS_HOST_AGENT_URL", "http://host.docker.internal:8199")

# Avoid re-hitting the host agent (docker stats / du) on every UI poll.
_AGENT_APPS_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
_AGENT_APPS_TTL_S = 20.0
_AGENT_APPS_STALE_S = 90.0
_AGENT_REFRESHING = False
_CPU_SAMPLE: dict[str, float | int] = {"idle": 0, "total": 0, "at": 0.0}


def _require_admin(user: User) -> None:
    if user.role.lower() not in {"admin", "super_admin", "super admin", "management"}:
        raise HTTPException(403, "Administrator access required")


def _agent_client(timeout: float) -> httpx.AsyncClient:
    if Path(HOST_AGENT_SOCK).exists():
        transport = httpx.AsyncHTTPTransport(uds=HOST_AGENT_SOCK)
        return httpx.AsyncClient(transport=transport, base_url="http://vps-agent", timeout=timeout)
    return httpx.AsyncClient(base_url=HOST_AGENT_URL, timeout=timeout)


async def _agent_get(path: str, timeout: float = 12.0) -> dict[str, Any]:
    try:
        async with _agent_client(timeout) as client:
            r = await client.get(path)
            r.raise_for_status()
            return r.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


async def _agent_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        async with _agent_client(180.0) as client:
            r = await client.post(path, json=payload)
            if r.status_code >= 400:
                detail = r.text
                try:
                    detail = r.json().get("error") or detail
                except Exception:  # noqa: BLE001
                    pass
                raise HTTPException(502, f"Host agent error: {detail}")
            return r.json()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Host agent unreachable: {exc}") from exc


class ClamAvIn(BaseModel):
    enabled: bool


def _read_cpu_times() -> tuple[int, int]:
    line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    parts = [int(x) for x in line.split()[1:8]]
    idle = parts[3] + parts[4]
    total = sum(parts)
    return idle, total


async def _cpu_percent() -> float:
    """Delta since last sample (no blocking sleep on steady polls)."""
    idle, total = _read_cpu_times()
    prev_at = float(_CPU_SAMPLE["at"])
    prev_idle = int(_CPU_SAMPLE["idle"])
    prev_total = int(_CPU_SAMPLE["total"])
    pct = 0.0
    if prev_at > 0 and total > prev_total:
        dt = total - prev_total
        di = idle - prev_idle
        if dt > 0:
            pct = round(max(0.0, min(100.0, 100.0 * (1.0 - di / dt))), 1)
    else:
        await asyncio.sleep(0.08)
        idle2, total2 = _read_cpu_times()
        dt = total2 - total
        di = idle2 - idle
        idle, total = idle2, total2
        if dt > 0:
            pct = round(max(0.0, min(100.0, 100.0 * (1.0 - di / dt))), 1)
    _CPU_SAMPLE["idle"] = idle
    _CPU_SAMPLE["total"] = total
    _CPU_SAMPLE["at"] = time.time()
    return pct


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        bits = rest.strip().split()
        if not bits:
            continue
        try:
            out[key] = int(bits[0]) * 1024
        except ValueError:
            continue
    return out


def _loadavg() -> dict[str, float | int]:
    raw = Path("/proc/loadavg").read_text(encoding="utf-8").strip().split()
    return {
        "load_1": float(raw[0]),
        "load_5": float(raw[1]),
        "load_15": float(raw[2]),
        "running": int(raw[3].split("/")[0]),
        "threads": int(raw[3].split("/")[1]),
    }


def _uptime_seconds() -> float:
    return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "Unknown"


def _hostname() -> str:
    for path in (Path("/etc/hostname"), Path("/host/hostname")):
        try:
            name = path.read_text(encoding="utf-8").strip()
            if name:
                return name
        except OSError:
            continue
    return platform.node() or "unknown"


def _disk(path: str = "/") -> dict[str, Any]:
    st = os.statvfs(path)
    total = st.f_frsize * st.f_blocks
    free = st.f_frsize * st.f_bavail
    used = total - free
    pct = round(100.0 * used / total, 1) if total else 0.0
    return {
        "path": path,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "percent": pct,
    }


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    mins, _ = divmod(s, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return " ".join(parts)


async def _fetch_agent_apps() -> dict[str, Any]:
    global _AGENT_REFRESHING
    data = await _agent_get("/apps", timeout=12.0)
    if data.get("ok"):
        _AGENT_APPS_CACHE["at"] = time.time()
        _AGENT_APPS_CACHE["data"] = data
    _AGENT_REFRESHING = False
    return data


async def _agent_apps_cached() -> dict[str, Any]:
    """Return cached app split quickly; refresh in background when stale."""
    global _AGENT_REFRESHING
    now = time.time()
    cached = _AGENT_APPS_CACHE["data"]
    age = now - float(_AGENT_APPS_CACHE["at"]) if cached else 9999.0
    if cached and age < _AGENT_APPS_TTL_S:
        return cached
    if cached and age < _AGENT_APPS_STALE_S:
        if not _AGENT_REFRESHING:
            _AGENT_REFRESHING = True
            asyncio.create_task(_fetch_agent_apps())
        return cached
    return await _fetch_agent_apps()


@router.get("")
async def vps_health(_user: CurrentUser) -> dict[str, Any]:
    _require_admin(_user)
    mem = _meminfo()
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", mem.get("MemFree", 0))
    used = max(0, total - available)
    mem_pct = round(100.0 * used / total, 1) if total else 0.0
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)
    load = _loadavg()
    uptime = _uptime_seconds()
    disk = _disk("/")
    cores = os.cpu_count() or 1
    # Host /proc metrics + agent apps in parallel (agent usually cached).
    cpu_pct, agent = await asyncio.gather(_cpu_percent(), _agent_apps_cached())
    return {
        "ok": True,
        "collected_at": time.time(),
        "hostname": _hostname(),
        "os": f"{platform.system()} {platform.release()}",
        "cpu": {
            "percent": cpu_pct,
            "cores": cores,
            "model": _cpu_model(),
            "load_1": load["load_1"],
            "load_5": load["load_5"],
            "load_15": load["load_15"],
        },
        "memory": {
            "total_bytes": total,
            "used_bytes": used,
            "available_bytes": available,
            "percent": mem_pct,
            "swap_total_bytes": swap_total,
            "swap_used_bytes": swap_used,
        },
        "storage": disk,
        "uptime_seconds": uptime,
        "uptime_human": _fmt_uptime(uptime),
        "processes": load["threads"],
        "apps": agent.get("apps") if agent.get("ok") else [],
        "clamav": agent.get("clamav") if agent.get("ok") else {"enabled": None, "error": agent.get("error")},
        "agent_ok": bool(agent.get("ok")),
        "poll_hint_ms": 12000,
    }


@router.get("/clamav")
async def clamav_get(_user: CurrentUser) -> dict[str, Any]:
    _require_admin(_user)
    data = await _agent_get("/clamav", timeout=8.0)
    if not data.get("ok", True) and "error" in data and "enabled" not in data:
        raise HTTPException(502, data.get("error") or "Host agent error")
    return data


@router.post("/clamav")
async def clamav_set(body: ClamAvIn, _user: CurrentUser) -> dict[str, Any]:
    _require_admin(_user)
    # Invalidate apps cache so next poll picks up ClamAV mem change.
    _AGENT_APPS_CACHE["at"] = 0.0
    _AGENT_APPS_CACHE["data"] = None
    return await _agent_post("/clamav", {"enabled": body.enabled})
