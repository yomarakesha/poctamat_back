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
def reset_sms():
    # Rebuilt per test so one test's outbox is never another's, and so a stray
    # SMS_PROVIDER in the environment cannot leave a real gateway cached.
    from app.modules.notify.sms import reset_sms_provider

    reset_sms_provider()
    yield
    reset_sms_provider()


@pytest.fixture(autouse=True)
def reset_kvstore():
    from app.core.kvstore import get_kvstore

    get_kvstore().clear()
    yield


@pytest.fixture
def count_queries(test_engine):
    """Every statement the engine issues while the fixture is live.

    Used by the tests that pin how many round trips a screen costs: a tile that
    costs a query per row is what turns a dashboard into sixty of them.
    """
    from sqlalchemy import event

    seen: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", before)
    yield seen
    event.remove(test_engine.sync_engine, "before_cursor_execute", before)


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
        "bookings.read", "bookings.write",
        "custody.read", "custody.write",
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
                   width_mm=200, height_mm=200, depth_mm=400)
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

    row = Client(phone="+99361000001", last_name="Отправителев",
                 first_name="Мырат")
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


@pytest.fixture
def book(client, client_token):
    """POST a booking with a fresh Idempotency-Key.

    The endpoint requires the header, and every call needs its own value —
    reusing one is what makes the second request replay the first instead of
    allocating. Tests that are about the header itself build their request by
    hand rather than going through this.
    """
    import uuid as _uuid

    async def _book(body, *, token=None, key=None):
        return await client.post("/api/v1/bookings", json=body, headers={
            "Authorization": f"Bearer {token or client_token}",
            "Idempotency-Key": key or str(_uuid.uuid4()),
        })

    return _book


@pytest.fixture
def post_action(client, client_token):
    """POST one of the booking's state-changing actions, with a fresh key.

    Cancel, extend-hold and both rotations all require `Idempotency-Key`: they
    move a booking or mint a code, and a retried request must not do it twice.
    """
    import uuid as _uuid

    async def _post(booking_id, action, body=None, *, token=None, key=None):
        return await client.post(
            f"/api/v1/bookings/{booking_id}/{action}", json=body,
            headers={
                "Authorization": f"Bearer {token or client_token}",
                "Idempotency-Key": key or str(_uuid.uuid4()),
            },
        )

    return _post


@pytest.fixture
def cancel(post_action):
    async def _cancel(booking_id, reason="передумал", **kwargs):
        return await post_action(booking_id, "cancel", {"reason": reason}, **kwargs)

    return _cancel
