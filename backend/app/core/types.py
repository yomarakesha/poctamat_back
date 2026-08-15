from datetime import datetime, timezone
from typing import Annotated

from pydantic import StringConstraints

PhoneNumber = Annotated[str, StringConstraints(pattern=r"^\+993[0-9]{8}$")]


def utc_isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
