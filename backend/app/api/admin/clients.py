import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session, utcnow
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import require_idempotency_key
from app.core.pagination import PageMeta, PageParams, page_params, paginate_page
from app.core.types import utc_isoformat
from app.modules.audit.models import Source
from app.modules.audit.service import record
from app.modules.booking.models import Booking
from app.modules.booking.views import FINISHED_STATUSES
from app.modules.catalog.models import Cell, Postamat
from app.modules.identity.models import AdminUser, Client

router = APIRouter(prefix="/admin/clients", tags=["admin-clients"])


class ActiveBooking(BaseModel):
    booking_id: uuid.UUID
    # Both strings: the table renders them together as «145[014]», and a door
    # label is not arithmetic.
    postamat_number: str
    cell_number: str


class ClientListItem(BaseModel):
    id: uuid.UUID
    full_name: str
    phone: str
    status: str
    registered_at: str
    active_bookings: list[ActiveBooking]


class ClientDetails(ClientListItem):
    city_id: uuid.UUID | None
    language: str
    blocked_reason: str | None
    blocked_at: str | None
    total_bookings: int


class ClientPage(BaseModel):
    items: list[ClientListItem]
    pagination: PageMeta


class BlockRequest(BaseModel):
    # Required by the contract, and rightly: a block with no reason is a decision
    # nobody can review later.
    reason: str = Field(min_length=3, max_length=500)


def _full_name(client: Client) -> str:
    parts = [client.last_name, client.first_name, client.middle_name]
    return " ".join(part for part in parts if part)


async def _active_bookings(
    session: AsyncSession, client_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ActiveBooking]]:
    """The live bookings for a page of clients, in one query for the page."""
    if not client_ids:
        return {}

    rows = await session.execute(
        select(Booking.id, Booking.client_id, Postamat.number, Cell.number)
        .join(Postamat, Postamat.id == Booking.postamat_id)
        .join(Cell, Cell.id == Booking.cell_id)
        .where(
            Booking.client_id.in_(client_ids),
            Booking.status.not_in(tuple(FINISHED_STATUSES)),
        )
        .order_by(Booking.created_at.desc())
    )
    found: dict[uuid.UUID, list[ActiveBooking]] = {}
    for booking_id, client_id, postamat_number, cell_number in rows:
        found.setdefault(client_id, []).append(ActiveBooking(
            booking_id=booking_id, postamat_number=postamat_number,
            cell_number=str(cell_number),
        ))
    return found


def _list_item(client: Client, active: list[ActiveBooking]) -> ClientListItem:
    return ClientListItem(
        id=client.id, full_name=_full_name(client), phone=client.phone,
        # Derived rather than stored: `is_blocked` is the fact, `blocked` is how
        # the contract spells it.
        status="blocked" if client.is_blocked else "active",
        registered_at=utc_isoformat(client.created_at),
        active_bookings=active,
    )


async def _details(session: AsyncSession, client: Client) -> ClientDetails:
    active = (await _active_bookings(session, [client.id])).get(client.id, [])
    total = await session.scalar(
        select(func.count()).select_from(Booking)
        .where(Booking.client_id == client.id)
    ) or 0
    return ClientDetails(
        **_list_item(client, active).model_dump(),
        city_id=client.city_id, language=client.language,
        blocked_reason=client.blocked_reason,
        blocked_at=utc_isoformat(client.blocked_at) if client.blocked_at else None,
        total_bookings=total,
    )


async def _client_or_404(session: AsyncSession, client_id: uuid.UUID) -> Client:
    client = await session.get(Client, client_id)
    if client is None:
        raise AppError(ErrorCode.NOT_FOUND, "No such client.", 404)
    return client


@router.get("", response_model=ClientPage)
async def list_clients(
    query: str | None = Query(default=None, alias="q", max_length=200),
    first_name: str | None = Query(default=None),
    last_name: str | None = Query(default=None),
    phone: str | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(active|blocked)$"),
    city_id: uuid.UUID | None = Query(default=None),
    params: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("clients.read")),
) -> ClientPage:
    stmt = select(Client).order_by(Client.created_at.desc())
    if query:
        like = f"%{query}%"
        stmt = stmt.where(or_(
            Client.phone.ilike(like), Client.last_name.ilike(like),
            Client.first_name.ilike(like), Client.middle_name.ilike(like),
        ))
    if first_name:
        stmt = stmt.where(Client.first_name.ilike(f"%{first_name}%"))
    if last_name:
        stmt = stmt.where(Client.last_name.ilike(f"%{last_name}%"))
    if phone:
        stmt = stmt.where(Client.phone.ilike(f"%{phone}%"))
    if status:
        stmt = stmt.where(Client.is_blocked.is_(status == "blocked"))
    if city_id:
        stmt = stmt.where(Client.city_id == city_id)

    rows, meta = await paginate_page(session, stmt, params)
    active = await _active_bookings(session, [row.id for row in rows])
    return ClientPage(
        items=[_list_item(row, active.get(row.id, [])) for row in rows],
        pagination=meta,
    )


@router.get("/{client_id}", response_model=ClientDetails)
async def get_client(
    client_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("clients.read")),
) -> ClientDetails:
    return await _details(session, await _client_or_404(session, client_id))


@router.post("/{client_id}/block", response_model=ClientDetails,
             dependencies=[Depends(require_idempotency_key)])
async def block_client(
    client_id: uuid.UUID,
    payload: BlockRequest,
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(require_permission("clients.write")),
) -> ClientDetails:
    client = await _client_or_404(session, client_id)
    # Blocking stops new bookings and kills live sessions at their next refresh.
    # It does not touch a parcel already inside a cell: that stays collectable,
    # or we would be holding somebody's property over an account decision.
    client.is_blocked = True
    client.blocked_reason = payload.reason
    client.blocked_at = utcnow()
    await record(
        session, event="client.blocked", source=Source.ADMIN, actor=actor.login,
        message=f"Клиент {client.phone} заблокирован.",
        details={"client_id": str(client.id), "reason": payload.reason},
    )
    await session.commit()
    return await _details(session, client)


@router.post("/{client_id}/unblock", response_model=ClientDetails,
             dependencies=[Depends(require_idempotency_key)])
async def unblock_client(
    client_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(require_permission("clients.write")),
) -> ClientDetails:
    client = await _client_or_404(session, client_id)
    client.is_blocked = False
    client.blocked_reason = None
    client.blocked_at = None
    await record(
        session, event="client.unblocked", source=Source.ADMIN, actor=actor.login,
        message=f"Клиент {client.phone} разблокирован.",
        details={"client_id": str(client.id)},
    )
    await session.commit()
    return await _details(session, client)
