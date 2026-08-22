### Task 3: Database layer and Alembic

**Files:**
- Create: `backend/app/core/db.py`, `backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/script.py.mako`, `backend/tests/conftest.py`
- Modify: `backend/.env` (already carries `TEST_DATABASE_URL` from Task 1 — no change needed)

**Interfaces:**
- Produces: `app.core.db.Base` (declarative base with a `uuid7`-style primary key mixin), `app.core.db.get_session()` FastAPI dependency, `app.core.db.session_scope()` async context manager for workers and tests.

- [ ] **Step 1: Write `backend/app/core/db.py`**

```python
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import DateTime, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
```

- [ ] **Step 2: Nothing to provision**

`TEST_DATABASE_URL` is already `sqlite+aiosqlite:///:memory:` from Task 1. Postgres is not
reachable on this machine yet, so the suite runs on SQLite until the human partner configures the
`postgresql-x64-18` service. Keep every model and query in this plan portable: `JSON` rather than
`JSONB`, no server-side defaults beyond `func.now()`, and no Postgres-only SQL. Plan 2, which needs
`FOR UPDATE SKIP LOCKED`, requires the real database and says so.

- [ ] **Step 3: Initialise Alembic**

```bash
cd backend
./.venv/Scripts/python.exe -m alembic init -t async alembic
```

- [ ] **Step 4: Point `backend/alembic/env.py` at the application metadata**

Replace the `target_metadata` line and the URL configuration:

```python
import os

from app.core.db import Base
from app.modules import audit, catalog, identity  # noqa: F401  register models

target_metadata = Base.metadata
config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
```

Leave the rest of the generated file as it is. Create empty `backend/app/modules/__init__.py`,
`backend/app/modules/audit/__init__.py`, `backend/app/modules/identity/__init__.py` and
`backend/app/modules/catalog/__init__.py` so the import above resolves.

- [ ] **Step 5: Write `backend/tests/conftest.py`**

```python
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base, get_session
from app.main import create_app

TEST_DATABASE_URL = os.environ["TEST_DATABASE_URL"]


@pytest.fixture(scope="session")
def test_engine():
    return create_async_engine(TEST_DATABASE_URL)


@pytest.fixture(autouse=True)
async def schema(test_engine):
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture
async def session(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as s:
        yield s


@pytest.fixture
async def client(session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
```

Delete the now-duplicated `client` fixture from `backend/tests/core/test_health.py`.

Because `TEST_DATABASE_URL` is an in-memory SQLite database, give `create_async_engine` a
`StaticPool` and `connect_args={"check_same_thread": False}` so every session in a test shares one
connection — otherwise each session gets its own empty database and the schema fixture's tables
vanish.

- [ ] **Step 6: Run the whole suite and confirm it still passes**

```bash
./.venv/Scripts/python.exe -m pytest -v
```

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "feat: async database layer, alembic, and test harness"
```

---

