import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_trace_id_is_returned_in_header(client):
    response = await client.get("/api/v1/health")
    assert response.headers["x-trace-id"]


async def test_trace_id_is_echoed_when_supplied(client):
    response = await client.get("/api/v1/health", headers={"X-Trace-Id": "abc-123"})
    assert response.headers["x-trace-id"] == "abc-123"
