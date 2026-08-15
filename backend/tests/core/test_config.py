from app.core.config import get_settings


def test_settings_expose_pinned_product_constants():
    settings = get_settings()
    assert settings.otp_length == 6
    assert settings.pin_length == 5
    assert settings.hold_minutes == 10
    assert settings.offline_ttl_hours == 24
    assert settings.rental_durations == (12, 24, 48)
    assert settings.default_language == "tk"


def test_get_settings_is_cached():
    assert get_settings() is get_settings()
