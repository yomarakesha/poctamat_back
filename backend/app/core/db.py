import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import DateTime, event, func
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


TXN_MODE_OPTION = "sqlite_txn"


def _apply_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    # isolation_level=None first: pysqlite otherwise opens a transaction of its
    # own choosing before anything else runs, which both hides the BEGIN from
    # SQLAlchemy and makes `PRAGMA journal_mode=WAL` fail as a statement inside
    # a transaction.
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    # WAL lets readers run while a writer holds the lock; busy_timeout turns a
    # lock conflict into a short wait instead of an immediate failure; foreign
    # keys are off by default in SQLite and would silently accept orphan rows.
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _emit_begin(conn) -> None:
    # SQLAlchemy no longer gets a BEGIN for free now that the driver is in
    # autocommit, so it is issued here — DEFERRED by default, IMMEDIATE when the
    # caller asked for the write lock up front.
    mode = conn.get_execution_options().get(TXN_MODE_OPTION, "DEFERRED")
    conn.exec_driver_sql(f"BEGIN {mode}")


def install_sqlite_pragmas(target) -> None:
    """Attach the pragma and BEGIN listeners to an engine. Idempotent."""
    sync_engine = getattr(target, "sync_engine", target)
    if sync_engine.dialect.name != "sqlite":
        return
    if not event.contains(sync_engine, "connect", _apply_sqlite_pragmas):
        event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    if not event.contains(sync_engine, "begin", _emit_begin):
        event.listen(sync_engine, "begin", _emit_begin)


@asynccontextmanager
async def write_transaction(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Run a block inside a transaction that takes the write lock up front.

    SQLite allows one writer at a time. A transaction that starts deferred and
    only later tries to write can find the lock already taken after it has read
    — and then it must fail, because its snapshot may be stale. BEGIN IMMEDIATE
    asks for the lock first, so a conflict costs a wait rather than a failure.

    Enter this before the session has issued any query: the execution option is
    read when the transaction begins, and by the first query it has begun.
    """
    await session.connection(execution_options={TXN_MODE_OPTION: "IMMEDIATE"})
    try:
        yield session
    except BaseException:
        await session.rollback()
        raise
    else:
        await session.commit()


engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
install_sqlite_pragmas(engine)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
