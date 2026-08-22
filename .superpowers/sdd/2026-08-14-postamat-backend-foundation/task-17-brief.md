### Task 17: Rate limiting on the public surface

**Files:**
- Create: `backend/app/core/ratelimit.py`, `backend/tests/core/test_ratelimit.py`
- Modify: `backend/app/api/public/catalog.py`, `backend/app/api/mobile/auth.py`

**Interfaces:**
- Produces: `rate_limit(bucket, limit, window_seconds)` dependency factory over the core key-value store, raising `RATE_LIMITED` with `retry_after_seconds` in `details`.

The public reads are the only unauthenticated surface, and OTP requests cost money per message.

- [ ] **Step 1: Write the failing test**

```python
async def test_repeated_otp_requests_are_limited(client):
    payload = {"phone": "+99362123456"}
    for _ in range(3):
        assert (await client.post("/api/v1/auth/otp/request", json=payload)).status_code == 200

    blocked = await client.post("/api/v1/auth/otp/request", json=payload)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"
    assert blocked.json()["error"]["details"]["retry_after_seconds"] > 0
```

- [ ] **Step 2: Run it and confirm it fails**

- [ ] **Step 3: Write `backend/app/core/ratelimit.py`**

```python
from fastapi import Request

from app.core.errors import AppError, ErrorCode
from app.core.kvstore import get_kvstore


def rate_limit(bucket: str, limit: int, window_seconds: int):
    async def dependency(request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        key = f"rl:{bucket}:{client_ip}"

        current, ttl = await get_kvstore().increment(key, window_seconds)
        if current > limit:
            raise AppError(
                ErrorCode.RATE_LIMITED, "Too many requests.", 429,
                details={"retry_after_seconds": max(ttl, 1)},
            )

    return dependency
```

- [ ] **Step 4: Apply it to the OTP route and the public catalog routes**

```python
from app.core.ratelimit import rate_limit

@router.post("/auth/otp/request", response_model=OtpRequestResult,
             dependencies=[Depends(rate_limit("otp", limit=3, window_seconds=600))])
```

Use `rate_limit("public", limit=120, window_seconds=60)` on `GET /cities` and `GET /cell-types`.

- [ ] **Step 5: Run the whole suite**

```bash
./.venv/Scripts/python.exe -m pytest -v
```

- [ ] **Step 6: Commit**

```bash
git add <only the files this task created or modified, listed explicitly> && git commit -m "feat: IP rate limiting on the public and OTP surface"
```

---

## Definition of done for this plan

- `./.venv/Scripts/python.exe -m uvicorn app.main:app` answers on `http://localhost:8000/api/v1/health`.
- `./.venv/Scripts/python.exe -m pytest` is green against SQLite.
- The Alembic revisions exist and are ordered; `alembic upgrade head` is verified once Postgres
  credentials are configured — it is **not** claimed as passing before then.
- A client can obtain tokens by phone; an admin can log in and is refused by permission.
- Cities, cell types, postamats with opening hours, cells and the tariff matrix are all
  readable and writable through the documented paths.

## Deliberately not in this plan

- `GET /postamats/{id}/availability` — needs bookings (Plan 2).
- `POST /admin/auth/2fa` — the flow is not drawn, and its body depends on TOTP versus SMS.
- Admin CRUD for cities — cities are seeded by migration until a second region appears.
- Photo upload for postamats — the only `DELETE` in the system lands with it, in Plan 2.
- Everything on the device and kiosk channels — Plan 3, when the RS-485 hardware arrives.
