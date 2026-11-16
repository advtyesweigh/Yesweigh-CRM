# YesWeigh CRM

Web + Capacitor mobile CRM for `https://crm.yesweigh.in`

## Modules (v0.1)

- Dashboard
- Lead management
- Work management
- HRM (employees + ADMS code mapping)
- WhatsApp via Evolution Go
- ADMS attendance gateway integration

## Stack

- FastAPI + Postgres (`yesweigh_crm` on existing Evolution Postgres host)
- React (Vite) web UI
- Capacitor (`in.yesweigh.crm`) for Android/iOS
- Caddy reverse proxy

## Local

```bash
# API
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# set DATABASE_URL then:
python -m app.migrate
uvicorn app.main:app --reload --port 8100

# Web
cd frontend && npm install && npm run dev
```

## Capacitor

```bash
cd frontend
npm run build
npx cap add android
npx cap sync
```

Point `VITE_API_URL` to `https://crm.yesweigh.in` for device builds.

## Catalog sync (VPS)

Weekday hourly Firebase → CRM product/stock sync: see [docs/catalog-sync.md](docs/catalog-sync.md).
