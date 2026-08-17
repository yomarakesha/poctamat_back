# Postamat booking core (Plan 2a) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A client can see how many cells of each size are free at a postamat, book one for a
fixed duration, receive three access codes, watch the booking's timeline, and cancel it — with the
cell allocated atomically so the same cell is never sold twice.

**Architecture:** A new `booking` module (`models.py`, `schemas.py`, `codes.py`, `service.py`) on
top of the catalog built in Plan 1. Allocation runs inside a single write transaction opened with
`BEGIN IMMEDIATE`, so SQLite's global write lock — not row locks — is what serialises two
concurrent bookings; a partial unique index on "one active booking per cell" backs it at the
database level in case application code is ever wrong. Money is not in this plan: `mark_paid` is a
service function whose caller (the bank webhook) arrives in Plan 2b, and it is exercised here by
tests and by the lifecycle that depends on it.

**Tech Stack:** Python 3.14.4, FastAPI 0.141+, SQLAlchemy 2.0 async, aiosqlite, Alembic, pytest +
pytest-asyncio, argon2/HMAC via the existing `app/core/security.py`.

**Spec:** `docs/superpowers/specs/2026-08-14-postamat-backend-design.md` (sections 3.4, 4.1, 4.2,
5.1, 6, 7, 8), as amended by the rulings below.

**Predecessor:** `docs/superpowers/plans/2026-08-14-postamat-backend-foundation.md` (Plan 1,
complete: tasks 1-17 landed, suite 75 passed).

## Global Constraints

- **Database is SQLite**, `sqlite+aiosqlite`, for both dev and tests. Models stay portable: `JSON`
  never `JSONB`, no server defaults beyond `func.now()`, no Postgres-only SQL outside the one
  seam named in Ruling P1.
- **Money** travels as `amount_minor: int` plus `currency: str` — never a float, never a single
  formatted string.
- **Timestamps** on the wire are UTC with a trailing `Z`, produced by
  `app.core.types.utc_isoformat`. SQLite returns naive datetimes for `DateTime(timezone=True)`
  columns; normalise with the module-local `_as_utc` helper before comparing against `utcnow()`,
  exactly as `app/modules/identity/service.py` already does.
- **PINs are 5 digits** (`settings.pin_length`) and are stored only as `hash_pin(...)` digests.
  Plaintext is returned exactly once, in the response that issues it, and never logged.
- **Modules do not import each other's models.** `booking` must not `import ... catalog.models`;
  it reaches cells through functions on `app.modules.catalog.service`. Routers call services and
  never build SQL themselves.
- **Product constants live in `Settings`**, not inline: `hold_minutes` (10), `pin_length` (5),
  `rental_durations` (12, 24, 48). The spec's SQL sketch says a 15-minute hold; `Settings` says 10
  and `Settings` wins — it is the value Plan 1 pinned and the one the code already carries.
- **Every task ends green:** `./.venv/Scripts/python.exe -m pytest -q` from `backend/`, plus the
  task's own file run verbosely.
- **Commits stage explicit paths.** Never `git add -A` — the repository root holds an unrelated
  untracked `postbox-contract/` directory.

## Rulings taken before execution

**Ruling P1 — SQLite stays; `FOR UPDATE SKIP LOCKED` is not used.** Plan 1's ruling R7 said Plan 2
could not start without Postgres. That was wrong about correctness and right about throughput.
SQLite permits exactly one write transaction at a time, so two concurrent allocations cannot both
observe the same cell as free — the second one waits. `SKIP LOCKED` exists to let concurrent
allocations take *different* rows instead of queueing; it buys parallelism, not safety. The costs
accepted here: bookings serialise, and background workers must tolerate waiting (hence WAL and
`busy_timeout` in Task 1). The dialect-dependent part is confined to
`catalog.service.free_cell_ids(...)`, which is the one function Postgres would later replace with
a `SELECT ... FOR UPDATE SKIP LOCKED`.

**Ruling P2 — the partial unique index is the real guarantee.** `uq_active_booking_per_cell` is a
unique index on `cell_id` restricted to the statuses that hold a cell. SQLite supports partial
indexes, so this survives the choice above. If application logic ever regresses, the database
refuses the second sale with an `IntegrityError` instead of double-selling.

**Ruling P3 — no cursor pagination yet.** Plan 1's ruling R3 deferred cursor helpers to "Plan 2,
beside the audit log and notification feed". Neither is in Plan 2a; the client's booking list is
short and bounded. Page pagination (`paginate_page`) is used. Cursors arrive with their first real
consumer.

**Ruling P4 — `expires_at` starts at deposit, not at payment.** Storage is what the customer buys,
and storage begins when the parcel is in the cell. Until then `hold_expires_at` governs. The
opening-hours push from spec §4.2 is applied at that moment by `storage_expiry(...)`, whose
consumer is `mark_deposited(...)` in this plan.

**Ruling P5 — the deposit and pickup transitions land here, without their HTTP surface.** Plan 3
owns `/device/codes/verify` and the kiosk. But `mark_deposited` and `mark_collected` are the
lifecycle, which the spec's §8 names as the place a bug is most expensive, and they are what free
the cell. They ship here as tested service functions with no route.

---

## File structure

| File | Responsibility |
|---|---|
| `backend/app/core/db.py` (modify) | SQLite pragmas on connect; `write_transaction()` opening `BEGIN IMMEDIATE` |
| `backend/app/core/errors.py` (modify) | Two new `ErrorCode` members |
| `backend/app/modules/booking/models.py` (create) | `Booking`, `BookingEvent`, `AccessCode`, their enums, the partial unique index |
| `backend/app/modules/booking/schemas.py` (create) | Request and response models for the mobile and admin surfaces |
| `backend/app/modules/booking/codes.py` (create) | Issuing and verifying the three PINs |
| `backend/app/modules/booking/service.py` (create) | Allocation, lifecycle transitions, timeline writes |
| `backend/app/modules/catalog/service.py` (modify) | `free_cell_ids(...)`, `cell_ids_by_type(...)`, `storage_expiry(...)` |
| `backend/app/api/public/postamats.py` (modify) | `GET /postamats/{id}/availability` |
| `backend/app/api/mobile/bookings.py` (create) | Client booking routes |
| `backend/app/api/admin/bookings.py` (create) | Admin booking routes |
| `backend/app/workers/holds.py` (create) | Releasing bookings whose hold expired |
| `backend/tests/booking/*` (create) | Unit and API tests, including the concurrency test |

---

### Task 1: SQLite under concurrent writers

**Files:**
- Modify: `backend/app/core/db.py`
- Test: `backend/tests/core/test_db_concurrency.py`

**Interfaces:**
- Produces: `write_transaction(session)` — an async context manager that opens the session's
  transaction with `BEGIN IMMEDIATE` and commits on exit, rolling back on exception.
- Produces: connect-time pragmas `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`.

Without this, a reader and the hold-release worker collide and SQLAlchemy raises
`database is locked` on a transaction that has already done work. `BEGIN IMMEDIATE` takes the
write lock at the start of the transaction rather than upgrading to it halfway through, which is
what turns a lock conflict into a short wait instead of a mid-transaction failure.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/core/test_db_concurrency.py
import asyncio

import pytest
from sqlalchemy import text

from app.core.db import write_transaction


async def test_pragmas_are_applied(test_engine):
    async with test_engine.connect() as conn:
        assert (await conn.scalar(text("PRAGMA foreign_keys"))) == 1
        assert (await conn.scalar(text("PRAGMA busy_timeout"))) == 5000


async def test_two_writers_serialise_instead_of_failing(test_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    order: list[str] = []

    async def writer(name: str, hold: float) -> None:
        async with maker() as session:
            async with write_transaction(session):
                order.append(f"{name}:start")
                await session.execute(text("CREATE TABLE IF NOT EXISTS probe (n INTEGER)"))
                await session.execute(text("INSERT INTO probe (n) VALUES (1)"))
                await asyncio.sleep(hold)
                order.append(f"{name}:end")

    await asyncio.gather(writer("a", 0.05), writer("b", 0.0))

    # Whichever went first finished before the other started: the write lock is
    # held for the whole transaction, so the two never interleave.
    assert order[1].endswith(":end")
    assert order[0].split(":")[0] == order[1].split(":")[0]
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/core/test_db_concurrency.py -v`
Expected: FAIL with `ImportError: cannot import name 'write_transaction'`.

- [ ] **Step 3: Add the pragmas and the helper**

In `backend/app/core/db.py`, after the existing `engine = create_async_engine(...)` line:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession


def _apply_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    # WAL lets readers run while a writer holds the lock; busy_timeout turns a
    # lock conflict into a short wait instead of an immediate failure; foreign
    # keys are off by default in SQLite and would silently accept orphan rows.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def install_sqlite_pragmas(target) -> None:
    """Attach the pragma listener to an engine. Safe to call more than once."""
    sync_engine = getattr(target, "sync_engine", target)
    if sync_engine.dialect.name != "sqlite":
        return
    if not event.contains(sync_engine, "connect", _apply_sqlite_pragmas):
        event.listen(sync_engine, "connect", _apply_sqlite_pragmas)


install_sqlite_pragmas(engine)


@asynccontextmanager
async def write_transaction(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Run a block inside a transaction that takes the write lock up front.

    SQLite allows one writer at a time. A transaction that starts deferred and
    only later tries to write can find the lock taken after it has already read
    — and then it must fail, because its snapshot may be stale. BEGIN IMMEDIATE
    asks for the lock first, so a conflict costs a wait rather than a retry.
    """
    await session.execute(text("BEGIN IMMEDIATE"))
    try:
        yield session
    except BaseException:
        await session.rollback()
        raise
    else:
        await session.commit()
```

Note for the implementer: SQLAlchemy's aiosqlite dialect emits its own `BEGIN` when a session
first touches the connection, so `write_transaction` must be entered before any query on that
session. Every caller in this plan does exactly that.

- [ ] **Step 4: Apply the same listener in the test harness**

In `backend/tests/conftest.py`, inside the `test_engine` fixture, after the engine is created:

```python
    from app.core.db import install_sqlite_pragmas

    install_sqlite_pragmas(engine)
    return engine
```

(The fixture currently returns the engine directly; bind it to a local `engine` variable first.)

- [ ] **Step 5: Run the test and confirm it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/core/test_db_concurrency.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 6: Run the whole suite and commit**

```bash
./.venv/Scripts/python.exe -m pytest -q
git add app/core/db.py tests/conftest.py tests/core/test_db_concurrency.py
git commit -m "feat: take the SQLite write lock up front for allocating transactions"
```

---

### Task 2: Booking, timeline and access-code tables

**Files:**
- Create: `backend/app/modules/booking/__init__.py`, `backend/app/modules/booking/models.py`
- Modify: `backend/alembic/env.py`
- Create: `backend/tests/booking/__init__.py`, `backend/tests/booking/test_models.py`

**Interfaces:**
- Produces: `BookingStatus`, `CELL_HELD_STATUSES`, `Depositor`, `CodePurpose`, `Booking`,
  `BookingEvent`, `AccessCode`, and the index `uq_active_booking_per_cell`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/booking/test_models.py
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.booking.models import Booking, BookingStatus, CELL_HELD_STATUSES


def _booking(cell_id: uuid.UUID, status: BookingStatus) -> Booking:
    return Booking(
        client_id=uuid.uuid4(), postamat_id=uuid.uuid4(), cell_id=cell_id,
        cell_type_id=uuid.uuid4(), duration_hours=24, amount_minor=1800,
        currency="TMT", status=status, recipient_phone="+99362123456",
        recipient_name="Получатель",
    )


def test_held_statuses_are_the_ones_that_occupy_a_cell():
    assert BookingStatus.PENDING_PAYMENT in CELL_HELD_STATUSES
    assert BookingStatus.AWAITING_PICKUP in CELL_HELD_STATUSES
    assert BookingStatus.CANCELLED not in CELL_HELD_STATUSES
    assert BookingStatus.COMPLETED not in CELL_HELD_STATUSES


async def test_one_active_booking_per_cell_is_enforced_by_the_database(session):
    cell_id = uuid.uuid4()
    session.add(_booking(cell_id, BookingStatus.PAID))
    await session.flush()

    session.add(_booking(cell_id, BookingStatus.PENDING_PAYMENT))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_a_finished_booking_frees_the_cell_for_a_new_one(session):
    cell_id = uuid.uuid4()
    session.add(_booking(cell_id, BookingStatus.COMPLETED))
    await session.flush()

    session.add(_booking(cell_id, BookingStatus.PENDING_PAYMENT))
    await session.flush()  # must not raise
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.modules.booking'`.

- [ ] **Step 3: Write `backend/app/modules/booking/models.py`**

```python
import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class BookingStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    AWAITING_DEPOSIT = "awaiting_deposit"
    AWAITING_PICKUP = "awaiting_pickup"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


# The statuses during which the cell belongs to this booking and no other. The
# partial unique index below is built from exactly this set, so adding a status
# here without regenerating the index would let a cell be sold twice.
CELL_HELD_STATUSES = frozenset({
    BookingStatus.PENDING_PAYMENT,
    BookingStatus.PAID,
    BookingStatus.AWAITING_DEPOSIT,
    BookingStatus.AWAITING_PICKUP,
})

_HELD_SQL = ", ".join(f"'{status.value}'" for status in sorted(CELL_HELD_STATUSES))


class Depositor(StrEnum):
    OWNER = "owner"
    COURIER = "courier"


class CodePurpose(StrEnum):
    DEPOSIT = "deposit"
    COURIER = "courier"
    PICKUP = "pickup"


class Booking(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "bookings"
    __table_args__ = (
        Index(
            "uq_active_booking_per_cell", "cell_id", unique=True,
            sqlite_where=text(f"status IN ({_HELD_SQL})"),
            postgresql_where=text(f"status IN ({_HELD_SQL})"),
        ),
        Index("ix_bookings_client_created", "client_id", "created_at"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(index=True)
    postamat_id: Mapped[uuid.UUID] = mapped_column(index=True)
    cell_id: Mapped[uuid.UUID] = mapped_column(index=True)
    cell_type_id: Mapped[uuid.UUID] = mapped_column(index=True)
    duration_hours: Mapped[int] = mapped_column(Integer)
    # Copied from the tariff, never referenced: prices change and history has to
    # stay true to what the customer agreed to pay.
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="TMT")
    status: Mapped[BookingStatus] = mapped_column(
        String(24), index=True, default=BookingStatus.PENDING_PAYMENT
    )
    depositor: Mapped[Depositor] = mapped_column(String(16), default=Depositor.OWNER)
    courier_phone: Mapped[str | None] = mapped_column(String(16))
    recipient_phone: Mapped[str] = mapped_column(String(16), index=True)
    recipient_name: Mapped[str | None] = mapped_column(String(200))
    # The 10-minute reservation before payment. Distinct from expires_at, which
    # is the storage clock and only starts when the parcel is in the cell.
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deposited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_reason: Mapped[str | None] = mapped_column(String(500))

    events: Mapped[list["BookingEvent"]] = relationship(
        back_populates="booking", lazy="selectin", cascade="all, delete-orphan",
        order_by="BookingEvent.created_at",
    )
    codes: Mapped[list["AccessCode"]] = relationship(
        back_populates="booking", lazy="selectin", cascade="all, delete-orphan",
    )


class BookingEvent(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "booking_events"

    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id"), index=True
    )
    # The timeline the app renders. One row per transition, so the screen never
    # has to reconstruct history from a single status column.
    status: Mapped[BookingStatus] = mapped_column(String(24))
    message: Mapped[str] = mapped_column(String(200))
    details: Mapped[dict | None] = mapped_column(JSON)

    booking: Mapped[Booking] = relationship(back_populates="events")


class AccessCode(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "access_codes"

    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id"), index=True
    )
    purpose: Mapped[CodePurpose] = mapped_column(String(16))
    # Only the digest. The plaintext is shown once, when it is issued.
    code_hash: Mapped[str] = mapped_column(String(64), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    booking: Mapped[Booking] = relationship(back_populates="codes")
```

- [ ] **Step 4: Register the models with Alembic**

In `backend/alembic/env.py`, beside the existing direct model-module imports, add:

```python
import app.modules.booking.models  # noqa: F401
```

Import the module directly, not the package — `app/modules/booking/__init__.py` is empty and
importing it registers nothing, which is the mistake Plan 1's Task 10 had to fix.

- [ ] **Step 5: Run the test and confirm it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_models.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 6: Generate the migration and check the partial index survived**

```bash
./.venv/Scripts/python.exe -m alembic revision --autogenerate -m "bookings, timeline, access codes"
```

Open the generated file. It must contain the three `create_table` calls and:

```python
    op.create_index('uq_active_booking_per_cell', 'bookings', ['cell_id'], unique=True,
                    sqlite_where=sa.text("status IN ('awaiting_deposit', 'awaiting_pickup', 'paid', 'pending_payment')"))
```

If autogenerate emitted the index without the `sqlite_where` clause, add it by hand — an
unconditional unique index on `cell_id` would forbid a cell from ever being booked twice in its
life. Then apply and verify:

```bash
./.venv/Scripts/python.exe -m alembic upgrade head
./.venv/Scripts/python.exe -c "import sqlite3; print([r[0] for r in sqlite3.connect('dev.db').execute(\"SELECT sql FROM sqlite_master WHERE name='uq_active_booking_per_cell'\")])"
```

Expected: the printed SQL contains `WHERE status IN (...)`.

- [ ] **Step 7: Commit**

```bash
git add app/modules/booking/__init__.py app/modules/booking/models.py alembic/env.py \
        alembic/versions/<new_revision>_bookings_timeline_access_codes.py \
        tests/booking/__init__.py tests/booking/test_models.py
git commit -m "feat: booking, timeline and access-code tables with one-active-booking-per-cell"
```

---

### Task 3: The lifecycle, as pure functions

**Files:**
- Create: `backend/app/modules/booking/service.py`
- Create: `backend/tests/booking/test_lifecycle.py`

**Interfaces:**
- Consumes: `BookingStatus`, `CELL_HELD_STATUSES` (Task 2).
- Produces: `ALLOWED_TRANSITIONS: dict[BookingStatus, frozenset[BookingStatus]]`,
  `can_transition(current, target) -> bool`, `assert_transition(current, target) -> None`
  (raises `AppError(ErrorCode.BOOKING_INVALID_STATE, ..., 409)`).

The state machine is separated from the database on purpose: it is the part of the system where a
wrong edge costs the most, and it can be exhaustively tested without a session.

- [ ] **Step 1: Add the error codes**

In `backend/app/core/errors.py`, inside `ErrorCode`, after `CELL_NUMBER_TAKEN`:

```python
    BOOKING_INVALID_STATE = "BOOKING_INVALID_STATE"
    SIZE_SOLD_OUT = "SIZE_SOLD_OUT"
```

- [ ] **Step 2: Write the failing test**

```python
# backend/tests/booking/test_lifecycle.py
import pytest

from app.core.errors import AppError, ErrorCode
from app.modules.booking.models import BookingStatus, CELL_HELD_STATUSES
from app.modules.booking.service import (
    ALLOWED_TRANSITIONS,
    assert_transition,
    can_transition,
)


def test_the_happy_path_is_walkable():
    path = [
        BookingStatus.PENDING_PAYMENT, BookingStatus.PAID,
        BookingStatus.AWAITING_DEPOSIT, BookingStatus.AWAITING_PICKUP,
        BookingStatus.COMPLETED,
    ]
    for current, target in zip(path, path[1:]):
        assert can_transition(current, target) is True


def test_a_collected_booking_is_final():
    assert ALLOWED_TRANSITIONS[BookingStatus.COMPLETED] == frozenset()
    assert ALLOWED_TRANSITIONS[BookingStatus.CANCELLED] == frozenset()


def test_a_deposited_parcel_can_no_longer_be_cancelled():
    # Cancelling would free a cell with someone's parcel still inside it.
    assert can_transition(BookingStatus.AWAITING_PICKUP, BookingStatus.CANCELLED) is False
    assert can_transition(BookingStatus.AWAITING_DEPOSIT, BookingStatus.CANCELLED) is True


def test_no_transition_skips_the_deposit():
    assert can_transition(BookingStatus.PAID, BookingStatus.COMPLETED) is False


def test_every_status_is_reachable_and_every_target_is_a_known_status():
    for current, targets in ALLOWED_TRANSITIONS.items():
        assert isinstance(current, BookingStatus)
        for target in targets:
            assert target in ALLOWED_TRANSITIONS


def test_leaving_a_holding_status_for_a_free_one_is_what_frees_the_cell():
    assert BookingStatus.AWAITING_PICKUP in CELL_HELD_STATUSES
    assert BookingStatus.COMPLETED not in CELL_HELD_STATUSES


def test_assert_transition_raises_a_409():
    with pytest.raises(AppError) as caught:
        assert_transition(BookingStatus.COMPLETED, BookingStatus.CANCELLED)
    assert caught.value.code == ErrorCode.BOOKING_INVALID_STATE
    assert caught.value.status_code == 409
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_lifecycle.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.modules.booking.service'`.

- [ ] **Step 4: Write the top of `backend/app/modules/booking/service.py`**

```python
from app.core.errors import AppError, ErrorCode
from app.modules.booking.models import BookingStatus

# Written as data rather than as if-branches so the whole machine can be read at
# once and tested exhaustively. Payment moves pending_payment -> paid (Plan 2b);
# the kiosk moves awaiting_deposit -> awaiting_pickup -> completed (Plan 3).
ALLOWED_TRANSITIONS: dict[BookingStatus, frozenset[BookingStatus]] = {
    BookingStatus.PENDING_PAYMENT: frozenset(
        {BookingStatus.PAID, BookingStatus.CANCELLED}
    ),
    BookingStatus.PAID: frozenset(
        {BookingStatus.AWAITING_DEPOSIT, BookingStatus.CANCELLED}
    ),
    BookingStatus.AWAITING_DEPOSIT: frozenset(
        {BookingStatus.AWAITING_PICKUP, BookingStatus.CANCELLED}
    ),
    # No cancellation from here on: the parcel is inside, and freeing the cell
    # would hand someone else a door with a stranger's parcel behind it.
    BookingStatus.AWAITING_PICKUP: frozenset({BookingStatus.COMPLETED}),
    BookingStatus.COMPLETED: frozenset(),
    BookingStatus.CANCELLED: frozenset(),
}


def can_transition(current: BookingStatus, target: BookingStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def assert_transition(current: BookingStatus, target: BookingStatus) -> None:
    if not can_transition(current, target):
        raise AppError(
            ErrorCode.BOOKING_INVALID_STATE,
            f"A booking in state {current.value} cannot become {target.value}.",
            409, details={"from": current.value, "to": target.value},
        )
```

- [ ] **Step 5: Run the test and confirm it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_lifecycle.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 6: Commit**

```bash
git add app/core/errors.py app/modules/booking/service.py tests/booking/test_lifecycle.py
git commit -m "feat: booking state machine as data with exhaustive tests"
```

---

### Task 4: The three access codes

**Files:**
- Create: `backend/app/modules/booking/codes.py`
- Create: `backend/tests/booking/test_codes.py`

**Interfaces:**
- Consumes: `hash_pin` from `app.core.security`; `AccessCode`, `CodePurpose` (Task 2).
- Produces: `issue_codes(booking) -> dict[CodePurpose, str]`,
  `reissue_code(booking, purpose) -> str`,
  `find_code(session, postamat_id, plaintext) -> AccessCode | None`.

`find_code` has no HTTP caller until Plan 3's `/device/codes/verify`; it ships here because it is
the function that decides whether a digest lookup is possible at all, and because the same hash
must be produced by the issuer and the verifier. Its per-device rate limiting belongs to Plan 3.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/booking/test_codes.py
import uuid

from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_pin
from app.modules.booking.codes import find_code, issue_codes, reissue_code
from app.modules.booking.models import AccessCode, Booking, BookingStatus, CodePurpose


def _booking() -> Booking:
    return Booking(
        client_id=uuid.uuid4(), postamat_id=uuid.uuid4(), cell_id=uuid.uuid4(),
        cell_type_id=uuid.uuid4(), duration_hours=24, amount_minor=1800,
        status=BookingStatus.PENDING_PAYMENT, recipient_phone="+99362123456",
    )


def test_three_codes_are_issued_one_per_purpose():
    booking = _booking()
    plaintext = issue_codes(booking)
    assert set(plaintext) == {CodePurpose.DEPOSIT, CodePurpose.COURIER, CodePurpose.PICKUP}
    assert len(booking.codes) == 3


def test_codes_are_five_digits():
    plaintext = issue_codes(_booking())
    for code in plaintext.values():
        assert len(code) == get_settings().pin_length
        assert code.isdigit()


def test_the_plaintext_is_never_stored():
    booking = _booking()
    plaintext = issue_codes(booking)
    stored = {code.code_hash for code in booking.codes}
    assert not (stored & set(plaintext.values()))
    assert {hash_pin(code) for code in plaintext.values()} == stored


def test_reissue_replaces_only_that_purpose():
    booking = _booking()
    first = issue_codes(booking)
    replacement = reissue_code(booking, CodePurpose.COURIER)

    assert replacement != first[CodePurpose.COURIER]
    by_purpose = {code.purpose: code.code_hash for code in booking.codes}
    assert by_purpose[CodePurpose.COURIER] == hash_pin(replacement)
    assert by_purpose[CodePurpose.DEPOSIT] == hash_pin(first[CodePurpose.DEPOSIT])
    assert len(booking.codes) == 3


async def test_find_code_matches_by_digest_within_one_postamat(session):
    booking = _booking()
    plaintext = issue_codes(booking)
    session.add(booking)
    await session.flush()

    found = await find_code(session, booking.postamat_id, plaintext[CodePurpose.PICKUP])
    assert found is not None
    assert found.purpose == CodePurpose.PICKUP

    # The same digits at a different cabinet are not this booking's code.
    assert await find_code(session, uuid.uuid4(), plaintext[CodePurpose.PICKUP]) is None


async def test_find_code_ignores_used_and_finished_codes(session):
    from app.core.db import utcnow

    booking = _booking()
    plaintext = issue_codes(booking)
    session.add(booking)
    await session.flush()

    used = await session.scalar(
        select(AccessCode).where(AccessCode.code_hash == hash_pin(plaintext[CodePurpose.DEPOSIT]))
    )
    used.used_at = utcnow()
    await session.flush()

    assert await find_code(session, booking.postamat_id, plaintext[CodePurpose.DEPOSIT]) is None
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_codes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.modules.booking.codes'`.

- [ ] **Step 3: Write `backend/app/modules/booking/codes.py`**

```python
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_pin
from app.modules.booking.models import (
    AccessCode,
    Booking,
    CELL_HELD_STATUSES,
    CodePurpose,
)


def _digits(length: int) -> str:
    # secrets, not random: these open a physical door.
    return "".join(secrets.choice("0123456789") for _ in range(length))


def issue_codes(booking: Booking) -> dict[CodePurpose, str]:
    """Create one code per purpose and return the plaintext to show once."""
    length = get_settings().pin_length
    plaintext: dict[CodePurpose, str] = {}
    for purpose in CodePurpose:
        code = _digits(length)
        plaintext[purpose] = code
        booking.codes.append(AccessCode(purpose=purpose, code_hash=hash_pin(code)))
    return plaintext


def reissue_code(booking: Booking, purpose: CodePurpose) -> str:
    """Replace one code — the courier's PIN, when the SMS never arrived.

    The old digest is overwritten rather than left beside the new one, so a code
    that was texted to the wrong number stops working the moment it is resent.
    """
    code = _digits(get_settings().pin_length)
    for existing in booking.codes:
        if existing.purpose == purpose:
            existing.code_hash = hash_pin(code)
            existing.attempts = 0
            existing.used_at = None
            return code
    booking.codes.append(AccessCode(purpose=purpose, code_hash=hash_pin(code)))
    return code


async def find_code(
    session: AsyncSession, postamat_id: uuid.UUID, plaintext: str
) -> AccessCode | None:
    """Look up an unused code at one postamat by its digest.

    Scoped to the postamat because five digits repeat across a fleet: the same
    PIN is very likely live at another cabinet, and opening the wrong door is
    not a recoverable mistake.
    """
    stmt = (
        select(AccessCode)
        .join(Booking, Booking.id == AccessCode.booking_id)
        .where(
            AccessCode.code_hash == hash_pin(plaintext),
            AccessCode.used_at.is_(None),
            Booking.postamat_id == postamat_id,
            Booking.status.in_(tuple(CELL_HELD_STATUSES)),
        )
    )
    return await session.scalar(stmt)
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_codes.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add app/modules/booking/codes.py tests/booking/test_codes.py
git commit -m "feat: three hashed access codes per booking, scoped to one postamat"
```

---

### Task 5: What the catalog exposes to booking

**Files:**
- Modify: `backend/app/modules/catalog/service.py`
- Create: `backend/tests/catalog/test_cell_pools.py`

**Interfaces:**
- Produces:
  - `async def cell_ids_by_type(session, postamat_id) -> dict[uuid.UUID, list[uuid.UUID]]` —
    usable cells per cell type, in door order, excluding blocked and maintenance cells.
  - `async def free_cell_ids(session, postamat_id, cell_type_id, taken: set[uuid.UUID]) -> list[uuid.UUID]`
  - `def storage_expiry(postamat, deposited_at, duration_hours) -> datetime`

`booking` must not import `Cell`, so these functions are the whole of its view onto the hardware.
`free_cell_ids` takes the taken set as an argument rather than joining bookings, because the
reverse — catalog importing `Booking` — is the dependency this rule exists to forbid. Under
Postgres this function is where `SELECT ... FOR UPDATE SKIP LOCKED` would land.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/catalog/test_cell_pools.py
import uuid
from datetime import datetime, time, timezone

from app.modules.catalog.models import Cell, PostamatSchedule
from app.modules.catalog.service import cell_ids_by_type, free_cell_ids, storage_expiry


async def _cells(session, postamat, cell_type, count: int) -> list[Cell]:
    rows = [
        Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
             board=1, output=n)
        for n in range(1, count + 1)
    ]
    session.add_all(rows)
    await session.commit()
    return rows


async def test_pools_are_grouped_by_type_and_ordered_by_door_number(
    session, postamat, cell_type
):
    await _cells(session, postamat, cell_type, 3)
    pools = await cell_ids_by_type(session, postamat.id)
    assert list(pools) == [cell_type.id]
    assert len(pools[cell_type.id]) == 3


async def test_blocked_and_maintenance_cells_are_not_in_the_pool(
    session, postamat, cell_type
):
    rows = await _cells(session, postamat, cell_type, 3)
    rows[0].is_blocked = True
    rows[1].is_maintenance = True
    await session.commit()

    pools = await cell_ids_by_type(session, postamat.id)
    assert pools[cell_type.id] == [rows[2].id]


async def test_free_cells_exclude_the_ones_already_taken(session, postamat, cell_type):
    rows = await _cells(session, postamat, cell_type, 3)
    free = await free_cell_ids(session, postamat.id, cell_type.id, taken={rows[0].id})
    assert free == [rows[1].id, rows[2].id]


async def test_an_unknown_type_has_an_empty_pool(session, postamat, cell_type):
    await _cells(session, postamat, cell_type, 2)
    assert await free_cell_ids(session, postamat.id, uuid.uuid4(), taken=set()) == []


def test_storage_expiry_is_pushed_past_the_next_opening(postamat):
    # Closes at 20:00; 12 hours bought at 19:00 would otherwise expire at 07:00,
    # an hour before the doors open and the recipient could physically arrive.
    postamat.schedule = [
        PostamatSchedule(weekday=day, opens_at=time(8, 0), closes_at=time(20, 0))
        for day in range(7)
    ]
    deposited = datetime(2026, 8, 17, 19, 0, tzinfo=timezone.utc)
    assert storage_expiry(postamat, deposited, 12) == datetime(
        2026, 8, 18, 8, 0, tzinfo=timezone.utc
    )


def test_storage_expiry_is_left_alone_when_the_site_is_open_at_that_hour(postamat):
    postamat.schedule = [
        PostamatSchedule(weekday=day, opens_at=time(8, 0), closes_at=time(20, 0))
        for day in range(7)
    ]
    deposited = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
    assert storage_expiry(postamat, deposited, 4) == datetime(
        2026, 8, 17, 13, 0, tzinfo=timezone.utc
    )


def test_storage_expiry_of_a_round_the_clock_site_is_exactly_the_duration(postamat):
    postamat.round_the_clock = True
    deposited = datetime(2026, 8, 17, 19, 0, tzinfo=timezone.utc)
    assert storage_expiry(postamat, deposited, 12) == datetime(
        2026, 8, 18, 7, 0, tzinfo=timezone.utc
    )
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/catalog/test_cell_pools.py -v`
Expected: FAIL with `ImportError: cannot import name 'cell_ids_by_type'`.

- [ ] **Step 3: Extend `backend/app/modules/catalog/service.py`**

Add at the top of the file, keeping the existing imports:

```python
import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import Cell
```

And at the bottom:

```python
def _usable_cells(postamat_id: uuid.UUID):
    return (
        select(Cell)
        .where(
            Cell.postamat_id == postamat_id,
            Cell.is_blocked.is_(False),
            Cell.is_maintenance.is_(False),
        )
        .order_by(Cell.number)
    )


async def cell_ids_by_type(
    session: AsyncSession, postamat_id: uuid.UUID
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Usable cells at one postamat, grouped by type, in door order."""
    pools: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for cell in await session.scalars(_usable_cells(postamat_id)):
        pools[cell.cell_type_id].append(cell.id)
    return dict(pools)


async def free_cell_ids(
    session: AsyncSession,
    postamat_id: uuid.UUID,
    cell_type_id: uuid.UUID,
    taken: set[uuid.UUID],
) -> list[uuid.UUID]:
    """Cells of one type that no caller-supplied booking is holding.

    `taken` is passed in rather than joined here because bookings belong to
    another module, and catalog importing them is the coupling the module rule
    forbids. On Postgres this is the function that would become
    `SELECT ... FOR UPDATE SKIP LOCKED`; the caller's contract does not change.
    """
    stmt = _usable_cells(postamat_id).where(Cell.cell_type_id == cell_type_id)
    return [cell.id for cell in await session.scalars(stmt) if cell.id not in taken]


def storage_expiry(postamat, deposited_at: datetime, duration_hours: int) -> datetime:
    """When storage ends, never before the recipient could physically arrive.

    A 12-hour rental deposited at 19:00 at a site closing at 20:00 would expire
    at 07:00, an hour before the doors open. The expiry is pushed to the next
    opening instead — the customer paid for reachable storage, not for hours
    behind a locked door.
    """
    plain = deposited_at + timedelta(hours=duration_hours)
    if is_open_at(postamat, plain):
        return plain
    opening = next_opening_after(postamat, plain)
    return plain if opening is None else opening
```

Note: `datetime` and `timedelta` are already imported at the top of this module.

- [ ] **Step 4: Run the test and confirm it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/catalog/test_cell_pools.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add app/modules/catalog/service.py tests/catalog/test_cell_pools.py
git commit -m "feat: cell pools and opening-hours-aware storage expiry"
```

---

### Task 6: Availability

**Files:**
- Modify: `backend/app/api/public/postamats.py`, `backend/app/modules/catalog/schemas.py`
- Create: `backend/tests/booking/test_availability.py`

**Interfaces:**
- Consumes: `cell_ids_by_type` (Task 5), `Tariff` (Plan 1 Task 16), `CELL_HELD_STATUSES` (Task 2).
- Produces: `GET /api/v1/postamats/{postamat_id}/availability` →
  `{"items": [{"cell_type_id", "code", "name", "width_cm", "height_cm", "depth_cm", "free", "prices": [{"duration_hours", "amount_minor", "currency"}]}]}`.

This is the screen that shows "Маленький 20×20×40 · 4 доступно" with prices, so counts and prices
have to come from one request.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/booking/test_availability.py
from app.modules.booking.models import Booking, BookingStatus
from app.modules.catalog.models import Cell


async def _cells(session, postamat, cell_type, count):
    rows = [
        Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
             board=1, output=n)
        for n in range(1, count + 1)
    ]
    session.add_all(rows)
    await session.commit()
    return rows


async def _price(client, admin_token, city, cell_type):
    await client.put("/api/v1/admin/tariffs",
                     headers={"Authorization": f"Bearer {admin_token}"},
                     json={"entries": [
                         {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
                          "duration_hours": hours, "amount_minor": amount}
                         for hours, amount in ((12, 1200), (24, 1800), (48, 2600))
                     ]})


async def test_availability_counts_free_cells_and_lists_prices(
    client, session, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 4)
    await _price(client, admin_token, city, cell_type)

    response = await client.get(f"/api/v1/postamats/{postamat.id}/availability")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["code"] == cell_type.code
    assert item["free"] == 4
    assert {price["duration_hours"] for price in item["prices"]} == {12, 24, 48}
    assert [p["amount_minor"] for p in item["prices"] if p["duration_hours"] == 24] == [1800]


async def test_a_held_cell_is_not_free(
    client, session, admin_token, city, cell_type, postamat
):
    rows = await _cells(session, postamat, cell_type, 2)
    await _price(client, admin_token, city, cell_type)
    session.add(Booking(
        client_id=postamat.id, postamat_id=postamat.id, cell_id=rows[0].id,
        cell_type_id=cell_type.id, duration_hours=24, amount_minor=1800,
        status=BookingStatus.AWAITING_PICKUP, recipient_phone="+99362123456",
    ))
    await session.commit()

    response = await client.get(f"/api/v1/postamats/{postamat.id}/availability")
    assert response.json()["items"][0]["free"] == 1


async def test_a_finished_booking_does_not_hold_a_cell(
    client, session, admin_token, city, cell_type, postamat
):
    rows = await _cells(session, postamat, cell_type, 1)
    await _price(client, admin_token, city, cell_type)
    session.add(Booking(
        client_id=postamat.id, postamat_id=postamat.id, cell_id=rows[0].id,
        cell_type_id=cell_type.id, duration_hours=24, amount_minor=1800,
        status=BookingStatus.COMPLETED, recipient_phone="+99362123456",
    ))
    await session.commit()

    response = await client.get(f"/api/v1/postamats/{postamat.id}/availability")
    assert response.json()["items"][0]["free"] == 1


async def test_availability_of_a_blocked_postamat_is_404(
    client, admin_token, cell_type, postamat
):
    await client.post(f"/api/v1/admin/postamats/{postamat.id}/block",
                      headers={"Authorization": f"Bearer {admin_token}"},
                      json={"reason": "vandalised"})
    response = await client.get(f"/api/v1/postamats/{postamat.id}/availability")
    assert response.status_code == 404
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_availability.py -v`
Expected: FAIL — 404 on an endpoint that does not exist yet.

- [ ] **Step 3: Add the response schemas**

At the end of `backend/app/modules/catalog/schemas.py`:

```python
class PriceOut(BaseModel):
    duration_hours: int
    amount_minor: int
    currency: str


class AvailabilityItem(BaseModel):
    cell_type_id: uuid.UUID
    code: str
    name: str
    width_cm: int
    height_cm: int
    depth_cm: int
    free: int
    prices: list[PriceOut]


class AvailabilityOut(BaseModel):
    items: list[AvailabilityItem]
```

- [ ] **Step 4: Add the route to `backend/app/api/public/postamats.py`**

```python
@router.get(
    "/postamats/{postamat_id}/availability", response_model=AvailabilityOut,
    dependencies=[Depends(rate_limit("public", limit=120, window_seconds=60))],
)
async def postamat_availability(
    postamat_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AvailabilityOut:
    postamat = await session.scalar(_visible().where(Postamat.id == postamat_id))
    if postamat is None:
        raise AppError(ErrorCode.NOT_FOUND, "Postamat not found.", 404)

    language = get_language(request)
    pools = await cell_ids_by_type(session, postamat.id)
    held = set(await session.scalars(
        select(Booking.cell_id).where(
            Booking.postamat_id == postamat.id,
            Booking.status.in_(tuple(CELL_HELD_STATUSES)),
        )
    ))
    types = {
        row.id: row
        for row in await session.scalars(
            select(CellType).where(CellType.id.in_(pools.keys() or [uuid.uuid4()]))
        )
    }
    tariffs = await session.scalars(
        select(Tariff).where(Tariff.city_id == postamat.city_id)
    )
    prices: dict[uuid.UUID, list[PriceOut]] = defaultdict(list)
    for tariff in tariffs:
        prices[tariff.cell_type_id].append(PriceOut(
            duration_hours=tariff.duration_hours,
            amount_minor=tariff.amount_minor, currency=tariff.currency,
        ))

    items = []
    for cell_type_id, cell_ids in pools.items():
        cell_type = types.get(cell_type_id)
        if cell_type is None or cell_type.is_blocked:
            continue
        items.append(AvailabilityItem(
            cell_type_id=cell_type_id, code=cell_type.code,
            name=localized(cell_type, language), width_cm=cell_type.width_cm,
            height_cm=cell_type.height_cm, depth_cm=cell_type.depth_cm,
            free=sum(1 for cell_id in cell_ids if cell_id not in held),
            prices=sorted(prices[cell_type_id], key=lambda p: p.duration_hours),
        ))
    items.sort(key=lambda item: (item.width_cm, item.code))
    return AvailabilityOut(items=items)
```

Add to that module's imports:

```python
from collections import defaultdict

from app.core.context import get_language
from app.modules.booking.models import Booking, CELL_HELD_STATUSES
from app.modules.catalog.models import CellType, Tariff
from app.modules.catalog.schemas import (
    AvailabilityItem,
    AvailabilityOut,
    PriceOut,
    localized,
)
from app.modules.catalog.service import cell_ids_by_type
```

This router reads `Booking` directly, which the module rule allows: the rule binds modules to each
other, and `api/` is the composition layer whose job is exactly to join them.

- [ ] **Step 5: Run the test and confirm it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_availability.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 6: Commit**

```bash
git add app/api/public/postamats.py app/modules/catalog/schemas.py tests/booking/test_availability.py
git commit -m "feat: free-cell counts and prices per size at a postamat"
```

---

### Task 7: Booking a cell

**Files:**
- Modify: `backend/app/modules/booking/service.py`
- Create: `backend/app/modules/booking/schemas.py`, `backend/app/api/mobile/bookings.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/booking/test_create.py`

**Interfaces:**
- Consumes: `write_transaction` (Task 1), `free_cell_ids` (Task 5), `issue_codes` (Task 4),
  `require_client` (Plan 1 Task 12).
- Produces:
  - `async def create_booking(session, *, client_id, postamat_id, cell_type_id, duration_hours, amount_minor, currency, recipient_phone, recipient_name, depositor, courier_phone) -> tuple[Booking, dict[CodePurpose, str]]` — keyword-only after `session`
  - `async def record_event(session, booking, status, message, details=None) -> BookingEvent`
  - `POST /api/v1/bookings`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/booking/test_create.py
from app.modules.booking.models import BookingStatus
from app.modules.catalog.models import Cell


async def _cells(session, postamat, cell_type, count):
    rows = [
        Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
             board=1, output=n)
        for n in range(1, count + 1)
    ]
    session.add_all(rows)
    await session.commit()
    return rows


async def _tariffs(client, admin_token, city, cell_type):
    await client.put("/api/v1/admin/tariffs",
                     headers={"Authorization": f"Bearer {admin_token}"},
                     json={"entries": [
                         {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
                          "duration_hours": hours, "amount_minor": amount}
                         for hours, amount in ((12, 1200), (24, 1800), (48, 2600))
                     ]})


def _body(postamat, cell_type):
    return {
        "postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
        "duration_hours": 24, "recipient_phone": "+99365000001",
        "recipient_name": "Получатель", "depositor": "owner",
    }


async def test_booking_holds_a_cell_and_returns_three_codes(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 2)
    await _tariffs(client, admin_token, city, cell_type)

    response = await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                                 headers={"Authorization": f"Bearer {client_token}"})
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == BookingStatus.PENDING_PAYMENT
    assert body["cell_number"] == 1
    assert body["amount_minor"] == 1800
    assert set(body["codes"]) == {"deposit", "courier", "pickup"}
    assert all(len(code) == 5 and code.isdigit() for code in body["codes"].values())
    assert body["hold_expires_at"].endswith("Z")


async def test_the_second_booking_takes_the_next_cell(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 2)
    await _tariffs(client, admin_token, city, cell_type)
    headers = {"Authorization": f"Bearer {client_token}"}

    first = await client.post("/api/v1/bookings", json=_body(postamat, cell_type), headers=headers)
    second = await client.post("/api/v1/bookings", json=_body(postamat, cell_type), headers=headers)
    assert [first.json()["cell_number"], second.json()["cell_number"]] == [1, 2]


async def test_a_sold_out_size_is_409(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 1)
    await _tariffs(client, admin_token, city, cell_type)
    headers = {"Authorization": f"Bearer {client_token}"}

    await client.post("/api/v1/bookings", json=_body(postamat, cell_type), headers=headers)
    sold_out = await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                                 headers=headers)
    assert sold_out.status_code == 409
    assert sold_out.json()["error"]["code"] == "SIZE_SOLD_OUT"


async def test_an_unpriced_size_cannot_be_booked(
    client, session, client_token, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 1)
    response = await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                                 headers={"Authorization": f"Bearer {client_token}"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TARIFF_INCOMPLETE"


async def test_a_blocked_postamat_cannot_be_booked(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 1)
    await _tariffs(client, admin_token, city, cell_type)
    await client.post(f"/api/v1/admin/postamats/{postamat.id}/block",
                      headers={"Authorization": f"Bearer {admin_token}"},
                      json={"reason": "vandalised"})

    response = await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                                 headers={"Authorization": f"Bearer {client_token}"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "POSTAMAT_BLOCKED"


async def test_the_timeline_starts_with_the_booking_itself(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    await _cells(session, postamat, cell_type, 1)
    await _tariffs(client, admin_token, city, cell_type)
    created = await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                                headers={"Authorization": f"Bearer {client_token}"})

    detail = await client.get(f"/api/v1/bookings/{created.json()['id']}",
                              headers={"Authorization": f"Bearer {client_token}"})
    assert [event["status"] for event in detail.json()["timeline"]] == ["pending_payment"]
```

- [ ] **Step 2: Add the `client_token` fixture to `backend/tests/conftest.py`**

```python
@pytest.fixture
async def booking_client(session):
    from app.modules.identity.models import Client

    row = Client(phone="+99361000001", full_name="Отправитель")
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@pytest.fixture
def client_token(booking_client):
    from app.core.security import create_access_token

    return create_access_token("client", booking_client.id)
```

- [ ] **Step 3: Run the test and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_create.py -v`
Expected: FAIL — 404, the route does not exist.

- [ ] **Step 4: Write `backend/app/modules/booking/schemas.py`**

```python
import uuid

from pydantic import BaseModel, Field

from app.core.pagination import PageMeta
from app.core.types import PhoneNumber
from app.modules.booking.models import BookingStatus, CodePurpose, Depositor


class BookingCreate(BaseModel):
    postamat_id: uuid.UUID
    cell_type_id: uuid.UUID
    duration_hours: int
    recipient_phone: PhoneNumber
    recipient_name: str | None = Field(default=None, max_length=200)
    depositor: Depositor = Depositor.OWNER
    courier_phone: PhoneNumber | None = None


class TimelineEntry(BaseModel):
    status: BookingStatus
    message: str
    at: str


class BookingOut(BaseModel):
    id: uuid.UUID
    status: BookingStatus
    postamat_id: uuid.UUID
    cell_number: int
    cell_type_id: uuid.UUID
    duration_hours: int
    amount_minor: int
    currency: str
    recipient_phone: str
    recipient_name: str | None
    depositor: Depositor
    hold_expires_at: str | None
    expires_at: str | None
    created_at: str
    timeline: list[TimelineEntry]


class BookingCreated(BookingOut):
    # The only response that ever carries plaintext PINs. They are shown once,
    # here, and exist as digests everywhere else.
    codes: dict[CodePurpose, str]


class BookingPage(BaseModel):
    items: list[BookingOut]
    pagination: PageMeta
```

- [ ] **Step 5: Extend `backend/app/modules/booking/service.py`**

```python
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import utcnow, write_transaction
from app.core.errors import AppError, ErrorCode
from app.modules.booking.codes import issue_codes
from app.modules.booking.models import (
    Booking,
    BookingEvent,
    BookingStatus,
    CELL_HELD_STATUSES,
    CodePurpose,
    Depositor,
)
from app.modules.catalog.service import free_cell_ids


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def record_event(
    session: AsyncSession, booking: Booking, status: BookingStatus,
    message: str, details: dict | None = None,
) -> BookingEvent:
    event = BookingEvent(status=status, message=message, details=details)
    booking.events.append(event)
    return event


async def _held_cell_ids(session: AsyncSession, postamat_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await session.scalars(
        select(Booking.cell_id).where(
            Booking.postamat_id == postamat_id,
            Booking.status.in_(tuple(CELL_HELD_STATUSES)),
        )
    )
    return set(rows)


async def create_booking(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    postamat_id: uuid.UUID,
    cell_type_id: uuid.UUID,
    duration_hours: int,
    amount_minor: int,
    currency: str,
    recipient_phone: str,
    recipient_name: str | None,
    depositor: Depositor,
    courier_phone: str | None,
) -> tuple[Booking, dict[CodePurpose, str]]:
    """Take one free cell of a size and hold it.

    The whole allocation runs inside one write transaction opened with BEGIN
    IMMEDIATE, so a second caller cannot read the same cell as free: SQLite
    admits one writer at a time and the second one waits here rather than
    racing. The partial unique index is the backstop if this is ever wrong.
    """
    settings = get_settings()
    async with write_transaction(session):
        taken = await _held_cell_ids(session, postamat_id)
        free = await free_cell_ids(session, postamat_id, cell_type_id, taken)
        if not free:
            raise AppError(
                ErrorCode.SIZE_SOLD_OUT,
                "No free cell of this size at this postamat.", 409,
                details={"cell_type_id": str(cell_type_id)},
            )

        booking = Booking(
            client_id=client_id, postamat_id=postamat_id, cell_id=free[0],
            cell_type_id=cell_type_id, duration_hours=duration_hours,
            amount_minor=amount_minor, currency=currency,
            status=BookingStatus.PENDING_PAYMENT, depositor=depositor,
            courier_phone=courier_phone, recipient_phone=recipient_phone,
            recipient_name=recipient_name,
            hold_expires_at=utcnow() + timedelta(minutes=settings.hold_minutes),
        )
        plaintext = issue_codes(booking)
        session.add(booking)
        await record_event(session, booking, BookingStatus.PENDING_PAYMENT,
                           "Забронировано")
        await session.flush()

    return booking, plaintext
```

- [ ] **Step 6: Write `backend/app/api/mobile/bookings.py`**

```python
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_client
from app.core.errors import AppError, ErrorCode
from app.core.types import utc_isoformat
from app.modules.booking import service
from app.modules.booking.models import Booking
from app.modules.booking.schemas import BookingCreate, BookingCreated, BookingOut
from app.modules.catalog.models import Cell, Postamat, PostamatStatus, Tariff
from app.modules.identity.models import Client

router = APIRouter(prefix="/bookings", tags=["bookings"])


def _timeline(booking: Booking) -> list[dict]:
    return [
        {"status": event.status, "message": event.message,
         "at": utc_isoformat(event.created_at)}
        for event in booking.events
    ]


async def _out(session: AsyncSession, booking: Booking) -> dict:
    cell = await session.get(Cell, booking.cell_id)
    return {
        "id": booking.id, "status": booking.status,
        "postamat_id": booking.postamat_id,
        "cell_number": cell.number if cell else 0,
        "cell_type_id": booking.cell_type_id,
        "duration_hours": booking.duration_hours,
        "amount_minor": booking.amount_minor, "currency": booking.currency,
        "recipient_phone": booking.recipient_phone,
        "recipient_name": booking.recipient_name, "depositor": booking.depositor,
        "hold_expires_at": (
            utc_isoformat(booking.hold_expires_at) if booking.hold_expires_at else None
        ),
        "expires_at": (
            utc_isoformat(booking.expires_at) if booking.expires_at else None
        ),
        "created_at": utc_isoformat(booking.created_at),
        "timeline": _timeline(booking),
    }


@router.post("", response_model=BookingCreated, status_code=201)
async def create_booking(
    payload: BookingCreate,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> BookingCreated:
    postamat = await session.get(Postamat, payload.postamat_id)
    if postamat is None:
        raise AppError(ErrorCode.NOT_FOUND, "Postamat not found.", 404)
    if postamat.status == PostamatStatus.BLOCKED:
        raise AppError(ErrorCode.POSTAMAT_BLOCKED, "This postamat is out of service.", 409)

    tariff = await session.scalar(
        select(Tariff).where(
            Tariff.city_id == postamat.city_id,
            Tariff.cell_type_id == payload.cell_type_id,
            Tariff.duration_hours == payload.duration_hours,
        )
    )
    if tariff is None:
        # Refusing rather than inventing a price: a booking whose amount was
        # guessed is a payment nobody agreed to.
        raise AppError(ErrorCode.TARIFF_INCOMPLETE,
                       "This size and duration are not priced at this postamat.", 422)

    booking, codes = await service.create_booking(
        session, client_id=client.id, postamat_id=postamat.id,
        cell_type_id=payload.cell_type_id, duration_hours=payload.duration_hours,
        amount_minor=tariff.amount_minor, currency=tariff.currency,
        recipient_phone=payload.recipient_phone, recipient_name=payload.recipient_name,
        depositor=payload.depositor, courier_phone=payload.courier_phone,
    )
    return BookingCreated(**await _out(session, booking), codes=codes)


@router.get("/{booking_id}", response_model=BookingOut)
async def get_booking(
    booking_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> BookingOut:
    booking = await session.get(Booking, booking_id)
    # A booking belonging to somebody else answers 404, not 403: whether a
    # booking id exists is not something a stranger gets to learn.
    if booking is None or booking.client_id != client.id:
        raise AppError(ErrorCode.NOT_FOUND, "Booking not found.", 404)
    return BookingOut(**await _out(session, booking))
```

- [ ] **Step 7: Register the router in `backend/app/main.py`**

```python
from app.api.mobile import bookings as mobile_bookings
...
    app.include_router(mobile_bookings.router, prefix=API_PREFIX)
```

- [ ] **Step 8: Run the tests and confirm they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_create.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 9: Commit**

```bash
git add app/modules/booking/service.py app/modules/booking/schemas.py \
        app/api/mobile/bookings.py app/main.py tests/conftest.py tests/booking/test_create.py
git commit -m "feat: book a cell by size with a held reservation and three codes"
```

---

### Task 8: Exactly one winner

**Files:**
- Create: `backend/tests/booking/test_concurrency.py`

**Interfaces:**
- Consumes: everything from Task 7. Adds no production code unless the test fails.

The spec calls for this test by name (§8): parallel bookings for the last cell of a size, exactly
one wins. It runs against real sessions rather than the shared-session fixture, because the point
is what two connections do to each other.

- [ ] **Step 1: Write the test**

```python
# backend/tests/booking/test_concurrency.py
import asyncio
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import AppError
from app.modules.booking.models import (
    Booking,
    BookingStatus,
    CELL_HELD_STATUSES,
    Depositor,
)
from app.modules.booking.service import create_booking
from app.modules.catalog.models import Cell, CellType, City, Postamat


async def test_only_one_of_ten_parallel_bookings_takes_the_last_cell(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async with maker() as setup:
        city = City(code="ashgabat", name_tk="Aşgabat", name_ru="Ашхабад", name_en="Ashgabat")
        cell_type = CellType(code="small", name_tk="Kiçi", name_ru="Маленький",
                             name_en="Small", width_cm=20, height_cm=20, depth_cm=40)
        setup.add_all([city, cell_type])
        await setup.flush()
        postamat = Postamat(number="10099", name="ТП #99", city_id=city.id,
                            address="ул. Ататюрк")
        setup.add(postamat)
        await setup.flush()
        setup.add(Cell(postamat_id=postamat.id, cell_type_id=cell_type.id,
                       number=1, board=1, output=1))
        await setup.commit()
        postamat_id, cell_type_id = postamat.id, cell_type.id

    async def attempt() -> str:
        async with maker() as session:
            try:
                await create_booking(
                    session, client_id=uuid.uuid4(), postamat_id=postamat_id,
                    cell_type_id=cell_type_id, duration_hours=24,
                    amount_minor=1800, currency="TMT",
                    recipient_phone="+99365000001", recipient_name=None,
                    depositor=Depositor.OWNER, courier_phone=None,
                )
                return "won"
            except AppError as error:
                return error.code

    results = await asyncio.gather(*(attempt() for _ in range(10)))

    assert results.count("won") == 1
    assert set(results) == {"won", "SIZE_SOLD_OUT"}

    async with maker() as check:
        held = await check.scalar(
            select(func.count()).select_from(Booking).where(
                Booking.status.in_(tuple(CELL_HELD_STATUSES))
            )
        )
        assert held == 1
```

- [ ] **Step 2: Run it**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_concurrency.py -v`
Expected: PASS.

If it fails with `database is locked`, the pragma listener from Task 1 is not attached to the test
engine — fix that rather than widening the timeout. If it fails with two winners, the allocation is
not inside `write_transaction`; fix the service, not the test.

- [ ] **Step 3: Commit**

```bash
git add tests/booking/test_concurrency.py
git commit -m "test: ten parallel bookings for the last cell leave exactly one winner"
```

---

### Task 9: Payment, deposit and collection as service functions

**Files:**
- Modify: `backend/app/modules/booking/service.py`
- Create: `backend/tests/booking/test_transitions.py`

**Interfaces:**
- Produces:
  - `async def mark_paid(session, booking) -> Booking` — `pending_payment → paid → awaiting_deposit`,
    clears `hold_expires_at`, sets `paid_at`.
  - `async def mark_deposited(session, booking, postamat, moment=None) -> Booking` —
    `awaiting_deposit → awaiting_pickup`, sets `deposited_at` and `expires_at` via `storage_expiry`.
  - `async def mark_collected(session, booking, moment=None) -> Booking` —
    `awaiting_pickup → completed`, sets `collected_at`, frees the cell.
  - `async def cancel(session, booking, reason, actor=None) -> Booking`.

`mark_paid` is called by Plan 2b's webhook and `mark_deposited` / `mark_collected` by Plan 3's
kiosk. They land here because they are the lifecycle, and because the cell is not free until they
run.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/booking/test_transitions.py
from datetime import datetime, time, timezone

import pytest

from app.core.errors import AppError, ErrorCode
from app.modules.booking.models import Booking, BookingStatus, CELL_HELD_STATUSES
from app.modules.booking.service import (
    cancel,
    mark_collected,
    mark_deposited,
    mark_paid,
)
from app.modules.catalog.models import PostamatSchedule


def _booking(**overrides) -> Booking:
    import uuid

    base = dict(
        client_id=uuid.uuid4(), postamat_id=uuid.uuid4(), cell_id=uuid.uuid4(),
        cell_type_id=uuid.uuid4(), duration_hours=12, amount_minor=1200,
        status=BookingStatus.PENDING_PAYMENT, recipient_phone="+99362123456",
    )
    base.update(overrides)
    return Booking(**base)


async def test_payment_moves_straight_to_awaiting_deposit(session):
    booking = _booking()
    session.add(booking)
    await session.flush()

    await mark_paid(session, booking)
    assert booking.status == BookingStatus.AWAITING_DEPOSIT
    assert booking.paid_at is not None
    # The hold is over: what protects the cell now is the booking itself.
    assert booking.hold_expires_at is None
    assert [event.status for event in booking.events][-1] == BookingStatus.AWAITING_DEPOSIT


async def test_paying_twice_is_refused(session):
    booking = _booking()
    session.add(booking)
    await session.flush()
    await mark_paid(session, booking)

    with pytest.raises(AppError) as caught:
        await mark_paid(session, booking)
    assert caught.value.code == ErrorCode.BOOKING_INVALID_STATE


async def test_deposit_starts_the_storage_clock_at_the_next_opening(session, postamat):
    postamat.schedule = [
        PostamatSchedule(weekday=day, opens_at=time(8, 0), closes_at=time(20, 0))
        for day in range(7)
    ]
    booking = _booking(status=BookingStatus.AWAITING_DEPOSIT, duration_hours=12)
    session.add(booking)
    await session.flush()

    deposited = datetime(2026, 8, 17, 19, 0, tzinfo=timezone.utc)
    await mark_deposited(session, booking, postamat, moment=deposited)

    assert booking.status == BookingStatus.AWAITING_PICKUP
    assert booking.deposited_at == deposited
    assert booking.expires_at == datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc)


async def test_collection_completes_and_frees_the_cell(session, postamat):
    booking = _booking(status=BookingStatus.AWAITING_PICKUP)
    session.add(booking)
    await session.flush()

    await mark_collected(session, booking)
    assert booking.status == BookingStatus.COMPLETED
    assert booking.collected_at is not None
    assert booking.status not in CELL_HELD_STATUSES


async def test_cancelling_records_the_reason_and_frees_the_cell(session):
    booking = _booking()
    session.add(booking)
    await session.flush()

    await cancel(session, booking, reason="Клиент отменил", actor="client")
    assert booking.status == BookingStatus.CANCELLED
    assert booking.cancelled_reason == "Клиент отменил"
    assert booking.status not in CELL_HELD_STATUSES


async def test_a_deposited_parcel_cannot_be_cancelled(session):
    booking = _booking(status=BookingStatus.AWAITING_PICKUP)
    session.add(booking)
    await session.flush()

    with pytest.raises(AppError) as caught:
        await cancel(session, booking, reason="передумал")
    assert caught.value.status_code == 409
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_transitions.py -v`
Expected: FAIL with `ImportError: cannot import name 'mark_paid'`.

- [ ] **Step 3: Extend `backend/app/modules/booking/service.py`**

```python
from app.modules.catalog.service import storage_expiry


async def _move(
    session: AsyncSession, booking: Booking, target: BookingStatus, message: str,
    details: dict | None = None,
) -> Booking:
    assert_transition(booking.status, target)
    booking.status = target
    await record_event(session, booking, target, message, details)
    return booking


async def mark_paid(session: AsyncSession, booking: Booking) -> Booking:
    """Settle the booking and put it straight into awaiting_deposit.

    `paid` exists as a state for the ledger, not as somewhere a booking waits:
    the customer's next physical act is to deposit, so the two transitions run
    together and the timeline shows both.
    """
    await _move(session, booking, BookingStatus.PAID, "Оплачено")
    booking.paid_at = utcnow()
    # The hold protected an unpaid cell. Payment replaces it: from here the
    # booking itself is what holds the cell, and the hold worker must skip it.
    booking.hold_expires_at = None
    return await _move(session, booking, BookingStatus.AWAITING_DEPOSIT,
                       "Ожидает отправителя")


async def mark_deposited(
    session: AsyncSession, booking: Booking, postamat, moment: datetime | None = None,
) -> Booking:
    at = moment or utcnow()
    booking.deposited_at = at
    booking.expires_at = storage_expiry(postamat, at, booking.duration_hours)
    return await _move(session, booking, BookingStatus.AWAITING_PICKUP,
                       "Посылка в ячейке")


async def mark_collected(
    session: AsyncSession, booking: Booking, moment: datetime | None = None
) -> Booking:
    booking.collected_at = moment or utcnow()
    return await _move(session, booking, BookingStatus.COMPLETED, "Получено")


async def cancel(
    session: AsyncSession, booking: Booking, reason: str, actor: str | None = None
) -> Booking:
    booking.cancelled_reason = reason
    return await _move(session, booking, BookingStatus.CANCELLED, "Отменено",
                       details={"reason": reason, "actor": actor})
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_transitions.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add app/modules/booking/service.py tests/booking/test_transitions.py
git commit -m "feat: paid, deposited, collected and cancelled transitions"
```

---

### Task 10: The client's own bookings

**Files:**
- Modify: `backend/app/api/mobile/bookings.py`
- Create: `backend/tests/booking/test_client_routes.py`

**Interfaces:**
- Produces: `GET /api/v1/bookings` (paged, newest first, optional `active=true`),
  `POST /api/v1/bookings/{id}/cancel`,
  `POST /api/v1/bookings/{id}/courier/resend` → `{"courier_code": "12345"}`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/booking/test_client_routes.py
from app.modules.booking.models import BookingStatus
from app.modules.catalog.models import Cell


async def _ready(client, session, admin_token, city, cell_type, postamat, cells=2):
    session.add_all([
        Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
             board=1, output=n)
        for n in range(1, cells + 1)
    ])
    await session.commit()
    await client.put("/api/v1/admin/tariffs",
                     headers={"Authorization": f"Bearer {admin_token}"},
                     json={"entries": [
                         {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
                          "duration_hours": hours, "amount_minor": 1800}
                         for hours in (12, 24, 48)
                     ]})


def _body(postamat, cell_type):
    return {"postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
            "duration_hours": 24, "recipient_phone": "+99365000001",
            "depositor": "courier", "courier_phone": "+99366000002"}


async def test_the_list_shows_the_clients_own_bookings_newest_first(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    first = await client.post("/api/v1/bookings", json=_body(postamat, cell_type), headers=headers)
    second = await client.post("/api/v1/bookings", json=_body(postamat, cell_type), headers=headers)

    listed = await client.get("/api/v1/bookings", headers=headers)
    assert listed.status_code == 200
    ids = [item["id"] for item in listed.json()["items"]]
    assert ids == [second.json()["id"], first.json()["id"]]


async def test_another_clients_booking_is_invisible(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    from app.core.security import create_access_token
    from app.modules.identity.models import Client

    await _ready(client, session, admin_token, city, cell_type, postamat)
    created = await client.post("/api/v1/bookings", json=_body(postamat, cell_type),
                                headers={"Authorization": f"Bearer {client_token}"})

    stranger = Client(phone="+99361000009", full_name="Чужой")
    session.add(stranger)
    await session.commit()
    await session.refresh(stranger)
    token = create_access_token("client", stranger.id)

    response = await client.get(f"/api/v1/bookings/{created.json()['id']}",
                                headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404


async def test_cancelling_frees_the_cell_for_the_next_booking(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat, cells=1)
    headers = {"Authorization": f"Bearer {client_token}"}
    created = await client.post("/api/v1/bookings", json=_body(postamat, cell_type), headers=headers)

    sold_out = await client.post("/api/v1/bookings", json=_body(postamat, cell_type), headers=headers)
    assert sold_out.status_code == 409

    cancelled = await client.post(f"/api/v1/bookings/{created.json()['id']}/cancel",
                                  headers=headers, json={"reason": "передумал"})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == BookingStatus.CANCELLED

    again = await client.post("/api/v1/bookings", json=_body(postamat, cell_type), headers=headers)
    assert again.status_code == 201


async def test_resending_the_courier_pin_returns_a_new_one(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    created = await client.post("/api/v1/bookings", json=_body(postamat, cell_type), headers=headers)
    original = created.json()["codes"]["courier"]

    resent = await client.post(
        f"/api/v1/bookings/{created.json()['id']}/courier/resend", headers=headers
    )
    assert resent.status_code == 200
    assert resent.json()["courier_code"] != original
    assert len(resent.json()["courier_code"]) == 5


async def test_resending_is_refused_when_nobody_is_couriering(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    await _ready(client, session, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    body = _body(postamat, cell_type) | {"depositor": "owner", "courier_phone": None}
    created = await client.post("/api/v1/bookings", json=body, headers=headers)

    response = await client.post(
        f"/api/v1/bookings/{created.json()['id']}/courier/resend", headers=headers
    )
    assert response.status_code == 409
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_client_routes.py -v`
Expected: FAIL — 405/404 on the routes that do not exist.

- [ ] **Step 3: Add the routes to `backend/app/api/mobile/bookings.py`**

```python
class CancelRequest(BaseModel):
    reason: str = Field(default="Отменено клиентом", min_length=1, max_length=500)


class CourierCodeOut(BaseModel):
    courier_code: str


async def _own_booking(session: AsyncSession, client: Client, booking_id: uuid.UUID) -> Booking:
    booking = await session.get(Booking, booking_id)
    if booking is None or booking.client_id != client.id:
        raise AppError(ErrorCode.NOT_FOUND, "Booking not found.", 404)
    return booking


@router.get("", response_model=BookingPage)
async def list_bookings(
    active: bool = Query(default=False),
    params: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> BookingPage:
    stmt = (
        select(Booking).where(Booking.client_id == client.id)
        .order_by(Booking.created_at.desc())
    )
    if active:
        stmt = stmt.where(Booking.status.in_(tuple(CELL_HELD_STATUSES)))
    rows, meta = await paginate_page(session, stmt, params)
    return BookingPage(
        items=[BookingOut(**await _out(session, row)) for row in rows], pagination=meta
    )


@router.post("/{booking_id}/cancel", response_model=BookingOut)
async def cancel_booking(
    booking_id: uuid.UUID,
    payload: CancelRequest,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> BookingOut:
    booking = await _own_booking(session, client, booking_id)
    await service.cancel(session, booking, reason=payload.reason, actor="client")
    await session.commit()
    return BookingOut(**await _out(session, booking))


@router.post("/{booking_id}/courier/resend", response_model=CourierCodeOut)
async def resend_courier_code(
    booking_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> CourierCodeOut:
    booking = await _own_booking(session, client, booking_id)
    if booking.depositor != Depositor.COURIER or not booking.courier_phone:
        # Nothing to resend, and issuing a courier PIN for a booking nobody is
        # couriering would put a live code in the sender's hands twice over.
        raise AppError(ErrorCode.BOOKING_INVALID_STATE,
                       "This booking has no courier.", 409)
    if booking.status not in {BookingStatus.PENDING_PAYMENT, BookingStatus.PAID,
                              BookingStatus.AWAITING_DEPOSIT}:
        raise AppError(ErrorCode.BOOKING_INVALID_STATE,
                       "The parcel has already been deposited.", 409)

    code = reissue_code(booking, CodePurpose.COURIER)
    await service.record_event(session, booking, booking.status,
                               "PIN курьера отправлен повторно")
    await session.commit()
    # Delivering it by SMS is the notify module's job, which arrives with the
    # provider in Plan 2b. Returning it here keeps the flow testable meanwhile.
    return CourierCodeOut(courier_code=code)
```

Extend that module's imports:

```python
from fastapi import Query
from pydantic import BaseModel, Field

from app.core.pagination import PageParams, page_params, paginate_page
from app.modules.booking.codes import reissue_code
from app.modules.booking.models import BookingStatus, CELL_HELD_STATUSES, CodePurpose, Depositor
from app.modules.booking.schemas import BookingPage
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_client_routes.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add app/api/mobile/bookings.py tests/booking/test_client_routes.py
git commit -m "feat: booking list, cancellation and courier PIN resend"
```

---

### Task 11: Releasing expired holds

**Files:**
- Create: `backend/app/workers/__init__.py`, `backend/app/workers/holds.py`
- Create: `backend/tests/booking/test_holds.py`

**Interfaces:**
- Produces: `async def release_expired_holds(session, now=None) -> list[uuid.UUID]` — cancels every
  `pending_payment` booking whose `hold_expires_at` has passed and returns their ids;
  `async def run_once() -> int` opening its own session for a scheduler or a manual run.

Without this the first unpaid booking holds a cell forever and the cabinet leaks capacity one cell
at a time.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/booking/test_holds.py
import uuid
from datetime import timedelta

from app.core.db import utcnow
from app.modules.booking.models import Booking, BookingStatus
from app.workers.holds import release_expired_holds


def _booking(status, hold_offset_minutes):
    return Booking(
        client_id=uuid.uuid4(), postamat_id=uuid.uuid4(), cell_id=uuid.uuid4(),
        cell_type_id=uuid.uuid4(), duration_hours=24, amount_minor=1800,
        status=status, recipient_phone="+99362123456",
        hold_expires_at=utcnow() + timedelta(minutes=hold_offset_minutes),
    )


async def test_an_expired_unpaid_hold_is_cancelled(session):
    stale = _booking(BookingStatus.PENDING_PAYMENT, -1)
    session.add(stale)
    await session.commit()

    released = await release_expired_holds(session)
    assert released == [stale.id]
    assert stale.status == BookingStatus.CANCELLED
    assert stale.cancelled_reason == "Бронь не оплачена вовремя"


async def test_a_live_hold_is_left_alone(session):
    fresh = _booking(BookingStatus.PENDING_PAYMENT, 5)
    session.add(fresh)
    await session.commit()

    assert await release_expired_holds(session) == []
    assert fresh.status == BookingStatus.PENDING_PAYMENT


async def test_a_paid_booking_is_never_released(session):
    # mark_paid clears hold_expires_at, but a row written before that fix, or by
    # a future code path, must still be safe: paid bookings are out of scope.
    paid = _booking(BookingStatus.PAID, -60)
    session.add(paid)
    await session.commit()

    assert await release_expired_holds(session) == []
    assert paid.status == BookingStatus.PAID


async def test_the_timeline_records_why_it_was_cancelled(session):
    stale = _booking(BookingStatus.PENDING_PAYMENT, -1)
    session.add(stale)
    await session.commit()

    await release_expired_holds(session)
    assert [event.status for event in stale.events][-1] == BookingStatus.CANCELLED
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_holds.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.workers'`.

- [ ] **Step 3: Write `backend/app/workers/holds.py`** (and an empty `__init__.py`)

```python
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_scope, utcnow
from app.modules.booking import service
from app.modules.booking.models import Booking, BookingStatus

RELEASE_REASON = "Бронь не оплачена вовремя"


async def release_expired_holds(
    session: AsyncSession, now: datetime | None = None
) -> list[uuid.UUID]:
    """Cancel unpaid bookings whose reservation has run out.

    Only `pending_payment` is in scope. A paid booking holds its cell because it
    was paid for, not because of a timer, and releasing one would hand a
    stranger a cell somebody is on their way to fill.
    """
    moment = now or utcnow()
    stale = await session.scalars(
        select(Booking).where(
            Booking.status == BookingStatus.PENDING_PAYMENT,
            Booking.hold_expires_at.is_not(None),
            Booking.hold_expires_at <= moment,
        )
    )
    released: list[uuid.UUID] = []
    for booking in stale:
        await service.cancel(session, booking, reason=RELEASE_REASON, actor="system")
        released.append(booking.id)
    if released:
        await session.commit()
    return released


async def run_once() -> int:
    """Entry point for a scheduler or a manual run: opens its own session."""
    async with session_scope() as session:
        return len(await release_expired_holds(session))


if __name__ == "__main__":
    import asyncio

    print(f"released {asyncio.run(run_once())} expired holds")
```

Note on the comparison: SQLite returns naive datetimes, and `hold_expires_at <= moment` is compared
inside the database, not in Python, so no `_as_utc` normalisation is needed here. The Python-side
assertions in the tests read the ORM attribute, which is why the tests compare statuses rather than
timestamps.

- [ ] **Step 4: Run the test and confirm it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_holds.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add app/workers/__init__.py app/workers/holds.py tests/booking/test_holds.py
git commit -m "feat: release cells held by unpaid bookings whose hold expired"
```

---

### Task 12: The admin's view of bookings

**Files:**
- Create: `backend/app/api/admin/bookings.py`
- Modify: `backend/app/main.py`, `backend/tests/conftest.py`
- Create: `backend/tests/booking/test_admin_routes.py`

**Interfaces:**
- Produces: `GET /api/v1/admin/bookings` (filters: `status`, `postamat_id`, `client_id`),
  `GET /api/v1/admin/bookings/{id}`, `POST /api/v1/admin/bookings/{id}/cancel`.
- Consumes: `require_permission("bookings.read" | "bookings.write")`.

- [ ] **Step 1: Add the two permissions to the test role**

In `backend/tests/conftest.py`, extend the `admin_user` fixture's permission list with
`"bookings.read"` and `"bookings.write"`.

- [ ] **Step 2: Write the failing test**

```python
# backend/tests/booking/test_admin_routes.py
from app.modules.booking.models import BookingStatus
from app.modules.catalog.models import Cell


async def _booked(client, session, client_token, admin_token, city, cell_type, postamat):
    session.add(Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=1,
                     board=1, output=1))
    await session.commit()
    await client.put("/api/v1/admin/tariffs",
                     headers={"Authorization": f"Bearer {admin_token}"},
                     json={"entries": [
                         {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
                          "duration_hours": hours, "amount_minor": 1800}
                         for hours in (12, 24, 48)
                     ]})
    created = await client.post(
        "/api/v1/bookings",
        json={"postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
              "duration_hours": 24, "recipient_phone": "+99365000001"},
        headers={"Authorization": f"Bearer {client_token}"},
    )
    return created.json()


async def test_admin_sees_every_booking_and_can_filter_by_status(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    booking = await _booked(client, session, client_token, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {admin_token}"}

    listed = await client.get("/api/v1/admin/bookings", headers=headers)
    assert [item["id"] for item in listed.json()["items"]] == [booking["id"]]

    filtered = await client.get("/api/v1/admin/bookings?status=completed", headers=headers)
    assert filtered.json()["items"] == []


async def test_admin_detail_carries_the_timeline_but_no_plaintext_codes(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    booking = await _booked(client, session, client_token, admin_token, city, cell_type, postamat)
    response = await client.get(f"/api/v1/admin/bookings/{booking['id']}",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200
    assert "codes" not in response.json()
    assert [event["status"] for event in response.json()["timeline"]] == ["pending_payment"]


async def test_admin_force_cancel_records_who_did_it(
    client, session, client_token, admin_token, city, cell_type, postamat
):
    booking = await _booked(client, session, client_token, admin_token, city, cell_type, postamat)
    response = await client.post(f"/api/v1/admin/bookings/{booking['id']}/cancel",
                                 headers={"Authorization": f"Bearer {admin_token}"},
                                 json={"reason": "жалоба клиента"})
    assert response.status_code == 200
    assert response.json()["status"] == BookingStatus.CANCELLED

    detail = await client.get(f"/api/v1/admin/bookings/{booking['id']}",
                              headers={"Authorization": f"Bearer {admin_token}"})
    assert detail.json()["timeline"][-1]["message"] == "Отменено"


async def test_bookings_need_the_read_permission(client, session, postamat):
    from app.core.security import create_access_token
    from app.modules.identity.models import AdminUser, Role

    role = Role(code="viewer", name="Viewer", permissions=["postamats.read"])
    session.add(role)
    await session.flush()
    admin = AdminUser(login="viewer_test", full_name="Смотров С.С.",
                      password_hash="x", role_id=role.id)
    session.add(admin)
    await session.commit()
    await session.refresh(admin)

    response = await client.get(
        "/api/v1/admin/bookings",
        headers={"Authorization": f"Bearer {create_access_token('admin', admin.id)}"},
    )
    assert response.status_code == 403
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_admin_routes.py -v`
Expected: FAIL — 404 on routes that do not exist.

- [ ] **Step 4: Write `backend/app/api/admin/bookings.py`**

```python
import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.core.pagination import PageParams, page_params, paginate_page
from app.core.types import utc_isoformat
from app.modules.audit.models import Source
from app.modules.audit.service import record
from app.modules.booking import service
from app.modules.booking.models import Booking, BookingStatus
from app.modules.booking.schemas import BookingOut, BookingPage
from app.modules.catalog.models import Cell
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/bookings", tags=["admin-bookings"])


class AdminCancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


async def _out(session: AsyncSession, booking: Booking) -> dict:
    cell = await session.get(Cell, booking.cell_id)
    return {
        "id": booking.id, "status": booking.status,
        "postamat_id": booking.postamat_id,
        "cell_number": cell.number if cell else 0,
        "cell_type_id": booking.cell_type_id,
        "duration_hours": booking.duration_hours,
        "amount_minor": booking.amount_minor, "currency": booking.currency,
        "recipient_phone": booking.recipient_phone,
        "recipient_name": booking.recipient_name, "depositor": booking.depositor,
        "hold_expires_at": (
            utc_isoformat(booking.hold_expires_at) if booking.hold_expires_at else None
        ),
        "expires_at": utc_isoformat(booking.expires_at) if booking.expires_at else None,
        "created_at": utc_isoformat(booking.created_at),
        "timeline": [
            {"status": event.status, "message": event.message,
             "at": utc_isoformat(event.created_at)}
            for event in booking.events
        ],
    }


@router.get("", response_model=BookingPage)
async def list_bookings(
    status: BookingStatus | None = Query(default=None),
    postamat_id: uuid.UUID | None = Query(default=None),
    client_id: uuid.UUID | None = Query(default=None),
    params: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("bookings.read")),
) -> BookingPage:
    stmt = select(Booking).order_by(Booking.created_at.desc())
    if status is not None:
        stmt = stmt.where(Booking.status == status)
    if postamat_id is not None:
        stmt = stmt.where(Booking.postamat_id == postamat_id)
    if client_id is not None:
        stmt = stmt.where(Booking.client_id == client_id)
    rows, meta = await paginate_page(session, stmt, params)
    return BookingPage(
        items=[BookingOut(**await _out(session, row)) for row in rows], pagination=meta
    )


@router.get("/{booking_id}", response_model=BookingOut)
async def get_booking(
    booking_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("bookings.read")),
) -> BookingOut:
    booking = await session.get(Booking, booking_id)
    if booking is None:
        raise AppError(ErrorCode.NOT_FOUND, "Booking not found.", 404)
    return BookingOut(**await _out(session, booking))


@router.post("/{booking_id}/cancel", response_model=BookingOut)
async def cancel_booking(
    booking_id: uuid.UUID,
    payload: AdminCancelRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("bookings.write")),
) -> BookingOut:
    booking = await session.get(Booking, booking_id)
    if booking is None:
        raise AppError(ErrorCode.NOT_FOUND, "Booking not found.", 404)
    await service.cancel(session, booking, reason=payload.reason, actor=admin.login)
    # An operator cancelling someone else's booking is exactly the kind of act
    # the audit log exists for: it moves a cell and it has a name attached.
    await record(session, event="booking.cancelled", source=Source.ADMIN,
                 message=payload.reason, actor=admin.login,
                 postamat_id=booking.postamat_id, cell_id=booking.cell_id,
                 details={"booking_id": str(booking.id)})
    await session.commit()
    return BookingOut(**await _out(session, booking))
```

- [ ] **Step 5: Register the router in `backend/app/main.py`**

```python
from app.api.admin import bookings as admin_bookings
...
    app.include_router(admin_bookings.router, prefix=API_PREFIX)
```

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_admin_routes.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 7: Commit**

```bash
git add app/api/admin/bookings.py app/main.py tests/conftest.py tests/booking/test_admin_routes.py
git commit -m "feat: admin booking list, detail and force-cancel with an audit entry"
```

---

### Task 13: Smoke the new surface and close the plan

**Files:**
- Modify: `backend/scripts/smoke.py`, `README.md`

**Interfaces:** none. This task makes the new endpoints visible to a human running the API
without Docker, the same way Plan 1's smoke script covers the older ones.

- [ ] **Step 1: Extend `backend/scripts/smoke.py`**

Add `"app.modules.booking.models"` to the tuple inside `_import_models()`.

Add this seeding helper above `main()`:

```python
async def _seed_fleet(maker) -> dict:
    """Put one postamat with two small cells and a full tariff matrix in place.

    Written straight through the session rather than through the admin API,
    because the script has no admin account to log in as — seeding is not what
    this script is here to exercise.
    """
    from app.modules.catalog.models import Cell, CellType, City, Postamat, Tariff

    async with maker() as session:
        city = City(code="ashgabat", name_tk="Aşgabat", name_ru="Ашхабад",
                    name_en="Ashgabat")
        cell_type = CellType(code="small", name_tk="Kiçi", name_ru="Маленький",
                             name_en="Small", width_cm=20, height_cm=20, depth_cm=40)
        session.add_all([city, cell_type])
        await session.flush()

        postamat = Postamat(number="10042", name="ТП #4", city_id=city.id,
                            address="ул. Ататюрк, 31", round_the_clock=True)
        session.add(postamat)
        await session.flush()

        session.add_all([
            Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
                 board=1, output=n)
            for n in (1, 2)
        ])
        session.add_all([
            Tariff(city_id=city.id, cell_type_id=cell_type.id,
                   duration_hours=hours, amount_minor=amount, currency="TMT")
            for hours, amount in ((12, 1200), (24, 1800), (48, 2600))
        ])
        await session.commit()
        return {"postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id)}
```

Call it right after `app.dependency_overrides[get_session] = override`:

```python
    try:
        fleet = await _seed_fleet(maker)
    except ImportError:
        fleet = {}
```

And add this section inside the `async with AsyncClient(...)` block, after the
`-- authenticated as a client --` section:

```python
        if access and fleet:
            print(f"\n{YELLOW}-- booking a cell --{RESET}")
            bearer = {"Authorization": f"Bearer {access}"}
            postamat_id = fleet["postamat_id"]

            before = await call(
                client, "GET", f"/api/v1/postamats/{postamat_id}/availability"
            )
            if before:
                print(f"{GREY}     free before booking: "
                      f"{before['items'][0]['free']}{RESET}")

            created = await call(
                client, "POST", "/api/v1/bookings", headers=bearer, expect=201,
                json={
                    "postamat_id": postamat_id,
                    "cell_type_id": fleet["cell_type_id"],
                    "duration_hours": 24,
                    "recipient_phone": "+99365000001",
                    "recipient_name": "Получатель",
                },
            )
            if created:
                # Lengths only. This output gets pasted into chats, and these
                # five digits open a physical door.
                lengths = {purpose: len(code) for purpose, code in created["codes"].items()}
                print(f"{GREY}     cell {created['cell_number']}, code lengths "
                      f"{lengths}{RESET}")

                await call(client, "GET", "/api/v1/bookings", headers=bearer)
                await call(
                    client, "GET", f"/api/v1/postamats/{postamat_id}/availability"
                )
                await call(
                    client, "POST", f"/api/v1/bookings/{created['id']}/cancel",
                    headers=bearer, json={"reason": "smoke run"},
                )
                after = await call(
                    client, "GET", f"/api/v1/postamats/{postamat_id}/availability"
                )
                if after and before:
                    # The whole plan in one line: cancelling gives the cell back.
                    freed = after["items"][0]["free"] == before["items"][0]["free"]
                    print(f"{GREY}     cell returned to the pool: {freed}{RESET}")
```

Move the existing `-- authenticated as a client --` block above this one if it is not already
there, so `bearer` is defined once.

- [ ] **Step 2: Run it**

```bash
./.venv/Scripts/python.exe scripts/smoke.py
```

Expected: every new line reports `OK`, `0 failed`.

- [ ] **Step 3: Update the README**

In the backend section, add the booking flow to the list of what the API can do, and note that
holds are released by `./.venv/Scripts/python.exe -m app.workers.holds`, which is a manual run
until a scheduler exists.

- [ ] **Step 4: Run the whole suite from both directories**

```bash
./.venv/Scripts/python.exe -m pytest -q
cd .. && backend/.venv/Scripts/python.exe -m pytest backend -q
```

Expected: green from both.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke.py ../README.md
git commit -m "chore: smoke the booking flow and document the hold worker"
```

---

## Definition of done for this plan

- `./.venv/Scripts/python.exe -m pytest` is green from `backend/` and from the repository root.
- `GET /api/v1/postamats/{id}/availability` reports free counts and prices per size.
- A client can book a cell by size, receives three 5-digit codes exactly once, sees the timeline,
  lists their bookings, cancels one, and the cell becomes free again.
- Ten parallel bookings for the last cell produce exactly one winner and nine `SIZE_SOLD_OUT`.
- `uq_active_booking_per_cell` exists in the database with its `WHERE status IN (...)` clause.
- An admin can list, read and force-cancel bookings, and the force-cancel writes an audit entry.
- `app/workers/holds.py` releases expired unpaid holds and never touches a paid booking.

## Deliberately not in this plan

- **Payments** — `POST /bookings/{id}/payment`, `GET /payments/{id}`, the bank webhook, refunds.
  Plan 2b. `mark_paid` is the seam they call.
- **Custody and the overdue escalation** — `expired → grace → overdue → removed`, the removal act
  with its photo, the counter handover. Plan 2b; they need a payments-aware refund story and a file
  upload path.
- **Notifications** — the SMS and push that carry the courier PIN and the reminders. Plan 2b, with
  the provider decision from open question 2 of the spec.
- **The kiosk and the device channel** — `/device/codes/verify` and the opening of an actual door,
  including the per-postamat brute-force limit that a 5-digit PIN makes mandatory. Plan 3.
  `find_code` is the seam they call.
- **Cursor pagination** — deferred again, per Ruling P3.
