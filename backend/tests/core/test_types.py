import pytest
from datetime import datetime, timezone
from pydantic import BaseModel, ValidationError

from app.core.types import PhoneNumber, utc_isoformat


class Sample(BaseModel):
    phone: PhoneNumber


def test_valid_turkmen_number_is_accepted():
    assert Sample(phone="+99362123456").phone == "+99362123456"


@pytest.mark.parametrize("value", ["+7999123456", "99362123456", "+9936212345", "+993621234567"])
def test_bad_numbers_are_rejected(value):
    with pytest.raises(ValidationError):
        Sample(phone=value)


def test_utc_isoformat_uses_z_suffix():
    moment = datetime(2026, 8, 14, 9, 30, tzinfo=timezone.utc)
    assert utc_isoformat(moment) == "2026-08-14T09:30:00Z"


def test_utc_isoformat_treats_naive_datetime_as_utc():
    moment = datetime(2026, 8, 14, 9, 30)
    assert utc_isoformat(moment) == "2026-08-14T09:30:00Z"
