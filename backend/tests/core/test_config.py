from app.core.config import get_settings


def test_settings_expose_pinned_product_constants():
    settings = get_settings()
    assert settings.otp_length == 6
    assert settings.pin_length == 5
    assert settings.hold_minutes == 10
    assert settings.offline_ttl_hours == 24
    assert settings.rental_durations == (12, 24, 48)
    assert settings.default_language == "tk"


def test_overdue_intervals_are_configuration_not_constants():
    settings = get_settings()
    assert settings.reminder_hours_before == 2
    assert settings.grace_hours == 2
    assert settings.removal_after_hours == 24
    assert settings.custody_disposal_days == 30


def test_the_providers_default_to_the_local_stand_ins():
    # A suite that can reach a real acquirer or send a real SMS eventually does.
    settings = get_settings()
    assert settings.payment_provider == "mock"
    assert settings.sms_provider == "log"


def test_get_settings_is_cached():
    assert get_settings() is get_settings()
