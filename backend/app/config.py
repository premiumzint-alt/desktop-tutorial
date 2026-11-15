from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./monitor.db"
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "server_monitor"
    jwt_secret_key: str = "local-development-only-change-this-secret"
    access_token_minutes: int = 30
    collector_api_key: str = ""
    bootstrap_token: str = ""
    metrics_retention_days: int = 7
    security_events_retention_days: int = 30
    scan_results_retention_days: int = 30
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15
    login_window_seconds: int = 300
    login_max_per_window: int = 20
    cors_origins: str = "http://localhost:8001,http://127.0.0.1:8001"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()