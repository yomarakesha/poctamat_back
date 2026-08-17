from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import utcnow
from app.core.errors import AppError, ErrorCode
from app.modules.audit.models import Severity, Source
from app.modules.audit.service import record as audit
from app.modules.booking import service as booking_service
from app.modules.booking.models import Booking, BookingStatus
from app.modules.custody.models import CustodyHandover, CustodyRecord, CustodyStatus

REMOVABLE = frozenset({BookingStatus.OVERDUE})


async def file_removal(
    session: AsyncSession, booking: Booking, cell_number: int, admin_login: str,
    description: str,
) -> CustodyRecord:
    """Record that a parcel was taken out, and only then free the cell.

    The order matters: the act exists before the cell is released, so there is
    no moment in which a cell is free and no record says where its contents
    went. That is the whole point of the procedure — a cell freed by a button
    is a parcel nobody can trace.
    """
    if booking.status not in REMOVABLE:
        raise AppError(ErrorCode.BOOKING_INVALID_STATE,
                       "Only an overdue parcel may be removed.", 409,
                       details={"status": str(booking.status)})

    record = CustodyRecord(
        booking_id=booking.id, postamat_id=booking.postamat_id,
        cell_id=booking.cell_id, cell_number=cell_number,
        removed_by=admin_login, removed_at=utcnow(), description=description,
    )
    session.add(record)
    await booking_service.mark_removed(session, booking)
    await audit(session, event="custody.removed", source=Source.ADMIN,
                severity=Severity.WARNING, message=description, actor=admin_login,
                postamat_id=booking.postamat_id, cell_id=booking.cell_id,
                details={"booking_id": str(booking.id)})
    return record


async def hand_over(
    session: AsyncSession, record: CustodyRecord, booking: Booking, admin_login: str,
    to_whom: str, note: str | None,
) -> CustodyHandover:
    if record.status != CustodyStatus.AT_COUNTER:
        raise AppError(ErrorCode.BOOKING_INVALID_STATE,
                       "This parcel is no longer at the counter.", 409)

    handover = CustodyHandover(to_whom=to_whom, by_admin=admin_login, note=note)
    record.handovers.append(handover)
    record.status = CustodyStatus.HANDED_OVER
    record.closed_by = admin_login
    record.closed_at = utcnow()
    record.closing_reason = f"Выдано: {to_whom}"
    await booking_service.close_custody(session, booking)
    await audit(session, event="custody.handed_over", source=Source.ADMIN,
                message=note or f"Выдано: {to_whom}", actor=admin_login,
                postamat_id=record.postamat_id, cell_id=record.cell_id,
                details={"booking_id": str(record.booking_id), "to_whom": to_whom})
    return handover


async def dispose(
    session: AsyncSession, record: CustodyRecord, booking: Booking, admin_login: str,
    reason: str,
) -> CustodyRecord:
    if record.status != CustodyStatus.AT_COUNTER:
        raise AppError(ErrorCode.BOOKING_INVALID_STATE,
                       "This parcel is no longer at the counter.", 409)

    record.status = CustodyStatus.DISPOSED
    record.closed_by = admin_login
    record.closed_at = utcnow()
    record.closing_reason = reason
    await booking_service.close_custody(session, booking)
    # Warning severity: this is the end of somebody's parcel, and the log is
    # where a dispute about it is answered.
    await audit(session, event="custody.disposed", source=Source.ADMIN,
                severity=Severity.WARNING, message=reason, actor=admin_login,
                postamat_id=record.postamat_id, cell_id=record.cell_id,
                details={"booking_id": str(record.booking_id)})
    return record
