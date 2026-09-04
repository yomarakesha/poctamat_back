import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session, utcnow
from app.core.deps import require_client
from app.core.errors import AppError, ErrorCode
from app.core.pagination import CursorMeta, CursorParams, cursor_params, paginate_cursor
from app.core.types import utc_isoformat
from app.modules.catalog.models import City
from app.modules.identity.models import Client
from app.modules.notify.models import (
    Notification,
    NotificationChannel,
    NotificationSettings,
)

router = APIRouter(tags=["profile"])


class ClientOut(BaseModel):
    id: uuid.UUID
    phone: str
    last_name: str | None
    first_name: str | None
    middle_name: str | None
    city_id: uuid.UUID | None
    language: str
    status: str
    profile_complete: bool


class ClientPatch(BaseModel):
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    middle_name: str | None = Field(default=None, max_length=100)
    city_id: uuid.UUID | None = None
    language: str | None = Field(default=None, pattern="^(tk|ru|en)$")


class SettingsOut(BaseModel):
    push_parcel_deposited: bool
    push_cell_opened: bool
    push_expiring_soon: bool
    push_marketing: bool
    # Constant on the wire: transactional SMS cannot be switched off, because
    # the pickup code travels that way and it is the only channel we can answer
    # for.
    sms_always_on: bool = True


class SettingsPatch(BaseModel):
    push_parcel_deposited: bool | None = None
    push_cell_opened: bool | None = None
    push_expiring_soon: bool | None = None
    push_marketing: bool | None = None


class NotificationOut(BaseModel):
    id: uuid.UUID
    type: str
    title: str
    body: str
    booking_id: uuid.UUID | None
    created_at: str
    read_at: str | None


class NotificationFeed(BaseModel):
    items: list[NotificationOut]
    unread_count: int
    pagination: CursorMeta


class MarkReadRequest(BaseModel):
    # Omitted means everything, which is what the "прочитать все" button sends.
    notification_ids: list[uuid.UUID] | None = None


class MarkReadResult(BaseModel):
    unread_count: int


def client_out(client: Client) -> ClientOut:
    return ClientOut(
        id=client.id, phone=client.phone, last_name=client.last_name,
        first_name=client.first_name, middle_name=client.middle_name,
        city_id=client.city_id, language=client.language,
        # Derived rather than stored: `is_blocked` is the fact, `blocked` is how
        # the contract spells it.
        status="blocked" if client.is_blocked else "active",
        profile_complete=client.profile_complete,
    )


def _notification_out(row: Notification) -> NotificationOut:
    return NotificationOut(
        id=row.id, type=row.kind, title=row.title, body=row.body,
        booking_id=row.booking_id, created_at=utc_isoformat(row.created_at),
        read_at=utc_isoformat(row.read_at) if row.read_at else None,
    )


async def _settings_for(session: AsyncSession, client: Client) -> NotificationSettings:
    """Read the client's preferences, creating the default row on first ask.

    Nobody visits a settings screen before their first notification, so writing
    the row at registration would only add a table full of defaults.
    """
    row = await session.scalar(
        select(NotificationSettings).where(NotificationSettings.client_id == client.id)
    )
    if row is None:
        row = NotificationSettings(client_id=client.id)
        session.add(row)
        await session.flush()
    return row


# The feed is the in-app channel and nothing else. A `Notification` row is
# written for every channel — a push carries its own `sent_at` and `error` so
# a failed delivery can be looked at — and the reminder that goes out both
# in-app and as a push writes two rows with the same title and body. Without
# this filter the client sees that reminder twice and its badge counts it
# twice.
_FEED = Notification.channel == NotificationChannel.IN_APP


async def _unread_count(session: AsyncSession, client: Client) -> int:
    return await session.scalar(
        select(func.count()).select_from(Notification).where(
            Notification.client_id == client.id, Notification.read_at.is_(None),
            _FEED,
        )
    ) or 0


@router.get("/me", response_model=ClientOut)
async def get_me(client: Client = Depends(require_client)) -> ClientOut:
    return client_out(client)


@router.patch("/me", response_model=ClientOut)
async def update_me(
    payload: ClientPatch,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> ClientOut:
    changes = payload.model_dump(exclude_unset=True)

    if changes.get("city_id") is not None:
        city = await session.get(City, changes["city_id"])
        # A city marked «скоро» is in the list but cannot be chosen yet; picking
        # one has to fail with a code the app can render, not silently stick.
        if city is None or not city.is_active:
            raise AppError(ErrorCode.CITY_NOT_AVAILABLE,
                           "This city is not available yet.", 422,
                           details={"city_id": str(changes["city_id"])})

    for field, value in changes.items():
        setattr(client, field, value)
    await session.commit()
    return client_out(client)


@router.get("/me/notification-settings", response_model=SettingsOut)
async def get_settings_(
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> SettingsOut:
    row = await _settings_for(session, client)
    await session.commit()
    return SettingsOut.model_validate(row, from_attributes=True)


@router.patch("/me/notification-settings", response_model=SettingsOut)
async def update_settings(
    payload: SettingsPatch,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> SettingsOut:
    row = await _settings_for(session, client)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    await session.commit()
    return SettingsOut.model_validate(row, from_attributes=True)


@router.get("/me/notifications", response_model=NotificationFeed)
async def list_notifications(
    unread_only: bool = False,
    params: CursorParams = Depends(cursor_params),
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> NotificationFeed:
    stmt = select(Notification).where(Notification.client_id == client.id, _FEED)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    rows, meta = await paginate_cursor(session, stmt, params, Notification.created_at)
    return NotificationFeed(
        items=[_notification_out(row) for row in rows],
        unread_count=await _unread_count(session, client),
        pagination=meta,
    )


@router.post("/me/notifications/read", response_model=MarkReadResult)
async def mark_read(
    payload: MarkReadRequest,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> MarkReadResult:
    stmt = (
        update(Notification)
        # Scoped to the caller on every path: an id list from a client is not a
        # licence to touch somebody else's row.
        .where(Notification.client_id == client.id, Notification.read_at.is_(None),
               _FEED)
        .values(read_at=utcnow())
    )
    if payload.notification_ids is not None:
        stmt = stmt.where(Notification.id.in_(payload.notification_ids))

    await session.execute(stmt)
    await session.commit()
    return MarkReadResult(unread_count=await _unread_count(session, client))
