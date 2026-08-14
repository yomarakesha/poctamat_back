# Postamat Backend — Foundation and Reference Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the postamat API service with its cross-cutting machinery (errors, idempotency, pagination, audit) and every reference-data entity the three clients need, so the frontend can build the catalog and admin screens against a real server.

**Architecture:** Modular monolith. FastAPI application in `backend/app`, split into `core` (infrastructure) and `modules` (domain), with routers grouped by audience under `api/`. Modules never import each other's models — they call each other's service functions. Everything runs in Docker Compose because the host Python is 3.14 and several async drivers have no wheels for it yet.

**Tech Stack:** Python 3.12 (container), FastAPI, SQLAlchemy 2.0 async + asyncpg, Alembic, Pydantic v2, Redis, PyJWT, argon2-cffi, pytest + pytest-asyncio + httpx.

**Spec:** `docs/superpowers/specs/2026-08-14-postamat-backend-design.md`, as amended by the contract reply of 2026-08-14 (five change requests, `offline_ttl` = 24 h).

## Scope

This plan covers the foundation and reference data only. Two further plans follow:

- **Plan 2 — booking core:** bookings, access codes, payments, custody, the overdue workers, `GET /postamats/{id}/availability`.
- **Plan 3 — devices:** the agent channel, the kiosk channel, the local lock agent. Starts when the RS-485 hardware arrives.

`GET /postamats/{id}/availability` is deliberately deferred to Plan 2: free-cell counts are derived from active bookings, and the booking table does not exist yet. Writing it here would mean writing it twice.

## Global Constraints

- Python **3.12** inside the container. Never assume the host interpreter.
- All API paths are prefixed **`/api/v1`**.
- Every error response uses the envelope `{"error": {"code", "message", "details", "trace_id"}}`. `code` is a stable machine constant, `message` is English for developers and is never shown to a user.
- `401` means an expired or missing token. `403` means insufficient rights. Never swap them.
- Every state-changing `POST` accepts an `Idempotency-Key` header. Same key + same body returns the original response; same key + different body returns `409 IDEMPOTENCY_KEY_REUSED`. `5xx` responses are never memoized.
- Money is `{"amount_minor": <int>, "currency": "TMT"}`. No floats anywhere near money.
- All timestamps are UTC ISO-8601 with a `Z` suffix.
- Phone numbers match `^\+993[0-9]{8}$`.
- `Accept-Language` accepts `tk`, `ru`, `en`; the default is **`tk`**.
- Login OTP is **6 digits**. Cell PIN is **5 digits**. They never share a validator.
- No `DELETE` endpoints except removing an uploaded photo. Everything else blocks or deactivates.
- Rental durations are exactly `12`, `24`, `48` hours.
- Contract naming wins over the names in the design document.

---

## File Structure

```
backend/
  docker-compose.yml          Postgres, Redis, api
  Dockerfile
  requirements.txt
  pytest.ini
  alembic.ini
  alembic/
    env.py
    versions/
  app/
    main.py                   application factory, router registration
    core/
      config.py               Settings
      db.py                   engine, session, Base
      errors.py               ErrorCode, AppError, handlers
      context.py              trace_id and language middleware
      types.py                PhoneNumber, Money
      pagination.py           PageParams, PageMeta, CursorParams
      idempotency.py          model + dependency
      security.py             hashing, JWT, auth dependencies
      ratelimit.py            IP limiter
    modules/
      audit/                  models.py, service.py
      identity/               models.py, schemas.py, service.py
      catalog/                models.py, schemas.py, service.py
    api/
      public/                 unauthenticated routers
      mobile/                 client-token routers
      admin/                  admin-session routers
  tests/
    conftest.py
    core/
    identity/
    catalog/
```

---

### Task 1: Repository skeleton and a running container

**Files:**
- Create: `.gitignore`, `backend/Dockerfile`, `backend/docker-compose.yml`, `backend/requirements.txt`, `backend/pytest.ini`, `backend/app/__init__.py`, `backend/app/main.py`, `backend/tests/__init__.py`, `backend/tests/conftest.py`, `backend/tests/core/test_health.py`

**Interfaces:**
- Produces: `app.main.create_app() -> FastAPI`, and a `GET /api/v1/health` route returning `{"status": "ok"}`.

- [ ] **Step 1: Initialise the repository**

```bash
cd /c/Users/yomarakesha/Desktop/projects/poctamat
git init
git add -A
git commit -m "chore: snapshot existing reverse-engineering material"
```

- [ ] **Step 2: Write `.gitignore` at the repository root**

```gitignore
__pycache__/
*.py[cod]
.venv/
.env
.pytest_cache/
.ruff_cache/
*.egg-info/
```

- [ ] **Step 3: Write `backend/requirements.txt`**

```
fastapi==0.115.6
uvicorn[standard]==0.34.0
sqlalchemy[asyncio]==2.0.36
asyncpg==0.30.0
alembic==1.14.0
pydantic==2.10.4
pydantic-settings==2.7.0
redis==5.2.1
pyjwt==2.10.1
argon2-cffi==23.1.0
python-multipart==0.0.20
pytest==8.3.4
pytest-asyncio==0.25.0
httpx==0.28.1
```

- [ ] **Step 4: Write `backend/Dockerfile`**

```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
```

- [ ] **Step 5: Write `backend/docker-compose.yml`**

```yaml
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: postamat
      POSTGRES_PASSWORD: postamat
      POSTGRES_DB: postamat
    ports: ["5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postamat"]
      interval: 3s
      timeout: 3s
      retries: 20

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]

  api:
    build: .
    environment:
      DATABASE_URL: postgresql+asyncpg://postamat:postamat@db:5432/postamat
      REDIS_URL: redis://redis:6379/0
      JWT_SECRET: dev-secret-change-me
      PIN_PEPPER: dev-pepper-change-me
    volumes: [".:/srv"]
    ports: ["8000:8000"]
    depends_on:
      db: {condition: service_healthy}
```

- [ ] **Step 6: Write `backend/pytest.ini`**

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

- [ ] **Step 7: Write the failing test in `backend/tests/core/test_health.py`**

```python
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_returns_ok(client):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 8: Run the test and confirm it fails**

```bash
cd backend
docker compose build api
docker compose run --rm api pytest tests/core/test_health.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.main'`.

- [ ] **Step 9: Write `backend/app/main.py`**

```python
from fastapi import APIRouter, FastAPI

API_PREFIX = "/api/v1"

health_router = APIRouter()


@health_router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def create_app() -> FastAPI:
    app = FastAPI(title="Postamat API", version="1.0.0")
    app.include_router(health_router, prefix=API_PREFIX)
    return app


app = create_app()
```

Also create empty `backend/app/__init__.py`, `backend/tests/__init__.py` and `backend/tests/core/__init__.py`.

- [ ] **Step 10: Run the test and confirm it passes**

```bash
docker compose run --rm api pytest tests/core/test_health.py -v
```

Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "feat: backend skeleton with health endpoint and docker compose"
```

---

### Task 2: Settings

**Files:**
- Create: `backend/app/core/__init__.py`, `backend/app/core/config.py`, `backend/tests/core/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `app.core.config.Settings` and `get_settings() -> Settings` with fields `database_url: str`, `redis_url: str`, `jwt_secret: str`, `pin_pepper: str`, `access_token_ttl_minutes: int = 30`, `refresh_token_ttl_days: int = 30`, `otp_length: int = 6`, `otp_ttl_seconds: int = 300`, `pin_length: int = 5`, `hold_minutes: int = 10`, `offline_ttl_hours: int = 24`, `rental_durations: tuple[int, ...] = (12, 24, 48)`, `default_language: str = "tk"`.

- [ ] **Step 1: Write the failing test**

```python
from app.core.config import get_settings


def test_settings_expose_pinned_product_constants():
    settings = get_settings()
    assert settings.otp_length == 6
    assert settings.pin_length == 5
    assert settings.hold_minutes == 10
    assert settings.offline_ttl_hours == 24
    assert settings.rental_durations == (12, 24, 48)
    assert settings.default_language == "tk"


def test_get_settings_is_cached():
    assert get_settings() is get_settings()
```

- [ ] **Step 2: Run it and confirm it fails**

```bash
docker compose run --rm api pytest tests/core/test_config.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.config'`.

- [ ] **Step 3: Write `backend/app/core/config.py`**

```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    jwt_secret: str
    pin_pepper: str

    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 30

    otp_length: int = 6
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 5

    pin_length: int = 5
    hold_minutes: int = 10
    offline_ttl_hours: int = 24

    rental_durations: tuple[int, ...] = (12, 24, 48)
    default_language: str = "tk"
    supported_languages: tuple[str, ...] = ("tk", "ru", "en")


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Run the test and confirm it passes**

```bash
docker compose run --rm api pytest tests/core/test_config.py -v
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: application settings with pinned product constants"
```

---

### Task 3: Database layer and Alembic

**Files:**
- Create: `backend/app/core/db.py`, `backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/script.py.mako`, `backend/tests/conftest.py`
- Modify: `backend/docker-compose.yml` (add a `db_test` service)

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

- [ ] **Step 2: Add the test database service to `backend/docker-compose.yml`**

```yaml
  db_test:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: postamat
      POSTGRES_PASSWORD: postamat
      POSTGRES_DB: postamat_test
    tmpfs: ["/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postamat"]
      interval: 3s
      timeout: 3s
      retries: 20
```

Add `db_test: {condition: service_healthy}` to the `api` service's `depends_on`, and
`TEST_DATABASE_URL: postgresql+asyncpg://postamat:postamat@db_test:5432/postamat_test` to its environment.

- [ ] **Step 3: Initialise Alembic**

```bash
docker compose run --rm api alembic init -t async alembic
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

- [ ] **Step 6: Run the whole suite and confirm it still passes**

```bash
docker compose run --rm api pytest -v
```

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "feat: async database layer, alembic, and test harness"
```

---

### Task 4: Error envelope

**Files:**
- Create: `backend/app/core/errors.py`, `backend/tests/core/test_errors.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces: `ErrorCode` (str enum), `AppError(code, message, status_code, details=None)`, `install_error_handlers(app)`.

The full 102-code enum lives in the frontend's `contracts/openapi.yaml`, which has not arrived yet. Define the codes this plan needs; extend the enum from the YAML when it lands. Adding a code is not a breaking change.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from fastapi import APIRouter

from app.core.errors import AppError, ErrorCode


@pytest.fixture
async def error_client(client):
    router = APIRouter()

    @router.get("/boom")
    async def boom():
        raise AppError(ErrorCode.CLIENT_BLOCKED, "Client is blocked.", 403,
                       details={"client_id": "42"})

    client._transport.app.include_router(router, prefix="/api/v1")
    return client


async def test_app_error_renders_envelope(error_client):
    response = await error_client.get("/api/v1/boom")
    assert response.status_code == 403
    body = response.json()["error"]
    assert body["code"] == "CLIENT_BLOCKED"
    assert body["message"] == "Client is blocked."
    assert body["details"] == {"client_id": "42"}
    assert body["trace_id"]


async def test_validation_error_renders_envelope(client):
    response = await client.post("/api/v1/auth/otp/request", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
```

The second test is expected to stay red until Task 11 adds the route; mark it
`@pytest.mark.xfail(reason="route arrives in task 11", strict=False)` and remove the marker there.

- [ ] **Step 2: Run it and confirm it fails**

```bash
docker compose run --rm api pytest tests/core/test_errors.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.errors'`.

- [ ] **Step 3: Write `backend/app/core/errors.py`**

```python
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorCode(StrEnum):
    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    PERMISSION_DENIED = "PERMISSION_DENIED"

    OTP_INVALID = "OTP_INVALID"
    OTP_EXPIRED = "OTP_EXPIRED"
    OTP_TOO_MANY_ATTEMPTS = "OTP_TOO_MANY_ATTEMPTS"
    OTP_REQUEST_TOO_SOON = "OTP_REQUEST_TOO_SOON"

    CLIENT_BLOCKED = "CLIENT_BLOCKED"
    ADMIN_INACTIVE = "ADMIN_INACTIVE"
    CREDENTIALS_INVALID = "CREDENTIALS_INVALID"

    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"

    TARIFF_INCOMPLETE = "TARIFF_INCOMPLETE"
    POSTAMAT_BLOCKED = "POSTAMAT_BLOCKED"
    CELL_NUMBER_TAKEN = "CELL_NUMBER_TAKEN"
    CITY_IN_USE = "CITY_IN_USE"


class AppError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


def _envelope(request: Request, code: str, message: str,
              details: dict[str, Any] | None, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "trace_id": getattr(request.state, "trace_id", None),
            }
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return _envelope(request, exc.code, exc.message, exc.details, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {"field": ".".join(str(p) for p in err["loc"][1:]), "reason": err["msg"]}
            for err in exc.errors()
        ]
        return _envelope(request, ErrorCode.VALIDATION_FAILED,
                         "Request body failed validation.", {"fields": fields}, 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.INTERNAL_ERROR
        return _envelope(request, code, str(exc.detail), None, exc.status_code)
```

- [ ] **Step 4: Register the handlers in `create_app()`**

```python
from app.core.errors import install_error_handlers

def create_app() -> FastAPI:
    app = FastAPI(title="Postamat API", version="1.0.0")
    install_error_handlers(app)
    app.include_router(health_router, prefix=API_PREFIX)
    return app
```

- [ ] **Step 5: Run the test and confirm the first case passes**

```bash
docker compose run --rm api pytest tests/core/test_errors.py -v
```

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: single error envelope with machine codes"
```

---

### Task 5: Request context — trace id and language

**Files:**
- Create: `backend/app/core/context.py`, `backend/tests/core/test_context.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces: `RequestContextMiddleware`, and `app.core.context.get_language(request) -> str`.

- [ ] **Step 1: Write the failing test**

```python
async def test_trace_id_is_returned_in_header(client):
    response = await client.get("/api/v1/health")
    assert response.headers["x-trace-id"]


async def test_trace_id_is_echoed_when_supplied(client):
    response = await client.get("/api/v1/health", headers={"X-Trace-Id": "abc-123"})
    assert response.headers["x-trace-id"] == "abc-123"
```

- [ ] **Step 2: Run it and confirm it fails**

```bash
docker compose run --rm api pytest tests/core/test_context.py -v
```

Expected: FAIL with `KeyError: 'x-trace-id'`.

- [ ] **Step 3: Write `backend/app/core/context.py`**

```python
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        request.state.trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())

        header = request.headers.get("Accept-Language", "")
        primary = header.split(",")[0].split("-")[0].strip().lower()
        request.state.language = (
            primary if primary in settings.supported_languages else settings.default_language
        )

        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response


def get_language(request: Request) -> str:
    return getattr(request.state, "language", get_settings().default_language)
```

- [ ] **Step 4: Add the middleware in `create_app()`, before the routers**

```python
from app.core.context import RequestContextMiddleware

app.add_middleware(RequestContextMiddleware)
```

- [ ] **Step 5: Run the test and confirm it passes**

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: trace id and language negotiation middleware"
```

---

### Task 6: Shared field types

**Files:**
- Create: `backend/app/core/types.py`, `backend/tests/core/test_types.py`

**Interfaces:**
- Produces: `PhoneNumber` (annotated `str`), `Money` (Pydantic model with `amount_minor: int`, `currency: str = "TMT"`), `utc_isoformat(dt) -> str`.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from datetime import datetime, timezone
from pydantic import BaseModel, ValidationError

from app.core.types import Money, PhoneNumber, utc_isoformat


class Sample(BaseModel):
    phone: PhoneNumber


def test_valid_turkmen_number_is_accepted():
    assert Sample(phone="+99362123456").phone == "+99362123456"


@pytest.mark.parametrize("value", ["+7999123456", "99362123456", "+9936212345", "+993621234567"])
def test_bad_numbers_are_rejected(value):
    with pytest.raises(ValidationError):
        Sample(phone=value)


def test_money_defaults_to_manat():
    assert Money(amount_minor=1200).model_dump() == {"amount_minor": 1200, "currency": "TMT"}


def test_utc_isoformat_uses_z_suffix():
    moment = datetime(2026, 8, 14, 9, 30, tzinfo=timezone.utc)
    assert utc_isoformat(moment) == "2026-08-14T09:30:00Z"
```

- [ ] **Step 2: Run it and confirm it fails**

```bash
docker compose run --rm api pytest tests/core/test_types.py -v
```

- [ ] **Step 3: Write `backend/app/core/types.py`**

```python
from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

PhoneNumber = Annotated[str, StringConstraints(pattern=r"^\+993[0-9]{8}$")]


class Money(BaseModel):
    amount_minor: int = Field(ge=0)
    currency: str = "TMT"


def utc_isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

- [ ] **Step 4: Run the test and confirm it passes**

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: shared phone, money and timestamp types"
```

---

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

### Task 8: Idempotency

**Files:**
- Create: `backend/app/core/idempotency.py`, `backend/tests/core/test_idempotency.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces: model `IdempotencyRecord`, and `IdempotencyMiddleware` which memoizes `2xx` and `4xx` responses for POST requests carrying an `Idempotency-Key` header, keyed by `(key, method, path, subject)`.

`5xx` responses are never stored — they must stay retryable.

- [ ] **Step 1: Write the failing test**

```python
import uuid

import pytest
from fastapi import APIRouter


@pytest.fixture
async def counter_client(client):
    router = APIRouter()
    state = {"calls": 0}

    @router.post("/count")
    async def count(payload: dict):
        state["calls"] += 1
        return {"calls": state["calls"], "echo": payload}

    client._transport.app.include_router(router, prefix="/api/v1")
    client.state = state
    return client


async def test_same_key_and_body_replays_first_response(counter_client):
    key = str(uuid.uuid4())
    headers = {"Idempotency-Key": key}
    first = await counter_client.post("/api/v1/count", json={"a": 1}, headers=headers)
    second = await counter_client.post("/api/v1/count", json={"a": 1}, headers=headers)
    assert first.json() == second.json()
    assert counter_client.state["calls"] == 1


async def test_same_key_different_body_is_rejected(counter_client):
    key = str(uuid.uuid4())
    headers = {"Idempotency-Key": key}
    await counter_client.post("/api/v1/count", json={"a": 1}, headers=headers)
    clash = await counter_client.post("/api/v1/count", json={"a": 2}, headers=headers)
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
```

- [ ] **Step 2: Run it and confirm it fails**

- [ ] **Step 3: Write `backend/app/core/idempotency.py`**

```python
import hashlib
import json

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import String, select
from sqlalchemy.orm import Mapped, mapped_column
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.db import Base, Timestamped, UUIDPrimaryKey, session_scope
from app.core.errors import ErrorCode


class IdempotencyRecord(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(128), index=True)
    scope: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int]
    response_body: Mapped[str] = mapped_column(String)


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        key = request.headers.get("Idempotency-Key")
        if request.method != "POST" or not key:
            return await call_next(request)

        body = await request.body()
        request_hash = hashlib.sha256(body).hexdigest()
        scope = f"{request.method} {request.url.path}"

        async with session_scope() as session:
            existing = await session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.key == key, IdempotencyRecord.scope == scope
                )
            )
            if existing and existing.request_hash != request_hash:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": {
                            "code": ErrorCode.IDEMPOTENCY_KEY_REUSED,
                            "message": "This Idempotency-Key was used with a different body.",
                            "details": None,
                            "trace_id": getattr(request.state, "trace_id", None),
                        }
                    },
                )
            if existing:
                return JSONResponse(
                    status_code=existing.status_code,
                    content=json.loads(existing.response_body),
                )

        response = await call_next(request)
        if response.status_code >= 500:
            return response

        chunks = [chunk async for chunk in response.body_iterator]
        payload = b"".join(chunks)

        async with session_scope() as session:
            session.add(
                IdempotencyRecord(
                    key=key,
                    scope=scope,
                    request_hash=request_hash,
                    status_code=response.status_code,
                    response_body=payload.decode(),
                )
            )
            await session.commit()

        return JSONResponse(
            status_code=response.status_code, content=json.loads(payload.decode())
        )
```

- [ ] **Step 4: Register the middleware in `create_app()` — before `RequestContextMiddleware`**

Starlette runs the last-added middleware outermost, so `add_middleware(IdempotencyMiddleware)`
must come first and `add_middleware(RequestContextMiddleware)` second. Otherwise idempotency
replies are produced before `trace_id` exists and their envelopes carry `null`.

```python
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(RequestContextMiddleware)
```

- [ ] **Step 5: Run the test and confirm it passes**

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: idempotency key replay and conflict detection"
```

---

### Task 9: Audit log

**Files:**
- Create: `backend/app/modules/audit/models.py`, `backend/app/modules/audit/service.py`, `backend/tests/audit/test_audit.py`

**Interfaces:**
- Produces: `AuditEntry` model, and `record(session, *, event, source, severity="info", message, actor=None, postamat_id=None, cell_id=None, details=None) -> AuditEntry`.

Sources match the mockups: `system`, `api`, or an admin login. Nothing ever updates or deletes an entry.

- [ ] **Step 1: Write `backend/app/modules/audit/models.py`**

```python
import uuid
from enum import StrEnum

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Source(StrEnum):
    SYSTEM = "system"
    API = "api"
    ADMIN = "admin"


class AuditEntry(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "audit_entries"

    event: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[Source] = mapped_column(String(16), index=True)
    severity: Mapped[Severity] = mapped_column(String(16), index=True, default=Severity.INFO)
    message: Mapped[str] = mapped_column(String(500))
    actor: Mapped[str | None] = mapped_column(String(64), index=True)
    postamat_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    cell_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    details: Mapped[dict | None] = mapped_column(JSON)
```

- [ ] **Step 2: Write `backend/app/modules/audit/service.py`**

```python
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditEntry, Severity, Source


async def record(
    session: AsyncSession,
    *,
    event: str,
    source: Source,
    message: str,
    severity: Severity = Severity.INFO,
    actor: str | None = None,
    postamat_id: uuid.UUID | None = None,
    cell_id: uuid.UUID | None = None,
    details: dict | None = None,
) -> AuditEntry:
    entry = AuditEntry(
        event=event,
        source=source,
        severity=severity,
        message=message,
        actor=actor,
        postamat_id=postamat_id,
        cell_id=cell_id,
        details=details,
    )
    session.add(entry)
    await session.flush()
    return entry
```

- [ ] **Step 3: Write the test**

```python
from sqlalchemy import select

from app.modules.audit.models import AuditEntry, Severity, Source
from app.modules.audit.service import record


async def test_record_persists_an_entry(session):
    await record(session, event="cell.remote_open", source=Source.ADMIN,
                 message="Manual open of cell 12", actor="admin_ivanov",
                 severity=Severity.WARNING, details={"reason": "stuck door"})
    await session.commit()

    entry = await session.scalar(select(AuditEntry))
    assert entry.event == "cell.remote_open"
    assert entry.actor == "admin_ivanov"
    assert entry.details == {"reason": "stuck door"}
```

- [ ] **Step 4: Run the test and confirm it passes**

```bash
docker compose run --rm api pytest tests/audit -v
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: append-only audit log"
```

---

### Task 10: Identity models

**Files:**
- Create: `backend/app/modules/identity/models.py`, `backend/tests/identity/test_models.py`

**Interfaces:**
- Produces: `Client`, `AdminUser`, `Role`, `RefreshToken`.

Roles carry a permission list rather than being a fixed enum — the contract exposes `GET /admin/roles` and returns `permissions[]` on `GET /admin/auth/me`.

- [ ] **Step 1: Write `backend/app/modules/identity/models.py`**

```python
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class Client(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "clients"

    phone: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(200))
    language: Mapped[str] = mapped_column(String(2), default="tk")
    default_city_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(500))


class Role(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "roles"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    permissions: Mapped[list] = mapped_column(JSON, default=list)


class AdminUser(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "admin_users"

    login: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)

    role: Mapped[Role] = relationship(lazy="joined")


class RefreshToken(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "refresh_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    subject_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[uuid.UUID] = mapped_column(index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 2: Write the test**

```python
from sqlalchemy import select

from app.modules.identity.models import Client


async def test_client_phone_is_unique(session):
    session.add(Client(phone="+99362123456"))
    await session.commit()

    stored = await session.scalar(select(Client))
    assert stored.language == "tk"
    assert stored.is_blocked is False
```

- [ ] **Step 3: Run the test and confirm it passes**

- [ ] **Step 4: Generate the first migration**

```bash
docker compose run --rm api alembic revision --autogenerate -m "identity, audit, idempotency"
docker compose run --rm api alembic upgrade head
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: identity models and first migration"
```

---

### Task 11: Client authentication by OTP

**Files:**
- Create: `backend/app/core/security.py`, `backend/app/modules/identity/schemas.py`, `backend/app/modules/identity/service.py`, `backend/app/api/mobile/auth.py`, `backend/tests/identity/test_client_auth.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces: `hash_secret(value) -> str`, `verify_secret(value, hashed) -> bool`, `create_access_token(subject_type, subject_id, extra) -> str`, `decode_token(token) -> dict`, `require_client` dependency returning a `Client`.
- Endpoints: `POST /api/v1/auth/otp/request`, `POST /api/v1/auth/otp/verify`, `POST /api/v1/auth/refresh`, `POST /api/v1/auth/logout`.

The OTP is 6 digits, lives in Redis under `otp:{phone}` with a TTL from settings, and carries an attempt counter. During development the code is returned in the response only when `settings.expose_otp` is true; wire that flag to `False` by default and set it in compose.

- [ ] **Step 1: Write the failing test**

```python
from app.modules.identity.service import peek_otp


async def test_otp_request_then_verify_issues_tokens(client):
    requested = await client.post("/api/v1/auth/otp/request",
                                  json={"phone": "+99362123456"})
    assert requested.status_code == 200
    assert requested.json()["code_length"] == 6

    code = await peek_otp("+99362123456")
    verified = await client.post("/api/v1/auth/otp/verify",
                                 json={"phone": "+99362123456", "code": code})
    assert verified.status_code == 200
    body = verified.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["is_new_client"] is True


async def test_wrong_code_is_rejected(client):
    await client.post("/api/v1/auth/otp/request", json={"phone": "+99362123456"})
    response = await client.post("/api/v1/auth/otp/verify",
                                 json={"phone": "+99362123456", "code": "000000"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OTP_INVALID"
    assert response.json()["error"]["details"]["attempts_left"] == 4


async def test_malformed_phone_is_rejected(client):
    response = await client.post("/api/v1/auth/otp/request", json={"phone": "+7999123456"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
```

Remove the `xfail` marker from `tests/core/test_errors.py::test_validation_error_renders_envelope` in this task.

- [ ] **Step 2: Run it and confirm it fails**

- [ ] **Step 3: Write `backend/app/core/security.py`**

```python
import hashlib
import hmac
import uuid
from datetime import timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import get_settings
from app.core.db import utcnow

_hasher = PasswordHasher()


def hash_secret(value: str) -> str:
    return _hasher.hash(value)


def verify_secret(value: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, value)
    except VerifyMismatchError:
        return False


def hash_pin(pin: str) -> str:
    settings = get_settings()
    return hmac.new(settings.pin_pepper.encode(), pin.encode(), hashlib.sha256).hexdigest()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_access_token(subject_type: str, subject_id: uuid.UUID,
                        extra: dict | None = None) -> str:
    settings = get_settings()
    payload = {
        "sub": str(subject_id),
        "typ": subject_type,
        "exp": utcnow() + timedelta(minutes=settings.access_token_ttl_minutes),
        "iat": utcnow(),
        **(extra or {}),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
```

`hash_pin` is defined here now because the pepper lives in settings; Plan 2 consumes it for access codes.

- [ ] **Step 4: Write `backend/app/modules/identity/schemas.py`**

```python
from pydantic import BaseModel

from app.core.types import PhoneNumber


class OtpRequest(BaseModel):
    phone: PhoneNumber


class OtpRequestResult(BaseModel):
    code_length: int
    expires_in_seconds: int


class OtpVerify(BaseModel):
    phone: PhoneNumber
    code: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    is_new_client: bool = False


class RefreshRequest(BaseModel):
    refresh_token: str
```

- [ ] **Step 5: Write `backend/app/modules/identity/service.py`**

```python
import secrets
import uuid
from datetime import timedelta

import redis.asyncio as redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import utcnow
from app.core.errors import AppError, ErrorCode
from app.core.security import create_access_token, hash_token
from app.modules.identity.models import Client, RefreshToken

_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


def _otp_key(phone: str) -> str:
    return f"otp:{phone}"


async def issue_otp(phone: str) -> int:
    settings = get_settings()
    code = "".join(secrets.choice("0123456789") for _ in range(settings.otp_length))
    client = get_redis()
    await client.hset(_otp_key(phone), mapping={"code": code, "attempts": "0"})
    await client.expire(_otp_key(phone), settings.otp_ttl_seconds)
    return settings.otp_ttl_seconds


async def peek_otp(phone: str) -> str | None:
    return await get_redis().hget(_otp_key(phone), "code")


async def verify_otp(phone: str, code: str) -> None:
    settings = get_settings()
    client = get_redis()
    stored = await client.hgetall(_otp_key(phone))
    if not stored:
        raise AppError(ErrorCode.OTP_EXPIRED, "No active code for this number.", 400)

    attempts = int(stored["attempts"]) + 1
    if attempts >= settings.otp_max_attempts:
        await client.delete(_otp_key(phone))
        raise AppError(ErrorCode.OTP_TOO_MANY_ATTEMPTS, "Too many attempts.", 429)

    if not secrets.compare_digest(stored["code"], code):
        await client.hset(_otp_key(phone), "attempts", str(attempts))
        raise AppError(
            ErrorCode.OTP_INVALID, "Wrong code.", 400,
            details={"attempts_left": settings.otp_max_attempts - attempts},
        )

    await client.delete(_otp_key(phone))


async def get_or_create_client(session: AsyncSession, phone: str) -> tuple[Client, bool]:
    existing = await session.scalar(select(Client).where(Client.phone == phone))
    if existing:
        if existing.is_blocked:
            raise AppError(ErrorCode.CLIENT_BLOCKED, "Client is blocked.", 403)
        return existing, False

    created = Client(phone=phone)
    session.add(created)
    await session.flush()
    return created, True


async def issue_token_pair(
    session: AsyncSession, subject_type: str, subject_id: uuid.UUID, extra: dict | None = None
) -> tuple[str, str]:
    settings = get_settings()
    access = create_access_token(subject_type, subject_id, extra)
    refresh = secrets.token_urlsafe(48)
    session.add(
        RefreshToken(
            token_hash=hash_token(refresh),
            subject_type=subject_type,
            subject_id=subject_id,
            expires_at=utcnow() + timedelta(days=settings.refresh_token_ttl_days),
        )
    )
    await session.flush()
    return access, refresh
```

- [ ] **Step 6: Write `backend/app/api/mobile/auth.py`**

```python
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.modules.identity import service
from app.modules.identity.schemas import (
    OtpRequest,
    OtpRequestResult,
    OtpVerify,
    TokenPair,
)

router = APIRouter(tags=["auth"])


@router.post("/auth/otp/request", response_model=OtpRequestResult)
async def request_otp(payload: OtpRequest) -> OtpRequestResult:
    ttl = await service.issue_otp(payload.phone)
    return OtpRequestResult(code_length=6, expires_in_seconds=ttl)


@router.post("/auth/otp/verify", response_model=TokenPair)
async def verify_otp(
    payload: OtpVerify, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    await service.verify_otp(payload.phone, payload.code)
    client, is_new = await service.get_or_create_client(session, payload.phone)
    access, refresh = await service.issue_token_pair(session, "client", client.id)
    await session.commit()
    return TokenPair(access_token=access, refresh_token=refresh, is_new_client=is_new)
```

- [ ] **Step 7: Register the router in `create_app()`**

```python
from app.api.mobile import auth as mobile_auth

app.include_router(mobile_auth.router, prefix=API_PREFIX)
```

- [ ] **Step 8: Run the tests and confirm they pass**

```bash
docker compose run --rm api pytest tests/identity tests/core -v
```

- [ ] **Step 9: Commit**

```bash
git add -A && git commit -m "feat: client authentication by one-time code"
```

---

### Task 12: Admin authentication and permissions

**Files:**
- Create: `backend/app/api/admin/auth.py`, `backend/app/core/deps.py`, `backend/tests/identity/test_admin_auth.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces: `require_client` and `require_admin` dependencies, `require_permission(code)` dependency factory.
- Endpoints: `POST /api/v1/admin/auth/login`, `POST /api/v1/admin/auth/refresh`, `POST /api/v1/admin/auth/logout`, `GET /api/v1/admin/auth/me`, `GET /api/v1/admin/roles`.

`POST /admin/auth/2fa` is deliberately absent: the flow is not drawn yet and its request body depends on whether it is TOTP, SMS or email.

- [ ] **Step 1: Write the failing test**

```python
from app.core.security import hash_secret
from app.modules.identity.models import AdminUser, Role


async def seed_admin(session) -> None:
    role = Role(code="admin", name="Administrator",
                permissions=["postamats.write", "cells.open"])
    session.add(role)
    await session.flush()
    session.add(AdminUser(login="admin_ivanov", full_name="Иванов И.И.",
                          password_hash=hash_secret("secret123"), role_id=role.id))
    await session.commit()


async def test_login_returns_tokens_and_permissions(client, session):
    await seed_admin(session)
    response = await client.post("/api/v1/admin/auth/login",
                                 json={"login": "admin_ivanov", "password": "secret123"})
    assert response.status_code == 200
    token = response.json()["access_token"]

    me = await client.get("/api/v1/admin/auth/me",
                          headers={"Authorization": f"Bearer {token}"})
    assert me.json()["permissions"] == ["postamats.write", "cells.open"]


async def test_wrong_password_is_401(client, session):
    await seed_admin(session)
    response = await client.post("/api/v1/admin/auth/login",
                                 json={"login": "admin_ivanov", "password": "nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "CREDENTIALS_INVALID"


async def test_missing_permission_is_403(client, session):
    await seed_admin(session)
    login = await client.post("/api/v1/admin/auth/login",
                              json={"login": "admin_ivanov", "password": "secret123"})
    token = login.json()["access_token"]
    response = await client.get("/api/v1/admin/roles",
                                headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
```

- [ ] **Step 2: Run it and confirm it fails**

- [ ] **Step 3: Write `backend/app/core/deps.py`**

```python
import uuid

import jwt
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.errors import AppError, ErrorCode
from app.core.security import decode_token
from app.modules.identity.models import AdminUser, Client


def _bearer(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise AppError(ErrorCode.TOKEN_INVALID, "Missing bearer token.", 401)
    return header.removeprefix("Bearer ")


def _claims(request: Request) -> dict:
    try:
        return decode_token(_bearer(request))
    except jwt.ExpiredSignatureError:
        raise AppError(ErrorCode.TOKEN_EXPIRED, "Access token expired.", 401) from None
    except jwt.PyJWTError:
        raise AppError(ErrorCode.TOKEN_INVALID, "Access token invalid.", 401) from None


async def require_client(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Client:
    claims = _claims(request)
    if claims.get("typ") != "client":
        raise AppError(ErrorCode.TOKEN_INVALID, "Not a client token.", 401)
    client = await session.get(Client, uuid.UUID(claims["sub"]))
    if client is None:
        raise AppError(ErrorCode.TOKEN_INVALID, "Unknown client.", 401)
    if client.is_blocked:
        raise AppError(ErrorCode.CLIENT_BLOCKED, "Client is blocked.", 403)
    return client


async def require_admin(
    request: Request, session: AsyncSession = Depends(get_session)
) -> AdminUser:
    claims = _claims(request)
    if claims.get("typ") != "admin":
        raise AppError(ErrorCode.TOKEN_INVALID, "Not an admin token.", 401)
    admin = await session.get(AdminUser, uuid.UUID(claims["sub"]))
    if admin is None or not admin.is_active:
        raise AppError(ErrorCode.ADMIN_INACTIVE, "Account is inactive.", 403)
    return admin


def require_permission(code: str):
    async def dependency(admin: AdminUser = Depends(require_admin)) -> AdminUser:
        if code not in (admin.role.permissions or []):
            raise AppError(
                ErrorCode.PERMISSION_DENIED, f"Permission {code} is required.", 403,
                details={"required": code},
            )
        return admin

    return dependency
```

- [ ] **Step 4: Write `backend/app/api/admin/auth.py`**

```python
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_admin, require_permission
from app.core.errors import AppError, ErrorCode
from app.core.security import verify_secret
from app.modules.identity import service
from app.modules.identity.models import AdminUser, Role

router = APIRouter(prefix="/admin", tags=["admin-auth"])


class LoginRequest(BaseModel):
    login: str
    password: str


class AdminTokens(BaseModel):
    access_token: str
    refresh_token: str


class AdminProfile(BaseModel):
    login: str
    full_name: str
    role: str
    permissions: list[str]


class RoleOut(BaseModel):
    code: str
    name: str
    permissions: list[str]


@router.post("/auth/login", response_model=AdminTokens)
async def login(
    payload: LoginRequest, session: AsyncSession = Depends(get_session)
) -> AdminTokens:
    admin = await session.scalar(select(AdminUser).where(AdminUser.login == payload.login))
    if admin is None or not verify_secret(payload.password, admin.password_hash):
        raise AppError(ErrorCode.CREDENTIALS_INVALID, "Login or password is wrong.", 401)
    if not admin.is_active:
        raise AppError(ErrorCode.ADMIN_INACTIVE, "Account is inactive.", 403)

    access, refresh = await service.issue_token_pair(session, "admin", admin.id)
    await session.commit()
    return AdminTokens(access_token=access, refresh_token=refresh)


@router.get("/auth/me", response_model=AdminProfile)
async def me(admin: AdminUser = Depends(require_admin)) -> AdminProfile:
    return AdminProfile(
        login=admin.login,
        full_name=admin.full_name,
        role=admin.role.code,
        permissions=admin.role.permissions or [],
    )


@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("roles.read")),
) -> list[RoleOut]:
    rows = await session.scalars(select(Role).order_by(Role.code))
    return [RoleOut(code=r.code, name=r.name, permissions=r.permissions or []) for r in rows]
```

- [ ] **Step 5: Register the router and run the tests**

```bash
docker compose run --rm api pytest tests/identity -v
```

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: admin login with permission-based authorisation"
```

---

### Task 13: Cities and cell types

**Files:**
- Create: `backend/app/modules/catalog/models.py`, `backend/app/modules/catalog/schemas.py`, `backend/app/api/public/catalog.py`, `backend/tests/catalog/test_reference.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces: `City`, `CellType` models; `GET /api/v1/cities`, `GET /api/v1/cell-types` — both public.

Names are stored per language rather than translated at runtime: the three languages are product data, not UI strings.

- [ ] **Step 1: Write `backend/app/modules/catalog/models.py`**

```python
from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class City(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "cities"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name_tk: Mapped[str] = mapped_column(String(100))
    name_ru: Mapped[str] = mapped_column(String(100))
    name_en: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class CellType(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "cell_types"

    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name_tk: Mapped[str] = mapped_column(String(50))
    name_ru: Mapped[str] = mapped_column(String(50))
    name_en: Mapped[str] = mapped_column(String(50))
    width_cm: Mapped[int] = mapped_column(Integer)
    height_cm: Mapped[int] = mapped_column(Integer)
    depth_cm: Mapped[int] = mapped_column(Integer)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
```

- [ ] **Step 2: Write `backend/app/modules/catalog/schemas.py`**

```python
import uuid

from pydantic import BaseModel


class CityOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str


class CellTypeOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    width_cm: int
    height_cm: int
    depth_cm: int


def localized(row, language: str) -> str:
    return getattr(row, f"name_{language}", row.name_tk)
```

- [ ] **Step 3: Write `backend/app/api/public/catalog.py`**

```python
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_language
from app.core.db import get_session
from app.modules.catalog.models import CellType, City
from app.modules.catalog.schemas import CellTypeOut, CityOut, localized

router = APIRouter(tags=["catalog"])


@router.get("/cities", response_model=list[CityOut])
async def list_cities(
    request: Request, session: AsyncSession = Depends(get_session)
) -> list[CityOut]:
    language = get_language(request)
    rows = await session.scalars(
        select(City).where(City.is_active.is_(True)).order_by(City.code)
    )
    return [
        CityOut(id=row.id, code=row.code, name=localized(row, language)) for row in rows
    ]


@router.get("/cell-types", response_model=list[CellTypeOut])
async def list_cell_types(
    request: Request, session: AsyncSession = Depends(get_session)
) -> list[CellTypeOut]:
    language = get_language(request)
    rows = await session.scalars(
        select(CellType).where(CellType.is_blocked.is_(False)).order_by(CellType.width_cm)
    )
    return [
        CellTypeOut(
            id=row.id, code=row.code, name=localized(row, language),
            width_cm=row.width_cm, height_cm=row.height_cm, depth_cm=row.depth_cm,
        )
        for row in rows
    ]
```

- [ ] **Step 4: Write the test**

```python
from app.modules.catalog.models import CellType, City


async def test_cities_are_localized(client, session):
    session.add(City(code="ashgabat", name_tk="Aşgabat", name_ru="Ашхабад", name_en="Ashgabat"))
    await session.commit()

    default = await client.get("/api/v1/cities")
    assert default.json()[0]["name"] == "Aşgabat"

    russian = await client.get("/api/v1/cities", headers={"Accept-Language": "ru"})
    assert russian.json()[0]["name"] == "Ашхабад"


async def test_blocked_cell_types_are_hidden(client, session):
    session.add(CellType(code="small", name_tk="Kiçi", name_ru="Маленький", name_en="Small",
                         width_cm=20, height_cm=20, depth_cm=40))
    session.add(CellType(code="huge", name_tk="X", name_ru="X", name_en="X",
                         width_cm=90, height_cm=90, depth_cm=90, is_blocked=True))
    await session.commit()

    response = await client.get("/api/v1/cell-types")
    assert [item["code"] for item in response.json()] == ["small"]
```

- [ ] **Step 5: Run the tests, generate the migration, commit**

```bash
docker compose run --rm api pytest tests/catalog -v
docker compose run --rm api alembic revision --autogenerate -m "cities and cell types"
git add -A && git commit -m "feat: public city and cell type reference data"
```

---

### Task 14: Postamats and opening hours

**Files:**
- Modify: `backend/app/modules/catalog/models.py`, `backend/app/modules/catalog/schemas.py`
- Create: `backend/app/modules/catalog/service.py`, `backend/app/api/admin/postamats.py`, `backend/app/api/public/postamats.py`, `backend/tests/catalog/test_postamats.py`

**Interfaces:**
- Produces: `Postamat`, `PostamatSchedule`, `Device` models; `is_open_at(postamat, moment) -> bool`; `next_opening_after(postamat, moment) -> datetime`.
- Endpoints: `GET /api/v1/postamats`, `GET /api/v1/postamats/{postamat_id}` public; `GET POST PATCH /api/v1/admin/postamats`, `POST /api/v1/admin/postamats/{id}/block`, `/unblock`, `PUT /api/v1/admin/postamats/{id}/schedule`.

`Postamat.status` is the operator's decision (`active`, `maintenance`, `blocked`). Hardware presence lives on `Device` (`online`, `offline`, `degraded`) with `last_seen_at`. Plan 3 fills the device side; the table exists now so the monitor screen has something to read.

- [ ] **Step 1: Add the models**

```python
import uuid
from datetime import datetime, time
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship


class PostamatStatus(StrEnum):
    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    BLOCKED = "blocked"


class DeviceStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"


class Postamat(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "postamats"

    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    city_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cities.id"), index=True)
    address: Mapped[str] = mapped_column(String(500))
    latitude: Mapped[float | None]
    longitude: Mapped[float | None]
    status: Mapped[PostamatStatus] = mapped_column(String(16), default=PostamatStatus.ACTIVE)
    round_the_clock: Mapped[bool] = mapped_column(Boolean, default=False)

    schedule: Mapped[list["PostamatSchedule"]] = relationship(
        back_populates="postamat", lazy="selectin", cascade="all, delete-orphan"
    )


class PostamatSchedule(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "postamat_schedules"

    postamat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("postamats.id"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = Monday
    opens_at: Mapped[time] = mapped_column(Time)
    closes_at: Mapped[time] = mapped_column(Time)

    postamat: Mapped[Postamat] = relationship(back_populates="schedule")


class Device(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "devices"

    postamat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("postamats.id"), unique=True)
    status: Mapped[DeviceStatus] = mapped_column(String(16), default=DeviceStatus.OFFLINE)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_version: Mapped[str | None] = mapped_column(String(32))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    mac_address: Mapped[str | None] = mapped_column(String(17))
```

- [ ] **Step 2: Write the failing test for opening hours**

```python
from datetime import datetime, time, timezone

from app.modules.catalog.models import Postamat, PostamatSchedule
from app.modules.catalog.service import is_open_at, next_opening_after


def build_postamat() -> Postamat:
    postamat = Postamat(number="10042", name="ТП #4", address="ул. Ататюрк",
                        city_id=None, round_the_clock=False)
    postamat.schedule = [
        PostamatSchedule(weekday=day, opens_at=time(8, 0), closes_at=time(20, 0))
        for day in range(7)
    ]
    return postamat


def test_closed_outside_working_hours():
    postamat = build_postamat()
    assert is_open_at(postamat, datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)) is True
    assert is_open_at(postamat, datetime(2026, 8, 14, 21, 0, tzinfo=timezone.utc)) is False


def test_round_the_clock_is_always_open():
    postamat = build_postamat()
    postamat.round_the_clock = True
    assert is_open_at(postamat, datetime(2026, 8, 14, 3, 0, tzinfo=timezone.utc)) is True


def test_next_opening_skips_to_the_morning():
    postamat = build_postamat()
    moment = datetime(2026, 8, 14, 21, 0, tzinfo=timezone.utc)
    assert next_opening_after(postamat, moment) == datetime(
        2026, 8, 15, 8, 0, tzinfo=timezone.utc
    )
```

- [ ] **Step 3: Run it and confirm it fails**

- [ ] **Step 4: Write `backend/app/modules/catalog/service.py`**

```python
from datetime import datetime, timedelta

from app.modules.catalog.models import Postamat


def _slot_for(postamat: Postamat, weekday: int):
    for slot in postamat.schedule:
        if slot.weekday == weekday:
            return slot
    return None


def is_open_at(postamat: Postamat, moment: datetime) -> bool:
    if postamat.round_the_clock:
        return True
    slot = _slot_for(postamat, moment.weekday())
    if slot is None:
        return False
    return slot.opens_at <= moment.time() < slot.closes_at


def next_opening_after(postamat: Postamat, moment: datetime) -> datetime:
    if postamat.round_the_clock:
        return moment
    for offset in range(8):
        day = moment + timedelta(days=offset)
        slot = _slot_for(postamat, day.weekday())
        if slot is None:
            continue
        candidate = day.replace(
            hour=slot.opens_at.hour, minute=slot.opens_at.minute, second=0, microsecond=0
        )
        if candidate > moment:
            return candidate
    return moment
```

- [ ] **Step 5: Run the test and confirm it passes**

- [ ] **Step 6: Write the admin router `backend/app/api/admin/postamats.py`**

```python
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.core.pagination import PageMeta, PageParams, page_params, paginate_page
from app.modules.audit.models import Source
from app.modules.audit.service import record
from app.modules.catalog.models import Postamat, PostamatStatus
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/postamats", tags=["admin-postamats"])


class PostamatIn(BaseModel):
    number: str
    name: str
    city_id: uuid.UUID
    address: str
    latitude: float | None = None
    longitude: float | None = None
    round_the_clock: bool = False


class PostamatOut(BaseModel):
    id: uuid.UUID
    number: str
    name: str
    address: str
    status: PostamatStatus
    round_the_clock: bool


class PostamatPage(BaseModel):
    items: list[PostamatOut]
    pagination: PageMeta


class BlockRequest(BaseModel):
    reason: str


@router.get("", response_model=PostamatPage)
async def list_postamats(
    params: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("postamats.read")),
) -> PostamatPage:
    rows, meta = await paginate_page(session, select(Postamat).order_by(Postamat.number), params)
    return PostamatPage(items=[PostamatOut.model_validate(r, from_attributes=True) for r in rows],
                        pagination=meta)


@router.post("", response_model=PostamatOut, status_code=201)
async def create_postamat(
    payload: PostamatIn,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatOut:
    postamat = Postamat(**payload.model_dump())
    session.add(postamat)
    await session.flush()
    await record(session, event="postamat.created", source=Source.ADMIN,
                 message=f"Postamat {postamat.number} created", actor=admin.login,
                 postamat_id=postamat.id)
    await session.commit()
    return PostamatOut.model_validate(postamat, from_attributes=True)


@router.post("/{postamat_id}/block", response_model=PostamatOut)
async def block_postamat(
    postamat_id: uuid.UUID,
    payload: BlockRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatOut:
    postamat = await session.get(Postamat, postamat_id)
    if postamat is None:
        raise AppError(ErrorCode.NOT_FOUND, "Postamat not found.", 404)
    postamat.status = PostamatStatus.BLOCKED
    await record(session, event="postamat.blocked", source=Source.ADMIN,
                 message=payload.reason, actor=admin.login, postamat_id=postamat.id)
    await session.commit()
    return PostamatOut.model_validate(postamat, from_attributes=True)
```

- [ ] **Step 7: Write the API test**

```python
async def test_create_and_block_postamat(client, session, admin_token, city):
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = await client.post("/api/v1/admin/postamats", headers=headers, json={
        "number": "10042", "name": "ТП #4", "city_id": str(city.id),
        "address": "ул. Ататюрк, 31", "round_the_clock": False,
    })
    assert created.status_code == 201
    postamat_id = created.json()["id"]

    blocked = await client.post(f"/api/v1/admin/postamats/{postamat_id}/block",
                                headers=headers, json={"reason": "vandalised"})
    assert blocked.json()["status"] == "blocked"
```

Add `admin_token` and `city` fixtures to `backend/tests/conftest.py`, seeding a role with
`["postamats.read", "postamats.write", "cells.write", "tariffs.write", "roles.read"]`.

- [ ] **Step 8: Run the tests, generate the migration, commit**

```bash
docker compose run --rm api pytest tests/catalog -v
docker compose run --rm api alembic revision --autogenerate -m "postamats, schedules, devices"
git add -A && git commit -m "feat: postamats with opening hours and operator status"
```

---

### Task 15: Cells

**Files:**
- Modify: `backend/app/modules/catalog/models.py`
- Create: `backend/app/api/admin/cells.py`, `backend/tests/catalog/test_cells.py`

**Interfaces:**
- Produces: `Cell` model with independent `number`, `row`, `col`, `board`, `output`; `GET POST PATCH /api/v1/admin/cells`, `POST /api/v1/admin/cells/{cell_id}/block`, `/unblock`.

`number` is the label on the door and is unique within its postamat. `board` and `output` are the lock-board address. On the pilot cabinet their values happen to line up; nothing in the code may rely on that.

- [ ] **Step 1: Add the model**

```python
class Cell(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "cells"
    __table_args__ = (UniqueConstraint("postamat_id", "number", name="uq_cell_number"),)

    postamat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("postamats.id"), index=True)
    cell_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cell_types.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    row: Mapped[int | None] = mapped_column(Integer)
    col: Mapped[int | None] = mapped_column(Integer)
    board: Mapped[int] = mapped_column(Integer)
    output: Mapped[int] = mapped_column(Integer)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    is_maintenance: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(500))
```

Import `UniqueConstraint` at the top of the module.

- [ ] **Step 2: Write the failing test**

```python
async def test_duplicate_cell_number_within_postamat_is_rejected(client, admin_token, postamat, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"}
    body = {"postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
            "cells": [{"number": 1, "board": 1, "output": 1}]}

    first = await client.post("/api/v1/admin/cells", headers=headers, json=body)
    assert first.status_code == 201

    duplicate = await client.post("/api/v1/admin/cells", headers=headers, json=body)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "CELL_NUMBER_TAKEN"


async def test_bulk_create_accepts_a_full_cabinet(client, admin_token, postamat, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"}
    cells = [{"number": n, "board": 1 if n <= 21 else 2,
              "output": n if n <= 21 else n - 21} for n in range(1, 44)]
    response = await client.post("/api/v1/admin/cells", headers=headers, json={
        "postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id), "cells": cells,
    })
    assert response.status_code == 201
    assert len(response.json()["items"]) == 43
```

- [ ] **Step 3: Run it and confirm it fails**

- [ ] **Step 4: Write `backend/app/api/admin/cells.py`**

```python
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.modules.catalog.models import Cell
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/cells", tags=["admin-cells"])


class CellSpec(BaseModel):
    number: int
    board: int
    output: int
    row: int | None = None
    col: int | None = None


class BulkCreate(BaseModel):
    postamat_id: uuid.UUID
    cell_type_id: uuid.UUID
    cells: list[CellSpec]


class CellOut(BaseModel):
    id: uuid.UUID
    number: int
    board: int
    output: int
    is_blocked: bool
    is_maintenance: bool


class CellList(BaseModel):
    items: list[CellOut]


@router.post("", response_model=CellList, status_code=201)
async def create_cells(
    payload: BulkCreate,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("cells.write")),
) -> CellList:
    rows = [
        Cell(postamat_id=payload.postamat_id, cell_type_id=payload.cell_type_id,
             **spec.model_dump())
        for spec in payload.cells
    ]
    session.add_all(rows)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError(ErrorCode.CELL_NUMBER_TAKEN,
                       "A cell with this number already exists in the postamat.", 409) from None

    return CellList(items=[CellOut.model_validate(r, from_attributes=True) for r in rows])


@router.get("", response_model=CellList)
async def list_cells(
    postamat_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("cells.read")),
) -> CellList:
    rows = await session.scalars(
        select(Cell).where(Cell.postamat_id == postamat_id).order_by(Cell.number)
    )
    return CellList(items=[CellOut.model_validate(r, from_attributes=True) for r in rows])
```

- [ ] **Step 5: Run the tests, generate the migration, commit**

```bash
docker compose run --rm api pytest tests/catalog/test_cells.py -v
docker compose run --rm api alembic revision --autogenerate -m "cells"
git add -A && git commit -m "feat: cells with independent door and board addressing"
```

---

### Task 16: Tariff matrix

**Files:**
- Modify: `backend/app/modules/catalog/models.py`
- Create: `backend/app/api/admin/tariffs.py`, `backend/tests/catalog/test_tariffs.py`

**Interfaces:**
- Produces: `Tariff` model keyed on `(city_id, cell_type_id, duration_hours)`; `GET /api/v1/admin/tariffs`, `PUT /api/v1/admin/tariffs`.

`PUT` replaces the whole matrix in one transaction — half-applied price changes are worse than none. Validation: a city that prices a cell type must price all three durations, otherwise `TARIFF_INCOMPLETE`.

- [ ] **Step 1: Add the model**

```python
class Tariff(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "tariffs"
    __table_args__ = (
        UniqueConstraint("city_id", "cell_type_id", "duration_hours", name="uq_tariff"),
    )

    city_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cities.id"), index=True)
    cell_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cell_types.id"), index=True)
    duration_hours: Mapped[int] = mapped_column(Integer)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="TMT")
```

- [ ] **Step 2: Write the failing test**

```python
async def test_incomplete_matrix_is_rejected(client, admin_token, city, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.put("/api/v1/admin/tariffs", headers=headers, json={
        "entries": [
            {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
             "duration_hours": 12, "amount_minor": 1200},
        ]
    })
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "TARIFF_INCOMPLETE"
    assert body["details"]["missing"] == [24, 48]


async def test_complete_matrix_replaces_previous(client, admin_token, city, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"}
    entries = [
        {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
         "duration_hours": hours, "amount_minor": amount}
        for hours, amount in ((12, 1200), (24, 1800), (48, 2600))
    ]
    first = await client.put("/api/v1/admin/tariffs", headers=headers, json={"entries": entries})
    assert first.status_code == 200

    entries[0]["amount_minor"] = 1400
    second = await client.put("/api/v1/admin/tariffs", headers=headers, json={"entries": entries})
    assert second.status_code == 200

    listed = await client.get("/api/v1/admin/tariffs", headers=headers)
    prices = {item["duration_hours"]: item["amount_minor"] for item in listed.json()["items"]}
    assert prices == {12: 1400, 24: 1800, 48: 2600}
```

- [ ] **Step 3: Run it and confirm it fails**

- [ ] **Step 4: Write `backend/app/api/admin/tariffs.py`**

```python
import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.modules.catalog.models import Tariff
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/tariffs", tags=["admin-tariffs"])


class TariffEntry(BaseModel):
    city_id: uuid.UUID
    cell_type_id: uuid.UUID
    duration_hours: int
    amount_minor: int
    currency: str = "TMT"


class TariffMatrix(BaseModel):
    entries: list[TariffEntry]


class TariffList(BaseModel):
    items: list[TariffEntry]


@router.get("", response_model=TariffList)
async def list_tariffs(
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("tariffs.read")),
) -> TariffList:
    rows = await session.scalars(select(Tariff))
    return TariffList(items=[TariffEntry.model_validate(r, from_attributes=True) for r in rows])


@router.put("", response_model=TariffList)
async def replace_tariffs(
    payload: TariffMatrix,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("tariffs.write")),
) -> TariffList:
    required = set(get_settings().rental_durations)
    seen: dict[tuple[uuid.UUID, uuid.UUID], set[int]] = defaultdict(set)
    for entry in payload.entries:
        seen[(entry.city_id, entry.cell_type_id)].add(entry.duration_hours)

    for (city_id, cell_type_id), durations in seen.items():
        missing = sorted(required - durations)
        if missing:
            raise AppError(
                ErrorCode.TARIFF_INCOMPLETE,
                "Every priced cell type must price all durations.", 422,
                details={"city_id": str(city_id), "cell_type_id": str(cell_type_id),
                         "missing": missing},
            )

    await session.execute(delete(Tariff))
    session.add_all([Tariff(**entry.model_dump()) for entry in payload.entries])
    await session.commit()
    return TariffList(items=payload.entries)
```

- [ ] **Step 5: Run the tests, generate the migration, commit**

```bash
docker compose run --rm api pytest tests/catalog/test_tariffs.py -v
docker compose run --rm api alembic revision --autogenerate -m "tariffs"
git add -A && git commit -m "feat: tariff matrix replaced atomically with completeness check"
```

---

### Task 17: Rate limiting on the public surface

**Files:**
- Create: `backend/app/core/ratelimit.py`, `backend/tests/core/test_ratelimit.py`
- Modify: `backend/app/api/public/catalog.py`, `backend/app/api/mobile/auth.py`

**Interfaces:**
- Produces: `rate_limit(bucket, limit, window_seconds)` dependency factory using Redis, raising `RATE_LIMITED` with `retry_after_seconds` in `details`.

The public reads are the only unauthenticated surface, and OTP requests cost money per message.

- [ ] **Step 1: Write the failing test**

```python
async def test_repeated_otp_requests_are_limited(client):
    payload = {"phone": "+99362123456"}
    for _ in range(3):
        assert (await client.post("/api/v1/auth/otp/request", json=payload)).status_code == 200

    blocked = await client.post("/api/v1/auth/otp/request", json=payload)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"
    assert blocked.json()["error"]["details"]["retry_after_seconds"] > 0
```

- [ ] **Step 2: Run it and confirm it fails**

- [ ] **Step 3: Write `backend/app/core/ratelimit.py`**

```python
from fastapi import Request

from app.core.errors import AppError, ErrorCode
from app.modules.identity.service import get_redis


def rate_limit(bucket: str, limit: int, window_seconds: int):
    async def dependency(request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        key = f"rl:{bucket}:{client_ip}"
        redis_client = get_redis()

        current = await redis_client.incr(key)
        if current == 1:
            await redis_client.expire(key, window_seconds)

        if current > limit:
            ttl = await redis_client.ttl(key)
            raise AppError(
                ErrorCode.RATE_LIMITED, "Too many requests.", 429,
                details={"retry_after_seconds": max(ttl, 1)},
            )

    return dependency
```

- [ ] **Step 4: Apply it to the OTP route and the public catalog routes**

```python
from app.core.ratelimit import rate_limit

@router.post("/auth/otp/request", response_model=OtpRequestResult,
             dependencies=[Depends(rate_limit("otp", limit=3, window_seconds=600))])
```

Use `rate_limit("public", limit=120, window_seconds=60)` on `GET /cities` and `GET /cell-types`.

- [ ] **Step 5: Run the whole suite**

```bash
docker compose run --rm api pytest -v
```

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: IP rate limiting on the public and OTP surface"
```

---

## Definition of done for this plan

- `docker compose up` brings up a service answering on `http://localhost:8000/api/v1/health`.
- `docker compose run --rm api pytest` is green.
- `docker compose run --rm api alembic upgrade head` builds the schema from scratch.
- A client can obtain tokens by phone; an admin can log in and is refused by permission.
- Cities, cell types, postamats with opening hours, cells and the tariff matrix are all
  readable and writable through the documented paths.

## Deliberately not in this plan

- `GET /postamats/{id}/availability` — needs bookings (Plan 2).
- `POST /admin/auth/2fa` — the flow is not drawn, and its body depends on TOTP versus SMS.
- Admin CRUD for cities — cities are seeded by migration until a second region appears.
- Photo upload for postamats — the only `DELETE` in the system lands with it, in Plan 2.
- Everything on the device and kiosk channels — Plan 3, when the RS-485 hardware arrives.
