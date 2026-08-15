from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# app/core/config.py -> app/core -> app -> backend
BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # Anchored to the backend directory rather than left relative, so the
    # settings load the same whether the process starts in backend/, in the
    # repository root, or anywhere else. Real environment variables still win
    # over the file, which is what deployment and CI will rely on.
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", extra="ignore"
    )

    database_url: str
    jwt_secret: str
    pin_pepper: str

    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 30

    otp_length: int = 6
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 5

    pin_length: int = 5
    hold_minutes: int = 10
    offline_ttl_hours: int = 24

    rental_durations: tuple[int, ...] = (12, 24, 48)
    default_language: str = "tk"
    supported_languages: tuple[str, ...] = ("tk", "ru", "en")


@lru_cache
def get_settings() -> Settings:
    return Settings()
