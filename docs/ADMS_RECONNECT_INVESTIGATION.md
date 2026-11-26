# ADMS reconnect investigation — X305 Pro vs CRM deploys

**Date:** 2026-11-25  
**Device:** ZKTeco X305 Pro/ID, FW `6.6.1 Build 41`, SN `0IN7010596123100370`  
**VPS:** `103.176.85.102` (SSH `22318`), ADMS host systemd `adms.service` on **`:8081`**  
**Focus window:** last poll reported **2026-11-24 17:55 IST ≈ 12:25 UTC**

---

## 1) Correlation table (~17:55 IST / 12:25 UTC 24 Nov)

Times are **UTC** unless marked IST. Evidence from `journalctl`, `comm_logs`, `device_commands`, docker journal.

| Time (UTC) | IST | Event | Deploy / outage? | Evidence |
|---|---|---|---|---|
| Nov 15 16:24 | — | Last VPS reboot | **No** reboot on 24 Nov | `who -b`, `last reboot`, boot id `710d78ad…` still running |
| Nov 24 04:11:36 | 09:41 | Device reconnect after earlier ADMS `service_restart` | ADMS restart earlier same day; CRM not causal | `connection_events` reconnect `after=service_restart`; polls resume |
| Nov 24 04:11:37 | 09:41 | `QUERY_ATTLOG` sent (cmd 503) | Command **timeout** (never ACKed) | `device_commands` id 503 `expired-unacked`; **polls continued** for hours |
| Nov 24 08:30–09:07 | 14:00–14:37 | **Frequent CRM** `docker compose` recreate (api/web/migrate) | **Yes** CRM deploys | docker journal `sbJoin … yesweigh-crm-api/web` |
| Nov 24 09:07–09:40:23 | 14:37–15:10 | Device keeps polling (~30s) through/after CRM churn | CRM **did not** stop `:8081` | `comm_logs` 53 polls `09:07:19`→`09:40:23`, duration 1–3 ms |
| Nov 24 **09:40:23** | **15:10** | **Last healthy getrequest before primary silence** | **No** ADMS stop; **No** VPS reboot | `comm_logs` poll 200; next event is watchdog |
| Nov 24 09:42:49 | 15:12 | Watchdog `heartbeat_timeout`, `tcp_closed=1` | ADMS still up | journal `released session … heartbeat_timeout`; PID `2400753` continuous |
| Nov 24 09:44–09:59 | 15:14–15:29 | Reconnect nudges → `DEVICE_SILENT` | Server cannot wake device | `tcp_closed=0` on nudges; alert at 09:59 |
| Nov 24 12:11–12:16 | 17:41–17:46 | More CRM compose recreates | **Yes** CRM deploy activity | docker `yesweigh-crm-*` sbJoin; ADMS health GETs continue |
| Nov 24 **12:25:47** | **17:55** | **Brief device reconnect** (user “last poll” time) | ADMS answering 200 | handshake `options=all` + INFO `Ver 6.6.1(build 41),…,192.168.1.201` |
| Nov 24 12:25:49 | 17:55 | `QUERY_ATTLOG` sent (cmd 504) | Recovery enqueue | `comm_logs` category=command; cmd 504 later `timeout` |
| Nov 24 **12:27:50** | **17:57** | Silence again (`heartbeat_timeout`, `tcp_closed=0`) | CRM recreate at ~12:27 **coincides** but ADMS never restarted | journal session release; no `Stopped ADMS` |
| Nov 24 12:44 | 18:14 | `DEVICE_SILENT` again | On-site required | watchdog alert |
| Nov 24 **15:07:11** | 20:37 | **Only ADMS restart on 24 Nov after silence** | Manual / `deploy_adms_only` — **after** outage | `systemd Stopped/Started ADMS`; new PID `3628188` |
| Nov 24 15:07–Nov 25 03:47 | overnight | Silent; CRM deploys continue; `:8081` healthy | CRM ≠ ADMS outage | continuous reconnect nudges `tcp_closed=0` |
| Nov 25 **03:47:25** | **09:17** | Device **self-recovered / onsite** reconnect | ADMS untouched since 15:07 | handshake + ATTLOG uploads; Online |

### Verdict on user theory (“CRM restarts leave old ADMS FW stuck”)

| Claim | Result |
|---|---|
| CRM deploy restarts `adms.service` | **Refuted** — `deploy_crm_ai.sh` never touches ADMS; only ADMS restart 24 Nov was **15:07 UTC**, hours after silence |
| CRM deploy blocks HTTP `:8081` | **Refuted** — polls 1–3 ms through CRM churn; health GETs during silent window; host process independent of compose |
| Frequent CRM deploys *correlated* with busy VPS | **Confirmed noise** — many recreates 08:30–13:00 UTC, but device stayed online until **09:40** with clean OK polls |
| Hours-long silence is device/firmware or office path | **Confirmed primary** — pattern matches prior Nov 21/22: healthy polls → sudden stop → nudges with `tcp_closed=0` |

**Root-cause confidence**

1. **Device/site stopped initiating Push HTTP** — **high** (primary).  
2. CRM restart of ADMS / blocking `:8081` — **ruled out** for this outage.  
3. `QUERY_ATTLOG` as sole hang trigger — **low–medium** (sent at 12:25 then drop; but same command at 04:11 and device stayed online hours). Live already has `RECONNECT_ATTLOG_RECOVERY=false`.  
4. VPS reboot / Caddy / Mailcow / Firebase — **ruled out** (no reboot; Firebase off; Caddy does not own `:8081`).

---

## 2) Does CRM deploy restart ADMS or block its HTTP?

**No.**

- Script header and footer: “does NOT restart ADMS/Mailcow/Evolution”.
- Remote actions: rsync CRM tree, optional Caddy CRM block for `crm.yesweigh.in` → `8100/8101`, `docker compose` for `migrate/api/web` only.
- ADMS is **host** `adms.service` listening `0.0.0.0:8081` (not a compose service).
- CRM binds `127.0.0.1:8100` / `8101` only.

---

## 3) ADMS independence

| Dependency | Relation |
|---|---|
| CRM docker compose | Independent; CRM *reads* ADMS via `http://127.0.0.1:8081` |
| Caddy | Serves CRM HTTPS; **not** the device Push path (`http://103.176.85.102:8081`) |
| Firebase | `FIREBASE_ENABLED=false` |
| Postgres `127.0.0.1:5433/adms` (`gatc-postgres`) | Optional durable mirror on ATTLOG save; pool `command_timeout=10` |
| Mailcow / Evolution | Untouched; different ports |

---

## 4) Why X305 may fail to reconnect after downtime

Push is **device-initiated**. After NAT/idle/firmware stall:

- Server watchdog releases session after 120s and RSTs peer sockets when present.
- When there are **no** ESTABLISHED sockets (`tcp_closed=0`), nudges cannot wake the unit.
- Firmware often stops retrying after ErrorDelay window for multi-hour gaps → needs **panel Cloud/ADMS toggle** (or power-cycle — ops only; not done by server).
- Server responses in this incident were **200** with correct handshake; failure mode is **no further client GETs**.

---

## 5) Handshake / intervals (code + last INFO)

From live `/health` and `protocol.handshake_response`:

| Param | Value |
|---|---|
| Delay | 15 |
| ErrorDelay | 30 |
| TransInterval | 1 |
| Realtime | 1 |
| TransTimes | `00:00;06:00;12:00;18:00` |
| TransFlag | `1011000000` (OpLog off) |

Last INFO (12:25 and 03:47 reconnects):

`Ver 6.6.1(build 41),24,45,<user count>,192.168.1.201,10,-1,0,0,100`  
→ LAN `192.168.1.201`, WAN peer historically `111.92.117.206`.

Poll latency: getrequest **avg ~2.7 ms** (max 75 ms) on 24 Nov — not DB-blocked.

---

## 6) Fast legacy `/iclock` path

- `getrequest` / handshake: SQLite touch + optional command dequeue; returns `OK` or `C:<id>:…` quickly.
- ATTLOG: SQLite insert then Postgres upsert before ACK (durable). Recent ATTLOG 58–253 ms observed; Postgres timeout 10s.
- Firebase never on request path.
- Live: `RECONNECT_ATTLOG_RECOVERY=false` so reconnect does not enqueue `QUERY_ATTLOG` (X305 never ACKs it).

---

## 7) Safe hardening applied (evidence-backed; no risky live ADMS restart)

| Change | Where | Why |
|---|---|---|
| Post-deploy ADMS health verify (warn only) | `yesweigh-crm/scripts/deploy_crm_ai.sh` | Prove CRM did not kill `:8081`; never `systemctl` ADMS |
| Default `reconnect_attlog_recovery=False` | `adms` `config.py` + `.env.example` | Align with live `.env` and unacked QUERY_ATTLOG evidence |
| This document | `docs/ADMS_RECONNECT_INVESTIGATION.md` | Timeline + procedure |

**Not done (intentionally):** ADMS process restart, Free443, exposing 4370, deleting attendance, power-cycle device, speculative postgres schema changes.

**Durable storage check (read-only):** SQLite `integrity_check=ok`, `journal_mode=wal`. New punches since 2026-11-24: SQLite 13 ≈ Postgres 13. Historical Postgres total **95** vs SQLite **~6813** → older rows never fully backfilled (follow-up, not outage cause). **No** `/opt/adms/data/backups` files found — recommend scheduled SQLite backup (separate change).

---

## 8) Simulated downtime test plan (≠ hardware)

1. From VPS: `curl -sS http://127.0.0.1:8081/health | jq .handshake,.devices`  
2. Simulate silent peer: stop generating requests; confirm Offline after 120s and `DEVICE_SILENT` after ~300s.  
3. Simulate CRM deploy: run `deploy_crm_ai.sh`; assert `:8081` health still `ok` and `systemctl show adms -p ActiveEnterTimestamp` unchanged.  
4. Optional HTTP client script posting ATTLOG + getrequest — validates server; **does not** prove X305 FW reconnect.  
5. Hardware-only: office Cloud toggle while capturing `journalctl -u adms -f` and `comm_logs`.

---

## 9) CRM update procedure that never interrupts ADMS

1. Use **only** `scripts/deploy_crm_ai.sh` for CRM/AI.  
2. Never `systemctl restart adms` from CRM workflows; use `scripts/deploy_adms_only.sh` only for ADMS code.  
3. Do not edit host Caddy beyond the CRM snippet (`crm.yesweigh.in` → 8100/8101).  
4. After deploy, confirm script prints `ADMS :8081 health OK`.  
5. Avoid overlapping parallel CRM deploys (flock already enforced).  
6. If device Offline, treat as **onsite** — do not cascade-restart ADMS/Mailcow “to fix badge”.

---

## 10) Separate ADMS VPS vs separate systemd only

| Option | Assessment |
|---|---|
| **Current: separate systemd on same VPS** | Sufficient for independence from CRM compose. Evidence: CRM churn did not stop `:8081`. **Keep.** |
| Separate ADMS VPS | Optional blast-radius reduction (CPU during `docker compose build --no-cache`, shared NIC). **Not required** to explain 24 Nov silence; cost/ops tradeoff. |
| Put ADMS inside CRM compose | **Avoid** — couples attendance to CRM restarts (the failure mode the theory feared). |

---

## 11) Permanent fix / rollback

**Permanent (ops + software already largely in place):**

1. Keep ADMS on host systemd `:8081`; CRM deploy guard.  
2. Keep OpStamp fix / TransFlag OpLog-off / watchdog / silent escalate.  
3. Keep `RECONNECT_ATTLOG_RECOVERY=false` on X305.  
4. Onsite checklist when `needs_onsite` / `DEVICE_SILENT`: Cloud toggle on panel; confirm reachability to `http://103.176.85.102:8081`.  
5. Optional later: SQLite backup cron; one-shot historical Postgres backfill; reduce CRM deploy frequency during office punch hours.

**Rollback of this investigation’s code tweaks:**

- Revert `deploy_crm_ai.sh` ADMS health block.  
- Revert `reconnect_attlog_recovery` default / `.env.example` if a site needs recovery enqueue (`RECONNECT_ATTLOG_RECOVERY=true` in `/opt/adms/.env`).  
- Live VPS already had recovery **false** — no live ADMS restart was performed for this write-up.

---

## Current status (investigation time)

Device **Online** again from **2026-11-25 03:47:25 UTC** (09:17 IST) with ATTLOG uploads and ~30s getrequest cadence. Gateway remained healthy throughout the multi-hour silent period; intervention required was on the **device/office path**, not CRM isolation as a causal fix for this incident (isolation remains good hygiene).
