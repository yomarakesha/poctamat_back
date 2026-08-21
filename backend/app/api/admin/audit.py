import csv
import io
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import get_session, utcnow
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.core.kvstore import get_kvstore
from app.core.pagination import CursorMeta, CursorParams, cursor_params, paginate_cursor
from app.core.types import utc_isoformat
from app.modules.audit import views
from app.modules.audit.models import AuditEntry, Source
from app.modules.audit.service import record
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/audit-log", tags=["admin-audit"])

# Entries older than this are cold storage's problem. Nothing moves them there
# yet, but the window is enforced on reads from the first day, so the panel is
# built against the behaviour it will meet in a year rather than against a table
# that happens to be young.
HOT_WINDOW_DAYS = 365

# A CSV bigger than this is not a report anybody opens; it is a database dump
# taken through a browser. The caller is told to narrow the range instead.
EXPORT_MAX_ROWS = 50_000
EXPORT_BATCH = 1_000

# One journal entry per operator per hour, not one per page of the log. Reading
# the log must leave a trace; writing a row for every refresh of an
# auto-refreshing screen would bury the entries worth reading under them.
READ_AUDIT_TTL_SECONDS = 3600

SEVERITY_PATTERN = "^(info|warning|error)(,(info|warning|error))*$"


class AuditPage(BaseModel):
    items: list[views.AuditLogEntry]
    pagination: CursorMeta
    # Cursor pagination plus the one field the contract reserves for this
    # endpoint: set when the requested range reaches past the hot window.
    truncated_at: str | None = None


class AuditFilters(BaseModel):
    query: str | None = None
    source: str | None = None
    actor_id: uuid.UUID | None = None
    event_type: str | None = None
    severity: str | None = None
    postamat_id: uuid.UUID | None = None
    cell_id: uuid.UUID | None = None
    since: datetime | None = None
    until: datetime | None = None


def filters(
    # `q` on the wire: the contract shares one search parameter across every
    # list that has one, and a panel generated from it sends that name.
    query: str | None = Query(default=None, alias="q", max_length=200),
    source: str | None = Query(default=None,
                               description="Comma-separated list of sources."),
    actor_id: uuid.UUID | None = Query(default=None),
    event_type: str | None = Query(default=None,
                                   description="Comma-separated list of events."),
    severity: str | None = Query(default=None, pattern=SEVERITY_PATTERN),
    postamat_id: uuid.UUID | None = Query(default=None),
    cell_id: uuid.UUID | None = Query(default=None),
    since: datetime | None = Query(default=None, alias="from"),
    until: datetime | None = Query(default=None, alias="to"),
) -> AuditFilters:
    return AuditFilters(
        query=query, source=source, actor_id=actor_id, event_type=event_type,
        severity=severity, postamat_id=postamat_id, cell_id=cell_id,
        since=since, until=until,
    )


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def hot_window_start() -> datetime:
    return utcnow() - timedelta(days=HOT_WINDOW_DAYS)


async def _actor_login(session: AsyncSession, actor_id: uuid.UUID) -> str | None:
    # The journal names its actor by login rather than by id, because a device or
    # the scheduler has no row in `admin_users` to point at. Filtering by a person
    # therefore starts by looking their login up.
    return await session.scalar(
        select(AdminUser.login).where(AdminUser.id == actor_id)
    )


async def build_query(
    session: AsyncSession, spec: AuditFilters
) -> tuple[Select, str | None]:
    floor = hot_window_start()
    stmt = select(AuditEntry).where(AuditEntry.created_at >= floor)
    truncated_at = (
        utc_isoformat(floor) if spec.since and spec.since < floor else None
    )

    if spec.since:
        stmt = stmt.where(AuditEntry.created_at >= spec.since)
    if spec.until:
        stmt = stmt.where(AuditEntry.created_at <= spec.until)
    if spec.postamat_id:
        stmt = stmt.where(AuditEntry.postamat_id == spec.postamat_id)
    if spec.cell_id:
        stmt = stmt.where(AuditEntry.cell_id == spec.cell_id)
    if sources := _split(spec.source):
        stmt = stmt.where(AuditEntry.source.in_(sources))
    if events := _split(spec.event_type):
        stmt = stmt.where(AuditEntry.event.in_(events))
    if severities := _split(spec.severity):
        stmt = stmt.where(AuditEntry.severity.in_(severities))
    if spec.actor_id:
        login = await _actor_login(session, spec.actor_id)
        # An unknown actor matches nothing rather than everything: a filter that
        # silently widens is how somebody reads a log they meant to narrow.
        stmt = stmt.where(AuditEntry.actor == login) if login else stmt.where(
            AuditEntry.id.is_(None)
        )
    if spec.query:
        needle = f"%{spec.query}%"
        stmt = stmt.where(or_(
            AuditEntry.message.ilike(needle),
            AuditEntry.event.ilike(needle),
            AuditEntry.actor.ilike(needle),
        ))
    return stmt, truncated_at


async def _note_read(session: AsyncSession, actor: AdminUser, spec: AuditFilters
                     ) -> None:
    store = get_kvstore()
    key = f"audit.read:{actor.id}"
    if await store.get(key):
        return
    await store.put(key, {"seen": "1"}, READ_AUDIT_TTL_SECONDS)
    await record(
        session, event="audit.read", source=Source.ADMIN, actor=actor.login,
        message="Просмотрен журнал событий.",
        details={"filters": spec.model_dump(mode="json", exclude_none=True)},
    )
    await session.commit()


@router.get("", response_model=AuditPage)
async def list_audit_log(
    spec: AuditFilters = Depends(filters),
    params: CursorParams = Depends(cursor_params),
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(require_permission("audit.read")),
) -> AuditPage:
    stmt, truncated_at = await build_query(session, spec)
    rows, meta = await paginate_cursor(session, stmt, params, AuditEntry.created_at)
    items = await views.entries(session, list(rows))
    await _note_read(session, actor, spec)
    return AuditPage(items=items, pagination=meta, truncated_at=truncated_at)


CSV_HEADER = [
    "occurred_at", "source", "actor", "event_type", "severity", "message",
    "postamat_number", "cell_number", "reason", "trace_id",
]

# Excel opens a BOM-less UTF-8 CSV as cp1251, and every Russian message in the
# file turns to mojibake.
BOM = "﻿"


def _csv_line(values: list[str]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerow(values)
    return buffer.getvalue()


async def _csv_rows(maker: async_sessionmaker, stmt: Select) -> AsyncIterator[str]:
    """Yield the export a batch at a time.

    On its own session, not the request's: FastAPI closes a dependency's session
    when the handler returns, and the body of a streamed response is produced
    after that.
    """
    yield BOM + _csv_line(CSV_HEADER)
    async with maker() as session:
        ordered = stmt.order_by(AuditEntry.created_at.desc(), AuditEntry.id.desc())
        offset = 0
        while True:
            rows = list(await session.scalars(
                ordered.limit(EXPORT_BATCH).offset(offset)
            ))
            if not rows:
                return
            numbers, cells = await views.labels(session, rows)
            for row in rows:
                one = views.entry(row, numbers, cells)
                yield _csv_line([
                    one.occurred_at, one.source, one.actor_label or "",
                    one.event_type, one.severity, one.message,
                    one.postamat_number or "", one.cell_number or "",
                    one.reason or "", one.trace_id or "",
                ])
            offset += len(rows)


@router.get("/export")
async def export_audit_log(
    spec: AuditFilters = Depends(filters),
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(require_permission("audit.read")),
) -> StreamingResponse:
    stmt, _ = await build_query(session, spec)
    matched = await session.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0
    if matched > EXPORT_MAX_ROWS:
        raise AppError(
            ErrorCode.EXPORT_TOO_LARGE,
            "Слишком много записей для выгрузки — сузьте период.", 422,
            details={"matched": matched, "max_rows": EXPORT_MAX_ROWS},
        )

    # An export is a copy of the journal leaving the building, so it is recorded
    # every time rather than once an hour like a screenful of it.
    await record(
        session, event="audit.exported", source=Source.ADMIN, actor=actor.login,
        message="Журнал событий выгружен в CSV.",
        details={"matched": matched,
                 "filters": spec.model_dump(mode="json", exclude_none=True)},
    )
    await session.commit()

    maker = async_sessionmaker(session.bind, expire_on_commit=False)
    filename = f"audit-log-{utcnow().date().isoformat()}.csv"
    return StreamingResponse(
        _csv_rows(maker, stmt),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
