import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class CustodyStatus(StrEnum):
    AT_COUNTER = "at_counter"
    HANDED_OVER = "handed_over"
    DISPOSED = "disposed"


class CustodyRecord(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "custody_records"

    # One act per booking: a parcel is removed from a cell exactly once, and a
    # second act for the same booking would mean two parcels or two stories.
    booking_id: Mapped[uuid.UUID] = mapped_column(index=True, unique=True)
    postamat_id: Mapped[uuid.UUID] = mapped_column(index=True)
    cell_id: Mapped[uuid.UUID] = mapped_column(index=True)
    cell_number: Mapped[int] = mapped_column(Integer)
    # Who emptied the cell and what they found in it. Per Ruling Q2 there is no
    # photograph here: the only photo the product wants is of the postamat.
    removed_by: Mapped[str] = mapped_column(String(64))
    removed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    description: Mapped[str] = mapped_column(String(500))
    status: Mapped[CustodyStatus] = mapped_column(
        String(16), index=True, default=CustodyStatus.AT_COUNTER
    )
    closed_by: Mapped[str | None] = mapped_column(String(64))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closing_reason: Mapped[str | None] = mapped_column(String(500))

    handovers: Mapped[list["CustodyHandover"]] = relationship(
        back_populates="record", lazy="selectin", cascade="all, delete-orphan"
    )


class CustodyHandover(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "custody_handovers"

    record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("custody_records.id"), index=True
    )
    to_whom: Mapped[str] = mapped_column(String(16))  # recipient | sender
    by_admin: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(500))

    record: Mapped[CustodyRecord] = relationship(back_populates="handovers")
