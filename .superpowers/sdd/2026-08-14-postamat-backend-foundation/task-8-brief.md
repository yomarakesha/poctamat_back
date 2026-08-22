### Task 8: Idempotency

**Files:**
- Create: `backend/app/core/idempotency.py`, `backend/tests/core/test_idempotency.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces: model `IdempotencyRecord`, and `IdempotencyMiddleware` which memoizes `2xx` and `4xx` responses for POST requests carrying an `Idempotency-Key` header, keyed by `(key, method, path, subject)`.

`5xx` responses are never stored — they must stay retryable.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it and confirm it fails**

- [ ] **Step 3: Write `backend/app/core/idempotency.py`**

```python
import hashlib
import json

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import String, select
from sqlalchemy.orm import Mapped, mapped_column
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.db import Base, Timestamped, UUIDPrimaryKey, session_scope
from app.core.errors import ErrorCode


class IdempotencyRecord(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(128), index=True)
    scope: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int]
    response_body: Mapped[str] = mapped_column(String)


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        key = request.headers.get("Idempotency-Key")
        if request.method != "POST" or not key:
            return await call_next(request)

        body = await request.body()
        request_hash = hashlib.sha256(body).hexdigest()
        scope = f"{request.method} {request.url.path}"

        async with session_scope() as session:
            existing = await session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.key == key, IdempotencyRecord.scope == scope
                )
            )
            if existing and existing.request_hash != request_hash:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": {
                            "code": ErrorCode.IDEMPOTENCY_KEY_REUSED,
                            "message": "This Idempotency-Key was used with a different body.",
                            "details": None,
                            "trace_id": getattr(request.state, "trace_id", None),
                        }
                    },
                )
            if existing:
                return JSONResponse(
                    status_code=existing.status_code,
                    content=json.loads(existing.response_body),
                )

        response = await call_next(request)
        if response.status_code >= 500:
            return response

        chunks = [chunk async for chunk in response.body_iterator]
        payload = b"".join(chunks)

        async with session_scope() as session:
            session.add(
                IdempotencyRecord(
                    key=key,
                    scope=scope,
                    request_hash=request_hash,
                    status_code=response.status_code,
                    response_body=payload.decode(),
                )
            )
            await session.commit()

        return JSONResponse(
            status_code=response.status_code, content=json.loads(payload.decode())
        )
```

- [ ] **Step 4: Register the middleware in `create_app()` — before `RequestContextMiddleware`**

Starlette runs the last-added middleware outermost, so `add_middleware(IdempotencyMiddleware)`
must come first and `add_middleware(RequestContextMiddleware)` second. Otherwise idempotency
replies are produced before `trace_id` exists and their envelopes carry `null`.

```python
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(RequestContextMiddleware)
```

- [ ] **Step 5: Run the test and confirm it passes**

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: idempotency key replay and conflict detection"
```

---

