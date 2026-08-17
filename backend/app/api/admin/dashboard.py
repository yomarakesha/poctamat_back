import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session, utcnow
from app.core.deps import require_permission
from app.core.types import utc_isoformat
from app.modules.audit.models import AuditEntry
from app.modules.booking.models import Booking, BookingStatus
from app.modules.catalog.service import cell_numbers
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/dashboard", tags=["admin-dashboard"])

# How many rows of each list travel with the counts. The screen shows a queue,
# not an archive; anything longer is a filtered list on its own page.
PREVIEW = 20


class OverdueItem(BaseModel):
    booking_id: uuid.UUID
    postamat_id: uuid.UUID
    cell_number: int
    recipient_phone: str
    expires_at: str | None
    remove_after: str | None


class PaymentIssue(BaseModel):
    at: str
    message: str
    details: dict | None


class AttentionOut(BaseModel):
    counts: dict[str, int]
    overdue: list[OverdueItem]
    to_remove: list[OverdueItem]
    payments_needing_attention: list[PaymentIssue]


@router.get("/attention", response_model=AttentionOut)
async def attention(
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("bookings.read")),
) -> AttentionOut:
    """What a person has to do something about today."""
    moment = utcnow()

    overdue_rows = list(await session.scalars(
        select(Booking)
        .where(Booking.status == BookingStatus.OVERDUE)
        .order_by(Booking.remove_after)
        .limit(PREVIEW)
    ))
    overdue_total = await session.scalar(
        select(func.count()).select_from(Booking)
        .where(Booking.status == BookingStatus.OVERDUE)
    ) or 0
    to_remove_total = await session.scalar(
        select(func.count()).select_from(Booking).where(
            Booking.status == BookingStatus.OVERDUE,
            Booking.remove_after.is_not(None),
            Booking.remove_after <= moment,
        )
    ) or 0

    numbers = await cell_numbers(session, [row.cell_id for row in overdue_rows])

    def _item(row: Booking) -> OverdueItem:
        return OverdueItem(
            booking_id=row.id, postamat_id=row.postamat_id,
            cell_number=numbers.get(row.cell_id, 0),
            recipient_phone=row.recipient_phone,
            expires_at=utc_isoformat(row.expires_at) if row.expires_at else None,
            remove_after=(
                utc_isoformat(row.remove_after) if row.remove_after else None
            ),
        )

    overdue = [_item(row) for row in overdue_rows]
    # "Ready to remove" is derived rather than stored as a status: it is the
    # same parcel, one interval later, and a status for it would be a state the
    # lifecycle has to be walked through for no reason.
    ready = [
        item for item, row in zip(overdue, overdue_rows)
        if row.remove_after is not None and row.remove_after.replace(tzinfo=None)
        <= moment.replace(tzinfo=None)
    ]

    payment_rows = list(await session.scalars(
        select(AuditEntry)
        .where(AuditEntry.event == "payment.needs_attention")
        .order_by(AuditEntry.created_at.desc())
        .limit(PREVIEW)
    ))
    payment_total = await session.scalar(
        select(func.count()).select_from(AuditEntry)
        .where(AuditEntry.event == "payment.needs_attention")
    ) or 0

    return AttentionOut(
        counts={
            "overdue": overdue_total,
            "to_remove": to_remove_total,
            "payments_needing_attention": payment_total,
        },
        overdue=overdue,
        to_remove=ready,
        payments_needing_attention=[
            PaymentIssue(at=utc_isoformat(row.created_at), message=row.message,
                         details=row.details)
            for row in payment_rows
        ],
    )
