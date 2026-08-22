### Task 11: Client authentication by OTP

**Files:**
- Create: `backend/app/core/security.py`, `backend/app/modules/identity/schemas.py`, `backend/app/modules/identity/service.py`, `backend/app/api/mobile/auth.py`, `backend/tests/identity/test_client_auth.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces: `hash_secret(value) -> str`, `verify_secret(value, hashed) -> bool`, `create_access_token(subject_type, subject_id, extra) -> str`, `decode_token(token) -> dict`, `require_client` dependency returning a `Client`.
- Endpoints: `POST /api/v1/auth/otp/request`, `POST /api/v1/auth/otp/verify`, `POST /api/v1/auth/refresh`, `POST /api/v1/auth/logout`.

The OTP is 6 digits, lives in the key-value store under `otp:{phone}` with a TTL from settings, and carries an attempt counter. It is never returned in an API response — tests read it through `peek_otp`, which exists for exactly that purpose and is not exposed by any route.

- [ ] **Step 1: Write the failing test**

```python
from app.modules.identity.service import peek_otp


async def test_otp_request_then_verify_issues_tokens(client):
    requested = await client.post("/api/v1/auth/otp/request",
                                  json={"phone": "+99362123456"})
    assert requested.status_code == 200
    assert requested.json()["code_length"] == 6

    code = await peek_otp("+99362123456")
    verified = await client.post("/api/v1/auth/otp/verify",
                                 json={"phone": "+99362123456", "code": code})
    assert verified.status_code == 200
    body = verified.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["is_new_client"] is True


async def test_wrong_code_is_rejected(client):
    await client.post("/api/v1/auth/otp/request", json={"phone": "+99362123456"})
    response = await client.post("/api/v1/auth/otp/verify",
                                 json={"phone": "+99362123456", "code": "000000"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OTP_INVALID"
    assert response.json()["error"]["details"]["attempts_left"] == 4


async def test_malformed_phone_is_rejected(client):
    response = await client.post("/api/v1/auth/otp/request", json={"phone": "+7999123456"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
```

Remove the `xfail` marker from `tests/core/test_errors.py::test_validation_error_renders_envelope` in this task.

- [ ] **Step 2: Run it and confirm it fails**

- [ ] **Step 3: Write `backend/app/core/security.py`**

```python
import hashlib
import hmac
import uuid
from datetime import timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import get_settings
from app.core.db import utcnow

_hasher = PasswordHasher()


def hash_secret(value: str) -> str:
    return _hasher.hash(value)


def verify_secret(value: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, value)
    except VerifyMismatchError:
        return False


def hash_pin(pin: str) -> str:
    settings = get_settings()
    return hmac.new(settings.pin_pepper.encode(), pin.encode(), hashlib.sha256).hexdigest()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_access_token(subject_type: str, subject_id: uuid.UUID,
                        extra: dict | None = None) -> str:
    settings = get_settings()
    payload = {
        "sub": str(subject_id),
        "typ": subject_type,
        "exp": utcnow() + timedelta(minutes=settings.access_token_ttl_minutes),
        "iat": utcnow(),
        **(extra or {}),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
```

`hash_pin` is defined here now because the pepper lives in settings; Plan 2 consumes it for access codes.

- [ ] **Step 4: Write `backend/app/modules/identity/schemas.py`**

```python
from pydantic import BaseModel

from app.core.types import PhoneNumber


class OtpRequest(BaseModel):
    phone: PhoneNumber


class OtpRequestResult(BaseModel):
    code_length: int
    expires_in_seconds: int


class OtpVerify(BaseModel):
    phone: PhoneNumber
    code: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    is_new_client: bool = False


class RefreshRequest(BaseModel):
    refresh_token: str
```

- [ ] **Step 5: Write `backend/app/modules/identity/service.py`**

```python
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import utcnow
from app.core.errors import AppError, ErrorCode
from app.core.kvstore import get_kvstore
from app.core.security import create_access_token, hash_token
from app.modules.identity.models import Client, RefreshToken


def _otp_key(phone: str) -> str:
    return f"otp:{phone}"


async def issue_otp(phone: str) -> int:
    settings = get_settings()
    code = "".join(secrets.choice("0123456789") for _ in range(settings.otp_length))
    await get_kvstore().put(
        _otp_key(phone), {"code": code, "attempts": "0"}, settings.otp_ttl_seconds
    )
    return settings.otp_ttl_seconds


async def peek_otp(phone: str) -> str | None:
    stored = await get_kvstore().get(_otp_key(phone))
    return stored["code"] if stored else None


async def verify_otp(phone: str, code: str) -> None:
    settings = get_settings()
    store = get_kvstore()
    stored = await store.get(_otp_key(phone))
    if not stored:
        raise AppError(ErrorCode.OTP_EXPIRED, "No active code for this number.", 400)

    attempts = int(stored["attempts"]) + 1
    if attempts >= settings.otp_max_attempts:
        await store.delete(_otp_key(phone))
        raise AppError(ErrorCode.OTP_TOO_MANY_ATTEMPTS, "Too many attempts.", 429)

    if not secrets.compare_digest(stored["code"], code):
        await store.set_field(_otp_key(phone), "attempts", str(attempts))
        raise AppError(
            ErrorCode.OTP_INVALID, "Wrong code.", 400,
            details={"attempts_left": settings.otp_max_attempts - attempts},
        )

    await store.delete(_otp_key(phone))


async def get_or_create_client(session: AsyncSession, phone: str) -> tuple[Client, bool]:
    existing = await session.scalar(select(Client).where(Client.phone == phone))
    if existing:
        if existing.is_blocked:
            raise AppError(ErrorCode.CLIENT_BLOCKED, "Client is blocked.", 403)
        return existing, False

    created = Client(phone=phone)
    session.add(created)
    await session.flush()
    return created, True


async def issue_token_pair(
    session: AsyncSession, subject_type: str, subject_id: uuid.UUID, extra: dict | None = None
) -> tuple[str, str]:
    settings = get_settings()
    access = create_access_token(subject_type, subject_id, extra)
    refresh = secrets.token_urlsafe(48)
    session.add(
        RefreshToken(
            token_hash=hash_token(refresh),
            subject_type=subject_type,
            subject_id=subject_id,
            expires_at=utcnow() + timedelta(days=settings.refresh_token_ttl_days),
        )
    )
    await session.flush()
    return access, refresh
```

- [ ] **Step 6: Write `backend/app/api/mobile/auth.py`**

```python
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.modules.identity import service
from app.modules.identity.schemas import (
    OtpRequest,
    OtpRequestResult,
    OtpVerify,
    TokenPair,
)

router = APIRouter(tags=["auth"])


@router.post("/auth/otp/request", response_model=OtpRequestResult)
async def request_otp(payload: OtpRequest) -> OtpRequestResult:
    ttl = await service.issue_otp(payload.phone)
    return OtpRequestResult(code_length=6, expires_in_seconds=ttl)


@router.post("/auth/otp/verify", response_model=TokenPair)
async def verify_otp(
    payload: OtpVerify, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    await service.verify_otp(payload.phone, payload.code)
    client, is_new = await service.get_or_create_client(session, payload.phone)
    access, refresh = await service.issue_token_pair(session, "client", client.id)
    await session.commit()
    return TokenPair(access_token=access, refresh_token=refresh, is_new_client=is_new)
```

- [ ] **Step 7: Register the router in `create_app()`**

```python
from app.api.mobile import auth as mobile_auth

app.include_router(mobile_auth.router, prefix=API_PREFIX)
```

- [ ] **Step 8: Run the tests and confirm they pass**

```bash
./.venv/Scripts/python.exe -m pytest tests/identity tests/core -v
```

- [ ] **Step 9: Commit**

```bash
git add <only the files this task created or modified, listed explicitly> && git commit -m "feat: client authentication by one-time code"
```

---

