"""Create tables, add missing columns safely, and seed admin user."""
import asyncio

from sqlalchemy import select, text

from app.auth import hash_password
from app.config import get_settings
from app.db import Base, SessionLocal, engine
from app import models as _models  # noqa: F401 — register all tables on Base.metadata
from app import models as _models  # noqa: F401 — register tables
from app.models import User, WhatsAppInstance

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
    "dealer_id": "INTEGER REFERENCES dealers(id)",
    "ai_outline": "TEXT DEFAULT ''",
    "ai_outline_at": "TIMESTAMPTZ",
    "ai_outline_fingerprint": "VARCHAR(80) DEFAULT ''",
    "customer_lid": "VARCHAR(40) DEFAULT ''",
}

WHATSAPP_INSTANCE_COLUMNS = {
    "employee_code": "VARCHAR(40) DEFAULT ''",
    "employee_name": "VARCHAR(160) DEFAULT ''",
    "history_sync_status": "VARCHAR(40) DEFAULT ''",
    "history_sync_at": "TIMESTAMPTZ",
    "history_sync_stats": "TEXT DEFAULT ''",
    "history_sync_error": "TEXT DEFAULT ''",
    "provider": "VARCHAR(40) DEFAULT 'evolution'",
}

WHATSAPP_MESSAGE_COLUMNS = {
    "transcript": "TEXT DEFAULT ''",
    "transcript_language": "VARCHAR(40) DEFAULT ''",
    "transcription_status": "VARCHAR(40) DEFAULT ''",
    "transcription_error": "VARCHAR(255) DEFAULT ''",
}

LEAD_COLUMNS = {
    "city": "VARCHAR(80) DEFAULT ''",
    "district": "VARCHAR(80) DEFAULT ''",
    "state": "VARCHAR(80) DEFAULT ''",
    "pin": "VARCHAR(20) DEFAULT ''",
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

AI_USAGE_LOG_COLUMNS = {
    "conversation_id": "INTEGER REFERENCES whatsapp_conversations(id)",
    "total_tokens": "INTEGER DEFAULT 0",
    "thoughts_tokens": "INTEGER DEFAULT 0",
}

AI_AGENT_RUN_COLUMNS = {
    "user_deleted": "BOOLEAN DEFAULT FALSE",
    "assistant_deleted": "BOOLEAN DEFAULT FALSE",
}

AI_MESSAGE_COLUMNS = {
    "run_id": "INTEGER REFERENCES ai_agent_runs(id) ON DELETE SET NULL",
    "deleted_at": "TIMESTAMPTZ",
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


async def backfill_outline_usage_employees(conn) -> None:
    """Fill null employee_id on WhatsApp outline usage from conversation owner."""
    result = await conn.execute(
        text(
            """
            UPDATE ai_usage_logs u
            SET employee_id = c.assigned_employee_id
            FROM whatsapp_conversations c
            WHERE u.conversation_id = c.id
              AND u.employee_id IS NULL
              AND c.assigned_employee_id IS NOT NULL
              AND (
                u.purpose = 'whatsapp_outline'
                OR u.purpose LIKE 'whatsapp_%'
              )
            """
        )
    )
    # Fallback: instance primary owner when conversation has no assignee
    result2 = await conn.execute(
        text(
            """
            UPDATE ai_usage_logs u
            SET employee_id = ie.employee_id
            FROM whatsapp_instance_employees ie
            WHERE u.whatsapp_instance_id = ie.whatsapp_instance_id
              AND ie.is_primary_owner IS TRUE
              AND u.employee_id IS NULL
              AND (
                u.purpose = 'whatsapp_outline'
                OR u.purpose LIKE 'whatsapp_%'
              )
            """
        )
    )
    n1 = result.rowcount if result.rowcount is not None and result.rowcount >= 0 else 0
    n2 = result2.rowcount if result2.rowcount is not None and result2.rowcount >= 0 else 0
    if n1 or n2:
        print(f"Backfilled outline usage employee_id: {n1} from conversation, {n2} from instance owner")


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


async def ensure_ai_knowledge_indexes(conn) -> None:
    """Case-insensitive unique glossary key per org+kind."""
    try:
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_knowledge_org_kind_key_ci
                ON ai_knowledge (organization_id, kind, lower(key))
                """
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Skipped uq_ai_knowledge_org_kind_key_ci ({exc})")


async def ensure_whatsapp_inbox_indexes(conn) -> None:
    """Composite index for account-chip filtered conversation lists."""
    try:
        await conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_wa_conv_org_instance_last_msg
                ON whatsapp_conversations (organization_id, whatsapp_instance_id, last_message_at DESC NULLS LAST)
                """
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Skipped ix_wa_conv_org_instance_last_msg ({exc})")
    try:
        await conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_wa_conv_org_instance_lid
                ON whatsapp_conversations (organization_id, whatsapp_instance_id, customer_lid)
                WHERE customer_lid IS NOT NULL AND customer_lid <> ''
                """
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Skipped ix_wa_conv_org_instance_lid ({exc})")


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
        await ensure_columns(conn, "whatsapp_messages", WHATSAPP_MESSAGE_COLUMNS)
        await ensure_columns(conn, "zoho_integrations", ZOHO_INTEGRATION_COLUMNS)
        await ensure_columns(conn, "zoho_oauth_states", ZOHO_OAUTH_STATE_COLUMNS)
        await ensure_columns(conn, "dealers", DEALER_COLUMNS)
        await ensure_columns(conn, "zoho_sales", ZOHO_SALE_COLUMNS)
        await ensure_columns(conn, "ai_usage_logs", AI_USAGE_LOG_COLUMNS)
        await ensure_columns(conn, "ai_agent_runs", AI_AGENT_RUN_COLUMNS)
        await ensure_columns(conn, "ai_messages", AI_MESSAGE_COLUMNS)
        await backfill_outline_usage_employees(conn)
        await widen_media_url(conn)
        await normalize_status(conn)
        await ensure_adms_map_index(conn)
        await ensure_whatsapp_inbox_indexes(conn)
        await ensure_ai_knowledge_indexes(conn)
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
        await ensure_care_instance(db, settings)
    await engine.dispose()


CARE_EVO_NAME = "care_meta_cloud"
CARE_DISPLAY_NAME = "Care"


async def ensure_care_instance(db, settings) -> None:
    """Seed the Meta Cloud WhatsApp line as inbox chip 'Care' (not Evolution)."""
    import uuid

    from sqlalchemy import or_

    row = (
        await db.execute(
            select(WhatsAppInstance).where(
                WhatsAppInstance.organization_id == "yesweigh",
                or_(
                    WhatsAppInstance.evolution_instance_name == CARE_EVO_NAME,
                    WhatsAppInstance.provider == "meta_cloud",
                    WhatsAppInstance.display_name == CARE_DISPLAY_NAME,
                ),
            )
        )
    ).scalars().first()
    phone_id = (settings.whatsapp_cloud_phone_number_id or "").strip()
    if row:
        changed = False
        if row.provider != "meta_cloud":
            row.provider = "meta_cloud"
            changed = True
        if row.display_name != CARE_DISPLAY_NAME:
            row.display_name = CARE_DISPLAY_NAME
            changed = True
        if row.integration_type != "Meta Cloud API":
            row.integration_type = "Meta Cloud API"
            changed = True
        if row.purpose != "Customer Care":
            row.purpose = "Customer Care"
            changed = True
        if row.deleted_at is not None:
            row.deleted_at = None
            changed = True
        if phone_id and row.evolution_instance_id != phone_id:
            row.evolution_instance_id = phone_id
            changed = True
        # Cloud line is always Connected for chip status (webhook-fed; no QR).
        if row.connection_status != "Connected":
            row.connection_status = "Connected"
            changed = True
        if changed:
            await db.commit()
            print(f"Updated Care WhatsApp instance ({row.public_id})")
        else:
            print(f"Care WhatsApp instance already present ({row.public_id})")
        return

    inst = WhatsAppInstance(
        public_id=str(uuid.uuid4()),
        organization_id="yesweigh",
        integration_id=None,
        evolution_instance_id=phone_id,
        evolution_instance_name=CARE_EVO_NAME,
        encrypted_instance_token="",
        display_name=CARE_DISPLAY_NAME,
        employee_code="",
        employee_name="",
        expected_phone_number="",
        connected_phone_number="",
        profile_name="YesWeigh Care",
        profile_photo="",
        integration_type="Meta Cloud API",
        provider="meta_cloud",
        department="Customer Care",
        branch="",
        purpose="Customer Care",
        description="YesOne Meta Cloud WhatsApp (service.yesweigh.in /super-admin/whatsapp) — webhook only",
        connection_status="Connected",
        registration_status="active",
    )
    db.add(inst)
    await db.commit()
    print(f"Seeded Care WhatsApp instance ({inst.public_id})")


if __name__ == "__main__":
    asyncio.run(main())
