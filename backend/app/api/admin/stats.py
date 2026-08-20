import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_permission
from app.core.types import Money, utc_isoformat
from app.modules.audit import views as audit_views
from app.modules.audit.models import AuditEntry
from app.modules.booking.models import Booking
from app.modules.catalog.models import Cell, CellType, Postamat
from app.modules.identity.models import AdminUser, Client
from app.modules.stats import service

router = APIRouter(prefix="/admin/stats", tags=["admin-stats"])


class Attention(BaseModel):
    overdue_bookings: int
    to_remove_bookings: int
    offline_devices: int
    failed_commands: int
    blocked_cells: int


class Overview(BaseModel):
    postamat_count: int
    cell_count: int
    occupied_count: int
    booked_count: int
    blocked_count: int
    maintenance_count: int


class SeriesPoint(BaseModel):
    date: str
    count: int


class BookingSeries(BaseModel):
    points: list[SeriesPoint]


class RevenuePoint(BaseModel):
    date: str
    amount: Money
    transaction_count: int


class RevenueSeries(BaseModel):
    total: Money
    points: list[RevenuePoint]


class PostamatUtilization(BaseModel):
    postamat_id: uuid.UUID
    name: str
    utilization_pct: float
    uses_per_cell: float


class Utilization(BaseModel):
    utilization_pct: float
    uses_per_cell: float
    by_postamat: list[PostamatUtilization]


class PeakBucket(BaseModel):
    weekday: int
    hour: int
    count: int


class PeakHours(BaseModel):
    timezone: str
    buckets: list[PeakBucket]


class RecentEvents(BaseModel):
    items: list[audit_views.AuditLogEntry]


class AdminBookingListItem(BaseModel):
    id: uuid.UUID
    status: str
    postamat_id: uuid.UUID
    postamat_name: str
    cell_number: str
    cell_type_name: str
    created_at: str
    expires_at: str | None
    client_id: uuid.UUID
    client_phone: str
    cell_type_code: str


class RecentBookings(BaseModel):
    items: list[AdminBookingListItem]


def _frame(period: str, since: datetime | None, until: datetime | None):
    return service.window(period, since, until)


@router.get("/attention", response_model=Attention)
async def attention_queue(
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("bookings.read")),
) -> Attention:
    """What is waiting on a person right now."""
    return Attention(**await service.attention(session))


@router.get("/overview", response_model=Overview)
async def overview(
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("bookings.read")),
) -> Overview:
    return Overview(**await service.overview(session))


@router.get("/bookings", response_model=BookingSeries)
async def bookings_series(
    period: str = Query(default="7d", pattern="^(7d|30d|90d|custom)$"),
    since: datetime | None = Query(default=None, alias="from"),
    until: datetime | None = Query(default=None, alias="to"),
    postamat_id: uuid.UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("bookings.read")),
) -> BookingSeries:
    points = await service.bookings_over_time(
        session, _frame(period, since, until), postamat_id
    )
    return BookingSeries(points=[SeriesPoint(**point) for point in points])


@router.get("/revenue", response_model=RevenueSeries)
async def revenue_series(
    period: str = Query(default="30d", pattern="^(7d|30d|90d|custom)$"),
    group_by: str = Query(default="day", pattern="^(day|week|month)$"),
    since: datetime | None = Query(default=None, alias="from"),
    until: datetime | None = Query(default=None, alias="to"),
    postamat_id: uuid.UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("bookings.read")),
) -> RevenueSeries:
    # `group_by` is accepted and ignored for now: the series is daily, and
    # rolling days into weeks is arithmetic the chart already does.
    data = await service.revenue(session, _frame(period, since, until), postamat_id)
    return RevenueSeries(
        total=Money(**data["total"]),
        points=[
            RevenuePoint(date=point["date"], amount=Money(**point["amount"]),
                         transaction_count=point["transaction_count"])
            for point in data["points"]
        ],
    )


@router.get("/utilization", response_model=Utilization)
async def utilization(
    period: str = Query(default="30d", pattern="^(7d|30d|90d|custom)$"),
    postamat_id: uuid.UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("bookings.read")),
) -> Utilization:
    data = await service.utilization(session, _frame(period, None, None), postamat_id)
    return Utilization(
        utilization_pct=data["utilization_pct"],
        uses_per_cell=data["uses_per_cell"],
        by_postamat=[PostamatUtilization(**row) for row in data["by_postamat"]],
    )


@router.get("/peak-hours", response_model=PeakHours)
async def peak_hours(
    period: str = Query(default="30d", pattern="^(7d|30d|90d)$"),
    postamat_id: uuid.UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("bookings.read")),
) -> PeakHours:
    data = await service.peak_hours(session, _frame(period, None, None), postamat_id)
    return PeakHours(timezone=data["timezone"],
                     buckets=[PeakBucket(**bucket) for bucket in data["buckets"]])


@router.get("/recent-events", response_model=RecentEvents)
async def recent_events(
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("bookings.read")),
) -> RecentEvents:
    rows = list(await session.scalars(
        select(AuditEntry).order_by(AuditEntry.created_at.desc()).limit(limit)
    ))
    return RecentEvents(items=await audit_views.entries(session, rows))


@router.get("/recent-bookings", response_model=RecentBookings)
async def recent_bookings(
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("bookings.read")),
) -> RecentBookings:
    rows = await session.execute(
        select(Booking, Postamat, Cell, CellType, Client)
        .join(Postamat, Postamat.id == Booking.postamat_id)
        .join(Cell, Cell.id == Booking.cell_id)
        .join(CellType, CellType.id == Booking.cell_type_id)
        .join(Client, Client.id == Booking.client_id)
        .order_by(Booking.created_at.desc())
        .limit(limit)
    )
    return RecentBookings(items=[
        AdminBookingListItem(
            id=booking.id, status=str(booking.status),
            postamat_id=postamat.id, postamat_name=postamat.name,
            cell_number=str(cell.number), cell_type_name=cell_type.name_ru,
            created_at=utc_isoformat(booking.created_at),
            expires_at=(utc_isoformat(booking.expires_at)
                        if booking.expires_at else None),
            client_id=client.id, client_phone=client.phone,
            cell_type_code=cell_type.code,
        )
        for booking, postamat, cell, cell_type, client in rows
    ])
