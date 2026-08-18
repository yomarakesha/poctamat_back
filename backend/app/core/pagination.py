import base64
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from fastapi import Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode


class PageMeta(BaseModel):
    page: int
    per_page: int
    total: int
    total_pages: int


def page_meta(page: int, per_page: int, total: int) -> PageMeta:
    total_pages = max(1, -(-total // per_page))
    return PageMeta(page=page, per_page=per_page, total=total, total_pages=total_pages)


@dataclass
class PageParams:
    page: int = 1
    per_page: int = 20


MAX_PAGE = 10_000


def page_params(
    page: int = Query(1, ge=1, le=MAX_PAGE), per_page: int = Query(20, ge=1, le=100)
) -> PageParams:
    return PageParams(page=page, per_page=per_page)


class CursorMeta(BaseModel):
    next_cursor: str | None = None
    has_more: bool


@dataclass
class CursorParams:
    limit: int = 50
    cursor: str | None = None


MAX_LIMIT = 200


def cursor_params(
    limit: int = Query(50, ge=1, le=MAX_LIMIT), cursor: str | None = Query(None)
) -> CursorParams:
    return CursorParams(limit=limit, cursor=cursor)


def encode_cursor(value: datetime, row_id: uuid.UUID) -> str:
    """Pack the ordering key and the row id into one opaque token.

    Both halves are needed: ordering by a timestamp alone cannot resume inside a
    group of rows written in the same second, and SQLite writes plenty of those.
    """
    raw = f"{value.isoformat()}|{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    padding = "=" * (-len(cursor) % 4)
    try:
        moment, row_id = base64.urlsafe_b64decode(cursor + padding).decode().split("|", 1)
        uuid.UUID(row_id)
    except (ValueError, UnicodeDecodeError, base64.binascii.Error) as error:
        # The cursor is client-supplied and opaque, so a bad one is a request
        # problem rather than a server error — and never a reason to fall back to
        # page one, which would silently repeat rows the caller has already seen.
        raise AppError(
            ErrorCode.VALIDATION_ERROR, "Malformed cursor.", 422,
            details={"cursor": cursor},
        ) from error
    return moment, row_id


async def paginate_cursor(
    session: AsyncSession, stmt: Select, params: CursorParams, order_column,
) -> tuple[Sequence[Any], CursorMeta]:
    """Newest-first keyset pagination over `order_column`, tie-broken by id.

    A feed grows while it is being read. Offsets shift under inserts and make a
    reader see one row twice; a keyset cursor asks for "older than this exact
    row" and cannot.
    """
    model = order_column.parent.class_
    limit = min(params.limit, MAX_LIMIT)
    stmt = stmt.order_by(order_column.desc(), model.id.desc())

    if params.cursor:
        moment, row_id = decode_cursor(params.cursor)
        # uuid.UUID, not the string: the id column is a Uuid type and hands the
        # driver a raw string it cannot bind.
        stmt = stmt.where(
            tuple_(order_column, model.id)
            < (datetime.fromisoformat(moment), uuid.UUID(row_id))
        )

    rows = list(await session.scalars(stmt.limit(limit + 1)))
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        encode_cursor(getattr(rows[-1], order_column.key), rows[-1].id)
        if rows and has_more
        else None
    )
    return rows, CursorMeta(next_cursor=next_cursor, has_more=has_more)


async def paginate_page(
    session: AsyncSession, stmt: Select, params: PageParams
) -> tuple[Sequence[Any], PageMeta]:
    # `page` is bounded so a caller cannot ask for OFFSET 20000000000 and make the
    # database scan and discard every row before it. Callers that construct
    # PageParams directly get the same ceiling as the query parameter.
    page = min(params.page, MAX_PAGE)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = await session.scalars(
        stmt.limit(params.per_page).offset((page - 1) * params.per_page)
    )
    return list(rows), page_meta(page, params.per_page, total or 0)
