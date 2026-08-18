import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class PaymentStatus(StrEnum):
    PENDING = "pending"
    # The bank has the money reserved but has not moved it. Nothing in this
    # system authorises without capturing yet; the value exists because the
    # contract defines it and an acquirer that works this way is likely.
    AUTHORIZED = "authorized"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # The customer backed out of the bank's page deliberately, as against a
    # session that simply ran out.
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# Banks the payment screen offers. All three are in the contract's enum; only
# the ones with a live integration are accepted, and the app greys out the rest
# rather than hiding them.
BANK_CODES = ("halk", "senagat", "rysgal")


class Payment(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "payments"

    booking_id: Mapped[uuid.UUID] = mapped_column(index=True)
    client_id: Mapped[uuid.UUID] = mapped_column(index=True)
    provider: Mapped[str] = mapped_column(String(32))
    # Unique so a webhook delivered twice — which every acquirer does — settles
    # once. There is no refunded state on purpose: this system does not refund
    # (Ruling Q1), and a state nothing can reach is a lie in the schema.
    provider_payment_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="TMT")
    status: Mapped[PaymentStatus] = mapped_column(
        String(16), index=True, default=PaymentStatus.PENDING
    )
    # Which bank the customer picked. Kept apart from `provider`, which is the
    # integration doing the talking: until an acquirer ships, every bank code
    # runs through the mock.
    bank_code: Mapped[str | None] = mapped_column(String(16))
    # The acquirer's hosted page. Stored rather than rebuilt, because a client
    # coming back to the result screen after a cold start needs the same URL.
    redirect_url: Mapped[str | None] = mapped_column(String(1000))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(200))
    details: Mapped[dict | None] = mapped_column(JSON)
