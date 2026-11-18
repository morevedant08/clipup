"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed runtime settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_env: str = Field(default="development")
    app_name: str = Field(default="Clipper")
    log_level: str = Field(default="INFO")
    backend_cors_origins: str = Field(default="http://localhost:3000")
    frontend_url: str = Field(default="http://localhost:3000")
    backend_url: str = Field(default="http://localhost:8000")

    # Security
    jwt_secret_key: str = Field(default="dev-insecure-jwt-secret-change-in-production")
    jwt_algorithm: str = Field(default="HS256")
    jwt_expire_minutes: int = Field(default=1440)
    token_encryption_key: str = Field(default="")
    enable_dev_auth: bool = Field(default=False)

    # Database
    database_url: str = Field(
        default="postgresql+psycopg://clipper:clipper_dev_password@localhost:5432/clipper"
    )

    # Redis & Celery
    redis_url: str = Field(default="redis://localhost:6379/0")
    celery_broker_url: str = Field(default="redis://localhost:6379/0")
    celery_result_backend: str = Field(default="redis://localhost:6379/1")

    # Google OAuth & YouTube Data API v3 (read-only scope only)
    google_client_id: str = Field(default="")
    google_client_secret: str = Field(default="")
    google_oauth_redirect_uri: str = Field(
        default="http://localhost:8000/api/auth/google/callback"
    )
    youtube_connect_redirect_uri: str = Field(
        default="http://localhost:8000/api/auth/youtube/callback"
    )
    youtube_api_key: str = Field(default="")

    # Media storage & clipping
    storage_root: str = Field(default="./storage")
    clip_engine: str = Field(default="stub")
    max_video_duration_sec: int = Field(default=7200)
    default_clip_count: int = Field(default=3)
    default_clip_duration_sec: int = Field(default=30)

    @property
    def cors_origins(self) -> list[str]:
        """Parse comma-separated CORS origins."""
        return [
            origin.strip()
            for origin in self.backend_cors_origins.split(",")
            if origin.strip()
        ]

    @property
    def storage_path(self) -> Path:
        """Return resolved Path object for media storage."""
        return Path(self.storage_root).resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()

