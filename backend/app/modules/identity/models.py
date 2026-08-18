import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class Client(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "clients"

    phone: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    # Three fields rather than one string: the registration screen asks for
    # surname, name and an optional patronymic, and a parcel handed over at the
    # counter is checked against a passport that spells them separately.
    last_name: Mapped[str | None] = mapped_column(String(100))
    first_name: Mapped[str | None] = mapped_column(String(100))
    middle_name: Mapped[str | None] = mapped_column(String(100))
    language: Mapped[str] = mapped_column(String(2), default="tk")
    city_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(500))
    # When, not merely whether: an operator looking at a blocked account needs to
    # know if the decision is a year old or from this morning.
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def profile_complete(self) -> bool:
        """Both names present. Booking is refused until they are.

        The counter needs somebody to hand the parcel to, and «Получатель» on
        the screen is a name, not a phone number.
        """
        return bool(self.last_name and self.first_name)


class Role(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "roles"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    permissions: Mapped[list] = mapped_column(JSON, default=list)


class AdminUser(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "admin_users"

    login: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    # Which postamats this account may act on. Empty means the whole fleet —
    # stored as a list rather than a join table because a role bound to two or
    # three machines is the whole of the requirement, and a table would add a
    # migration for every read.
    postamat_ids: Mapped[list] = mapped_column(JSON, default=list)
    # Shown in the users list so an operator can see a dormant account. Written
    # by the login route, not by the token middleware: an access token minted an
    # hour ago is not a sign of life.
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    role: Mapped[Role] = relationship(lazy="joined")


class RefreshToken(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "refresh_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    subject_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[uuid.UUID] = mapped_column(index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PushToken(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "push_tokens"
    __table_args__ = (
        # One row per device per client, so two launches racing each other cannot
        # leave the same token registered twice.
        Index("uq_push_token_per_client", "client_id", "token", unique=True),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(index=True)
    # The device token itself. Long, opaque and reissued by the platform
    # whenever it feels like it, which is why re-registering the same value has
    # to be idempotent rather than a duplicate row.
    token: Mapped[str] = mapped_column(String(512))
    platform: Mapped[str] = mapped_column(String(16))
    app_version: Mapped[str | None] = mapped_column(String(32))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
