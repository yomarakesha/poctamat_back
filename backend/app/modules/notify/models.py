import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class NotificationChannel(StrEnum):
    IN_APP = "in_app"
    SMS = "sms"


class NotificationKind(StrEnum):
    OTP = "otp"
    COURIER_CODE = "courier_code"
    BOOKING_PAID = "booking_paid"
    BOOKING_EXPIRING = "booking_expiring"
    BOOKING_EXPIRED = "booking_expired"
    BOOKING_OVERDUE = "booking_overdue"
    PARCEL_REMOVED = "parcel_removed"


class Notification(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "notifications"

    # Null for a recipient or a courier who has no account: the phone is the
    # only identity they have, and the parcel still has to reach them.
    client_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    booking_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    kind: Mapped[NotificationKind] = mapped_column(String(32), index=True)
    channel: Mapped[NotificationChannel] = mapped_column(String(16))
    phone: Mapped[str | None] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(120))
    # What the app shows. Secrets never land here: a one-time code or a door PIN
    # belongs in the SMS text and nowhere that is stored.
    body: Mapped[str] = mapped_column(String(500))
    details: Mapped[dict | None] = mapped_column(JSON)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(500))
