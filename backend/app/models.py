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
    SmallInteger,
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
    # Single active web session: bumped on each login; JWT carries matching sv + jti.
    session_version: Mapped[int] = mapped_column(Integer, default=0)
    active_session_id: Mapped[str] = mapped_column(String(64), default="")
    # Separate Android YES CRM session — does not revoke the web session.
    mobile_session_id: Mapped[str] = mapped_column(String(64), default="")
    # Last Android YES CRM call-sync heartbeat (connected if recent).
    call_app_last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(40), default="", index=True)
    email: Mapped[str] = mapped_column(String(255), default="")
    company: Mapped[str] = mapped_column(String(160), default="")
    contact_person: Mapped[str] = mapped_column(String(160), default="")
    business_type: Mapped[str] = mapped_column(String(120), default="")
    customer_website: Mapped[str] = mapped_column(String(500), default="")
    lead_score: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    city: Mapped[str] = mapped_column(String(80), default="")
    district: Mapped[str] = mapped_column(String(80), default="")
    state: Mapped[str] = mapped_column(String(80), default="")
    pin: Mapped[str] = mapped_column(String(20), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(80), default="manual")
    status: Mapped[str] = mapped_column(String(40), default="new")
    priority: Mapped[str] = mapped_column(String(40), default="medium")
    product_category: Mapped[str] = mapped_column(String(160), default="")
    estimated_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    campaign: Mapped[str] = mapped_column(String(200), default="")
    tags: Mapped[str] = mapped_column(String(500), default="")
    next_follow_up: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_contacted: Mapped[date | None] = mapped_column(Date, nullable=True)
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
    # Sarvam Instant Voice Cloning — employee outbound sample → saved voice_id (svc-…).
    # Customer TTS send requires voice_tts_enabled (admin opt-in); never auto-send.
    sarvam_voice_id: Mapped[str] = mapped_column(String(120), default="")
    sarvam_voice_name: Mapped[str] = mapped_column(String(120), default="")
    sarvam_voice_sample_url: Mapped[str] = mapped_column(Text, default="")
    sarvam_voice_sample_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sarvam_voice_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voice_tts_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
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
    # AI/heuristic: sales (pricing, quotes) vs service (repairs, visits).
    service_category: Mapped[str] = mapped_column(String(20), default="", index=True)
    tag: Mapped[str] = mapped_column(String(80), default="")
    assigned_employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True, index=True)
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    last_message_preview: Mapped[str] = mapped_column(String(255), default="")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    # True = last customer turn still needs a human reply (DM threads only).
    awaiting_staff_reply: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # When the current awaiting-reply window started (DM only; cleared on staff reply or ack-close).
    awaiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Soft spam flag — hidden from normal inbox / unreplied counts; messages kept.
    is_spam: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    spam_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    spam_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    language: Mapped[str] = mapped_column(String(40), default="")
    region: Mapped[str] = mapped_column(String(120), default="")
    customer_type: Mapped[str] = mapped_column(String(40), default="Customer")
    # Optional link to CRM dealer master (phone match or manual link).
    dealer_id: Mapped[int | None] = mapped_column(ForeignKey("dealers.id"), nullable=True, index=True)
    notes_summary: Mapped[str] = mapped_column(Text, default="")
    # Cached Gemini quick outline for the open chat (text + STT transcripts).
    ai_outline: Mapped[str] = mapped_column(Text, default="")
    ai_outline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ai_outline_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    # Selective auto-reply / learning controls (additive — defaults keep AI silent outbound).
    ai_human_takeover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ai_opt_out: Mapped[bool] = mapped_column(Boolean, default=False)
    preferred_reply_language: Mapped[str] = mapped_column(String(20), default="")
    # New-messenger automation (dealer vs customer → card OCR → lead). Mirrored from
    # whatsapp_new_messenger_onboarding for inbox badges; phone-keyed table is source of truth.
    onboarding_step: Mapped[str] = mapped_column(String(40), default="", index=True)
    onboarding_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    onboarding_lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WhatsAppNewMessengerOnboarding(Base):
    """Idempotent per-phone state for new WhatsApp messengers (cross-instance)."""

    __tablename__ = "whatsapp_new_messenger_onboarding"
    __table_args__ = (
        Index(
            "uq_wa_new_messenger_org_phone",
            "organization_id",
            "phone_key",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    phone_key: Mapped[str] = mapped_column(String(20), index=True)  # last-10 Indian mobile
    # asked_role | asked_card | lead_created | customer | skipped
    step: Mapped[str] = mapped_column(String(40), default="", index=True)
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("whatsapp_conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    whatsapp_instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("whatsapp_instances.id", ondelete="SET NULL"), nullable=True
    )
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True)
    log: Mapped[str] = mapped_column(Text, default="[]")
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
    # Voice: Sarvam STT (+ ml translate). Text/captions: Malayalam translation of body.
    transcript: Mapped[str] = mapped_column(Text, default="")
    # Sarvam STT detected spoken language (before ml translate); drives contact language.
    spoken_language: Mapped[str] = mapped_column(String(40), default="")
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


class WhatsAppReplyEvent(Base):
    """One staff reply that closed an awaiting-customer-reply window (1:1 only)."""

    __tablename__ = "whatsapp_reply_events"
    __table_args__ = (
        Index("ix_wa_reply_evt_org_staff_replied", "organization_id", "whatsapp_instance_id", "replied_at"),
        Index("ix_wa_reply_evt_org_conv", "organization_id", "conversation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("whatsapp_conversations.id", ondelete="CASCADE"), index=True
    )
    whatsapp_instance_id: Mapped[int] = mapped_column(ForeignKey("whatsapp_instances.id"), index=True)
    staff_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    staff_employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    staff_name: Mapped[str] = mapped_column(String(160), default="")
    customer_jid: Mapped[str] = mapped_column(String(120), default="")
    awaiting_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reply_seconds: Mapped[int] = mapped_column(Integer)
    # Elapsed inside / outside IST office windows (sum equals reply_seconds).
    office_seconds: Mapped[int] = mapped_column(Integer, default=0)
    off_seconds: Mapped[int] = mapped_column(Integer, default=0)
    # crm | phone_sync | care_cloud | backfill
    source: Mapped[str] = mapped_column(String(40), default="crm")
    message_id: Mapped[int | None] = mapped_column(ForeignKey("whatsapp_messages.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
    # Intelligence Observe layer. Default off. Separate from the ₹25/₹500 agent caps.
    intel_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    intel_daily_budget_inr: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("10"), server_default="10"
    )
    intel_monthly_budget_inr: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("150"), server_default="150"
    )
    intel_reserved_daily_inr: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("0"), server_default="0"
    )
    intel_reserved_monthly_inr: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("0"), server_default="0"
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AiJob(Base):
    """Postgres queue for intelligence work. Webhooks only insert a row."""

    __tablename__ = "ai_jobs"
    __table_args__ = (
        Index("ix_ai_jobs_claim", "status", "priority", "not_before"),
        Index("uq_ai_jobs_idempotency_key", "idempotency_key", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    job_type: Mapped[str] = mapped_column(String(60), index=True)
    subject_type: Mapped[str] = mapped_column(String(60), default="")
    subject_id: Mapped[str] = mapped_column(String(80), default="")
    idempotency_key: Mapped[str] = mapped_column(String(200))
    input_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    last_error: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[int] = mapped_column(SmallInteger, default=100)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AiFinding(Base):
    """Materialised SQL/rule finding. Every row has at least one source."""

    __tablename__ = "ai_findings"
    __table_args__ = (
        Index(
            "uq_ai_findings_fingerprint",
            "finding_type",
            "subject_type",
            "subject_id",
            "fingerprint",
            unique=True,
        ),
        Index("ix_ai_findings_open", "finding_type", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    finding_type: Mapped[str] = mapped_column(String(40), index=True)
    subject_type: Mapped[str] = mapped_column(String(60))
    subject_id: Mapped[str] = mapped_column(String(80))
    dealer_id: Mapped[int | None] = mapped_column(ForeignKey("dealers.id"), nullable=True, index=True)
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("whatsapp_conversations.id"), nullable=True, index=True
    )
    severity: Mapped[str] = mapped_column(String(20), default="medium")
    title: Mapped[str] = mapped_column(String(240), default="")
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    source_json: Mapped[str] = mapped_column(Text, default="[]")
    fingerprint: Mapped[str] = mapped_column(String(128), default="open")
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)
    work_item_id: Mapped[int | None] = mapped_column(ForeignKey("work_items.id"), nullable=True)
    generated_by: Mapped[str] = mapped_column(String(20), default="sql")
    narration: Mapped[str] = mapped_column(Text, default="")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AiCommitment(Base):
    """Promise extracted from a DM. Does not write awaiting_staff_reply."""

    __tablename__ = "ai_commitments"
    __table_args__ = (
        Index("uq_ai_commitments_fp", "conversation_id", "fingerprint", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("whatsapp_conversations.id", ondelete="CASCADE"), index=True
    )
    dealer_id: Mapped[int | None] = mapped_column(ForeignKey("dealers.id"), nullable=True, index=True)
    promise_text: Mapped[str] = mapped_column(String(240), default="")
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    owner_side: Mapped[str] = mapped_column(String(20), default="unclear")
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)
    evidence_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("whatsapp_messages.id"), nullable=True
    )
    verifying_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("whatsapp_messages.id"), nullable=True
    )
    extractor: Mapped[str] = mapped_column(String(20), default="rule")
    confidence: Mapped[str] = mapped_column(String(20), default="low")
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AiThreadInsight(Base):
    """Secondary intents for a DM. Does not replace service_category."""

    __tablename__ = "ai_thread_insights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("whatsapp_conversations.id", ondelete="CASCADE"), unique=True, index=True
    )
    primary_category: Mapped[str] = mapped_column(String(20), default="")
    secondary_intents: Mapped[str] = mapped_column(Text, default="[]")
    sentiment_label: Mapped[str] = mapped_column(String(20), default="unclear")
    sentiment_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("whatsapp_messages.id"), nullable=True
    )
    outline_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    # Cached Enquiry gate (rules version + fingerprint). Page loads do not call Gemini.
    enquiry_cache: Mapped[str] = mapped_column(Text, default="")
    # Manual Intelligence stage: enquiry|sales_order|waiting|paid|stamping|service|commitments|closed
    stage_override: Mapped[str] = mapped_column(String(40), default="", index=True)
    stage_override_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stage_override_by: Mapped[str] = mapped_column(String(120), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AiStageMove(Base):
    """Training signal: human moved a chat between Intelligence stages (or Closed)."""

    __tablename__ = "ai_stage_moves"
    __table_args__ = (Index("ix_ai_stage_moves_conv_created", "conversation_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("whatsapp_conversations.id", ondelete="CASCADE"), index=True
    )
    customer_jid: Mapped[str] = mapped_column(String(80), default="", index=True)
    from_stage: Mapped[str] = mapped_column(String(40), default="")
    to_stage: Mapped[str] = mapped_column(String(40), default="", index=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor_email: Mapped[str] = mapped_column(String(255), default="")
    # Recent inbound/outbound text that justified the move (for training / audit).
    snippet: Mapped[str] = mapped_column(Text, default="")
    # JSON: {phrases, inbound, outbound, signals, media_hints}
    signals_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiStageKb(Base):
    """Durable stage-classification knowledge from human Move-to labels.

    Phrases extracted on each stage move. Classification consults these so similar
    future chats score toward the human-chosen stage (or exclude for Closed).
    """

    __tablename__ = "ai_stage_kb"
    __table_args__ = (
        Index(
            "uq_ai_stage_kb_org_stage_phrase_pol",
            "organization_id",
            "stage",
            "phrase",
            "polarity",
            unique=True,
        ),
        Index("ix_ai_stage_kb_org_stage", "organization_id", "stage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    # Target stage key (enquiry|sales_order|…|closed). For polarity=exclude use closed.
    stage: Mapped[str] = mapped_column(String(40), default="", index=True)
    # Normalized phrase / pattern fragment (English or Malayalam).
    phrase: Mapped[str] = mapped_column(String(240), default="")
    # positive = evidence for stage; negative = evidence against from_stage; exclude = closed/blast
    polarity: Mapped[str] = mapped_column(String(20), default="positive")
    weight: Mapped[int] = mapped_column(Integer, default=1)
    hit_count: Mapped[int] = mapped_column(Integer, default=1)
    example_snippet: Mapped[str] = mapped_column(Text, default="")
    source_move_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_stage_moves.id", ondelete="SET NULL"), nullable=True
    )
    source_jid: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AiDealerHealth(Base):
    """Daily SQL snapshot. health_band is a documented rule, not a model score."""

    __tablename__ = "ai_dealer_health"
    __table_args__ = (
        Index("uq_ai_dealer_health_day", "dealer_id", "as_of", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    dealer_id: Mapped[int] = mapped_column(ForeignKey("dealers.id", ondelete="CASCADE"), index=True)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    sales_fy: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    sales_prior_fy: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    last_sale_date: Mapped[str] = mapped_column(String(40), default="")
    invoice_count_12m: Mapped[int] = mapped_column(Integer, default=0)
    open_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    open_service_threads: Mapped[int] = mapped_column(Integer, default=0)
    unreplied_threads: Mapped[int] = mapped_column(Integer, default=0)
    open_commitments: Mapped[int] = mapped_column(Integer, default=0)
    health_band: Mapped[str] = mapped_column(String(20), default="steady")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiJobCheckpoint(Base):
    """Backfill cursor. Observe does not start a backfill on deploy."""

    __tablename__ = "ai_job_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stream: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    cursor_id: Mapped[int] = mapped_column(Integer, default=0)
    rows_done: Mapped[int] = mapped_column(Integer, default=0)
    rows_skipped: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_inr: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    spent_inr: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    paused: Mapped[bool] = mapped_column(Boolean, default=True)
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


class AiWorkReport(Base):
    """Task log for Work Reports tab — significant AI Agent tool outcomes."""

    __tablename__ = "ai_work_reports"
    __table_args__ = (
        Index("ix_ai_work_reports_org_occurred", "organization_id", "occurred_at"),
        Index("ix_ai_work_reports_org_status", "organization_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tool_call_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_tool_calls.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(80), default="", index=True)
    task_name: Mapped[str] = mapped_column(String(200), default="")
    agent_name: Mapped[str] = mapped_column(String(200), default="")
    task_details: Mapped[str] = mapped_column(Text, default="")
    # completed | failed | stopped | running
    status: Mapped[str] = mapped_column(String(40), default="completed", index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


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


# ---------------------------------------------------------------------------
# AI Learning / FAQ Knowledge Base / Selective Auto-Reply
# (Separate from ai_knowledge glossary used by the /work AI Agent.)
# ---------------------------------------------------------------------------


class KbArticle(Base):
    """FAQ / business knowledge articles. Never auto-published — admin approves."""

    __tablename__ = "kb_articles"
    __table_args__ = (
        Index("ix_kb_articles_org_status", "organization_id", "status"),
        Index("ix_kb_articles_org_updated", "organization_id", "updated_at"),
        Index("ix_kb_articles_org_category", "organization_id", "category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    title: Mapped[str] = mapped_column(String(240), default="")
    question: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    # greeting|product_info|catalogue|warranty|dealer_registration|troubleshooting|
    # company_info|working_hours|software|sales|complaint|other
    category: Mapped[str] = mapped_column(String(60), default="other", index=True)
    product: Mapped[str] = mapped_column(String(160), default="")
    keywords: Mapped[str] = mapped_column(Text, default="")  # comma-separated
    language: Mapped[str] = mapped_column(String(20), default="en", index=True)
    # suggested|under_review|approved|rejected|archived
    status: Mapped[str] = mapped_column(String(40), default="suggested", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    source_conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("whatsapp_conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_message_ids: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of public_ids
    similar_to_id: Mapped[int | None] = mapped_column(ForeignKey("kb_articles.id"), nullable=True)
    contradiction_of_id: Mapped[int | None] = mapped_column(ForeignKey("kb_articles.id"), nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    helpful_count: Mapped[int] = mapped_column(Integer, default=0)
    incorrect_count: Mapped[int] = mapped_column(Integer, default=0)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_notes: Mapped[str] = mapped_column(Text, default="")
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class KbArticleVersion(Base):
    __tablename__ = "kb_article_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("kb_articles.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(240), default="")
    question: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(60), default="")
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    change_reason: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KbLearningCheckpoint(Base):
    """Per-conversation incremental learning cursor — skip unchanged threads."""

    __tablename__ = "kb_learning_checkpoints"
    __table_args__ = (
        Index("uq_kb_learn_conv", "conversation_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("whatsapp_conversations.id", ondelete="CASCADE"), index=True
    )
    last_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_fingerprint: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(40), default="idle")  # idle|processing|done|skipped|failed
    suggestions_created: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_analysed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AiCorrection(Base):
    """Employee corrections to transcripts / translations / summaries / AI replies / KB."""

    __tablename__ = "ai_corrections"
    __table_args__ = (
        Index("ix_ai_corrections_org_type", "organization_id", "field_type"),
        Index("ix_ai_corrections_target", "target_type", "target_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    # transcript|translation|summary|suggested_reply|kb_suggestion
    field_type: Mapped[str] = mapped_column(String(40), default="", index=True)
    # message|conversation|kb_article|suggestion
    target_type: Mapped[str] = mapped_column(String(40), default="")
    target_id: Mapped[str] = mapped_column(String(80), default="")  # public_id or numeric as str
    original_value: Mapped[str] = mapped_column(Text, default="")
    corrected_value: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    locked: Mapped[bool] = mapped_column(Boolean, default=True)  # jobs must not overwrite
    meta_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class WaAutoReplyConfig(Base):
    """Per WhatsApp instance auto-reply policy. Auto-send OFF until admin enables."""

    __tablename__ = "wa_auto_reply_configs"
    __table_args__ = (
        Index("uq_wa_auto_reply_instance", "whatsapp_instance_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    whatsapp_instance_id: Mapped[int] = mapped_column(
        ForeignKey("whatsapp_instances.id", ondelete="CASCADE"), index=True
    )
    # disabled | suggest | rule_based | hybrid — default suggest (no auto-send)
    mode: Mapped[str] = mapped_column(String(40), default="suggest")
    # CRITICAL: must stay false until admin explicitly enables customer auto-send
    auto_send_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    emergency_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled_topics: Mapped[str] = mapped_column(
        Text,
        default=(
            '["greeting","product_info","catalogue","warranty","dealer_registration",'
            '"troubleshooting","company_info","working_hours","software"]'
        ),
    )
    require_approved_kb: Mapped[bool] = mapped_column(Boolean, default=True)
    max_auto_replies_per_day: Mapped[int] = mapped_column(Integer, default=50)
    max_auto_replies_per_conversation: Mapped[int] = mapped_column(Integer, default=3)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WaAiSuggestion(Base):
    """Suggested (or optionally sent) AI reply for a WhatsApp conversation."""

    __tablename__ = "wa_ai_suggestions"
    __table_args__ = (
        Index("ix_wa_ai_sugg_conv_status", "conversation_id", "status"),
        Index("ix_wa_ai_sugg_org_created", "organization_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("whatsapp_conversations.id", ondelete="CASCADE"), index=True
    )
    whatsapp_instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("whatsapp_instances.id"), nullable=True, index=True
    )
    trigger_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("whatsapp_messages.id", ondelete="SET NULL"), nullable=True
    )
    intent: Mapped[str] = mapped_column(String(80), default="")
    product: Mapped[str] = mapped_column(String(160), default="")
    reply_text: Mapped[str] = mapped_column(Text, default="")
    reply_language: Mapped[str] = mapped_column(String(20), default="en")
    kb_article_ids: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of kb public_ids
    matched_knowledge_title: Mapped[str] = mapped_column(String(240), default="")
    confidence: Mapped[str] = mapped_column(String(20), default="low")  # low|medium|high
    needs_handover: Mapped[bool] = mapped_column(Boolean, default=False)
    next_action: Mapped[str] = mapped_column(String(120), default="")
    # pending|edited|sent|discarded|handover|auto_sent|blocked
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    feedback: Mapped[str] = mapped_column(String(40), default="")  # helpful|incorrect|""
    is_automated: Mapped[bool] = mapped_column(Boolean, default=False)
    block_reason: Mapped[str] = mapped_column(String(255), default="")
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    sent_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("whatsapp_messages.id", ondelete="SET NULL"), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CampaignMediaFolder(Base):
    """Lightweight folder for organizing campaign media library items."""

    __tablename__ = "campaign_media_folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CampaignMedia(Base):
    """Uploaded campaign media (video / image / document) stored on VPS disk."""

    __tablename__ = "campaign_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Public 4-digit UID (1000–9999) for AI Agent / UI reference — not the PK.
    uid: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True, index=True)
    # Display topic/name — duplicates allowed; public disambiguation is uid (1000–9999).
    title: Mapped[str] = mapped_column(String(200), default="")
    file_name: Mapped[str] = mapped_column(String(255), default="")
    stored_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    stored_path: Mapped[str] = mapped_column(String(500), default="")
    cover_name: Mapped[str] = mapped_column(String(255), default="")
    cover_path: Mapped[str] = mapped_column(String(500), default="")
    mime_type: Mapped[str] = mapped_column(String(120), default="")
    # video | image | document — inferred from mime/extension on upload
    media_kind: Mapped[str] = mapped_column(String(40), default="video", index=True)
    tags: Mapped[str] = mapped_column(Text, default="[]")  # JSON string list
    folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaign_media_folders.id"), nullable=True, index=True
    )
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    # CRM market language bucket (ml, ta, hi, …) — same codes as lead language tiles.
    language: Mapped[str] = mapped_column(String(20), default="", index=True)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Campaign(Base):
    """Lightweight campaign record; media linked via JSON id list for later send flows."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(40), default="draft", index=True)  # draft|active|completed
    description: Mapped[str] = mapped_column(Text, default="")
    media_ids: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of campaign_media.id
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CampaignRun(Base):
    """One WhatsApp blast / campaign send — powers Campaigns → Report table."""

    __tablename__ = "campaign_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Public campaign number / UID shown on Report (starts at 111)
    uid: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True, index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True, index=True)
    batch_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    tool_name: Mapped[str] = mapped_column(String(80), default="", index=True)
    agent_name: Mapped[str] = mapped_column(String(200), default="")
    media_name: Mapped[str] = mapped_column(String(240), default="")
    media_id: Mapped[int | None] = mapped_column(ForeignKey("campaign_media.id"), nullable=True, index=True)
    audience_name: Mapped[str] = mapped_column(String(200), default="")
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    # Recipients who sent an inbound WhatsApp reply after this run's outbound.
    replied_count: Mapped[int] = mapped_column(Integer, default=0)
    from_instance_public_id: Mapped[str] = mapped_column(String(80), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Paced sends (24h/48h background worker): completed|running|queued|paused|failed|stopped
    status: Mapped[str] = mapped_column(String(40), default="completed", index=True)
    total_hours: Mapped[int] = mapped_column(Integer, default=0)
    next_index: Mapped[int] = mapped_column(Integer, default=0)
    recipients_json: Mapped[str] = mapped_column(Text, default="[]")
    gaps_json: Mapped[str] = mapped_column(Text, default="[]")
    caption: Mapped[str] = mapped_column(Text, default="")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    filter_meta_json: Mapped[str] = mapped_column(Text, default="{}")


class CustomAudience(Base):
    """Custom audience — geo filters and/or static phone lists resolve at read time."""

    __tablename__ = "custom_audiences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Public 2-digit UID (11–99), like campaign_media.uid but shorter
    uid: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    # dealers | leads — maps to CRM left-menu Dealer / Leads
    source: Mapped[str] = mapped_column(String(40), default="dealers", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # JSON: {"state":"Keralam"} and/or {"phones":"91…|91…"} for static lists — extensible
    filters_json: Mapped[str] = mapped_column(Text, default="{}")
    # Cached display count from last resolve (live count still computed on read)
    last_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_count_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Dealer Finder (Gemini location plans + Places batch + search history)
# ---------------------------------------------------------------------------


class DealerLocationPlan(Base):
    """Cached Gemini expansion plan: state → districts → cities/towns."""

    __tablename__ = "dealer_location_plans"
    __table_args__ = (
        Index(
            "uq_dealer_location_plans_org_state_ver",
            "organization_id",
            "state",
            "plan_version",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    country: Mapped[str] = mapped_column(String(8), default="IN")
    state: Mapped[str] = mapped_column(String(80), index=True)
    plan_version: Mapped[str] = mapped_column(String(40), default="v1-district-city-expand")
    # JSON: {"districts":[{"name":"Chennai","cities":["T. Nagar",...]}, ...], "source":"gemini+verified"}
    plan_json: Mapped[str] = mapped_column(Text, default="{}")
    district_count: Mapped[int] = mapped_column(Integer, default=0)
    city_count: Mapped[int] = mapped_column(Integer, default=0)
    gemini_model: Mapped[str] = mapped_column(String(80), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DealerFinderSettings(Base):
    """Admin cost / cooldown controls for Dealer Finder."""

    __tablename__ = "dealer_finder_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", unique=True, index=True)
    cooldown_days: Mapped[int] = mapped_column(Integer, default=30)
    # Two-tier Places cost model (monthly):
    #   1) free_requests_per_month — Google-style FREE request quota (default 7000).
    #      Not a hard stop: after free is used, paid requests continue.
    #   2) max_monthly_spend_usd — paid ceiling AFTER free tier (default $100).
    #      New Places calls stop when estimated paid spend reaches this cap.
    # max_requests_per_month is a legacy alias of the free quota (same meaning).
    # Job/day caps are operational safety only (must be high enough not to block the $100 paid allowance).
    max_requests_per_job: Mapped[int] = mapped_column(Integer, default=100_000)
    max_requests_per_day: Mapped[int] = mapped_column(Integer, default=100_000)
    free_requests_per_month: Mapped[int] = mapped_column(Integer, default=7000)
    max_requests_per_month: Mapped[int] = mapped_column(Integer, default=7000)  # alias of free quota
    max_monthly_spend_usd: Mapped[Numeric] = mapped_column(Numeric(12, 2), default=Decimal("100.00"))
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, default=1)
    # always | when_needed | never — Stage-2 phone/website fetch policy
    expensive_field_policy: Mapped[str] = mapped_column(String(40), default="when_needed")
    persist_website_on_import: Mapped[bool] = mapped_column(Boolean, default=True)
    # All-India discovery gates (never auto-start full country on deploy).
    all_india_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    pilot_district_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    pilot_state: Mapped[str] = mapped_column(String(80), default="Rajasthan")
    pilot_district: Mapped[str] = mapped_column(String(80), default="Jaipur")
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DealerAllIndiaRun(Base):
    """Orchestrates sequential statewide Dealer Finder jobs (pilot or full India)."""

    __tablename__ = "dealer_all_india_runs"
    __table_args__ = (
        Index("ix_dealer_all_india_runs_org_status", "organization_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    mode: Mapped[str] = mapped_column(String(20), default="pilot")  # pilot | full
    status: Mapped[str] = mapped_column(String(40), default="Pending", index=True)
    states_json: Mapped[str] = mapped_column(Text, default="[]")
    skipped_states_json: Mapped[str] = mapped_column(Text, default="[]")
    state_index: Mapped[int] = mapped_column(Integer, default=0)
    completed_states: Mapped[int] = mapped_column(Integer, default=0)
    pilot_state: Mapped[str] = mapped_column(String(80), default="")
    pilot_district: Mapped[str] = mapped_column(String(80), default="")
    active_job_public_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    logs_json: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DealerSearchJob(Base):
    """Batch job: state → district/city × category Places expansion."""

    __tablename__ = "dealer_search_jobs"
    __table_args__ = (
        Index("ix_dealer_search_jobs_org_status", "organization_id", "status"),
        Index("ix_dealer_search_jobs_org_state", "organization_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    status: Mapped[str] = mapped_column(String(40), default="Pending", index=True)
    state: Mapped[str] = mapped_column(String(80), default="", index=True)
    district_filter: Mapped[str] = mapped_column(String(80), default="")
    city_filter: Mapped[str] = mapped_column(String(80), default="")
    categories_json: Mapped[str] = mapped_column(Text, default="[]")
    search_depth: Mapped[str] = mapped_column(String(40), default="full")  # full|district_only|city_only
    lead_owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    location_plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("dealer_location_plans.id"), nullable=True
    )
    force_refresh: Mapped[bool] = mapped_column(Boolean, default=False)
    total_units: Mapped[int] = mapped_column(Integer, default=0)
    completed_units: Mapped[int] = mapped_column(Integer, default=0)
    skipped_units: Mapped[int] = mapped_column(Integer, default=0)
    failed_units: Mapped[int] = mapped_column(Integer, default=0)
    places_found: Mapped[int] = mapped_column(Integer, default=0)
    places_new: Mapped[int] = mapped_column(Integer, default=0)
    api_requests: Mapped[int] = mapped_column(Integer, default=0)
    next_unit_index: Mapped[int] = mapped_column(Integer, default=0)
    units_json: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DealerSearchLog(Base):
    """Per location×category Places search history (cooldown identity)."""

    __tablename__ = "dealer_search_logs"
    __table_args__ = (
        Index("ix_dealer_search_logs_org_state", "organization_id", "state"),
        Index("ix_dealer_search_logs_searched_at", "searched_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    country: Mapped[str] = mapped_column(String(8), default="IN")
    state: Mapped[str] = mapped_column(String(80), default="", index=True)
    district: Mapped[str] = mapped_column(String(80), default="", index=True)
    city: Mapped[str] = mapped_column(String(80), default="", index=True)
    category: Mapped[str] = mapped_column(String(160), default="", index=True)
    language: Mapped[str] = mapped_column(String(16), default="en")
    plan_version: Mapped[str] = mapped_column(String(40), default="")
    max_results: Mapped[int] = mapped_column(Integer, default=60)
    page_size: Mapped[int] = mapped_column(Integer, default=20)
    text_query: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(40), default="Completed", index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("dealer_search_jobs.id"), nullable=True)
    pages_fetched: Mapped[int] = mapped_column(Integer, default=0)
    results_count: Mapped[int] = mapped_column(Integer, default=0)
    with_phone_count: Mapped[int] = mapped_column(Integer, default=0)
    with_website_count: Mapped[int] = mapped_column(Integer, default=0)
    api_requests: Mapped[int] = mapped_column(Integer, default=0)
    next_page_token: Mapped[str] = mapped_column(String(500), default="")
    pages_done: Mapped[int] = mapped_column(Integer, default=0)
    force_refresh: Mapped[bool] = mapped_column(Boolean, default=False)
    searched_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    searched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DealerSearchPlace(Base):
    """Places discovered by a search log (job-scoped; import into CRM when eligible)."""

    __tablename__ = "dealer_search_places"
    __table_args__ = (
        Index("uq_dealer_search_places_log_place", "search_log_id", "place_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_log_id: Mapped[int] = mapped_column(
        ForeignKey("dealer_search_logs.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("dealer_search_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    place_id: Mapped[str] = mapped_column(String(255), default="", index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    city: Mapped[str] = mapped_column(String(80), default="")
    district: Mapped[str] = mapped_column(String(80), default="")
    state: Mapped[str] = mapped_column(String(80), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    website: Mapped[str] = mapped_column(String(500), default="")
    category: Mapped[str] = mapped_column(String(160), default="")
    maps_url: Mapped[str] = mapped_column(String(500), default="")
    lead_source: Mapped[str] = mapped_column(String(80), default="Google")
    crm_status: Mapped[str] = mapped_column(String(40), default="new")  # new|lead|dealer
    crm_match: Mapped[str] = mapped_column(String(120), default="")
    imported_lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DealerPlacesUsage(Base):
    """Track Places API request counts / estimated billing tier spend."""

    __tablename__ = "dealer_places_usage"
    __table_args__ = (
        Index("ix_dealer_places_usage_org_day", "organization_id", "usage_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(80), default="yesweigh", index=True)
    usage_date: Mapped[date] = mapped_column(Date, index=True)
    stage1_requests: Mapped[int] = mapped_column(Integer, default=0)
    stage2_requests: Mapped[int] = mapped_column(Integer, default=0)
    estimated_usd: Mapped[Numeric] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CallLog(Base):
    """Agent device call events synced from the YES CRM Android app."""

    __tablename__ = "call_logs"
    __table_args__ = (
        Index(
            "uq_call_logs_agent_phone_started_type",
            "agent_user_id",
            "phone_normalized",
            "started_at",
            "call_type",
            unique=True,
        ),
        Index("ix_call_logs_started_at", "started_at"),
        Index("ix_call_logs_call_type", "call_type"),
        Index("ix_call_logs_agent_started", "agent_user_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    phone_number: Mapped[str] = mapped_column(String(40), default="")
    phone_normalized: Mapped[str] = mapped_column(String(40), default="", index=True)
    contact_name: Mapped[str] = mapped_column(String(160), default="")
    # missed | received | rejected | outbound
    # index via __table_args__ ix_call_logs_call_type (do not also set index=True — duplicate name aborts create_all)
    call_type: Mapped[str] = mapped_column(String(40), default="")
    # inbound | outbound
    direction: Mapped[str] = mapped_column(String(20), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    android_call_id: Mapped[str] = mapped_column(String(80), default="")
    android_type_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    device_id: Mapped[str] = mapped_column(String(120), default="")
    device_label: Mapped[str] = mapped_column(String(160), default="")
    client_event_id: Mapped[str] = mapped_column(String(120), default="")
    meta_json: Mapped[str] = mapped_column(Text, default="")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
