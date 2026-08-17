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
    from app.core.db import install_sqlite_pragmas

    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    # The suite must run under the same pragmas production does, or a locking
    # bug only shows up outside the tests.
    install_sqlite_pragmas(engine)
    return engine


@pytest.fixture
async def file_engine(tmp_path):
    """A throwaway file database with its own connection pool.

    `test_engine` holds a single shared in-memory connection (StaticPool), so
    two sessions on it are the same connection and cannot contend for a lock at
    all. Anything testing what concurrent writers do to each other needs real
    separate connections, which means a file.
    """
    from app.core.db import install_sqlite_pragmas

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/probe.db")
    install_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


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
async def admin_user(session):
    from app.core.security import hash_secret
    from app.modules.identity.models import AdminUser, Role

    role = Role(code="operator", name="Operator", permissions=[
        "postamats.read", "postamats.write", "cells.read", "cells.write",
        "tariffs.read", "tariffs.write", "roles.read",
    ])
    session.add(role)
    await session.flush()
    admin = AdminUser(login="admin_test", full_name="Тестов Т.Т.",
                      password_hash=hash_secret("secret123"), role_id=role.id)
    session.add(admin)
    await session.commit()
    # The router reads admin.role.permissions. The instance this fixture just
    # created lives in the same identity map the request will hit, so it comes
    # back without its joined role unless it is refreshed here — and a lazy load
    # inside async code raises MissingGreenlet rather than loading.
    await session.refresh(admin)
    return admin


@pytest.fixture
def admin_token(admin_user):
    # Minted directly rather than through the login endpoint: a fixture that
    # logs in makes every admin test depend on the login route staying green.
    from app.core.security import create_access_token

    return create_access_token("admin", admin_user.id)


@pytest.fixture
async def city(session):
    from app.modules.catalog.models import City

    row = City(code="ashgabat", name_tk="Aşgabat", name_ru="Ашхабад",
               name_en="Ashgabat")
    session.add(row)
    await session.commit()
    return row


@pytest.fixture
async def cell_type(session):
    from app.modules.catalog.models import CellType

    row = CellType(code="small", name_tk="Kiçi", name_ru="Маленький", name_en="Small",
                   width_cm=20, height_cm=20, depth_cm=40)
    session.add(row)
    await session.commit()
    return row


@pytest.fixture
async def postamat(session, city):
    from app.modules.catalog.models import Postamat

    row = Postamat(number="10001", name="ТП #1", city_id=city.id,
                   address="ул. Ататюрк, 31")
    session.add(row)
    await session.commit()
    # Same reason as admin_user: the request reuses this session, so the row is
    # served from the identity map and its schedule collection has to be loaded
    # here rather than lazily inside the request.
    await session.refresh(row)
    return row


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
    # Minted directly rather than through the OTP flow: a fixture that logs in
    # makes every booking test depend on the login routes staying green.
    from app.core.security import create_access_token

    return create_access_token("client", booking_client.id)


@pytest.fixture
async def client(session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
