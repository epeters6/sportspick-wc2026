from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str

    twitter_auth_token: str = ""
    twitter_ct0: str = ""

    tiktok_session_id: str = ""
    tiktok_ms_token: str = ""

    instagram_username: str = ""
    instagram_password: str = ""

    youtube_api_key: str = ""

    wc_api_key: str = ""
    wc_api_base: str = "https://api.wc2026api.com/v1"

    app_env: str = "development"
    log_level: str = "INFO"
    scrape_interval_minutes: int = 30
    top_influencers_seed: int = 100

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
