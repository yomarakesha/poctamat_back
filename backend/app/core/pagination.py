from dataclasses import dataclass
from typing import Any, Sequence

from fastapi import Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession


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


def page_params(
    page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=100)
) -> PageParams:
    return PageParams(page=page, per_page=per_page)


async def paginate_page(
    session: AsyncSession, stmt: Select, params: PageParams
) -> tuple[Sequence[Any], PageMeta]:
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = await session.scalars(
        stmt.limit(params.per_page).offset((params.page - 1) * params.per_page)
    )
    return list(rows), page_meta(params.page, params.per_page, total or 0)
