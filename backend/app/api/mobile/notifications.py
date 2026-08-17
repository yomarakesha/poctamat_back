import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_client
from app.core.errors import AppError, ErrorCode
from app.core.pagination import PageMeta, PageParams, page_params, paginate_page
from app.core.types import utc_isoformat
from app.modules.identity.models import Client
from app.modules.notify.models import Notification, NotificationKind

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: uuid.UUID
    kind: NotificationKind
    title: str
    body: str
    booking_id: uuid.UUID | None
    is_read: bool
    created_at: str


class NotificationPage(BaseModel):
    items: list[NotificationOut]
    pagination: PageMeta


def _out(row: Notification) -> NotificationOut:
    return NotificationOut(
        id=row.id, kind=row.kind, title=row.title, body=row.body,
        booking_id=row.booking_id, is_read=row.is_read,
        created_at=utc_isoformat(row.created_at),
    )


@router.get("", response_model=NotificationPage)
async def list_notifications(
    unread: bool = Query(default=False),
    params: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> NotificationPage:
    stmt = (
        select(Notification).where(Notification.client_id == client.id)
        .order_by(Notification.created_at.desc())
    )
    if unread:
        stmt = stmt.where(Notification.is_read.is_(False))
    rows, meta = await paginate_page(session, stmt, params)
    return NotificationPage(items=[_out(row) for row in rows], pagination=meta)


# Declared before the parameterised route so "read-all" is never read as an id.
@router.post("/read-all", status_code=204)
async def mark_all_read(
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> Response:
    await session.execute(
        update(Notification)
        .where(Notification.client_id == client.id, Notification.is_read.is_(False))
        .values(is_read=True)
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{notification_id}/read", status_code=204)
async def mark_read(
    notification_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> Response:
    row = await session.get(Notification, notification_id)
    # Someone else's notification is not found rather than forbidden: its
    # existence is not a fact this caller gets to confirm.
    if row is None or row.client_id != client.id:
        raise AppError(ErrorCode.NOT_FOUND, "Notification not found.", 404)
    row.is_read = True
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
