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
    firebase_password: str = ""
    catalog_image_dir: str = "/data/catalog-images"


@lru_cache
def get_settings() -> Settings:
    return Settings()
