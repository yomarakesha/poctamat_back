### Task 5: Request context — trace id and language

**Files:**
- Create: `backend/app/core/context.py`, `backend/tests/core/test_context.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces: `RequestContextMiddleware`, and `app.core.context.get_language(request) -> str`.

- [ ] **Step 1: Write the failing test**

```python
async def test_trace_id_is_returned_in_header(client):
    response = await client.get("/api/v1/health")
    assert response.headers["x-trace-id"]


async def test_trace_id_is_echoed_when_supplied(client):
    response = await client.get("/api/v1/health", headers={"X-Trace-Id": "abc-123"})
    assert response.headers["x-trace-id"] == "abc-123"
```

- [ ] **Step 2: Run it and confirm it fails**

```bash
docker compose run --rm api pytest tests/core/test_context.py -v
```

Expected: FAIL with `KeyError: 'x-trace-id'`.

- [ ] **Step 3: Write `backend/app/core/context.py`**

```python
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        request.state.trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())

        header = request.headers.get("Accept-Language", "")
        primary = header.split(",")[0].split("-")[0].strip().lower()
        request.state.language = (
            primary if primary in settings.supported_languages else settings.default_language
        )

        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response


def get_language(request: Request) -> str:
    return getattr(request.state, "language", get_settings().default_language)
```

- [ ] **Step 4: Add the middleware in `create_app()`, before the routers**

```python
from app.core.context import RequestContextMiddleware

app.add_middleware(RequestContextMiddleware)
```

- [ ] **Step 5: Run the test and confirm it passes**

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: trace id and language negotiation middleware"
```

---

