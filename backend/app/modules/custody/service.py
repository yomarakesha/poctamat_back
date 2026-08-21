from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import utcnow
from app.core.errors import AppError, ErrorCode
from app.modules.audit.models import Severity, Source
from app.modules.audit.service import record as audit
from app.modules.booking import service as booking_service
from app.modules.booking.models import Booking, BookingStatus
from app.modules.booking.service import as_utc
from app.modules.custody.models import CustodyHandover, CustodyRecord, CustodyStatus

# Removal follows the flag, not the mere fact of being late: the worker moves a
# parcel to `to_remove` when its interval elapses, and that is the queue staff
# work from.
REMOVABLE = frozenset({BookingStatus.TO_REMOVE})

# Where a disposal outcome leaves the record. Both close it; which one is on the
# act is what a later dispute turns on.
DISPOSAL_STATUS = {
    "returned_to_sender": CustodyStatus.RETURNED_TO_SENDER,
    "disposed": CustodyStatus.DISPOSED,
}


async def file_removal(
    session: AsyncSession, booking: Booking, cell_number: int, admin_login: str,
    reason: str, description: str,
) -> CustodyRecord:
    """Record that a parcel was taken out, and only then free the cell.

    The order matters: the act exists before the cell is released, so there is
    no moment in which a cell is free and no record says where its contents
    went. That is the whole point of the procedure — a cell freed by a button
    is a parcel nobody can trace.
    """
    if booking.status not in REMOVABLE:
        raise AppError(ErrorCode.BOOKING_NOT_EXPIRED,
                       "Only a parcel flagged for removal may be taken out.", 409,
                       details={"status": str(booking.status)})

    removed_at = utcnow()
    record = CustodyRecord(
        booking_id=booking.id, postamat_id=booking.postamat_id,
        cell_id=booking.cell_id, cell_number=cell_number,
        removed_by=admin_login, removed_at=removed_at, reason=reason,
        description=description,
        disposal_due_at=removed_at
        + timedelta(days=get_settings().custody_disposal_days),
    )
    session.add(record)
    await booking_service.mark_removed(session, booking)
    await audit(session, event="custody.removed", source=Source.ADMIN,
                severity=Severity.WARNING, message=description, actor=admin_login,
                postamat_id=booking.postamat_id, cell_id=booking.cell_id,
                details={"booking_id": str(booking.id), "reason": reason})
    return record


def _open(record: CustodyRecord) -> None:
    if record.status != CustodyStatus.AT_COUNTER:
        raise AppError(ErrorCode.CUSTODY_ALREADY_CLOSED,
                       "This parcel is no longer at the counter.", 409,
                       details={"status": str(record.status)})


async def hand_over(
    session: AsyncSession, record: CustodyRecord, booking: Booking, admin_login: str,
    to_whom: str, document_ref: str | None, note: str | None,
) -> CustodyHandover:
    _open(record)

    handover = CustodyHandover(to_whom=to_whom, by_admin=admin_login,
                               document_ref=document_ref, note=note)
    record.handovers.append(handover)
    record.status = CustodyStatus.HANDED_OVER
    record.closed_by = admin_login
    record.closed_at = utcnow()
    record.closing_reason = f"Выдано: {to_whom}"
    await booking_service.close_custody(session, booking)
    await audit(session, event="custody.handed_over", source=Source.ADMIN,
                message=note or f"Выдано: {to_whom}", actor=admin_login,
                postamat_id=record.postamat_id, cell_id=record.cell_id,
                details={"booking_id": str(record.booking_id), "to_whom": to_whom,
                         "document_ref": document_ref})
    return handover


async def dispose(
    session: AsyncSession, record: CustodyRecord, booking: Booking, admin_login: str,
    outcome: str, reason: str,
) -> CustodyRecord:
    _open(record)

    # The waiting period is the parcel's only protection once its owner has
    # stopped answering, so it is checked here rather than left to the panel to
    # grey out a button.
    due = record.disposal_due_at
    if due is not None and utcnow() < as_utc(due):
        raise AppError(ErrorCode.CUSTODY_NOT_DUE,
                       "This parcel may not be disposed of yet.", 422,
                       details={"disposal_due_at": due.isoformat()})

    record.status = DISPOSAL_STATUS[outcome]
    record.closed_by = admin_login
    record.closed_at = utcnow()
    record.closing_reason = reason
    await booking_service.close_custody(session, booking)
    # Warning severity: this is the end of somebody's parcel, and the log is
    # where a dispute about it is answered.
    await audit(session, event=f"custody.{outcome}", source=Source.ADMIN,
                severity=Severity.WARNING, message=reason, actor=admin_login,
                postamat_id=record.postamat_id, cell_id=record.cell_id,
                details={"booking_id": str(record.booking_id), "outcome": outcome})
    return record
