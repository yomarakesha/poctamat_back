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


@pytest.fixture
async def failing_client(client):
    router = APIRouter()
    state = {"calls": 0}

    @router.post("/boom")
    async def boom(payload: dict):
        state["calls"] += 1
        raise RuntimeError("boom")

    client._transport.app.include_router(router, prefix="/api/v1")
    client.state = state
    return client


async def test_5xx_response_is_never_memoized(failing_client):
    key = str(uuid.uuid4())
    headers = {"Idempotency-Key": key}
    from httpx import ASGITransport

    transport = failing_client._transport
    assert isinstance(transport, ASGITransport)
    transport.raise_app_exceptions = False

    first = await failing_client.post("/api/v1/boom", json={"a": 1}, headers=headers)
    second = await failing_client.post("/api/v1/boom", json={"a": 1}, headers=headers)

    assert first.status_code == 500
    assert second.status_code == 500
    assert failing_client.state["calls"] == 2


@pytest.fixture
async def owner_client(client):
    from starlette.middleware.base import BaseHTTPMiddleware

    router = APIRouter()
    state = {"calls": 0}

    @router.post("/owner-count")
    async def owner_count(payload: dict):
        state["calls"] += 1
        return {"calls": state["calls"], "echo": payload}

    class FakeSubjectMiddleware(BaseHTTPMiddleware):
        """Stand-in for the Task 12 auth middleware: lets the test pick an
        owner via a header instead of a real authenticated subject."""

        async def dispatch(self, request, call_next):
            subject = request.headers.get("X-Test-Subject")
            if subject:
                request.state.subject_id = subject
            return await call_next(request)

    app = client._transport.app
    app.include_router(router, prefix="/api/v1")
    app.add_middleware(FakeSubjectMiddleware)
    client.state = state
    return client


async def test_same_key_and_body_different_owners_both_execute(owner_client):
    key = str(uuid.uuid4())
    body = {"a": 1}

    first = await owner_client.post(
        "/api/v1/owner-count",
        json=body,
        headers={"Idempotency-Key": key, "X-Test-Subject": "user-a"},
    )
    second = await owner_client.post(
        "/api/v1/owner-count",
        json=body,
        headers={"Idempotency-Key": key, "X-Test-Subject": "user-b"},
    )

    assert first.json()["calls"] == 1
    assert second.json()["calls"] == 2
    assert owner_client.state["calls"] == 2
