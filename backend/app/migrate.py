"""Create tables, add missing columns safely, and seed admin user."""
import asyncio

from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError

from app.auth import hash_password
from app.config import get_settings
from app.db import Base, SessionLocal, engine
from app import models as _models  # noqa: F401 — register all tables on Base.metadata
from app import models as _models  # noqa: F401 — register tables
from app.models import CallLog, User, WhatsAppInstance

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
    "session_version": "INTEGER DEFAULT 0",
    "active_session_id": "VARCHAR(64) DEFAULT ''",
    "mobile_session_id": "VARCHAR(64) DEFAULT ''",
    "call_app_last_seen_at": "TIMESTAMPTZ",
}

WHATSAPP_CONVERSATION_COLUMNS = {
    "customer_photo": "TEXT DEFAULT ''",
    "dealer_id": "INTEGER REFERENCES dealers(id)",
    "ai_outline": "TEXT DEFAULT ''",
    "ai_outline_at": "TIMESTAMPTZ",
    "ai_outline_fingerprint": "VARCHAR(128) DEFAULT ''",
    "customer_lid": "VARCHAR(40) DEFAULT ''",
    "ai_human_takeover_at": "TIMESTAMPTZ",
    "ai_opt_out": "BOOLEAN DEFAULT FALSE",
    "preferred_reply_language": "VARCHAR(20) DEFAULT ''",
    "service_category": "VARCHAR(20) DEFAULT ''",
    "awaiting_staff_reply": "BOOLEAN DEFAULT FALSE",
    "awaiting_since": "TIMESTAMPTZ",
    "is_spam": "BOOLEAN DEFAULT FALSE",
    "spam_at": "TIMESTAMPTZ",
    "spam_by_user_id": "INTEGER REFERENCES users(id)",
    "onboarding_step": "VARCHAR(40) DEFAULT ''",
    "onboarding_at": "TIMESTAMPTZ",
    "onboarding_lead_id": "INTEGER REFERENCES leads(id)",
}

WHATSAPP_REPLY_EVENT_COLUMNS = {
    "office_seconds": "INTEGER DEFAULT 0",
    "off_seconds": "INTEGER DEFAULT 0",
}

WHATSAPP_INSTANCE_COLUMNS = {
    "employee_code": "VARCHAR(40) DEFAULT ''",
    "employee_name": "VARCHAR(160) DEFAULT ''",
    "history_sync_status": "VARCHAR(40) DEFAULT ''",
    "history_sync_at": "TIMESTAMPTZ",
    "history_sync_stats": "TEXT DEFAULT ''",
    "history_sync_error": "TEXT DEFAULT ''",
    "provider": "VARCHAR(40) DEFAULT 'evolution'",
    "sarvam_voice_id": "VARCHAR(120) DEFAULT ''",
    "sarvam_voice_name": "VARCHAR(120) DEFAULT ''",
    "sarvam_voice_sample_url": "TEXT DEFAULT ''",
    "sarvam_voice_sample_message_id": "INTEGER",
    "sarvam_voice_created_at": "TIMESTAMPTZ",
    "voice_tts_enabled": "BOOLEAN DEFAULT FALSE",
}

WHATSAPP_MESSAGE_COLUMNS = {
    "transcript": "TEXT DEFAULT ''",
    "spoken_language": "VARCHAR(40) DEFAULT ''",
    "transcript_language": "VARCHAR(40) DEFAULT ''",
    "transcription_status": "VARCHAR(40) DEFAULT ''",
    "transcription_error": "VARCHAR(255) DEFAULT ''",
}

LEAD_COLUMNS = {
    "city": "VARCHAR(80) DEFAULT ''",
    "district": "VARCHAR(80) DEFAULT ''",
    "state": "VARCHAR(80) DEFAULT ''",
    "pin": "VARCHAR(20) DEFAULT ''",
    "contact_person": "VARCHAR(160) DEFAULT ''",
    "business_type": "VARCHAR(120) DEFAULT ''",
    "address": "TEXT DEFAULT ''",
    "priority": "VARCHAR(40) DEFAULT 'medium'",
    "product_category": "VARCHAR(160) DEFAULT ''",
    "estimated_value": "NUMERIC(14,2)",
    "campaign": "VARCHAR(200) DEFAULT ''",
    "tags": "VARCHAR(500) DEFAULT ''",
    "next_follow_up": "DATE",
    "last_contacted": "DATE",
    "customer_website": "VARCHAR(500) DEFAULT ''",
    "lead_score": "SMALLINT",
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

# Dealer Finder two-tier cost controls (free quota + paid $ ceiling).
DEALER_FINDER_SETTINGS_COLUMNS = {
    "free_requests_per_month": "INTEGER DEFAULT 7000",
    "all_india_enabled": "BOOLEAN DEFAULT FALSE",
    "pilot_district_completed": "BOOLEAN DEFAULT FALSE",
    "pilot_state": "VARCHAR(80) DEFAULT 'Rajasthan'",
    "pilot_district": "VARCHAR(80) DEFAULT 'Jaipur'",
}

ZOHO_SALE_COLUMNS = {
    # Tax-exclusive amount (Zoho sub_total / CSV SubTotal)
    "sub_total": "NUMERIC(14,2)",
    # Zoho Books CSV "Sales person" / API salesperson_name
    "salesperson_name": "VARCHAR(160) DEFAULT ''",
}

AI_THREAD_INSIGHT_COLUMNS = {
    "enquiry_cache": "TEXT DEFAULT ''",
    "stage_override": "VARCHAR(40) DEFAULT ''",
    "stage_override_at": "TIMESTAMPTZ",
    "stage_override_by": "VARCHAR(120) DEFAULT ''",
}

AI_STAGE_MOVE_COLUMNS = {
    "snippet": "TEXT DEFAULT ''",
    "signals_json": "TEXT DEFAULT ''",
}

AI_SETTINGS_COLUMNS = {
    "intel_enabled": "BOOLEAN DEFAULT FALSE",
    "intel_daily_budget_inr": "NUMERIC(12,2) DEFAULT 10",
    "intel_monthly_budget_inr": "NUMERIC(12,2) DEFAULT 150",
    "intel_reserved_daily_inr": "NUMERIC(12,4) DEFAULT 0",
    "intel_reserved_monthly_inr": "NUMERIC(12,4) DEFAULT 0",
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

CAMPAIGN_MEDIA_COLUMNS = {
    "cover_name": "VARCHAR(255) DEFAULT ''",
    "cover_path": "VARCHAR(500) DEFAULT ''",
    "media_kind": "VARCHAR(40) DEFAULT 'video'",
    "tags": "TEXT DEFAULT '[]'",
    "folder_id": "INTEGER",
    "duration_seconds": "INTEGER DEFAULT 0",
    "uid": "INTEGER",
    "language": "VARCHAR(20) DEFAULT ''",
}

CUSTOM_AUDIENCE_COLUMNS = {
    "uid": "INTEGER",
}

CAMPAIGN_RUN_COLUMNS = {
    "status": "VARCHAR(40) DEFAULT 'completed'",
    "total_hours": "INTEGER DEFAULT 0",
    "next_index": "INTEGER DEFAULT 0",
    "recipients_json": "TEXT DEFAULT '[]'",
    "gaps_json": "TEXT DEFAULT '[]'",
    "caption": "TEXT DEFAULT ''",
    "user_id": "INTEGER",
    "started_at": "TIMESTAMPTZ",
    "finished_at": "TIMESTAMPTZ",
    "filter_meta_json": "TEXT DEFAULT '{}'",
    "replied_count": "INTEGER DEFAULT 0",
    "uid": "INTEGER",
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


async def widen_outline_fingerprint(conn) -> None:
    """Outline fingerprint includes prompt version + message digest (was too long for VARCHAR(80))."""
    col = await conn.execute(
        text(
            """
            SELECT character_maximum_length
            FROM information_schema.columns
            WHERE table_name = 'whatsapp_conversations' AND column_name = 'ai_outline_fingerprint'
            """
        )
    )
    row = col.first()
    if row and (row[0] or 0) < 128:
        await conn.execute(
            text(
                "ALTER TABLE whatsapp_conversations "
                "ALTER COLUMN ai_outline_fingerprint TYPE VARCHAR(128)"
            )
        )
        print("Widened whatsapp_conversations.ai_outline_fingerprint to VARCHAR(128)")


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


async def seed_awaiting_staff_reply_from_unread(conn) -> None:
    """Bootstrap reply-needed flag for existing open DMs until heuristic backfill runs."""
    await conn.execute(
        text(
            """
            UPDATE whatsapp_conversations
            SET awaiting_staff_reply = TRUE
            WHERE COALESCE(awaiting_staff_reply, FALSE) = FALSE
              AND COALESCE(unread_count, 0) > 0
              AND customer_jid NOT ILIKE '%@g.us%'
              AND COALESCE(status, '') <> 'Resolved'
            """
        )
    )


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


async def normalize_dealer_finder_cost_controls(conn) -> None:
    """Align Places budgets: 7000 free / month, then $100 paid ceiling (not a 7000 hard stop)."""
    exists = await conn.scalar(
        text(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'dealer_finder_settings'
            """
        )
    )
    if not exists:
        return
    # Sync free quota from legacy max_requests_per_month when free is null/0.
    await conn.execute(
        text(
            """
            UPDATE dealer_finder_settings
            SET free_requests_per_month = COALESCE(
                  NULLIF(free_requests_per_month, 0),
                  NULLIF(max_requests_per_month, 0),
                  7000
                ),
                max_requests_per_month = COALESCE(
                  NULLIF(free_requests_per_month, 0),
                  NULLIF(max_requests_per_month, 0),
                  7000
                ),
                max_monthly_spend_usd = COALESCE(max_monthly_spend_usd, 100),
                max_requests_per_job = CASE
                  WHEN max_requests_per_job IS NULL OR max_requests_per_job <= 7000
                    THEN 100000
                  ELSE max_requests_per_job
                END,
                max_requests_per_day = CASE
                  WHEN max_requests_per_day IS NULL OR max_requests_per_day <= 7000
                    THEN 100000
                  ELSE max_requests_per_day
                END
            """
        )
    )
    # Keep free + legacy alias equal after the CASE above may have left them divergent.
    await conn.execute(
        text(
            """
            UPDATE dealer_finder_settings
            SET max_requests_per_month = free_requests_per_month
            WHERE free_requests_per_month IS NOT NULL
              AND (
                max_requests_per_month IS NULL
                OR max_requests_per_month <> free_requests_per_month
              )
            """
        )
    )
    print(
        "Normalized dealer_finder_settings: free_requests_per_month=7000-tier, "
        "default max_monthly_spend_usd=100 when null, job/day ops caps raised if needed"
    )


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


async def normalize_lead_state_script_labels(conn) -> None:
    """Rewrite non-canonical lead.state spellings (incl. Indic scripts) to English CRM names."""
    from app.state_normalize import STATE_WRITE_BACK

    for alias, canon in STATE_WRITE_BACK:
        result = await conn.execute(
            text(
                """
                UPDATE leads
                SET state = :canon
                WHERE trim(coalesce(state, '')) = :alias
                  AND state IS DISTINCT FROM :canon
                """
            ),
            {"alias": alias, "canon": canon},
        )
        n = result.rowcount or 0
        if n:
            print(f"Normalized {n} leads.state {alias!r} → {canon}")


async def normalize_kerala_state_labels(conn) -> None:
    """Merge Kerala → Keralam everywhere state is stored (dealers, leads, Zoho staging, audiences).

    Canonical display/storage name is **Keralam** so map ranks and filters stay one bucket.
    """
    for table, col in (
        ("dealers", "state"),
        ("leads", "state"),
        ("zoho_dealers", "billing_state"),
        ("employees", "state"),
    ):
        exists = await conn.scalar(
            text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_name = :table AND column_name = :col
                """
            ),
            {"table": table, "col": col},
        )
        if not exists:
            continue
        result = await conn.execute(
            text(
                f"""
                UPDATE "{table}"
                SET "{col}" = 'Keralam'
                WHERE lower(trim(coalesce("{col}", ''))) IN ('kerala', 'keralam')
                  AND trim("{col}") IS DISTINCT FROM 'Keralam'
                """
            )
        )
        n = result.rowcount or 0
        if n:
            print(f"Normalized {n} {table}.{col} → Keralam")

    aud_exists = await conn.scalar(
        text(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'custom_audiences'
            """
        )
    )
    if aud_exists:
        # Cover spaced / compact JSON and either Kerala or Keralam already.
        for old, new in (
            ('"state": "Kerala"', '"state": "Keralam"'),
            ('"state":"Kerala"', '"state":"Keralam"'),
            ("'state': 'Kerala'", "'state': 'Keralam'"),
            ("'state':'Kerala'", "'state':'Keralam'"),
        ):
            result = await conn.execute(
                text(
                    """
                    UPDATE custom_audiences
                    SET filters_json = replace(filters_json, :old, :new)
                    WHERE filters_json LIKE :pat
                    """
                ),
                {"old": old, "new": new, "pat": f"%{old}%"},
            )
            n = result.rowcount or 0
            if n:
                print(f"Normalized {n} custom_audiences filters ({old} → {new})")


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


async def ensure_kb_fts_index(conn) -> None:
    """Postgres FTS for FAQ KB (simple config — Malayalam + English tokens)."""
    try:
        await conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_kb_articles_fts
                ON kb_articles USING gin (
                  to_tsvector(
                    'simple',
                    coalesce(title,'') || ' ' || coalesce(question,'') || ' ' ||
                    coalesce(answer,'') || ' ' || coalesce(keywords,'') || ' ' || coalesce(product,'')
                  )
                )
                """
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Skipped ix_kb_articles_fts ({exc})")


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


async def ensure_whatsapp_reply_event_indexes(conn) -> None:
    try:
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_wa_reply_evt_message_id
                ON whatsapp_reply_events (message_id)
                WHERE message_id IS NOT NULL
                """
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Skipped uq_wa_reply_evt_message_id ({exc})")


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


async def ensure_schema(conn) -> None:
    """create_all is not always idempotent for indexes — tolerate existing objects.

    Use a SAVEPOINT so a duplicate-index ProgrammingError does not abort the
    outer migrate transaction (asyncpg InFailedSQLTransactionError cascade).
    """
    try:
        async with conn.begin_nested():
            await conn.run_sync(Base.metadata.create_all)
    except ProgrammingError as exc:
        msg = str(exc).lower()
        if "already exists" in msg or "duplicate" in msg:
            print(f"create_all partial skip ({exc.orig.__class__.__name__ if exc.orig else exc})")
        else:
            raise


async def ensure_call_logs_table(conn) -> None:
    """Call List depends on call_logs — create explicitly if create_all skipped it."""
    try:
        async with conn.begin_nested():
            await conn.run_sync(lambda sync_conn: CallLog.__table__.create(sync_conn, checkfirst=True))
        print("Ensured call_logs table")
    except ProgrammingError as exc:
        msg = str(exc).lower()
        if "already exists" in msg or "duplicate" in msg:
            print(f"call_logs ensure skip ({exc.orig.__class__.__name__ if exc.orig else exc})")
        else:
            raise


async def main() -> None:
    settings = get_settings()
    async with engine.begin() as conn:
        await ensure_schema(conn)
        await ensure_call_logs_table(conn)
        zoho_tables = sorted(t for t in Base.metadata.tables if t.startswith("zoho") or t == "dealers")
        print(f"Ensured tables including: {', '.join(zoho_tables)}")
        await migrate_aadhaar_column(conn)
        await ensure_columns(conn, "employees", EMPLOYEE_COLUMNS)
        await ensure_columns(conn, "users", USER_COLUMNS)
        await ensure_columns(conn, "leads", LEAD_COLUMNS)
        await ensure_columns(conn, "whatsapp_conversations", WHATSAPP_CONVERSATION_COLUMNS)
        await ensure_columns(conn, "whatsapp_reply_events", WHATSAPP_REPLY_EVENT_COLUMNS)
        await ensure_whatsapp_reply_event_indexes(conn)
        await seed_awaiting_staff_reply_from_unread(conn)
        await ensure_columns(conn, "whatsapp_instances", WHATSAPP_INSTANCE_COLUMNS)
        await ensure_columns(conn, "whatsapp_messages", WHATSAPP_MESSAGE_COLUMNS)
        await ensure_columns(conn, "zoho_integrations", ZOHO_INTEGRATION_COLUMNS)
        await ensure_columns(conn, "zoho_oauth_states", ZOHO_OAUTH_STATE_COLUMNS)
        await ensure_columns(conn, "dealers", DEALER_COLUMNS)
        await ensure_columns(conn, "dealer_finder_settings", DEALER_FINDER_SETTINGS_COLUMNS)
        await normalize_dealer_finder_cost_controls(conn)
        await ensure_columns(conn, "zoho_sales", ZOHO_SALE_COLUMNS)
        await ensure_columns(conn, "ai_settings", AI_SETTINGS_COLUMNS)
        await ensure_columns(conn, "ai_thread_insights", AI_THREAD_INSIGHT_COLUMNS)
        await ensure_columns(conn, "ai_stage_moves", AI_STAGE_MOVE_COLUMNS)
        await ensure_columns(conn, "ai_usage_logs", AI_USAGE_LOG_COLUMNS)
        await ensure_columns(conn, "ai_agent_runs", AI_AGENT_RUN_COLUMNS)
        await ensure_columns(conn, "ai_messages", AI_MESSAGE_COLUMNS)
        await ensure_columns(conn, "campaign_media", CAMPAIGN_MEDIA_COLUMNS)
        await ensure_campaign_media_duplicate_titles_allowed(conn)
        await ensure_columns(conn, "custom_audiences", CUSTOM_AUDIENCE_COLUMNS)
        await ensure_columns(conn, "campaign_runs", CAMPAIGN_RUN_COLUMNS)
        await backfill_campaign_media_uids(conn)
        await backfill_custom_audience_uids(conn)
        await backfill_campaign_run_uids(conn)
        await heal_campaign_run_media_names(conn)
        await backfill_outline_usage_employees(conn)
        await widen_media_url(conn)
        await widen_outline_fingerprint(conn)
        await normalize_status(conn)
        await normalize_kerala_state_labels(conn)
        await normalize_lead_state_script_labels(conn)
        await ensure_adms_map_index(conn)
        await ensure_whatsapp_inbox_indexes(conn)
        await ensure_ai_knowledge_indexes(conn)
        await ensure_kb_fts_index(conn)
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
        await ensure_faisal_display_name(db)
        # Do not re-seed Kerala / GATC RAM demo campaign on deploy — user deleted it.
        try:
            from app.ai.work_reports import backfill_from_tool_calls

            n = await backfill_from_tool_calls(db, limit=500)
            await db.commit()
            if n:
                print(f"Backfilled {n} AI work report(s) from tool history")
        except Exception as exc:  # noqa: BLE001
            print(f"Skipped ai_work_reports backfill ({exc})")
        try:
            from app.audiences import seed_default_audiences

            seeded = await seed_default_audiences(db)
            await db.commit()
            if seeded:
                print(f"Seeded custom audiences: {', '.join(seeded)}")
            else:
                print("Custom audiences already present")
        except Exception as exc:  # noqa: BLE001
            print(f"Skipped custom audience seed ({exc})")
        try:
            from app.ai.knowledge import seed_builtin_knowledge

            kb = await seed_builtin_knowledge(db)
            await db.commit()
            if kb:
                print(f"Seeded AI knowledge rules: {', '.join(kb)}")
        except Exception as exc:  # noqa: BLE001
            print(f"Skipped AI knowledge seed ({exc})")
    await engine.dispose()


async def heal_campaign_run_media_names(conn) -> None:
    """Rewrite Campaign Report media_name to 'title · uid' from joined campaign_media."""
    try:
        result = await conn.execute(
            text(
                """
                UPDATE campaign_runs AS r
                SET media_name = CASE
                  WHEN m.title IS NOT NULL AND btrim(m.title) <> '' AND m.uid IS NOT NULL
                    THEN left(btrim(m.title) || ' · ' || m.uid::text, 240)
                  WHEN m.title IS NOT NULL AND btrim(m.title) <> ''
                    THEN left(btrim(m.title), 240)
                  WHEN m.file_name IS NOT NULL AND btrim(m.file_name) <> '' AND m.uid IS NOT NULL
                    THEN left(btrim(m.file_name) || ' · ' || m.uid::text, 240)
                  ELSE r.media_name
                END
                FROM campaign_media AS m
                WHERE r.media_id = m.id
                  AND (
                    r.media_name IS NULL
                    OR r.media_name = ''
                    OR r.media_name LIKE '% / %'
                    OR (m.uid IS NOT NULL AND r.media_name NOT LIKE '%' || m.uid::text || '%')
                  )
                """
            )
        )
        n = result.rowcount if result is not None else 0
        if n:
            print(f"Healed {n} campaign_runs media_name(s) to title · uid")
    except Exception as exc:  # noqa: BLE001
        print(f"Skipped heal_campaign_run_media_names ({exc})")


async def backfill_campaign_run_uids(conn) -> None:
    """Assign public campaign numbers (uid) from 111 upward by created_at / occurred_at."""
    try:
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_campaign_runs_uid
                ON campaign_runs (uid)
                WHERE uid IS NOT NULL
                """
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Skipped uq_campaign_runs_uid ({exc})")

    rows = (
        await conn.execute(
            text(
                """
                SELECT id FROM campaign_runs
                WHERE uid IS NULL
                ORDER BY
                  COALESCE(created_at, occurred_at, started_at) ASC NULLS LAST,
                  id ASC
                """
            )
        )
    ).fetchall()
    if not rows:
        return
    used = {
        int(r[0])
        for r in (
            await conn.execute(
                text("SELECT uid FROM campaign_runs WHERE uid IS NOT NULL")
            )
        ).fetchall()
        if r[0] is not None
    }
    next_uid = 111
    assigned = 0
    for (row_id,) in rows:
        while next_uid in used:
            next_uid += 1
        await conn.execute(
            text("UPDATE campaign_runs SET uid = :uid WHERE id = :id"),
            {"uid": next_uid, "id": row_id},
        )
        used.add(next_uid)
        next_uid += 1
        assigned += 1
    if assigned:
        print(f"Backfilled {assigned} campaign_runs uid(s) from 111")


async def ensure_campaign_media_duplicate_titles_allowed(conn) -> None:
    """Ensure campaign media topic/title is not unique — only uid is unique."""
    for idx in (
        "uq_campaign_media_title",
        "uq_campaign_media_topic",
        "ix_campaign_media_title",
        "campaign_media_title_key",
    ):
        try:
            await conn.execute(text(f'DROP INDEX IF EXISTS "{idx}"'))
        except Exception as exc:  # noqa: BLE001
            print(f"Skipped drop {idx} ({exc})")
    try:
        await conn.execute(
            text(
                """
                DO $$
                DECLARE r record;
                BEGIN
                  FOR r IN
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = 'campaign_media'::regclass
                      AND contype = 'u'
                      AND pg_get_constraintdef(oid) ILIKE '%title%'
                  LOOP
                    EXECUTE format(
                      'ALTER TABLE campaign_media DROP CONSTRAINT IF EXISTS %I',
                      r.conname
                    );
                  END LOOP;
                END $$;
                """
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Skipped campaign_media title unique constraints ({exc})")


async def backfill_campaign_media_uids(conn) -> None:
    """Assign unique 4-digit UIDs (1000–9999) to campaign_media rows missing uid."""
    try:
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_campaign_media_uid
                ON campaign_media (uid)
                WHERE uid IS NOT NULL
                """
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Skipped uq_campaign_media_uid ({exc})")
    rows = (
        await conn.execute(
            text(
                """
                SELECT id FROM campaign_media
                WHERE uid IS NULL
                ORDER BY id ASC
                """
            )
        )
    ).fetchall()
    if not rows:
        return
    used = {
        int(r[0])
        for r in (
            await conn.execute(
                text("SELECT uid FROM campaign_media WHERE uid IS NOT NULL")
            )
        ).fetchall()
        if r[0] is not None
    }
    next_uid = 1000
    assigned = 0
    for (row_id,) in rows:
        while next_uid <= 9999 and next_uid in used:
            next_uid += 1
        if next_uid > 9999:
            print("No free campaign_media UIDs left (1000–9999)")
            break
        await conn.execute(
            text("UPDATE campaign_media SET uid = :uid WHERE id = :id"),
            {"uid": next_uid, "id": row_id},
        )
        used.add(next_uid)
        next_uid += 1
        assigned += 1
    if assigned:
        print(f"Backfilled {assigned} campaign_media uid(s)")


async def backfill_custom_audience_uids(conn) -> None:
    """Assign unique 2-digit UIDs (11–99) to custom_audiences missing uid.

    Preferred seeds: Dealer Kerala → 11, Leads Kerala → 12; then next free by id.
    """
    try:
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_custom_audiences_uid
                ON custom_audiences (uid)
                WHERE uid IS NOT NULL
                """
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Skipped uq_custom_audiences_uid ({exc})")

    used = {
        int(r[0])
        for r in (
            await conn.execute(
                text("SELECT uid FROM custom_audiences WHERE uid IS NOT NULL")
            )
        ).fetchall()
        if r[0] is not None
    }
    assigned = 0

    preferred = [
        ("Dealer Kerala", 11),
        ("Leads Kerala", 12),
    ]
    for name, want_uid in preferred:
        row = (
            await conn.execute(
                text(
                    """
                    SELECT id, uid FROM custom_audiences
                    WHERE lower(name) = lower(:name)
                    LIMIT 1
                    """
                ),
                {"name": name},
            )
        ).fetchone()
        if not row:
            continue
        row_id, existing_uid = row[0], row[1]
        if existing_uid is not None:
            continue
        if want_uid in used:
            continue
        await conn.execute(
            text("UPDATE custom_audiences SET uid = :uid WHERE id = :id"),
            {"uid": want_uid, "id": row_id},
        )
        used.add(want_uid)
        assigned += 1

    rows = (
        await conn.execute(
            text(
                """
                SELECT id FROM custom_audiences
                WHERE uid IS NULL
                ORDER BY id ASC
                """
            )
        )
    ).fetchall()
    next_uid = 11
    for (row_id,) in rows:
        while next_uid <= 99 and next_uid in used:
            next_uid += 1
        if next_uid > 99:
            print("No free custom_audience UIDs left (11–99)")
            break
        await conn.execute(
            text("UPDATE custom_audiences SET uid = :uid WHERE id = :id"),
            {"uid": next_uid, "id": row_id},
        )
        used.add(next_uid)
        next_uid += 1
        assigned += 1
    if assigned:
        print(f"Backfilled {assigned} custom_audience uid(s)")


async def ensure_faisal_display_name(db) -> None:
    """Rename Farook MZN → Faisal AK on user, employees, and WhatsApp sender labels."""
    from sqlalchemy import text

    row = (
        await db.execute(select(User).where(User.email == "fak.mzn@gmail.com"))
    ).scalar_one_or_none()
    if row and (row.name or "").strip() != "Faisal AK":
        row.name = "Faisal AK"
        print("Updated users.name → Faisal AK (fak.mzn@gmail.com)")

    # Employees linked by email / name variants
    await db.execute(
        text(
            """
            UPDATE employees
            SET name = 'Faisal AK'
            WHERE lower(coalesce(email, '')) = 'fak.mzn@gmail.com'
               OR lower(coalesce(name, '')) IN ('farook mzn', 'farook', 'faisal')
               OR name ILIKE 'Farook%'
            """
        )
    )
    # Inbox outgoing bubble labels are stored on messages
    res = await db.execute(
        text(
            """
            UPDATE whatsapp_messages
            SET sender_name = 'Faisal AK'
            WHERE sender_name ILIKE 'Farook%'
               OR sender_name IN ('Farook MZN', 'Farook', 'Faisal')
            """
        )
    )
    # Instance display names that still say Farook
    await db.execute(
        text(
            """
            UPDATE whatsapp_instances
            SET display_name = 'Faisal AK'
            WHERE display_name ILIKE 'Farook%'
               OR display_name IN ('Farook MZN', 'Farook')
            """
        )
    )
    await db.execute(
        text(
            """
            UPDATE whatsapp_instances
            SET employee_name = 'Faisal AK'
            WHERE employee_name ILIKE 'Farook%'
               OR employee_name IN ('Farook MZN', 'Farook')
            """
        )
    )
    await db.commit()
    try:
        n = res.rowcount if res.rowcount is not None else 0
        if n:
            print(f"Updated {n} whatsapp_messages.sender_name Farook* → Faisal AK")
    except Exception:
        pass


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
