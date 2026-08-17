from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints

PhoneNumber = Annotated[str, StringConstraints(pattern=r"^\+993[0-9]{8}$")]
# The cell PIN. Five digits, and deliberately not the six-digit login code.
Pin = Annotated[str, StringConstraints(pattern=r"^[0-9]{5}$")]


class Money(BaseModel):
    """Integer minor units plus a currency, as one object on the wire.

    The contract types every amount this way. Two loose fields would let a client
    render `amount_minor` from one booking beside `currency` from another, and a
    float would not survive being money at all.
    """

    amount_minor: int
    currency: Literal["TMT"] = "TMT"


def utc_isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
