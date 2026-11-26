# YES CRM (Android)

Company call-log sync app for YesWeigh sales agents. Agents sign in with the **same CRM credentials** used at [crm.yesweigh.in](https://crm.yesweigh.in). After granting call-log permission, sync **starts automatically** — missed / received / rejected / outbound calls go to the VPS Call List. A light **heartbeat** every **60s** keeps the agent green (connected) on CRM Call List while the foreground service is up.

This is **not** spyware: login + explicit consent + foreground notification. Only the signed-in agent’s device logs are associated with their CRM user.

## Requirements

- Android **8.0+** (API 26)
- JDK **17**
- Android SDK (compile/target SDK 35)
- Network access to `https://crm.yesweigh.in`

## How sync stays alive

| Mechanism | Role |
|-----------|------|
| **Auto-enable** | After login + required permissions, sync turns **on** immediately (no Start sync tap). Preference persists across reopen / reboot |
| **Foreground Service** (`CallObserverService`) | Persistent notification; `ContentObserver` on call log; **60s** heartbeat on a background `HandlerThread` (no busy loop) |
| **ContentObserver** | Call ends → debounce **~400ms** → WorkManager one-shot POST to VPS (not main-thread network) |
| **WorkManager** | Immediate + **15 min** periodic backup; exponential backoff when offline |
| **BootReceiver** | Restarts FGS after reboot / quick-boot / app update if logged in and sync not admin-stopped |
| **onTaskRemoved / onDestroy** | Attempts restart when user swipes app away (where OEM allows) |

CRM marks agent **online** if `call_app_last_seen_at` is within **2.5 minutes**.

Optional **Stop sync** / **Resume sync** is available for admins; default after setup is always auto-on.

## Battery / autostart (agents — important on Xiaomi etc.)

The app does **not** wait for a Start sync button. OEM battery killers can still stop background work — guide agents for reliable green status:

1. In YES CRM tap **Allow unrestricted battery** (or Phone Settings → Apps → YES CRM → Battery → **Unrestricted**).
2. **Xiaomi / Redmi / POCO:** Security / Permissions → Autostart → enable YES CRM; also “No restrictions” for battery.
3. **Oppo / Realme / Vivo / IQOO:** Allow background activity / autostart / high background power consumption.
4. **Samsung:** Sleeping apps → remove YES CRM; allow background activity.
5. Keep the “YES CRM syncing calls” notification — dismissing/force-stopping the app stops sync until reopened (then it auto-starts again if still logged in).

## Build APK

```bash
cd /Users/mac/Projects/yesweigh-crm/android/yes-crm
# ensure local.properties has sdk.dir=…
./gradlew assembleDebug
```

Output (renamed):

`app/build/outputs/apk/debug/YES-CRM.apk`

Release (debug-signed for internal install):

```bash
./gradlew assembleRelease
# app/build/outputs/apk/release/YES-CRM.apk
```

Copy for agents:

```bash
cp app/build/outputs/apk/debug/YES-CRM.apk ~/Desktop/YES-CRM.apk
```

## Install on an agent phone

1. Enable **Install unknown apps** for Files / Drive / ADB on the phone.
2. Transfer `YES-CRM.apk` and install.
3. Open **YES CRM** → read consent → sign in with CRM email/Aadhaar + password (e.g. Biju, Visakh).
4. Allow **call / phone / notification** permissions when prompted (or tap **Grant call permissions**).
5. **Done** — sync starts automatically (green status + sync notification). No Start sync tap.
6. Tap **Allow unrestricted battery** and complete OEM autostart steps above (Xiaomi etc.).
7. New calls appear under CRM **Call List** (`/calling`) within seconds when online; agent name shows a **green** (connected) or **red** (disconnected) dot from heartbeat.
8. Closing / swiping away the app should keep the foreground service running (OEM permitting).

Offline: WorkManager retries with backoff when connectivity returns.

## API used by the app

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/auth/mobile-login` | OAuth2 form login (`username`, `password`) → JWT (`client=mobile`) |
| `POST` | `/api/call-logs/heartbeat` | Presence ping (~60s) → updates `call_app_last_seen_at` |
| `POST` | `/api/call-logs/events` | Push one call event (Bearer JWT); also refreshes presence |
| `POST` | `/api/call-logs/events/batch` | Retry backlog |

Event body (JSON): `phone_number`, `started_at` (ISO-8601), `call_type` (`missed`\|`received`\|`rejected`\|`outbound`), `duration_seconds`, optional `contact_name`, `device_id`, `device_label`, `android_call_id`.

Upserts are idempotent on `(agent, phone_normalized, started_at, call_type)`.

## Project path

`/Users/mac/Projects/yesweigh-crm/android/yes-crm`
