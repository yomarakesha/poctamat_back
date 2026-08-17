import uuid

import pytest


@pytest.fixture
def book(client, client_token):
    """POST a booking with a fresh Idempotency-Key.

    The endpoint requires the header, and every call needs its own value —
    reusing one is what makes the second request replay the first instead of
    allocating. Tests that are about the header itself build their request by
    hand rather than going through this.
    """

    async def _book(body, *, token=None, key=None):
        return await client.post("/api/v1/bookings", json=body, headers={
            "Authorization": f"Bearer {token or client_token}",
            "Idempotency-Key": key or str(uuid.uuid4()),
        })

    return _book
