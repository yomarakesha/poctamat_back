import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


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
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(200))
    details: Mapped[dict | None] = mapped_column(JSON)
