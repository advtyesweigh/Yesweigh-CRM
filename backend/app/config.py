from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "YesWeigh CRM"
    database_url: str = "postgresql+asyncpg://crm:crm@localhost:5432/yesweigh_crm"
    secret_key: str = "change-me"
    admin_email: str = "admin@yesweigh.in"
    admin_password: str = "Yesweigh@2026"
    evolution_base_url: str = "http://127.0.0.1:18082"
    evolution_api_key: str = ""
    adms_base_url: str = "http://127.0.0.1:8081"
    adms_username: str = "admin"
    adms_password: str = "ChangeMeNow!"
    adms_sync_enabled: bool = True
    adms_sync_interval_seconds: int = 20
    adms_sync_batch_size: int = 500
    # Fallback env knobs; live report uses Mon–Fri 09:30–17:30 / Sat 09:30–17:00 / Sun off.
    adms_shift_start: str = "09:30"
    adms_shift_end: str = "17:30"
    cors_origins: str = "*"
    access_token_expire_minutes: int = 60 * 24 * 7
    public_base_url: str = "https://crm.yesweigh.in"
    # Zoho Books OAuth (India DC defaults). Leave blank until secrets are set on the VPS.
    zoho_client_id: str = ""
    zoho_client_secret: str = ""
    zoho_accounts_url: str = "https://accounts.zoho.in"
    zoho_api_base_url: str = "https://www.zohoapis.in"
    zoho_oauth_scopes: str = "ZohoBooks.fullaccess.all"
    zoho_webhook_secret: str = ""  # optional env bootstrap; DB secret preferred
    # Firebase catalog sync (yesweigh-service). Credentials via env only — never commit.
    firebase_api_key: str = "AIzaSyBiLI--JR640YlXb2uiuBrLJ83XtFMPncE"
    firebase_project_id: str = "yesweigh-service"
    firebase_email: str = ""
    firebase_login_id: str = ""  # Aadhaar / phone → synthetic @yesweigh.auth email
    firebase_password: str = ""
    catalog_image_dir: str = "/data/catalog-images"
    # Google Places enrichment (server-side only — never expose to frontend / git).
    google_maps_api_key: str = ""
    google_places_api_key: str = ""
    # Extra confirmation for destructive WhatsApp instance delete (override via env on VPS).
    whatsapp_instance_delete_password: str = "1010"
    # AI Agent (Gemini). Never commit real GEMINI_API_KEY — public repos.
    ai_provider: str = "gemini"
    ai_model: str = "gemini-3.5-flash-lite"
    gemini_api_key: str = ""
    ai_enabled: bool = True
    ai_monthly_budget_inr: float = 500.0
    ai_daily_budget_inr: float = 25.0
    ai_max_output_tokens: int = 2048
    ai_worker_concurrency: int = 2
    # Live AI tool factory (Cursor Cloud Agents). Prefer CURSOR_FAISAL_API_KEY on VPS.
    cursor_faisal_api_key: str = ""
    cursor_api_key: str = ""  # optional alias
    ai_tool_factory_enabled: bool = True
    # Cursor factory poll budget (keep short — builtins cover product×dealer sales).
    ai_tool_factory_timeout_s: float = 90.0
    # Hard cap for one AI command (Gemini + tools + optional factory).
    ai_run_timeout_s: float = 180.0
    # Mark abandoned "running" rows failed (API restart / cancelled background task).
    ai_run_stale_s: float = 600.0
    # Host/volume path for generated tool modules (survives image rebuilds).
    ai_generated_tools_dir: str = "/data/ai-generated-tools"
    # Rough Gemini Flash-Lite list pricing (USD / 1M tokens) for INR estimates.
    ai_input_usd_per_1m: float = 0.10
    ai_output_usd_per_1m: float = 0.40
    ai_usd_inr: float = 84.0
    # Sarvam AI speech-to-text (WhatsApp voice notes). Server-only — never commit real key.
    sarvam_api_key: str = ""
    sarvam_base_url: str = "https://api.sarvam.ai"
    sarvam_stt_model: str = "saaras:v3"
    # Care = YesOne Meta Cloud WhatsApp (service.yesweigh.in). Secrets via env only.
    care_webhook_secret: str = ""
    # Meta hub.verify_token for /api/webhooks/meta-whatsapp (optional; else fetched from YesOne).
    care_meta_verify_token: str = ""
    # Optional override; default dual-forwards to YesOne Cloud ingest.
    yesone_cloud_webhook_url: str = (
        "https://asia-south1-yesweigh-service.cloudfunctions.net/ingestWhatsAppCloudWebhook"
    )
    # Optional direct Graph send (prefer YesOne callable / proxy so token stays in Firestore).
    whatsapp_cloud_access_token: str = ""
    whatsapp_cloud_phone_number_id: str = ""
    whatsapp_cloud_graph_version: str = "v21.0"
    # YesOne HTTP proxy for Care replies (optional; callable is preferred until deployed).
    yesone_care_send_url: str = (
        "https://asia-south1-yesweigh-service.cloudfunctions.net/crmCareSendWhatsApp"
    )
    care_sync_conversation_limit: int = 120
    care_sync_message_limit: int = 200


@lru_cache
def get_settings() -> Settings:
    return Settings()
