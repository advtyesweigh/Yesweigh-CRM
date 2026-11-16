# Firebase catalog sync (CRM VPS)

Hourly sync of YesWeigh Firebase catalog → CRM Postgres (`catalog_categories` / `catalog_products`): new products, stock, prices. Images are **not** downloaded on cron (`DOWNLOAD_IMAGES=0`) to avoid OOM.

## Schedule

| Item | Value |
|------|--------|
| Host | `/opt/yesweigh-crm` |
| Cron file | `/etc/cron.d/yesweigh-crm-catalog-sync` |
| Expression | `0 * * * 1-5` with `CRON_TZ=Asia/Kolkata` |
| Meaning | Every hour on the hour, Monday–Friday IST |
| Wrapper | `/opt/yesweigh-crm/scripts/run_catalog_sync_cron.sh` |
| Log | `/var/log/yesweigh-crm-catalog-sync.log` |

Weekends are skipped. There is no Indian holiday calendar yet.

## Excluded categories (never re-inserted)

Defined in `backend/app/catalog_exclusions.py`:

- `GENERIC SPARE PARTS`
- `STAMPING GJ`
- `STAMPING KL`
- `INACTIVE`

Sync upserts only allowed categories/products, then deletes CRM rows for excluded or removed Firebase entries.

## Credentials

Stored only on the VPS in `/opt/yesweigh-crm/.env` (mode `600`, not git):

- `FIREBASE_LOGIN_ID` (or `FIREBASE_EMAIL`)
- `FIREBASE_PASSWORD`
- optional: `FIREBASE_API_KEY`, `FIREBASE_PROJECT_ID`

Compose already loads `.env` for the API container; the cron wrapper sources the same file and passes vars into `docker exec`.

## Manual / dry-run

```bash
# Dry-run (fetch + exclude only, no DB writes)
DRY_RUN=1 /opt/yesweigh-crm/scripts/run_catalog_sync_cron.sh

# Full sync once
/opt/yesweigh-crm/scripts/run_catalog_sync_cron.sh

# Or via helper (loads FIREBASE_* from /opt/yesweigh-crm/.env)
DOWNLOAD_IMAGES=0 /opt/yesweigh-crm/scripts/sync_firebase_catalog.sh
```

## How to check last run

```bash
# Last start / finish lines
sudo tail -n 80 /var/log/yesweigh-crm-catalog-sync.log

# Confirm cron is installed
cat /etc/cron.d/yesweigh-crm-catalog-sync

# Last successful sync timestamps in DB (from API container)
docker exec yesweigh-crm-api python -c "
import asyncio
from sqlalchemy import select, func
from app.db import SessionLocal
from app.models import CatalogCategory, CatalogProduct

async def main():
    async with SessionLocal() as db:
        cmax = (await db.execute(select(func.max(CatalogCategory.synced_at)))).scalar()
        pmax = (await db.execute(select(func.max(CatalogProduct.synced_at)))).scalar()
        nc = (await db.execute(select(func.count()).select_from(CatalogCategory))).scalar()
        np = (await db.execute(select(func.count()).select_from(CatalogProduct))).scalar()
        print('categories', nc, 'products', np)
        print('category_synced_at_max', cmax)
        print('product_synced_at_max', pmax)
asyncio.run(main())
"
```

Look for `catalog sync ok` in the log after each hourly tick on a weekday.
