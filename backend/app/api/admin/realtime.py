"""The monitor's event stream.

Server-sent events tailed off `audit_entries` (Plan 2d, D5). No broker: the
journal is already written for every state change worth watching, so a restart
loses nothing and a second worker duplicates nothing — each connection reads the
table for itself.

The panel starts on 5-second polling and moves to this endpoint without changing
what it renders, which is why the payload is the same `AuditLogEntry` the log
screen already knows how to draw.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import get_session, utcnow
from app.core.deps import require_permission
from app.core.types import utc_isoformat
from app.modules.audit import views
from app.modules.audit.models import AuditEntry
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/realtime", tags=["admin-realtime"])

# Two seconds is well inside the five the panel polls at today, and a query that
# returns nothing on a quiet fleet costs one indexed lookup.
POLL_SECONDS = 2.0

# Proxies and load balancers kill an idle connection somewhere around a minute.
# A comment line is a legal SSE frame that no client dispatches as an event.
HEARTBEAT_SECONDS = 20.0

# Newest first is what the log screen wants; a stream wants the opposite, and a
# reconnecting client that missed a burst should not get the whole table.
CATCH_UP_LIMIT = 50

EVENT_TYPES = {
    "cell.opened": "cell.door_opened",
    "cell.remote_opened": "cell.door_opened",
    "cell.door_opened": "cell.door_opened",
    "cell.door_closed": "cell.door_closed",
    "cell.blocked": "cell.status_changed",
    "cell.unblocked": "cell.status_changed",
    "cell.status_changed": "cell.status_changed",
    "cell.maintenance": "cell.status_changed",
    "booking.created": "booking.created",
    "device.online": "device.presence_changed",
    "device.offline": "device.presence_changed",
    "device.tamper": "device.alert",
    "lock_agent.error": "device.alert",
}


def realtime_type(event: str) -> str:
    """Map a journal event onto the contract's `RealtimeEventType`.

    Anything the map does not name is still worth streaming — it is what the
    monitor's «Последние события» panel shows — so it arrives as
    `audit.entry_created` rather than being dropped.
    """
    if event in EVENT_TYPES:
        return EVENT_TYPES[event]
    if event.startswith("booking.") or event.startswith("parcel."):
        return "booking.status_changed"
    if event.startswith("device."):
        return "device.presence_changed"
    return "audit.entry_created"


def frame(entry: views.AuditLogEntry) -> str:
    body = {
        "id": str(entry.id),
        "type": realtime_type(entry.event_type),
        "occurred_at": entry.occurred_at,
        "payload": entry.model_dump(mode="json"),
    }
    # `id:` as well as the JSON field, so a browser's own EventSource sends
    # Last-Event-ID on reconnect without the panel having to track it.
    return (f"id: {entry.id}\n"
            f"event: {body['type']}\n"
            f"data: {json.dumps(body, ensure_ascii=False)}\n\n")


async def _resume_from(
    session: AsyncSession, last_event_id: str | None
) -> tuple[datetime, uuid.UUID] | None:
    """The keyset position to read after, or None to start from now.

    An unknown or malformed id starts from now rather than from the beginning:
    a client resuming with a stale token wants the live stream, not a replay of
    the entire journal.
    """
    if not last_event_id:
        return None
    try:
        row_id = uuid.UUID(last_event_id)
    except ValueError:
        return None
    row = await session.get(AuditEntry, row_id)
    return (row.created_at, row.id) if row else None


async def events(
    request: Request,
    maker: async_sessionmaker,
    postamat_id: uuid.UUID | None = None,
    last_event_id: str | None = None,
) -> AsyncIterator[str]:
    async with maker() as session:
        # Without a resume point the stream starts from now: a monitor that has
        # just been opened wants what happens next, not the last week.
        after = await _resume_from(session, last_event_id) or (
            utcnow(), uuid.UUID(int=0)
        )
        quiet_for = 0.0
        while True:
            # Checked before the query as well as after the sleep: a client that
            # closed the tab must not keep this loop reading the database.
            if await request.is_disconnected():
                return

            stmt = (
                select(AuditEntry)
                .where(tuple_(AuditEntry.created_at, AuditEntry.id) > after)
                .order_by(AuditEntry.created_at, AuditEntry.id)
                .limit(CATCH_UP_LIMIT)
            )
            if postamat_id:
                stmt = stmt.where(AuditEntry.postamat_id == postamat_id)
            rows = list(await session.scalars(stmt))

            if rows:
                numbers, cells = await views.labels(session, rows)
                for row in rows:
                    yield frame(views.entry(row, numbers, cells))
                after = (rows[-1].created_at, rows[-1].id)
                quiet_for = 0.0
            else:
                quiet_for += POLL_SECONDS
                if quiet_for >= HEARTBEAT_SECONDS:
                    quiet_for = 0.0
                    yield f": ping {utc_isoformat(utcnow())}\n\n"

            await asyncio.sleep(POLL_SECONDS)


@router.get("/stream")
async def stream(
    request: Request,
    postamat_id: uuid.UUID | None = Query(default=None),
    last_event_id: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("bookings.read")),
) -> StreamingResponse:
    # Its own session, like the CSV export: FastAPI closes a dependency's session
    # when the handler returns, and this body is produced for as long as the
    # monitor stays open.
    maker = async_sessionmaker(session.bind, expire_on_commit=False)
    return StreamingResponse(
        events(request, maker, postamat_id, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx buffers a proxied response by default, which turns a live
            # stream into a file that arrives when it ends.
            "X-Accel-Buffering": "no",
        },
    )
