import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditEntry, Severity, Source


async def record(
    session: AsyncSession,
    *,
    event: str,
    source: Source,
    message: str,
    severity: Severity = Severity.INFO,
    actor: str | None = None,
    postamat_id: uuid.UUID | None = None,
    cell_id: uuid.UUID | None = None,
    details: dict | None = None,
) -> AuditEntry:
    entry = AuditEntry(
        event=event,
        source=source,
        severity=severity,
        message=message,
        actor=actor,
        postamat_id=postamat_id,
        cell_id=cell_id,
        details=details,
    )
    session.add(entry)
    await session.flush()
    return entry
