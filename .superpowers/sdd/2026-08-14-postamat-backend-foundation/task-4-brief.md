### Task 4: Error envelope

**Files:**
- Create: `backend/app/core/errors.py`, `backend/tests/core/test_errors.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces: `ErrorCode` (str enum), `AppError(code, message, status_code, details=None)`, `install_error_handlers(app)`.

The full 102-code enum lives in the frontend's `contracts/openapi.yaml`, which has not arrived yet. Define the codes this plan needs; extend the enum from the YAML when it lands. Adding a code is not a breaking change.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from fastapi import APIRouter

from app.core.errors import AppError, ErrorCode


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
```

The second test is expected to stay red until Task 11 adds the route; mark it
`@pytest.mark.xfail(reason="route arrives in task 11", strict=False)` and remove the marker there.

- [ ] **Step 2: Run it and confirm it fails**

```bash
docker compose run --rm api pytest tests/core/test_errors.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.errors'`.

- [ ] **Step 3: Write `backend/app/core/errors.py`**

```python
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorCode(StrEnum):
    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    PERMISSION_DENIED = "PERMISSION_DENIED"

    OTP_INVALID = "OTP_INVALID"
    OTP_EXPIRED = "OTP_EXPIRED"
    OTP_TOO_MANY_ATTEMPTS = "OTP_TOO_MANY_ATTEMPTS"
    OTP_REQUEST_TOO_SOON = "OTP_REQUEST_TOO_SOON"

    CLIENT_BLOCKED = "CLIENT_BLOCKED"
    ADMIN_INACTIVE = "ADMIN_INACTIVE"
    CREDENTIALS_INVALID = "CREDENTIALS_INVALID"

    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"

    TARIFF_INCOMPLETE = "TARIFF_INCOMPLETE"
    POSTAMAT_BLOCKED = "POSTAMAT_BLOCKED"
    CELL_NUMBER_TAKEN = "CELL_NUMBER_TAKEN"
    CITY_IN_USE = "CITY_IN_USE"


class AppError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


def _envelope(request: Request, code: str, message: str,
              details: dict[str, Any] | None, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "trace_id": getattr(request.state, "trace_id", None),
            }
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return _envelope(request, exc.code, exc.message, exc.details, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {"field": ".".join(str(p) for p in err["loc"][1:]), "reason": err["msg"]}
            for err in exc.errors()
        ]
        return _envelope(request, ErrorCode.VALIDATION_FAILED,
                         "Request body failed validation.", {"fields": fields}, 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.INTERNAL_ERROR
        return _envelope(request, code, str(exc.detail), None, exc.status_code)
```

- [ ] **Step 4: Register the handlers in `create_app()`**

```python
from app.core.errors import install_error_handlers

def create_app() -> FastAPI:
    app = FastAPI(title="Postamat API", version="1.0.0")
    install_error_handlers(app)
    app.include_router(health_router, prefix=API_PREFIX)
    return app
```

- [ ] **Step 5: Run the test and confirm the first case passes**

```bash
docker compose run --rm api pytest tests/core/test_errors.py -v
```

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: single error envelope with machine codes"
```

---

