import uuid
from datetime import datetime, time
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class City(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "cities"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # The three names are product data rather than interface strings: a city is
    # spelled differently in each language and an operator edits all three.
    name_tk: Mapped[str] = mapped_column(String(100))
    name_ru: Mapped[str] = mapped_column(String(100))
    name_en: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class CellType(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "cell_types"

    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name_tk: Mapped[str] = mapped_column(String(50))
    name_ru: Mapped[str] = mapped_column(String(50))
    name_en: Mapped[str] = mapped_column(String(50))
    # Millimetres, as the contract types them. One unit end to end beats a
    # conversion applied at the edge that somebody eventually forgets.
    width_mm: Mapped[int] = mapped_column(Integer)
    height_mm: Mapped[int] = mapped_column(Integer)
    depth_mm: Mapped[int] = mapped_column(Integer)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PostamatStatus(StrEnum):
    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    BLOCKED = "blocked"


class DeviceStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"


class Postamat(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "postamats"

    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    city_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cities.id"), index=True)
    address: Mapped[str] = mapped_column(String(500))
    latitude: Mapped[float | None]
    longitude: Mapped[float | None]
    # The operator's decision about the machine, not the hardware's own state:
    # a postamat can be `active` while its device is offline, and `blocked`
    # while the device is happily online. Presence lives on Device.
    status: Mapped[PostamatStatus] = mapped_column(
        String(16), default=PostamatStatus.ACTIVE
    )
    round_the_clock: Mapped[bool] = mapped_column(Boolean, default=False)

    schedule: Mapped[list["PostamatSchedule"]] = relationship(
        back_populates="postamat", lazy="selectin", cascade="all, delete-orphan"
    )
    photos: Mapped[list["PostamatPhoto"]] = relationship(
        back_populates="postamat", lazy="selectin", cascade="all, delete-orphan",
        order_by="PostamatPhoto.position",
    )


class PostamatSchedule(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "postamat_schedules"

    postamat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("postamats.id"), index=True
    )
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = Monday
    opens_at: Mapped[time] = mapped_column(Time)
    closes_at: Mapped[time] = mapped_column(Time)

    postamat: Mapped[Postamat] = relationship(back_populates="schedule")


class PostamatPhoto(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "postamat_photos"

    postamat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("postamats.id"), index=True
    )
    # The photograph is of the machine, not of anything inside it: it is how a
    # customer recognises the postamat in a lobby and how an operator confirms
    # which one they are looking at.
    storage_key: Mapped[str] = mapped_column(String(64))
    content_type: Mapped[str] = mapped_column(String(32))
    caption: Mapped[str | None] = mapped_column(String(200))
    position: Mapped[int] = mapped_column(Integer, default=0)

    postamat: Mapped[Postamat] = relationship(back_populates="photos")


class Cell(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "cells"
    __table_args__ = (UniqueConstraint("postamat_id", "number", name="uq_cell_number"),)

    postamat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("postamats.id"), index=True
    )
    cell_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cell_types.id"), index=True
    )
    # The label on the door. `board` and `output` are the lock-board address,
    # which is a separate fact: on the pilot cabinet the two happen to line up,
    # and nothing may rely on that.
    number: Mapped[int] = mapped_column(Integer)
    row: Mapped[int | None] = mapped_column(Integer)
    col: Mapped[int | None] = mapped_column(Integer)
    board: Mapped[int] = mapped_column(Integer)
    output: Mapped[int] = mapped_column(Integer)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    is_maintenance: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(500))


class Tariff(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "tariffs"
    __table_args__ = (
        UniqueConstraint("city_id", "cell_type_id", "duration_hours", name="uq_tariff"),
    )

    city_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cities.id"), index=True)
    cell_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cell_types.id"), index=True
    )
    duration_hours: Mapped[int] = mapped_column(Integer)
    # Minor units and an explicit currency, never a float: prices are counted in
    # tenge, not approximated in binary fractions.
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="TMT")


class Device(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "devices"

    postamat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("postamats.id"), unique=True
    )
    status: Mapped[DeviceStatus] = mapped_column(
        String(16), default=DeviceStatus.OFFLINE
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_version: Mapped[str | None] = mapped_column(String(32))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    mac_address: Mapped[str | None] = mapped_column(String(17))
