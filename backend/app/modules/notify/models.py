import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class NotificationChannel(StrEnum):
    IN_APP = "in_app"
    SMS = "sms"
    PUSH = "push"


class NotificationKind(StrEnum):
    OTP = "otp"
    # The grant that opens an empty cell. A courier gets this one and only this
    # one, which is why it is named for the act rather than for who carries it.
    DEPOSIT_CODE = "deposit_code"
    PICKUP_CODE_SENT = "pickup_code_sent"
    PICKUP_TRANSFERRED = "pickup_transferred"
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
    # When the client opened it, not merely that they did: the feed sorts and
    # the app badges from this, and "read at some point" answers neither.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(500))


class NotificationSettings(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "notification_settings"

    client_id: Mapped[uuid.UUID] = mapped_column(index=True, unique=True)
    push_parcel_deposited: Mapped[bool] = mapped_column(Boolean, default=True)
    push_cell_opened: Mapped[bool] = mapped_column(Boolean, default=True)
    push_expiring_soon: Mapped[bool] = mapped_column(Boolean, default=True)
    push_marketing: Mapped[bool] = mapped_column(Boolean, default=False)
    # Transactional SMS is not switchable and therefore not stored: the pickup
    # code goes by SMS and it is the only channel we can answer for. The wire
    # carries `sms_always_on: true` as a constant.
