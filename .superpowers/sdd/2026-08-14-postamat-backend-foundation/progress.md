# SDD ledger — plan: docs/superpowers/plans/2026-08-14-postamat-backend-foundation.md

Spec: docs/superpowers/specs/2026-08-14-postamat-backend-design.md (read, as amended by the
contract reply of 2026-08-14). Branch: feat/backend-foundation. Base commit: eae2fc4.

## Pre-flight conflict scan

### Cross-task pairs sharing a file or an interface

| Tasks | Produces → consumes | Finding |
|---|---|---|
| 1 → 3 | `tests/core/test_health.py` defines a `client` fixture; conftest defines another | Plan already instructs Task 3 to delete the duplicate. Clean. |
| 1 → 4, 5, 8, 11, 12, 13 | `create_app()` in `main.py` is modified by six later tasks | Sequential edits to distinct lines. Clean. |
| 3 → 10, 13, 14, 15, 16 | `alembic/env.py` imports `app.modules.{audit,catalog,identity}` | Task 3 creates the packages empty; models arrive later. Autogenerate at Task 10 picks them up. Clean. |
| 4 → 5 | `_envelope` reads `request.state.trace_id`, set by Task 5's middleware | Task 4 ships before Task 5 but uses `getattr(..., None)`. Degrades, never raises. Clean. |
| 4 → 11 | `test_errors.py` calls `/auth/otp/request`, created in Task 11 | Plan marks it `xfail` and removes the marker in Task 11. Clean. |
| 5 → 8 | Middleware nesting order | Was wrong in the first draft; corrected before execution — idempotency registers first so context stays outermost. Clean. |
| 6 → 11 | `PhoneNumber` → identity schemas | Clean. |
| 6 → — | `Money`, `utc_isoformat` | **No consumer in this plan.** See rulings R4, R5. |
| 7 → 14 | `page_params`, `paginate_page`, `PageMeta` → postamat list | Clean. |
| 7 → — | `CursorParams`, `cursor_params`, `encode_cursor`, `decode_cursor`, `CursorMeta` | **No consumer in this plan.** See ruling R3. |
| 8 → 10 | `IdempotencyRecord` → first migration | Task 10's migration message names idempotency. Clean. |
| 9 → 14 | `audit.service.record(...)` → postamat create/block | Signatures match. Clean. |
| 10 → 11, 12 | `Client`, `AdminUser`, `Role`, `RefreshToken` → auth | Clean. |
| 11 → 12 | `issue_token_pair(session, subject_type, subject_id, extra)` | Called identically from both. Clean. |
| 11 → 17 | `identity.service.get_redis()` → `core.ratelimit` | **`core` importing a module inverts the layering.** See ruling R2. |
| 12 → 14, 15, 16 | `require_permission(code)` → admin routers | Clean. |
| 13 → 14, 16 | `City`, `CellType` → foreign keys | Clean. |
| 14 → 15 | `Postamat` → `Cell.postamat_id` | Clean. |
| 14 → 15, 16 | conftest fixtures `admin_token`, `city` | **Tasks 15 and 16 also need `postamat` and `cell_type`, which no task creates; the seeded permission set is missing `cells.read` and `tariffs.read`.** See ruling R1. |

### Per-task self-consistency

| Task | Finding |
|---|---|
| 1 | Test matches the route it specifies. Step 1 (`git init`) done during setup. Clean. |
| 2 | Test asserts exactly the constants the code defines. Clean. |
| 3 | Files created match files referenced. Clean. |
| 4 | Enum members cover every code raised across tasks 11–16. Clean. |
| 5 | Test matches middleware behaviour. Clean. |
| 6 | See R4, R5. |
| 7 | See R3. |
| 8 | Test exercises replay and conflict; code covers both. Clean. |
| 9 | Test matches the service signature. Clean. |
| 10 | Model fields match the test. Clean. |
| 11 | Tests match routes; `peek_otp` exists for the test's use. Clean. |
| 12 | Three tests, three code paths. Clean. |
| 13 | Localisation test matches `localized()`. Clean. |
| 14 | Unit tests build `Postamat` without a session, so the non-nullable `city_id` is not exercised. Intentional. Clean. |
| 15 | Unique constraint matches the 409 test. Clean. |
| 16 | Completeness check matches the `missing: [24, 48]` assertion. Clean. |
| 17 | Limit of 3 per 10 min matches the test's fourth call. Clean. |

## Rulings

Ruling R1 (Task 14, carried into 15 and 16): conftest gains four fixtures — `admin_token`,
`city`, `cell_type`, `postamat` — and the seeded role carries the full permission set
`["postamats.read", "postamats.write", "cells.read", "cells.write", "tariffs.read",
"tariffs.write", "roles.read"]`. Why: tasks 15 and 16 consume fixtures and permissions the plan
never creates, so both would fail on a missing fixture. Cost if wrong: test-only scaffolding,
trivially reshaped.

Ruling R2 (Tasks 11 and 17): the Redis client moves to `app/core/redis.py` exposing
`get_redis()`; `identity.service` and `core.ratelimit` both import from there. Why: the plan had
`core` importing `modules.identity`, which inverts the layering the spec's module rule exists to
protect. Cost if wrong: one import line per consumer.

Ruling R3 (Task 7): cursor helpers are dropped from this plan; Task 7 ships only `PageMeta`,
`page_meta`, `PageParams`, `page_params` and `paginate_page`. Why: nothing in this plan paginates
a growing list — the audit log and notification feeds that need cursors arrive in Plan 2, and
unused helpers are the exact shape reviewers reject as YAGNI. Cost if wrong: Plan 2 adds them
where their first consumer lives.

Ruling R4 (Task 6): `Money` is dropped from this plan. Why: tariffs carry `amount_minor` and
`currency` as explicit fields on their own schema, so the Global Constraint on money shape is
already satisfied on the wire; `Money` has no consumer until payments in Plan 2. Cost if wrong:
Plan 2 introduces it beside its first user.

Ruling R5 (Tasks 6 and 14): `utc_isoformat` stays, and Task 14's `PostamatOut` gains
`created_at: str` serialized through it. Why: Pydantic renders aware datetimes as `+00:00`, not
the `Z` the Global Constraints require, so the helper is load-bearing — but only once something
returns a timestamp. This gives it its first consumer and a test. Cost if wrong: one field on one
response model.

## Progress

Ruling R6 (Tasks 2, 11, 17): Redis is dropped from this plan. `Settings` loses `redis_url`; one-time
codes and rate-limit counters move behind `app/core/kvstore.py`, an in-process TTL store created in
Task 11. Why: the human partner decided against installing Redis for now, and both usages are
short-lived keyed values an interface can absorb. Cost if wrong: counters diverge across uvicorn
workers until a Redis-backed implementation replaces the class — callers do not change.

Ruling R7 (every task with a model): the suite and the dev database run on SQLite until Postgres
credentials exist, so models stay portable — `JSON` not `JSONB`, no server defaults beyond
`func.now()`, no Postgres-only SQL, and no claiming `alembic upgrade head` passed on Postgres.
Why: Postgres 18 is installed as a service but unconfigured, and Alembic needs a reachable database
to autogenerate against. Cost if wrong: migrations get re-verified against Postgres before Plan 2,
which needs it for FOR UPDATE SKIP LOCKED anyway.

Ruling R8 (Task 1, environment): no Docker. Python 3.14.4 on the host in `backend/.venv`. Verified
by installing each dependency with `--only-binary=:all:`: `asyncpg` needs 0.31+ and `pydantic`
needs 2.13+ (the plan's original 0.30 / 2.10.4 pins have no 3.14 wheels), `psycopg` has no 3.14
wheel at all, and `uvicorn[standard]` must not be used because `uvloop` does not build on Windows.
requirements.txt therefore carries version floors rather than exact pins. Cost if wrong: a pin
adjustment.

Task 1: complete — 170e55e. Done by the controller, not a subagent: two background implementers
in a row were lost when the Claude Code process exited, and Task 1 is environment setup whose
slow part is `pip install`. Docker files and the stale `tests/conftest.py` from the aborted first
attempt were deleted. `backend/.venv` runs Python 3.14.4; every dependency installed from a wheel
under `--only-binary=:all:` (fastapi 0.141.1, pydantic 2.13.4, asyncpg 0.31.0, sqlalchemy 2.0.52,
alembic 1.19.1, pytest 9.1.1, pytest-asyncio 1.4.0). Health test red then green.

Task 2: complete — 87cd3f3. `Settings` carries the pinned product constants and no `redis_url`,
per R6. Red step surfaced as `No module named 'app.core'` rather than `app.core.config`, because
the package itself did not exist yet — same failure, one level up.

Task 6: complete — 39fc45e. `PhoneNumber` and `utc_isoformat` only, per R4. One test beyond the
brief covers R5's naive-datetime rule explicitly.

Task 7: complete — 7dac8e1. Page-based helpers only, per R3; no cursor helpers, no `base64`.

Tasks 2, 4, 6 and 7 ran concurrently against disjoint file sets, each staging by explicit path.

Task 4: complete — 4db7d46. `ErrorCode`, `AppError`, `install_error_handlers`; `main.py` installs
them. Two deviations, both accepted: the `client` fixture is local to `test_errors.py` because
`conftest.py` does not exist yet (Task 3 creates it, Task 14 adds fixtures per R1), and the
`trace_id` assertion was weakened to a key-presence check because no middleware sets a trace id
until Task 5. Task 5 is instructed to tighten it back to a truthiness assertion and remove the
stale comment.

Task 5: complete — ff6c1fd. `RequestContextMiddleware` sets `trace_id` and negotiates language;
registered last so it stays outermost, with a comment recording that Task 8's middleware must
register ahead of it. The weakened `trace_id` assertion from Task 4 is tightened back to a
truthiness check. `test_context.py` carries a local `client` fixture for the same reason Task 4
did — `conftest.py` was still being written concurrently.

Security fix (out of band): 07c765f. An automated review of Task 7 flagged that `per_page` was
capped at 100 but `page` was not, so `page=10**9` became `OFFSET 20000000000` on a public
unauthenticated list endpoint. `page` is now bounded by `MAX_PAGE = 10_000` in the query parameter
and clamped again inside `paginate_page` for callers that build `PageParams` directly. Two tests
cover the 422 and the accepted ceiling.

Task 3: complete — c85bec0. `Base`, mixins, engine, session helpers, Alembic scaffolding and the
shared `conftest.py` fixtures. The duplicate `client` fixture in `test_health.py` is gone. Alembic
ran `upgrade head` against SQLite only, per R7; `versions/` is still empty because no models exist
yet. Deviation, accepted: the brief's `os.environ["DATABASE_URL"]` and
`os.environ["TEST_DATABASE_URL"]` would `KeyError`, because `pydantic-settings` parses `.env`
internally without exporting into the process environment. Both `alembic/env.py` and `conftest.py`
now call `load_dotenv()` first. Worth revisiting when CI exists, where the variables will be real
environment variables and `.env` will not exist.

Checkpoint after Tasks 1-7: full suite 17 passed, 1 xfailed.

Task 9: complete — a769b6e. `Severity`, `Source`, `AuditEntry` and `audit.service.record(...)`
with the signature Tasks 14-16 call. Added `tests/audit/__init__.py`, which the brief omitted but
the existing test layout requires.

Task 10 autogenerates the first migration and names the idempotency table in it, so it cannot
start until Task 8 has committed its model. Task 13 edits `main.py`, which Task 8 is editing.
Both wait on Task 8; nothing else is dispatchable meanwhile.

Task 8: complete — 10fcd43. Replay, 409 conflict, and no memoization of 5xx. The subagent was
stopped partway; the controller finished it. An automated review had caught a real defect while it
was still in flight: records were looked up by `(key, scope)` alone, so a client sending a key
another client had already used on that path received the other client's memoized response body —
disclosure of cell numbers and PINs once Plan 2 lands. Records now carry an `owner`, and both the
SELECT and the unique constraint are `(owner, key, scope)`. Owner is `subject:<id>` from
`request.state.subject_id` when auth has set it, `ip:<host>` otherwise, in separate namespaces.
The regression test was checked by removing the owner filter and confirming it fails.

Also fixed while finishing Task 8: the middleware-order comment in `main.py` stated the rule
backwards. `add_middleware` inserts at the front and the stack is built in reverse, so the
middleware registered LAST is outermost. The code was already correct; only the comment lied.

Fix 911bc34: `Settings`, `conftest.py` and `alembic/env.py` all resolved `.env` relative to the
current directory, so anything launched from the repository root died on a missing `pin_pepper`.
All three now anchor to `backend/.env` via `BACKEND_DIR`. Suite verified from both directories.

Checkpoint after Tasks 1-9: 22 passed, 1 xfailed, from backend/ and from the repository root.

Remaining: 10 (identity models + first migration), 11 (OTP login), 12 (admin auth and
permissions), 13 (catalog reference data), 14 (postamats), 15 (cells), 16 (tariffs), 17 (rate
limiting). Task 10 is next and unblocks 11 and 12.

Task 10: complete — 8a89865. `Client`, `Role`, `AdminUser`, `RefreshToken`, and the first
migration `c5094fa66f11`, which creates all six tables that exist so far: `audit_entries`,
`clients`, `idempotency_records`, `refresh_tokens`, `roles`, `admin_users`. Verified by reading
the migration: `uq_idempotency_owner_key_scope` on `(owner, key, scope)` survived autogeneration,
and nothing Postgres-only leaked in. Applied to SQLite only, per R7. `AdminUser.role` uses
`relationship(lazy="joined")` so Task 12 can read `admin.role.permissions` without a lazy load
inside async code.

Deviation, necessary: `alembic/env.py` imported the model *packages*
(`from app.modules import audit, catalog, identity`), which registers nothing, because every
`modules/*/__init__.py` is empty. It also never imported `app.core.idempotency` at all. The agent
confirmed `Base.metadata.tables` was empty under the old import chain — autogenerate would have
produced an empty migration. It now imports the model modules directly. The `catalog` import was
dropped because that package still has no `models.py`; Task 13 must add it back the same way.

Out of band, e39c362: `run.ps1` and `backend/scripts/smoke.py`, so the human partner can run and
inspect the API without Docker. `run.ps1` builds the venv if absent, migrates, and serves with
reload, printing the Swagger/ReDoc/OpenAPI URLs. `smoke.py` walks the API in-process over an ASGI
transport — not over the network, because one-time codes live in the in-process key-value store
and an external client could never read one to finish a login. It uses a throwaway SQLite file and
reports endpoints from unlanded tasks as SKIP. Currently 2 ok, 0 failed, 9 not built yet.

Also corrected in briefs 11-17: `docker compose run --rm api ...` replaced with
`./.venv/Scripts/python.exe -m ...` (R8), and `git add -A` replaced with an instruction to stage
explicit paths, since concurrent agents on one branch would otherwise commit each other's
half-written files.

Task 11 dispatched alone. It touches `main.py`, which nearly every remaining task also touches, so
tasks are serialised on that file rather than run in parallel. Task 11 additionally has to create
`app/core/kvstore.py`, which R6 specifies but no landed task had written yet.

Task 11: complete — da0cdd0. `app/core/kvstore.py` (R6's in-process store, `increment` included for
Task 17), `app/core/security.py`, identity `schemas.py` and `service.py`, `app/api/mobile/auth.py`,
and the four endpoints `POST /api/v1/auth/{otp/request, otp/verify, refresh, logout}`. The `xfail`
came off `test_validation_error_renders_envelope` and it passes for real. Suite: 34 passed.
Verified independently with `scripts/smoke.py`: the full login round trip works end to end, and
the live 409 `IDEMPOTENCY_KEY_REUSED` fires on a reused key with a changed body.

Deviations, all accepted: `code_length` reads `settings.otp_length` rather than the brief's
hardcoded 6, so the response cannot contradict the code just issued; refresh and logout share one
loader that answers unknown, revoked and expired tokens identically with 401 `TOKEN_INVALID`, so
the caller cannot probe for tokens they do not hold; logout returns 204; an `_as_utc` helper
normalises the naive datetimes SQLite returns for `DateTime(timezone=True)` columns, which is a
no-op once Postgres returns aware values (R7).

Defect found by the Task 11 agent and folded into Task 12: refresh-token rotation never re-checks
the subject, so an administrator blocking a client revoked nothing — the client's session survived
for up to `refresh_token_ttl_days`. The same hole applied to deactivated admins. Task 12 was
dispatched with closing it as an explicit deliverable, with a test on each subject type.

Known and left alone: the dev `JWT_SECRET` in `backend/.env` is 20 bytes, so PyJWT emits
`InsecureKeyLengthWarning` (HS256 wants 32). Development credentials, not a code defect. It must
not survive into deployment.

Note: `.superpowers/` is git-ignored, so this ledger is untracked by design.

Task 12: complete — 63d55ae. `app/core/deps.py` (`require_client`, `require_admin`,
`require_permission`), `app/api/admin/auth.py` with login, refresh, logout, me and roles. Suite: 39
passed. Smoke: 11 ok, 0 failed.

The blocked-subject re-check landed as `_assert_subject_usable(...)` in identity's `service.py`,
called from `rotate_refresh_token` right after the token is loaded. That is the one choke point
every refresh passes through for both subject types, and the only place where the subject's type
and id are already known from the stored row, so neither router has to load the subject itself.
Deliberately not inside `_load_usable_refresh_token`, because that would leak the check into
`revoke_refresh_token` and logout must keep working for a blocked client — tearing your own session
down is giving up access, not using it.

Second defect the agent found on its own, and it is the more serious of the two: reusing Task 11's
rotation for the admin routes meant a *client* refresh token presented at `/admin/auth/refresh`
would have been rotated into a `typ=admin` access token. Straight privilege escalation. Fixed by
making `subject_type` a required argument of `rotate_refresh_token` and `revoke_refresh_token` and
rejecting a mismatch — folded into the existing indistinguishable branch, so it still answers 401
`TOKEN_INVALID` alongside unknown, revoked and expired. Required rather than defaulted, because a
default that silently settles a security question gets copied wrongly later. This is why
`app/api/mobile/auth.py` changed in a task whose brief listed only `main.py`.

Task 13 dispatched. It must add `catalog` back to `alembic/env.py` as a direct model-module import
and generate the second migration.

Task 13: complete — fcf5aba. `City` and `CellType`, their public localized list endpoints, and the
second migration `f1115e0c48e5` (down_revision `c5094fa66f11`), which creates `cities` and
`cell_types` and touches nothing else. Applied to SQLite. Suite: 41 passed. Smoke: 14 ok, 0 failed.
`alembic/env.py` now imports `app.modules.catalog.models` directly; with the full import set
loaded, `Base.metadata.tables` holds all eight tables.

Out of band, 2dd0ef3: `README.md` was corrupted. The GitHub bootstrap line `# poctamat_back` had
been appended in UTF-16LE onto a UTF-8 file, leaving embedded NUL bytes, so git classified the file
as binary — `git show 237d8fe -- README.md` printed "Binary files differ" and the file's history
stopped being diffable. Removed the line, re-encoded as plain UTF-8, and added the section the
README was missing entirely: how to run `backend/` without Docker, how to smoke the API, and the
layout. Also flagged there that both projects in this repository default to port 8000, so they
cannot be run simultaneously without `-Port`.

Task 14 dispatched, with the opening-hours boundaries called out explicitly: the exact opening and
closing minute, a closed day, round-the-clock, a window crossing midnight either implemented or
rejected at validation, and `next_opening_after` terminating on a permanently closed postamat.

Task 14: complete — fdc98a8. `Postamat`, `PostamatSchedule`, `Device`, the schedule service, the
admin router (list, read, create, patch, block, unblock, whole-week schedule replacement), the
public list and detail, and the third migration `6d13cda6dc34`. Suite: 59 passed. Every boundary
the dispatch called out has a test.

Decisions taken while implementing, all recorded in code comments: a window crossing midnight is
**rejected at validation** rather than modelled, because a machine that serves through the night is
`round_the_clock` and accepting 22:00-06:00 would silently mean "closed all day" everywhere
`is_open_at` runs; `next_opening_after` returns `datetime | None` rather than the brief's plain
`datetime`, because handing back the moment it was given reads as "open now" for a postamat that
never opens; the schedule PUT replaces the whole week, since a per-day edit leaves days nobody
meant to keep; blocked postamats are invisible to clients rather than listed as unavailable, while
`maintenance` stays visible because it ends; `PostamatPatch` carries neither `number` nor `status`,
because the number is the label on the machine and a status change has to carry a reason, which is
what block/unblock are for. R5 is satisfied: `PostamatOut.created_at` is a string through
`utc_isoformat`, asserted to end in `Z`.

Schemas for postamats live in `modules/catalog/schemas.py` rather than inside the admin router as
the brief sketched, because the public router needs them too.

R1's fixtures landed in `tests/conftest.py`: `admin_user`, `admin_token`, `city`, `cell_type`,
`postamat`, with the full permission set. Two of them call `await session.refresh(...)` after the
commit, and the reason is worth keeping: the suite overrides `get_session` with one session shared
by the fixture and the request, so a row the fixture created comes back out of the identity map
with its joined `role` and its `schedule` collection unloaded — and a lazy load inside async code
raises `MissingGreenlet` rather than loading. Production is unaffected: each request opens its own
session and `session.get` applies the mapper's eager loading.

Task 15: complete — e02e63f. `Cell` unique on `(postamat_id, number)`, bulk create, list, patch,
block, unblock, and migration `afb57d4e3c91`. Suite: 66 passed.

One real defect caught by the test rather than by review: the audit `record(...)` call flushes, so
with the audit write ahead of the flush the unique constraint fired from inside it — outside the
`try` that turns it into 409 `CELL_NUMBER_TAKEN`, giving a 500. The cells are now flushed first and
the audit entry written after. The rollback that follows a rejected batch expires every object in
the shared test session, which is why `test_a_duplicate_inside_one_request_is_rejected_whole`
reloads the admin and captures `postamat.id` before the failing call; that is harness bookkeeping,
commented as such, not product behaviour.

Task 16: complete — cd04d5f. `Tariff` keyed on `(city_id, cell_type_id, duration_hours)`, `GET` and
a whole-matrix `PUT`, migration `49fa8bc984a9`. Suite: 72 passed. Completeness is checked before
the delete, so a rejected matrix leaves the previous one standing — tested. Two additions beyond
the brief: an empty matrix is explicitly allowed and clears every price (a region that has not
opened is not an incomplete one), and `duration_hours` outside `settings.rental_durations` is
rejected at validation, since a price on a duration nothing can be booked for is unreachable money.

Task 17: complete — 4a13dd1. `app/core/ratelimit.py` over the key-value store; 3 OTP requests per
10 minutes, 120 public reads per minute. Suite: 75 passed. Smoke: 15 ok, 0 failed. Extended beyond
the brief to the public postamat routes, which are the same unauthenticated surface the brief's
reason applies to. R2 is moot under R6: there is no Redis module, so `core.ratelimit` imports
`core.kvstore` and no layering inversion arises.

Plan complete: tasks 1-17 all landed. Definition of done — suite green against SQLite (75 passed),
five migrations ordered `c5094fa66f11 -> f1115e0c48e5 -> 6d13cda6dc34 -> afb57d4e3c91 ->
49fa8bc984a9`, applied to SQLite only per R7 and **not** claimed against Postgres.
