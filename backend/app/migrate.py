"""Create tables, add missing columns safely, and seed admin user."""
import asyncio

from sqlalchemy import select, text

from app.auth import hash_password
from app.config import get_settings
from app.db import Base, SessionLocal, engine
from app import models as _models  # noqa: F401 — register all tables on Base.metadata
from app import models as _models  # noqa: F401 — register tables
from app.models import User

EMPLOYEE_COLUMNS = {
    "whatsapp": "VARCHAR(40) DEFAULT ''",
    "dob": "DATE",
    "gender": "VARCHAR(40) DEFAULT ''",
    "blood_group": "VARCHAR(20) DEFAULT ''",
    "aadhaar": "VARCHAR(12) DEFAULT ''",
    "pan": "VARCHAR(20) DEFAULT ''",
    "languages": "VARCHAR(255) DEFAULT ''",
    "address": "TEXT DEFAULT ''",
    "state": "VARCHAR(80) DEFAULT ''",
    "district": "VARCHAR(80) DEFAULT ''",
    "city": "VARCHAR(80) DEFAULT ''",
    "pin": "VARCHAR(20) DEFAULT ''",
    "emergency_name": "VARCHAR(120) DEFAULT ''",
    "emergency_phone": "VARCHAR(40) DEFAULT ''",
    "emergency_relation": "VARCHAR(80) DEFAULT ''",
    "photo_url": "VARCHAR(500) DEFAULT ''",
    "company": "VARCHAR(160) DEFAULT 'YesWeigh'",
    "branch": "VARCHAR(120) DEFAULT ''",
    "reporting_manager": "VARCHAR(160) DEFAULT ''",
    "employment_type": "VARCHAR(40) DEFAULT 'Permanent'",
    "shift": "VARCHAR(80) DEFAULT 'General shift'",
    "working_hours": "VARCHAR(80) DEFAULT ''",
    "weekly_holiday": "VARCHAR(40) DEFAULT ''",
    "salary": "NUMERIC(12,2)",
    "probation_months": "INTEGER",
    "adms_device": "VARCHAR(120) DEFAULT ''",
    "adms_employee_code": "VARCHAR(40) DEFAULT ''",
    "user_id": "INTEGER REFERENCES users(id)",
    "crm_role": "VARCHAR(60) DEFAULT 'Employee'",
    "account_status": "VARCHAR(40) DEFAULT ''",
    "branch_access": "VARCHAR(80) DEFAULT 'Assigned branch only'",
    "lead_visibility": "VARCHAR(80) DEFAULT 'Assigned records only'",
    "deactivated_at": "TIMESTAMPTZ",
    "updated_at": "TIMESTAMPTZ DEFAULT NOW()",
}

USER_COLUMNS = {
    "must_change_password": "BOOLEAN DEFAULT FALSE",
}

WHATSAPP_CONVERSATION_COLUMNS = {
    "customer_photo": "TEXT DEFAULT ''",
}

WHATSAPP_INSTANCE_COLUMNS = {
    "employee_code": "VARCHAR(40) DEFAULT ''",
    "employee_name": "VARCHAR(160) DEFAULT ''",
}

LEAD_COLUMNS = {
    "city": "VARCHAR(80) DEFAULT ''",
    "district": "VARCHAR(80) DEFAULT ''",
    "state": "VARCHAR(80) DEFAULT ''",
}

ZOHO_INTEGRATION_COLUMNS = {
    "client_id": "VARCHAR(200) DEFAULT ''",
    "encrypted_client_secret": "TEXT DEFAULT ''",
}

ZOHO_OAUTH_STATE_COLUMNS = {
    "popup": "BOOLEAN DEFAULT FALSE",
}

DEALER_COLUMNS = {
    "assigned_salesman": "VARCHAR(160)",
}

ZOHO_SALE_COLUMNS = {
    # Tax-exclusive amount (Zoho sub_total / CSV SubTotal)
    "sub_total": "NUMERIC(14,2)",
    # Zoho Books CSV "Sales person" / API salesperson_name
    "salesperson_name": "VARCHAR(160) DEFAULT ''",
}


async def migrate_aadhaar_column(conn) -> None:
    """Rename aadhaar_last4 → aadhaar (12) or add aadhaar and backfill."""
    has_last4 = await conn.scalar(
        text(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'employees' AND column_name = 'aadhaar_last4'
            """
        )
    )
    has_aadhaar = await conn.scalar(
        text(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'employees' AND column_name = 'aadhaar'
            """
        )
    )
    if has_last4 and not has_aadhaar:
        await conn.execute(text('ALTER TABLE employees RENAME COLUMN aadhaar_last4 TO aadhaar'))
        await conn.execute(text("ALTER TABLE employees ALTER COLUMN aadhaar TYPE VARCHAR(12)"))
        print("Renamed employees.aadhaar_last4 → aadhaar (VARCHAR(12))")
        return
    if not has_aadhaar:
        await conn.execute(text("ALTER TABLE employees ADD COLUMN aadhaar VARCHAR(12) DEFAULT ''"))
        print("Added employees.aadhaar")
    if has_last4 and has_aadhaar:
        await conn.execute(
            text(
                """
                UPDATE employees
                SET aadhaar = aadhaar_last4
                WHERE (aadhaar IS NULL OR aadhaar = '')
                  AND aadhaar_last4 IS NOT NULL
                  AND aadhaar_last4 <> ''
                """
            )
        )


async def widen_media_url(conn) -> None:
    """media_url must hold public paths; widen off VARCHAR(500) if needed."""
    col = await conn.execute(
        text(
            """
            SELECT data_type, character_maximum_length
            FROM information_schema.columns
            WHERE table_name = 'whatsapp_messages' AND column_name = 'media_url'
            """
        )
    )
    row = col.first()
    if row and row[0] == "character varying":
        await conn.execute(text("ALTER TABLE whatsapp_messages ALTER COLUMN media_url TYPE TEXT"))
        print("Widened whatsapp_messages.media_url to TEXT")


async def ensure_columns(conn, table: str, columns: dict[str, str]) -> None:
    for name, ddl in columns.items():
        exists = await conn.scalar(
            text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_name = :table AND column_name = :col
                """
            ),
            {"table": table, "col": name},
        )
        if not exists:
            await conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}'))
            print(f"Added {table}.{name}")


async def normalize_status(conn) -> None:
    await conn.execute(
        text(
            """
            UPDATE employees SET status = CASE lower(status)
              WHEN 'active' THEN 'Active'
              WHEN 'inactive' THEN 'Inactive'
              WHEN 'on leave' THEN 'On Leave'
              WHEN 'on_leave' THEN 'On Leave'
              WHEN 'probation' THEN 'Probation'
              WHEN 'suspended' THEN 'Suspended'
              WHEN 'resigned' THEN 'Resigned'
              WHEN 'terminated' THEN 'Terminated'
              ELSE status
            END
            WHERE status IS NOT NULL
            """
        )
    )


async def ensure_adms_map_index(conn) -> None:
    """Prevent duplicate company + device serial + enrollment ID mappings."""
    try:
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_employees_adms_map
                ON employees (company, (COALESCE(adms_device, '')), adms_employee_code)
                WHERE adms_employee_code IS NOT NULL AND adms_employee_code <> ''
                """
            )
        )
    except Exception as exc:  # noqa: BLE001
        # Existing duplicate mappings block the index; import still de-dupes in app logic
        print(f"Skipped uq_employees_adms_map ({exc})")


async def main() -> None:
    settings = get_settings()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        zoho_tables = sorted(t for t in Base.metadata.tables if t.startswith("zoho") or t == "dealers")
        print(f"Ensured tables including: {', '.join(zoho_tables)}")
        await migrate_aadhaar_column(conn)
        await ensure_columns(conn, "employees", EMPLOYEE_COLUMNS)
        await ensure_columns(conn, "users", USER_COLUMNS)
        await ensure_columns(conn, "leads", LEAD_COLUMNS)
        await ensure_columns(conn, "whatsapp_conversations", WHATSAPP_CONVERSATION_COLUMNS)
        await ensure_columns(conn, "whatsapp_instances", WHATSAPP_INSTANCE_COLUMNS)
        await ensure_columns(conn, "zoho_integrations", ZOHO_INTEGRATION_COLUMNS)
        await ensure_columns(conn, "zoho_oauth_states", ZOHO_OAUTH_STATE_COLUMNS)
        await ensure_columns(conn, "dealers", DEALER_COLUMNS)
        await ensure_columns(conn, "zoho_sales", ZOHO_SALE_COLUMNS)
        await widen_media_url(conn)
        await normalize_status(conn)
        await ensure_adms_map_index(conn)
    async with SessionLocal() as db:
        existing = (await db.execute(select(User).where(User.email == settings.admin_email))).scalar_one_or_none()
        if not existing:
            db.add(
                User(
                    email=settings.admin_email,
                    name="Admin",
                    password_hash=hash_password(settings.admin_password),
                    role="admin",
                )
            )
            await db.commit()
            print(f"Seeded admin {settings.admin_email}")
        else:
            print("Admin already exists")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
