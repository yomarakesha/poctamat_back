import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient

from app.core.errors import AppError, ErrorCode
from app.main import create_app


@pytest.fixture
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


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
