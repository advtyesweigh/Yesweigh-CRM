# AI Agent — Google Places dealer discovery

## Routing (critical)

| User intent | Tool |
|-------------|------|
| **Whole state** (+ optional word **google** / “from google”), no city | **`start_dealer_finder`** — full district→city×categories job |
| **City** + state quick lookup, NEW vs CRM | `find_new_dealers` (city required) |
| **City** + state raw Google list | `search_google_businesses` (city required) |

**Canonical statewide phrase:**  
`find weighing scale dealer tamilnadu from google` → Dealer Finder only (never the ~60-result card).

State-only calls to `find_new_dealers` / `search_google_businesses` return `redirect_to_dealer_finder` and do **not** run Places.

See `docs/AI_DEALER_FINDER_SKILL.md` and `docs/DEALER_FINDER.md`.

## What city-level tools do

Staff can ask for **weighing scale dealers** in a **city** via Google Places Text Search, cross-check CRM **Dealers** and **Leads**, and return **NEW only**.

| Concept | Meaning |
|--------|---------|
| CRM Dealers | Left menu → Dealer (`dealers` table) |
| CRM Leads | Left menu → Leads (`leads` table) |
| Google | External Places discovery |
| **New** | Google hit not matched to dealers/leads by **phone** or fuzzy **name+city** |

Search is **read-only** (no confirm). Confirm before any import/write. Statewide import uses `import_dealer_finder_leads`.

## How many results? (city quick tools only)

| Limit | Value |
|-------|-------|
| Our / Google **per request** (`pageSize`) | **20** (API max; values &gt;20 coerced to 20) |
| Pages we fetch | **up to 3** (`nextPageToken`) |
| **Total cap** (us + Google Text Search) | **60** |
| Default `max_results` | **60** |

This 60-cap applies **only** to city-level quick tools. Statewide coverage uses Dealer Finder (many units × categories × pagination) — never present 34/60 as “Tamil Nadu complete”.

## Env (VPS `/opt/yesweigh-crm/.env`)

```bash
GOOGLE_MAPS_API_KEY=…   # or GOOGLE_PLACES_API_KEY
```

Enable **Places API (New)** Text Search for the key. Never commit real keys. Settings → AI Agent shows whether Places is configured (yes/no only).

## Smoke (city-level)

```bash
docker exec yesweigh-crm-api python -c '
import asyncio
from app.db import SessionLocal
from app.models import User
from app.ai.tools import tool_find_new_dealers
from sqlalchemy import select

async def main():
  async with SessionLocal() as db:
    user = (await db.execute(select(User).limit(1))).scalar_one()
    # State-only must redirect:
    redirect = await tool_find_new_dealers(db, user, {"state":"Tamil Nadu"})
    print("redirect", redirect.get("redirect_to_dealer_finder"), redirect.get("ok"))
    out = await tool_find_new_dealers(db, user, {"keyword":"weighing scale dealers","state":"UP","city":"Lucknow","max_results":10})
    print({k: out.get(k) for k in ("ok","count","google_total","already_in_crm","error","state","city")})
asyncio.run(main())
'
```
