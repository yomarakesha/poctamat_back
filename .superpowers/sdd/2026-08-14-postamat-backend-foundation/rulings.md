# Controller rulings that override the plan text

These were decided before execution began, after a conflict scan across the whole plan. Where a
ruling contradicts your task brief, the ruling wins. Each says which tasks it binds.

## R1 — test fixtures and seeded permissions (binds Tasks 14, 15, 16)

`backend/tests/conftest.py` gains four fixtures, created in Task 14 and reused afterwards:

- `city` — a persisted `City` (code `ashgabat`, names `Aşgabat` / `Ашхабад` / `Ashgabat`).
- `cell_type` — a persisted `CellType` (code `small`, names `Kiçi` / `Маленький` / `Small`,
  20 × 20 × 40 cm).
- `postamat` — a persisted `Postamat` in `city`, number `10042`, name `ТП #4`, address
  `ул. Ататюрк, 31`, `round_the_clock=False`.
- `admin_token` — an access token for a seeded `AdminUser` whose `Role` carries the permission
  list `["postamats.read", "postamats.write", "cells.read", "cells.write", "tariffs.read",
  "tariffs.write", "roles.read"]`.

Reason: Tasks 15 and 16 consume fixtures and permissions that no task in the plan creates, so they
would fail on a missing fixture or a 403.

## R2 — SUPERSEDED by R6

The original R2 moved the Redis client into `core`. Redis is gone from this plan entirely; see R6.
The layering point it made still holds: `core` never imports from `modules`.

## R6 — no Redis; an in-process key-value store in core (binds Tasks 2, 11, 17)

Redis is not installed on this machine and the human partner has decided not to add it yet. The
plan's Redis usage is two things — one-time codes and rate-limit counters — and both are
short-lived keyed values with a TTL. They move behind an interface.

`Settings` loses `redis_url` entirely (Task 2).

Task 11 creates `backend/app/core/kvstore.py`:

```python
import time
from dataclasses import dataclass


@dataclass
class _Entry:
    value: dict[str, str]
    expires_at: float


class KeyValueStore:
    """Short-lived keyed values with a TTL.

    In-process stand-in for Redis: correct for a single worker, and the only
    implementation this deployment needs until Redis is introduced. Swapping in a
    Redis-backed implementation means replacing this class, not its callers.
    """

    def __init__(self) -> None:
        self._data: dict[str, _Entry] = {}

    def _live(self, key: str) -> _Entry | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            del self._data[key]
            return None
        return entry

    async def put(self, key: str, value: dict[str, str], ttl_seconds: int) -> None:
        self._data[key] = _Entry(dict(value), time.monotonic() + ttl_seconds)

    async def get(self, key: str) -> dict[str, str] | None:
        entry = self._live(key)
        return dict(entry.value) if entry else None

    async def set_field(self, key: str, field: str, value: str) -> None:
        entry = self._live(key)
        if entry is not None:
            entry.value[field] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def increment(self, key: str, ttl_seconds: int) -> tuple[int, int]:
        """Return the new counter value and the seconds left in its window."""
        entry = self._live(key)
        if entry is None:
            self._data[key] = _Entry({"n": "1"}, time.monotonic() + ttl_seconds)
            return 1, ttl_seconds
        entry.value["n"] = str(int(entry.value["n"]) + 1)
        return int(entry.value["n"]), max(1, int(entry.expires_at - time.monotonic()))

    def clear(self) -> None:
        self._data.clear()


_store = KeyValueStore()


def get_kvstore() -> KeyValueStore:
    return _store
```

Task 11 also adds an autouse fixture to `backend/tests/conftest.py` so state does not leak between
tests:

```python
@pytest.fixture(autouse=True)
def reset_kvstore():
    from app.core.kvstore import get_kvstore

    get_kvstore().clear()
    yield
```

Note in the module docstring that this is single-worker only — with more than one uvicorn worker
the counters diverge. That is acceptable now and is the reason Redis returns later.

## R7 — SQLite for now, so keep the models portable (binds every task with a model)

`DATABASE_URL` is a SQLite file and `TEST_DATABASE_URL` is in-memory SQLite until Postgres
credentials exist. Therefore:

- use `JSON`, never `JSONB`;
- no server-side defaults beyond `func.now()`;
- no Postgres-only SQL anywhere in this plan;
- never claim `alembic upgrade head` passes on Postgres — it has not been run there. Say it ran on
  SQLite, because that is what happened.

Plan 2 needs `FOR UPDATE SKIP LOCKED` and cannot be started until the real database is up.

## R3 — no cursor pagination in this plan (binds Task 7)

Task 7 ships only `PageMeta`, `page_meta`, `PageParams`, `page_params` and `paginate_page`. Omit
`CursorMeta`, `CursorParams`, `cursor_params`, `encode_cursor` and `decode_cursor` entirely,
including their imports (`base64`).

Reason: nothing in this plan paginates a growing list. Cursors arrive in Plan 2 beside the audit
log and the notification feed, which are their first consumers.

## R4 — no `Money` model in this plan (binds Task 6)

Task 6 ships `PhoneNumber` and `utc_isoformat` only. Omit the `Money` model and its test, and drop
`BaseModel` and `Field` from the imports.

Reason: tariffs carry `amount_minor` and `currency` as explicit fields on their own schema, so the
money constraint is already satisfied on the wire. `Money` has no consumer until payments in
Plan 2.

## R5 — `utc_isoformat` gets its consumer in Task 14 (binds Tasks 6, 14)

Keep `utc_isoformat` in Task 6 with its test. In Task 14, `PostamatOut` carries an extra field:

```python
created_at: str
```

built with `utc_isoformat(postamat.created_at)` wherever `PostamatOut` is constructed. Because the
field is computed, build `PostamatOut` explicitly rather than through
`model_validate(..., from_attributes=True)`, and assert in the Task 14 API test that the value
ends with `Z`.

Reason: Pydantic renders aware datetimes as `+00:00`, not the `Z` the global constraints require,
so the helper is load-bearing — but only once a response actually returns a timestamp.
