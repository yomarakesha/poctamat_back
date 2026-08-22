### Task 7: Pagination helpers

**Files:**
- Create: `backend/app/core/pagination.py`, `backend/tests/core/test_pagination.py`

**Interfaces:**
- Produces: `PageParams(page, per_page)` dependency, `PageMeta(page, per_page, total, total_pages)`, `paginate_page(session, stmt, params) -> tuple[list, PageMeta]`, `CursorParams(cursor, limit)`, `CursorMeta(next_cursor, has_more)`, `paginate_cursor(session, stmt, params, cursor_column) -> tuple[list, CursorMeta]`.

Reference data uses pages because the admin footer needs a total. Growing lists use cursors.

- [ ] **Step 1: Write the failing test**

```python
from app.core.pagination import PageMeta, page_meta


def test_total_pages_rounds_up():
    assert page_meta(page=1, per_page=20, total=124) == PageMeta(
        page=1, per_page=20, total=124, total_pages=7
    )


def test_zero_total_still_reports_one_page():
    assert page_meta(page=1, per_page=20, total=0).total_pages == 1
```

- [ ] **Step 2: Run it and confirm it fails**

- [ ] **Step 3: Write `backend/app/core/pagination.py`**

```python
import base64
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


class CursorMeta(BaseModel):
    next_cursor: str | None
    has_more: bool


@dataclass
class CursorParams:
    cursor: str | None = None
    limit: int = 50


def cursor_params(
    cursor: str | None = Query(None), limit: int = Query(50, ge=1, le=200)
) -> CursorParams:
    return CursorParams(cursor=cursor, limit=limit)


def encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode()


def decode_cursor(value: str) -> str:
    return base64.urlsafe_b64decode(value.encode()).decode()
```

- [ ] **Step 4: Run the test and confirm it passes**

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: page and cursor pagination helpers"
```

---

