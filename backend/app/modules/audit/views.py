"""Wire shapes for the journal.

The one place `audit_entries` rows meet the contract's `AuditLogEntry`: the
dashboard's «Последние события» panel, the audit log screen and the realtime
stream all read the same table and must show a row the same way.
"""

import uuid

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types import utc_isoformat
from app.modules.audit.models import AuditEntry
from app.modules.catalog.models import Cell, Postamat


class AuditLogEntry(BaseModel):
    id: uuid.UUID
    occurred_at: str
    source: str
    actor_id: uuid.UUID | None
    actor_label: str | None
    event_type: str
    severity: str
    message: str
    postamat_id: uuid.UUID | None
    postamat_number: str | None
    cell_id: uuid.UUID | None
    cell_number: str | None
    booking_id: uuid.UUID | None
    reason: str | None
    details: dict | None
    trace_id: str | None


async def labels(
    session: AsyncSession, rows: list[AuditEntry]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, int]]:
    """Postamat numbers and cell numbers for a page of rows.

    Two lookups for the page rather than two per row: this feeds the dashboard,
    which is the one screen everybody keeps open.
    """
    postamat_ids = {row.postamat_id for row in rows if row.postamat_id}
    cell_ids = {row.cell_id for row in rows if row.cell_id}

    numbers: dict[uuid.UUID, str] = {}
    if postamat_ids:
        numbers = {
            row_id: number
            for row_id, number in await session.execute(
                select(Postamat.id, Postamat.number)
                .where(Postamat.id.in_(postamat_ids))
            )
        }
    cells: dict[uuid.UUID, int] = {}
    if cell_ids:
        cells = {
            row_id: number
            for row_id, number in await session.execute(
                select(Cell.id, Cell.number).where(Cell.id.in_(cell_ids))
            )
        }
    return numbers, cells


def entry(
    row: AuditEntry, numbers: dict[uuid.UUID, str], cells: dict[uuid.UUID, int]
) -> AuditLogEntry:
    return AuditLogEntry(
        id=row.id, occurred_at=utc_isoformat(row.created_at),
        source=str(row.source), actor_id=None,
        # A label, not an id: the actor may be a person, the system or a
        # device, and only the first of those has a row anywhere.
        actor_label=row.actor,
        event_type=row.event, severity=str(row.severity), message=row.message,
        postamat_id=row.postamat_id,
        postamat_number=numbers.get(row.postamat_id) if row.postamat_id else None,
        cell_id=row.cell_id,
        cell_number=str(cells[row.cell_id]) if row.cell_id in cells else None,
        booking_id=(uuid.UUID(row.details["booking_id"])
                    if row.details and row.details.get("booking_id") else None),
        reason=(row.details or {}).get("reason"),
        details=row.details, trace_id=row.trace_id,
    )


async def entries(
    session: AsyncSession, rows: list[AuditEntry]
) -> list[AuditLogEntry]:
    numbers, cells = await labels(session, rows)
    return [entry(row, numbers, cells) for row in rows]
