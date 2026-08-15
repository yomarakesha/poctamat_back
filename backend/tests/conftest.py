import os

import pytest
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import BACKEND_DIR
from app.core.db import Base, get_session
from app.main import create_app

# Anchored to the backend directory so the suite runs from anywhere, not only
# from backend/. A real environment variable still wins, which is how CI will
# supply this once there is no .env on disk.
load_dotenv(BACKEND_DIR / ".env")

TEST_DATABASE_URL = os.environ["TEST_DATABASE_URL"]


@pytest.fixture(scope="session")
def test_engine():
    return create_async_engine(
        TEST_DATABASE_URL,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


@pytest.fixture(autouse=True)
async def schema(test_engine):
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture(autouse=True)
def reset_kvstore():
    from app.core.kvstore import get_kvstore

    get_kvstore().clear()
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
