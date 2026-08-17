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

    # Payments. The provider is named rather than imported, so the mock and the
    # real acquirer are chosen by configuration and not by an edit.
    payment_provider: str = "mock"
    payment_webhook_secret: str = "dev-webhook-secret-change-me"
    payment_return_url: str = "https://example.invalid/payment/done"

    # SMS. One channel carries both the login code and the courier PIN.
    # "log" in development and in tests; "post_tm" against the real gateway.
    sms_provider: str = "log"
    sms_api_token: str = ""
    sms_base_url: str = "https://sms.post.tm"
    sms_timeout_seconds: float = 10.0

    # The overdue clock. Every stage is configuration because the product will
    # tune these without a deploy, and because a literal buried in a worker is a
    # rule nobody can find.
    reminder_hours_before: int = 2
    grace_hours: int = 2
    removal_after_hours: int = 24
    custody_disposal_days: int = 30

    # Uploads. Photographs of a postamat, so a customer can recognise the
    # machine and an operator can confirm which one they are looking at.
    media_root: str = "media"
    max_upload_bytes: int = 5 * 1024 * 1024

    rental_durations: tuple[int, ...] = (12, 24, 48)
    default_language: str = "tk"
    supported_languages: tuple[str, ...] = ("tk", "ru", "en")


@lru_cache
def get_settings() -> Settings:
    return Settings()
