import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.db import write_transaction


async def test_pragmas_are_applied(file_engine):
    async with file_engine.connect() as conn:
        assert (await conn.scalar(text("PRAGMA foreign_keys"))) == 1
        assert (await conn.scalar(text("PRAGMA busy_timeout"))) == 5000
        assert (await conn.scalar(text("PRAGMA journal_mode"))) == "wal"


async def test_two_writers_serialise_instead_of_failing(file_engine):
    maker = async_sessionmaker(file_engine, expire_on_commit=False)
    order: list[str] = []

    async def writer(name: str, hold: float) -> None:
        async with maker() as session:
            async with write_transaction(session):
                order.append(f"{name}:start")
                await session.execute(
                    text("CREATE TABLE IF NOT EXISTS probe (n INTEGER)")
                )
                await session.execute(text("INSERT INTO probe (n) VALUES (1)"))
                await asyncio.sleep(hold)
                order.append(f"{name}:end")

    await asyncio.gather(writer("a", 0.05), writer("b", 0.0))

    # Whichever went first finished before the other started: the write lock is
    # held for the whole transaction, so the two never interleave.
    assert order[1].endswith(":end")
    assert order[0].split(":")[0] == order[1].split(":")[0]
