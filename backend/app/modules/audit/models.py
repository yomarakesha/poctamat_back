import uuid
from enum import StrEnum

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Source(StrEnum):
    SYSTEM = "system"
    API = "api"
    ADMIN = "admin"


class AuditEntry(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "audit_entries"

    event: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[Source] = mapped_column(String(16), index=True)
    severity: Mapped[Severity] = mapped_column(String(16), index=True, default=Severity.INFO)
    message: Mapped[str] = mapped_column(String(500))
    actor: Mapped[str | None] = mapped_column(String(64), index=True)
    postamat_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    cell_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    details: Mapped[dict | None] = mapped_column(JSON)
    # Ties this row to the request that caused it, so a support ticket quoting
    # one trace id finds the journal entry as well as the error.
    trace_id: Mapped[str | None] = mapped_column(String(36), index=True)
