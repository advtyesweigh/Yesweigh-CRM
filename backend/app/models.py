from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(40), default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(40), default="", index=True)
    email: Mapped[str] = mapped_column(String(255), default="")
    company: Mapped[str] = mapped_column(String(160), default="")
    city: Mapped[str] = mapped_column(String(80), default="")
    district: Mapped[str] = mapped_column(String(80), default="")
    state: Mapped[str] = mapped_column(String(80), default="")
    pin: Mapped[str] = mapped_column(String(20), default="")
    source: Mapped[str] = mapped_column(String(80), default="manual")
    status: Mapped[str] = mapped_column(String(40), default="new")
    notes: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(40), default="")
    whatsapp: Mapped[str] = mapped_column(String(40), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    dob: Mapped[date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[str] = mapped_column(String(40), default="")
    blood_group: Mapped[str] = mapped_column(String(20), default="")
    aadhaar: Mapped[str] = mapped_column(String(12), default="")
    pan: Mapped[str] = mapped_column(String(20), default="")
    languages: Mapped[str] = mapped_column(String(255), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(80), default="")
    district: Mapped[str] = mapped_column(String(80), default="")
    city: Mapped[str] = mapped_column(String(80), default="")
    pin: Mapped[str] = mapped_column(String(20), default="")
    emergency_name: Mapped[str] = mapped_column(String(120), default="")
    emergency_phone: Mapped[str] = mapped_column(String(40), default="")
    emergency_relation: Mapped[str] = mapped_column(String(80), default="")
    photo_url: Mapped[str] = mapped_column(String(500), default="")

    company: Mapped[str] = mapped_column(String(160), default="YesWeigh")
    branch: Mapped[str] = mapped_column(String(120), default="")
    department: Mapped[str] = mapped_column(String(80), default="")
    designation: Mapped[str] = mapped_column(String(80), default="")
    reporting_manager: Mapped[str] = mapped_column(String(160), default="")
    join_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    employment_type: Mapped[str] = mapped_column(String(40), default="Permanent")
    shift: Mapped[str] = mapped_column(String(80), default="General shift")
    working_hours: Mapped[str] = mapped_column(String(80), default="")
    weekly_holiday: Mapped[str] = mapped_column(String(40), default="")
    salary: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    probation_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    adms_employee_code: Mapped[str] = mapped_column(String(40), default="")
    adms_device: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(40), default="Active", index=True)

    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    crm_role: Mapped[str] = mapped_column(String(60), default="Employee")
    account_status: Mapped[str] = mapped_column(String(40), default="")
    branch_access: Mapped[str] = mapped_column(String(80), default="Assigned branch only")
    lead_visibility: Mapped[str] = mapped_column(String(80), default="Assigned records only")
    notes: Mapped[str] = mapped_column(Text, default="")
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    documents: Mapped[list["EmployeeDocument"]] = relationship(back_populates="employee")


class EmployeeDocument(Base):
    __tablename__ = "employee_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), index=True)
    doc_type: Mapped[str] = mapped_column(String(120))
    file_name: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(120), default="")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    verification_status: Mapped[str] = mapped_column(String(40), default="Pending")
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    employee: Mapped["Employee"] = relationship(back_populates="documents")


class EmployeeTarget(Base):
    __tablename__ = "employee_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), index=True)
    target_type: Mapped[str] = mapped_column(String(80))
    target_value: Mapped[str] = mapped_column(String(120), default="")
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmployeeActivityLog(Base):
    __tablename__ = "employee_activity_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), index=True)
    activity_type: Mapped[str] = mapped_column(String(80))
    summary: Mapped[str] = mapped_column(String(255), default="")
    meta: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkItem(Base):
    __tablename__ = "work_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="todo")
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EvolutionIntegration(Base):
    __tablename__ = "evolution_integrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    server_url: Mapped[str] = mapped_column(String(500), default="")
    encrypted_api_key: Mapped[str] = mapped_column(Text, default="")
    api_version: Mapped[str] = mapped_column(String(40), default="evolution-go")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    connection_status: Mapped[str] = mapped_column(String(60), default="Disconnected")
    last_health_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    webhook_base_url: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WhatsAppInstance(Base):
    __tablename__ = "whatsapp_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    integration_id: Mapped[int | None] = mapped_column(ForeignKey("evolution_integrations.id"), nullable=True)
    evolution_instance_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    evolution_instance_name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    encrypted_instance_token: Mapped[str] = mapped_column(Text, default="")
    display_name: Mapped[str] = mapped_column(String(160))
    employee_code: Mapped[str] = mapped_column(String(40), default="", index=True)
    employee_name: Mapped[str] = mapped_column(String(160), default="")
    expected_phone_number: Mapped[str] = mapped_column(String(40), default="")
    connected_phone_number: Mapped[str] = mapped_column(String(40), default="")
    profile_name: Mapped[str] = mapped_column(String(160), default="")
    profile_photo: Mapped[str] = mapped_column(String(500), default="")
    integration_type: Mapped[str] = mapped_column(String(80), default="WhatsApp Web / Baileys")
    # evolution = Evolution GO personal phones; meta_cloud = YesOne Meta Cloud API (Care)
    provider: Mapped[str] = mapped_column(String(40), default="evolution", index=True)
    department: Mapped[str] = mapped_column(String(80), default="")
    branch: Mapped[str] = mapped_column(String(120), default="")
    purpose: Mapped[str] = mapped_column(String(80), default="General Communication")
    description: Mapped[str] = mapped_column(Text, default="")
    connection_status: Mapped[str] = mapped_column(String(60), default="Disconnected", index=True)
    registration_status: Mapped[str] = mapped_column(String(60), default="pending")
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    history_sync_status: Mapped[str] = mapped_column(String(40), default="")
    history_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    history_sync_stats: Mapped[str] = mapped_column(Text, default="")
    history_sync_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WhatsAppInstanceEmployee(Base):
    __tablename__ = "whatsapp_instance_employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    whatsapp_instance_id: Mapped[int] = mapped_column(
        ForeignKey("whatsapp_instances.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), index=True)
    is_primary_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    permissions: Mapped[str] = mapped_column(Text, default="")
    assigned_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class WhatsAppInstanceActivityLog(Base):
    __tablename__ = "whatsapp_instance_activity_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh")
    whatsapp_instance_id: Mapped[int] = mapped_column(
        ForeignKey("whatsapp_instances.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    event_type: Mapped[str] = mapped_column(String(80), default="")
    event_details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvolutionWebhookEvent(Base):
    __tablename__ = "evolution_webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh")
    whatsapp_instance_id: Mapped[int | None] = mapped_column(ForeignKey("whatsapp_instances.id"), nullable=True)
    external_event_id: Mapped[str] = mapped_column(String(160), default="", index=True)
    event_type: Mapped[str] = mapped_column(String(80), default="")
    processing_status: Mapped[str] = mapped_column(String(40), default="received")
    payload: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WhatsAppConversation(Base):
    __tablename__ = "whatsapp_conversations"
    __table_args__ = (
        # Speeds inbox list filtered by account chip (instance_id) + recent sort
        Index(
            "ix_wa_conv_org_instance_last_msg",
            "organization_id",
            "whatsapp_instance_id",
            "last_message_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    whatsapp_instance_id: Mapped[int] = mapped_column(ForeignKey("whatsapp_instances.id"), index=True)
    customer_phone: Mapped[str] = mapped_column(String(40), index=True)
    customer_name: Mapped[str] = mapped_column(String(160), default="")
    customer_photo: Mapped[str] = mapped_column(Text, default="")
    customer_jid: Mapped[str] = mapped_column(String(120), default="", index=True)
    # WhatsApp LID (Linked ID) digits — maps @lid chats to the canonical PN conversation.
    customer_lid: Mapped[str] = mapped_column(String(40), default="", index=True)
    status: Mapped[str] = mapped_column(String(40), default="Open", index=True)
    priority: Mapped[str] = mapped_column(String(40), default="")
    tag: Mapped[str] = mapped_column(String(80), default="")
    assigned_employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True, index=True)
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    last_message_preview: Mapped[str] = mapped_column(String(255), default="")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    language: Mapped[str] = mapped_column(String(40), default="")
    region: Mapped[str] = mapped_column(String(120), default="")
    customer_type: Mapped[str] = mapped_column(String(40), default="Customer")
    # Optional link to CRM dealer master (phone match or manual link).
    dealer_id: Mapped[int | None] = mapped_column(ForeignKey("dealers.id"), nullable=True, index=True)
    notes_summary: Mapped[str] = mapped_column(Text, default="")
    # Cached Gemini quick outline for the open chat (text + STT transcripts).
    ai_outline: Mapped[str] = mapped_column(Text, default="")
    ai_outline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ai_outline_fingerprint: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WhatsAppMessage(Base):
    __tablename__ = "whatsapp_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh")
    conversation_id: Mapped[int] = mapped_column(ForeignKey("whatsapp_conversations.id", ondelete="CASCADE"), index=True)
    whatsapp_instance_id: Mapped[int] = mapped_column(ForeignKey("whatsapp_instances.id"), index=True)
    direction: Mapped[str] = mapped_column(String(20))  # in|out|note
    body: Mapped[str] = mapped_column(Text, default="")
    media_url: Mapped[str] = mapped_column(Text, default="")
    media_type: Mapped[str] = mapped_column(String(80), default="")
    media_name: Mapped[str] = mapped_column(String(255), default="")
    # Sarvam STT — voice/ptt/audio only; body stays the media placeholder.
    transcript: Mapped[str] = mapped_column(Text, default="")
    transcript_language: Mapped[str] = mapped_column(String(40), default="")
    transcription_status: Mapped[str] = mapped_column(String(40), default="")  # pending|processing|completed|failed
    transcription_error: Mapped[str] = mapped_column(String(255), default="")
    external_message_id: Mapped[str] = mapped_column(String(160), default="", index=True)
    client_message_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    status: Mapped[str] = mapped_column(String(40), default="sent")  # queued|sending|sent|delivered|read|failed
    sender_employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    sender_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    sender_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class WhatsAppAgentPresence(Base):
    __tablename__ = "whatsapp_agent_presence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh")
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    availability: Mapped[str] = mapped_column(String(40), default="Available")
    open_conversations: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AdmsAttendanceLog(Base):
    """Mirror of ADMS SQLite attendance punches in CRM Postgres."""

    __tablename__ = "adms_attendance_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    adms_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    employee_code: Mapped[str] = mapped_column(String(40), index=True, default="")
    employee_name: Mapped[str] = mapped_column(String(160), default="")
    device_serial: Mapped[str] = mapped_column(String(80), index=True, default="")
    device_name: Mapped[str] = mapped_column(String(120), default="")
    branch: Mapped[str] = mapped_column(String(120), default="")
    punch_time: Mapped[str] = mapped_column(String(40), index=True, default="")
    punch_date: Mapped[str] = mapped_column(String(20), index=True, default="")
    punch_status: Mapped[str] = mapped_column(String(20), default="")
    punch_label: Mapped[str] = mapped_column(String(40), default="")
    verify_method: Mapped[str] = mapped_column(String(20), default="")
    verify_label: Mapped[str] = mapped_column(String(40), default="")
    work_code: Mapped[str] = mapped_column(String(40), default="")
    firebase_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    received_at: Mapped[str] = mapped_column(String(40), default="")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AdmsSyncState(Base):
    __tablename__ = "adms_sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    value: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ZohoIntegration(Base):
    """Single-org Zoho Books connection for YesWeigh CRM."""

    __tablename__ = "zoho_integrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    api_product: Mapped[str] = mapped_column(String(40), default="books")  # books | inventory
    accounts_url: Mapped[str] = mapped_column(String(200), default="")
    api_base_url: Mapped[str] = mapped_column(String(200), default="")
    zoho_org_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    zoho_org_name: Mapped[str] = mapped_column(String(200), default="")
    zoho_org_number: Mapped[str] = mapped_column(String(80), default="")
    # OAuth app credentials (admin-saved). Env ZOHO_CLIENT_* overrides when set.
    client_id: Mapped[str] = mapped_column(String(200), default="")
    encrypted_client_secret: Mapped[str] = mapped_column(Text, default="")
    encrypted_access_token: Mapped[str] = mapped_column(Text, default="")
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, default="")
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    encrypted_webhook_secret: Mapped[str] = mapped_column(Text, default="")
    connection_status: Mapped[str] = mapped_column(String(60), default="disconnected", index=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_status: Mapped[str] = mapped_column(String(60), default="")
    last_sync_error: Mapped[str] = mapped_column(Text, default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connected_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ZohoOAuthState(Base):
    __tablename__ = "zoho_oauth_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    popup: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ZohoWebhookEvent(Base):
    __tablename__ = "zoho_webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_event_id: Mapped[str] = mapped_column(String(160), default="", index=True)
    event_type: Mapped[str] = mapped_column(String(80), default="", index=True)
    module: Mapped[str] = mapped_column(String(80), default="")
    zoho_record_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    processing_status: Mapped[str] = mapped_column(String(40), default="received", index=True)
    payload: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ZohoSale(Base):
    """Normalized Zoho Books invoices / sales documents."""

    __tablename__ = "zoho_sales"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zoho_record_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    document_type: Mapped[str] = mapped_column(String(40), default="invoice", index=True)
    invoice_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    date: Mapped[str] = mapped_column(String(40), default="", index=True)
    customer_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    customer_name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(60), default="", index=True)
    currency_code: Mapped[str] = mapped_column(String(20), default="INR")
    total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    # Tax-exclusive / pre-GST amount (Zoho sub_total, CSV "SubTotal")
    sub_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    # Zoho Books "Sales person" / salesperson_name
    salesperson_name: Mapped[str] = mapped_column(String(160), default="", index=True)
    balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="poll")
    raw_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ZohoInvoiceLineItem(Base):
    """Per-line Zoho invoice items (from backup CSV) for product sales breakdown."""

    __tablename__ = "zoho_invoice_line_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zoho_invoice_id: Mapped[str] = mapped_column(String(80), index=True, default="")
    invoice_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    invoice_date: Mapped[str] = mapped_column(String(40), default="", index=True)
    invoice_status: Mapped[str] = mapped_column(String(60), default="", index=True)
    line_index: Mapped[int] = mapped_column(Integer, default=0)
    item_name: Mapped[str] = mapped_column(String(300), default="")
    item_desc: Mapped[str] = mapped_column(Text, default="")
    zoho_category: Mapped[str] = mapped_column(String(160), default="", index=True)
    product_category: Mapped[str] = mapped_column(String(80), default="", index=True)
    product_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    item_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency_code: Mapped[str] = mapped_column(String(20), default="INR")
    customer_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    customer_name: Mapped[str] = mapped_column(String(200), default="")
    source: Mapped[str] = mapped_column(String(40), default="backup")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ZohoCreditNote(Base):
    """Normalized Zoho Books credit notes / sales returns."""

    __tablename__ = "zoho_credit_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zoho_record_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    creditnote_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    date: Mapped[str] = mapped_column(String(40), default="", index=True)
    customer_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    customer_name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(60), default="", index=True)
    currency_code: Mapped[str] = mapped_column(String(20), default="INR")
    total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    reference_invoice_id: Mapped[str] = mapped_column(String(80), default="")
    source: Mapped[str] = mapped_column(String(40), default="poll")
    raw_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Dealer(Base):
    """CRM dealer master (fed from Zoho dealers staging / future imports)."""

    __tablename__ = "dealers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    company: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(40), default="", index=True)
    email: Mapped[str] = mapped_column(String(255), default="")
    city: Mapped[str] = mapped_column(String(80), default="")
    state: Mapped[str] = mapped_column(String(80), default="")
    gstin: Mapped[str] = mapped_column(String(40), default="")
    zoho_contact_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    source: Mapped[str] = mapped_column(String(40), default="zoho")
    status: Mapped[str] = mapped_column(String(40), default="active", index=True)
    # Key Account Manager / assigned salesman (nullable until set).
    assigned_salesman: Mapped[str | None] = mapped_column(String(160), nullable=True, default=None)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ZohoDealer(Base):
    """Staging table for Zoho Books contacts (customers / dealers)."""

    __tablename__ = "zoho_dealers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zoho_contact_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    contact_name: Mapped[str] = mapped_column(String(200), default="")
    company_name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    mobile: Mapped[str] = mapped_column(String(40), default="")
    gst_no: Mapped[str] = mapped_column(String(40), default="")
    billing_city: Mapped[str] = mapped_column(String(80), default="")
    billing_state: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(60), default="")
    dealer_id: Mapped[int | None] = mapped_column(ForeignKey("dealers.id"), nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CatalogCategory(Base):
    """YesWeigh service catalog categories (synced from Firebase catalogCategories)."""

    __tablename__ = "catalog_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    firebase_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    display_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    product_count: Mapped[int] = mapped_column(Integer, default=0)
    thumbnail_url: Mapped[str] = mapped_column(Text, default="")
    local_image_path: Mapped[str] = mapped_column(String(500), default="")
    is_weighing_scale: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CatalogProduct(Base):
    """YesWeigh service catalog products (synced from Firebase catalogProducts)."""

    __tablename__ = "catalog_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    firebase_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300), default="", index=True)
    sku: Mapped[str] = mapped_column(String(120), default="", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    unit: Mapped[str] = mapped_column(String(40), default="pcs")
    rate: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    stock: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    stock_status: Mapped[str] = mapped_column(String(40), default="", index=True)
    image_url: Mapped[str] = mapped_column(Text, default="")
    image_urls_json: Mapped[str] = mapped_column(Text, default="[]")
    local_image_path: Mapped[str] = mapped_column(String(500), default="")
    category_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    category_name: Mapped[str] = mapped_column(String(200), default="", index=True)
    status: Mapped[str] = mapped_column(String(40), default="active", index=True)
    hsn: Mapped[str] = mapped_column(String(40), default="")
    tax_name: Mapped[str] = mapped_column(String(80), default="")
    tax_percentage: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    hidden_from_catalog: Mapped[bool] = mapped_column(Boolean, default=False)
    model_number: Mapped[str] = mapped_column(String(120), default="")
    discontinued_soon: Mapped[bool] = mapped_column(Boolean, default=False)
    new_arrival: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# AI Agent (Phase 1 foundation)
# ---------------------------------------------------------------------------


class AiSettings(Base):
    """Org-level AI toggles/budgets. API key stays in env only."""

    __tablename__ = "ai_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    provider: Mapped[str] = mapped_column(String(40), default="gemini")
    model: Mapped[str] = mapped_column(String(80), default="gemini-3.5-flash-lite")
    monthly_budget_inr: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("500.00"))
    daily_budget_inr: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("25.00"))
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=2048)
    reserved_daily_inr: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    reserved_monthly_inr: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AiSession(Base):
    __tablename__ = "ai_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(40), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AiMessage(Base):
    __tablename__ = "ai_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("ai_sessions.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    role: Mapped[str] = mapped_column(String(40))  # user | assistant | system | tool
    content: Mapped[str] = mapped_column(Text, default="")
    tool_name: Mapped[str] = mapped_column(String(80), default="")
    meta_json: Mapped[str] = mapped_column(Text, default="")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiAgentRun(Base):
    __tablename__ = "ai_agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("ai_sessions.id"), nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    command: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    provider: Mapped[str] = mapped_column(String(40), default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    result_summary: Mapped[str] = mapped_column(Text, default="")
    events_json: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str] = mapped_column(Text, default="")
    # Soft-hide chat bubbles (per-message ⋮ Delete on /work). Both true → omit run from thread.
    user_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    assistant_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiToolCall(Base):
    __tablename__ = "ai_tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("ai_agent_runs.id", ondelete="CASCADE"), index=True)
    tool_name: Mapped[str] = mapped_column(String(80), index=True)
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="")
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiKnowledge(Base):
    """Learned glossary: aliases, abbreviations, machine short names, report types, facts."""

    __tablename__ = "ai_knowledge"
    __table_args__ = (
        Index("ix_ai_knowledge_org_kind_key", "organization_id", "kind", "key"),
        Index("ix_ai_knowledge_org_updated", "organization_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    # alias | abbreviation | report_type | machine_short_name | fact
    kind: Mapped[str] = mapped_column(String(40), default="fact", index=True)
    key: Mapped[str] = mapped_column(String(120), default="")
    value: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(40), default="chat")  # chat | manual | tool
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AiUsageLog(Base):
    __tablename__ = "ai_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("ai_agent_runs.id"), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    whatsapp_instance_id: Mapped[int | None] = mapped_column(ForeignKey("whatsapp_instances.id"), nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("whatsapp_conversations.id"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(40), default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    purpose: Mapped[str] = mapped_column(String(80), default="command")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    thoughts_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_inr: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    reserved_cost_inr: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
