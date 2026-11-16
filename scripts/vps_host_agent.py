#!/usr/bin/env python3
"""Localhost-only host agent for CRM VPS Health (app split + ClamAV toggle)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

HOST = "127.0.0.1"
PORT = 8199
SOCK = Path("/var/run/yesweigh-vps-agent.sock")
MAILCOW_DIR = Path("/opt/mailcow-dockerized")
MAILCOW_CONF = MAILCOW_DIR / "mailcow.conf"
CLAMD_NAME = "mailcowdockerized-clamd-mailcow-1"

# Fresh enough for the UI; avoid re-running docker stats on every CRM poll.
APPS_TTL_S = 25.0
APPS_STALE_S = 120.0
DISK_TTL_S = 120.0
CLAM_TTL_S = 20.0

APP_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("Mailcow", re.compile(r"^mailcowdockerized-", re.I)),
    ("Evolution API", re.compile(r"^evolution-go", re.I)),
    ("YesWeigh CRM", re.compile(r"^yesweigh-crm-", re.I)),
    ("Caddy", re.compile(r"^caddy$", re.I)),
]

DISK_SOURCES: dict[str, list[str]] = {
    "Mailcow": ["/opt/mailcow-dockerized"],
    "Evolution API": [
        "/opt/evolution-go",
        "/var/lib/docker/volumes/evolution-go_evogo_pgdata/_data",
        "/var/lib/docker/volumes/evolution-go_evogo_logs/_data",
        "/var/lib/docker/volumes/evolution-go_evogo_data/_data",
    ],
    "YesWeigh CRM": [
        "/opt/yesweigh-crm",
        "/var/lib/docker/volumes/yesweigh-crm_wa_media/_data",
        "/var/lib/docker/volumes/yesweigh-crm_employee_docs/_data",
    ],
    "ADMS": ["/opt/adms"],
    "Caddy": ["/opt/caddy", "/etc/caddy"],
}


def _run(cmd: list[str], timeout: float = 20) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return (p.stdout or "") + (p.stderr or "" if p.returncode else "")
    except Exception as exc:  # noqa: BLE001
        return f"ERR:{exc}"


def _du_bytes(path: str) -> int:
    if not Path(path).exists():
        return 0
    out = _run(["du", "-sb", path], timeout=60)
    try:
        return int(out.strip().split()[0])
    except (ValueError, IndexError):
        return 0


def _parse_mem(s: str) -> int:
    s = (s or "").strip()
    if not s or s == "0B":
        return 0
    m = re.match(r"^([\d.]+)([KMGT]?i?B)$", s.replace(" ", ""))
    if not m:
        return 0
    n = float(m.group(1))
    unit = m.group(2)
    mult = {
        "B": 1,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "TiB": 1024**4,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
    }.get(unit, 1)
    return int(n * mult)


def _parse_cpu(s: str) -> float:
    try:
        return float((s or "0").replace("%", "").strip() or 0)
    except ValueError:
        return 0.0


def _classify(name: str) -> str:
    for label, pat in APP_RULES:
        if pat.search(name):
            return label
    return "Other"


def _docker_stats() -> list[dict[str, Any]]:
    out = _run(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}",
        ],
        timeout=25,
    )
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        if "\t" not in line or line.startswith("ERR:"):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name, cpu, mem_usage, mem_pct = parts[0], parts[1], parts[2], parts[3]
        used_s = mem_usage.split("/")[0].strip() if "/" in mem_usage else mem_usage
        limit_s = mem_usage.split("/")[1].strip() if "/" in mem_usage else ""
        rows.append(
            {
                "name": name,
                "app": _classify(name),
                "cpu_percent": _parse_cpu(cpu),
                "mem_bytes": _parse_mem(used_s),
                "mem_limit_bytes": _parse_mem(limit_s),
                "mem_percent": _parse_cpu(mem_pct),
            }
        )
    return rows


def _adms_stats() -> dict[str, Any] | None:
    pgrep = _run(["pgrep", "-af", "adms|run_prod.py"], timeout=5)
    pid = None
    for line in pgrep.splitlines():
        if "run_prod.py" in line or "/opt/adms/" in line:
            pid = line.strip().split()[0]
            break
    if not pid:
        return None
    st = _run(["ps", "-o", "pcpu=,rss=", "-p", pid], timeout=5).strip()
    if not st:
        return None
    bits = st.split()
    if len(bits) < 2:
        return None
    try:
        cpu = float(bits[0])
        rss_kb = int(bits[1])
    except ValueError:
        return None
    return {
        "name": "adms.service",
        "app": "ADMS",
        "cpu_percent": cpu,
        "mem_bytes": rss_kb * 1024,
        "mem_limit_bytes": 0,
        "mem_percent": 0.0,
    }


def _disk_by_app() -> dict[str, int]:
    out: dict[str, int] = {}
    for app, paths in DISK_SOURCES.items():
        total = 0
        for p in paths:
            total += _du_bytes(p)
        out[app] = total
    # Add mailcow docker volumes roughly via docker system (best effort)
    vol_out = _run(["docker", "volume", "ls", "-q"], timeout=10)
    for vol in vol_out.splitlines():
        vol = vol.strip()
        if not vol:
            continue
        app = None
        if "mailcow" in vol.lower():
            app = "Mailcow"
        elif vol.startswith("evolution-go_"):
            app = "Evolution API"
        elif vol.startswith("yesweigh-crm_"):
            app = "YesWeigh CRM"
        if not app:
            continue
        mp = _run(["docker", "volume", "inspect", vol, "--format", "{{.Mountpoint}}"], timeout=5).strip()
        if not mp or mp.startswith("ERR:"):
            continue
        already = any(mp.rstrip("/") == Path(p).as_posix().rstrip("/") for p in DISK_SOURCES.get(app, []))
        if already:
            continue
        if app == "Mailcow":
            out[app] = out.get(app, 0) + _du_bytes(mp)
    return out


def _read_skip_clamd() -> bool:
    if not MAILCOW_CONF.exists():
        return False
    for line in MAILCOW_CONF.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.strip().startswith("SKIP_CLAMD="):
            val = line.split("=", 1)[1].strip().lower()
            return val in {"y", "yes", "true", "1"}
    return False


def _set_skip_clamd(skip: bool) -> None:
    if not MAILCOW_CONF.exists():
        raise FileNotFoundError(str(MAILCOW_CONF))
    text = MAILCOW_CONF.read_text(encoding="utf-8", errors="ignore")
    new_val = "y" if skip else "n"
    if re.search(r"^SKIP_CLAMD=.*$", text, flags=re.M):
        text = re.sub(r"^SKIP_CLAMD=.*$", f"SKIP_CLAMD={new_val}", text, flags=re.M)
    else:
        text = text.rstrip() + f"\nSKIP_CLAMD={new_val}\n"
    MAILCOW_CONF.write_text(text, encoding="utf-8")


def _clamd_running() -> bool:
    out = _run(["docker", "inspect", "-f", "{{.State.Running}}", CLAMD_NAME], timeout=8).strip()
    return out == "true"


def _clamd_mem_from_cgroup() -> int:
    """Cheap RAM read without `docker stats` (which samples ~1s)."""
    out = _run(
        ["docker", "inspect", "-f", "{{.Id}} {{.State.Pid}} {{.HostConfig.CgroupParent}}", CLAMD_NAME],
        timeout=5,
    ).strip()
    if not out or out.startswith("ERR:"):
        return 0
    cid = out.split()[0] if out.split() else ""
    candidates = [
        f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/memory.current",
        f"/sys/fs/cgroup/memory/docker/{cid}/memory.usage_in_bytes",
        f"/sys/fs/cgroup/docker/{cid}/memory.current",
    ]
    for path in candidates:
        try:
            return int(Path(path).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
    return 0


def clamav_status(containers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    skip = _read_skip_clamd()
    enabled = not skip
    mem = 0
    running = False
    if containers is not None:
        for c in containers:
            name = str(c.get("name") or "")
            if name == CLAMD_NAME or "clamd-mailcow" in name:
                running = True
                mem = int(c.get("mem_bytes") or 0)
                break
    else:
        running = _clamd_running()
        if running:
            mem = _clamd_mem_from_cgroup()
    scanning = enabled and running and mem > 50 * 1024 * 1024
    return {
        "ok": True,
        "enabled": enabled,
        "skip_clamd": skip,
        "container_running": running,
        "scanning": scanning,
        "mem_bytes": mem,
        "approx_save_bytes": 1200 * 1024 * 1024,
    }


def set_clamav(enabled: bool) -> dict[str, Any]:
    skip = not enabled
    _set_skip_clamd(skip)
    try:
        p = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(MAILCOW_CONF),
                "up",
                "-d",
                "--force-recreate",
                "clamd-mailcow",
            ],
            cwd=str(MAILCOW_DIR),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "compose failed").strip()[:500])
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(exc)) from exc
    time.sleep(3)
    with _cache_lock:
        _CLAM_CACHE["at"] = 0.0
        _CLAM_CACHE["data"] = None
        _APPS_CACHE["at"] = 0.0
    return clamav_status()


_cache_lock = threading.Lock()
_APPS_CACHE: dict[str, Any] = {"at": 0.0, "data": None, "refreshing": False}
_DISK_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
_CLAM_CACHE: dict[str, Any] = {"at": 0.0, "data": None}


def _clamav_cached(containers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    now = time.time()
    with _cache_lock:
        if _CLAM_CACHE["data"] and now - float(_CLAM_CACHE["at"]) < CLAM_TTL_S:
            return dict(_CLAM_CACHE["data"])
    data = clamav_status(containers)
    with _cache_lock:
        _CLAM_CACHE["at"] = now
        _CLAM_CACHE["data"] = data
    return data


def _collect_apps_fresh() -> dict[str, Any]:
    now = time.time()
    containers = _docker_stats()
    adms = _adms_stats()
    if adms:
        containers.append(adms)
    with _cache_lock:
        disk_age = now - float(_DISK_CACHE["at"]) if _DISK_CACHE["data"] else 9999.0
        disk = _DISK_CACHE["data"] if _DISK_CACHE["data"] and disk_age < DISK_TTL_S else None
    if disk is None:
        disk = _disk_by_app()
        with _cache_lock:
            _DISK_CACHE["at"] = now
            _DISK_CACHE["data"] = disk
    apps: dict[str, dict[str, Any]] = {}
    for row in containers:
        app = row["app"]
        slot = apps.setdefault(
            app,
            {
                "name": app,
                "cpu_percent": 0.0,
                "mem_bytes": 0,
                "disk_bytes": disk.get(app, 0),
                "containers": [],
            },
        )
        slot["cpu_percent"] = round(slot["cpu_percent"] + float(row["cpu_percent"]), 2)
        slot["mem_bytes"] += int(row["mem_bytes"])
        slot["containers"].append(
            {
                "name": row["name"],
                "cpu_percent": row["cpu_percent"],
                "mem_bytes": row["mem_bytes"],
            }
        )
    for app, bytes_ in disk.items():
        if app not in apps:
            apps[app] = {
                "name": app,
                "cpu_percent": 0.0,
                "mem_bytes": 0,
                "disk_bytes": bytes_,
                "containers": [],
            }
        else:
            apps[app]["disk_bytes"] = bytes_
    items = sorted(apps.values(), key=lambda x: x["mem_bytes"], reverse=True)
    clam = clamav_status(containers)
    payload = {
        "ok": True,
        "collected_at": now,
        "apps": items,
        "clamav": clam,
    }
    with _cache_lock:
        _APPS_CACHE["at"] = now
        _APPS_CACHE["data"] = payload
        _CLAM_CACHE["at"] = now
        _CLAM_CACHE["data"] = clam
        _APPS_CACHE["refreshing"] = False
    return payload


def _kick_apps_refresh() -> None:
    with _cache_lock:
        if _APPS_CACHE["refreshing"]:
            return
        _APPS_CACHE["refreshing"] = True

    def _run_refresh() -> None:
        try:
            _collect_apps_fresh()
        except Exception:  # noqa: BLE001
            with _cache_lock:
                _APPS_CACHE["refreshing"] = False

    threading.Thread(target=_run_refresh, daemon=True, name="apps-refresh").start()


def collect_apps() -> dict[str, Any]:
    now = time.time()
    with _cache_lock:
        cached = _APPS_CACHE["data"]
        age = now - float(_APPS_CACHE["at"]) if cached else 9999.0
    if cached and age < APPS_TTL_S:
        out = dict(cached)
        out["clamav"] = _clamav_cached()
        return out
    if cached and age < APPS_STALE_S:
        # Serve last good snapshot immediately; refresh docker stats in background.
        _kick_apps_refresh()
        out = dict(cached)
        out["clamav"] = _clamav_cached()
        out["stale"] = True
        return out
    return _collect_apps_fresh()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        return

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/health", "/"):
            self._json(200, {"ok": True})
            return
        if path == "/apps":
            self._json(200, collect_apps())
            return
        if path == "/clamav":
            self._json(200, _clamav_cached())
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "invalid json"})
            return
        if path == "/clamav":
            enabled = bool(data.get("enabled"))
            try:
                status = set_clamav(enabled)
                self._json(200, {"ok": True, **status})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"ok": False, "error": str(exc)})
            return
        self._json(404, {"ok": False, "error": "not found"})


def main() -> None:
    import socket

    class _UnixHTTPServer(ThreadingHTTPServer):
        address_family = socket.AF_UNIX

        def server_bind(self) -> None:
            path = self.server_address
            if isinstance(path, str) and os.path.exists(path):
                os.unlink(path)
            ThreadingHTTPServer.server_bind(self)
            try:
                os.chmod(self.server_address, 0o666)  # type: ignore[arg-type]
            except OSError:
                pass

    tcp = ThreadingHTTPServer((HOST, PORT), Handler)
    userver = _UnixHTTPServer(SOCK.as_posix(), Handler)
    threading.Thread(target=tcp.serve_forever, daemon=True, name="tcp-agent").start()
    print(f"vps_host_agent tcp http://{HOST}:{PORT}", flush=True)
    print(f"vps_host_agent unix {SOCK}", flush=True)
    userver.serve_forever()


if __name__ == "__main__":
    main()
