# Admin API handover — 21 August 2026

The admin half of the backend now answers what `openapi.yaml` in this folder says
it answers. Generate the panel's client from that file; the departures from it
are listed at the bottom of this page, and there are three of them.

Generated clients: `orval` / `openapi-typescript`. Do not hand-edit generated
code — edit `openapi.yaml`, and tell the backend, because that file is what the
backend is checked against.

## Base URL and running it

There is no deployed environment yet. Run it locally:

```bash
cd backend
cp .env.example .env          # fill in JWT_SECRET
./.venv/Scripts/python.exe -m alembic upgrade head
./.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

- Base URL: `http://localhost:8000/api/v1`
- Interactive docs: `http://localhost:8000/docs`
- The server's own OpenAPI: `http://localhost:8000/openapi.json` — useful for
  debugging, but `openapi.yaml` here is the contract. Where they disagree, that
  is a backend bug; report it.

An empty database answers 404 to nearly everything. `backend/scripts/serve_seeded.py`
starts the same app on :8100 against a throwaway database with one postamat, ten
cells, a full tariff matrix, one client with a booking, and one operator holding
every permission. It prints a ready-made **admin** bearer token and the seeded
ids as JSON on its first line — the fastest way to get a screen rendering
against real data.

## Signing in

1. `POST /admin/auth/login` `{login, password}` → `{access_token, refresh_token,
   token_type: "Bearer", expires_in, user}`. `user` carries the operator's role
   and its `permissions`.
2. Send `Authorization: Bearer <access_token>` on everything else.
3. `POST /admin/auth/refresh` `{refresh_token}` rotates the pair. The old refresh
   token dies immediately, so store the new one before the next request.
4. `POST /admin/auth/logout` `{refresh_token}` ends that session.
5. `GET /admin/auth/me` re-reads the operator, including permissions — call it
   after a role change rather than trusting the token's age.

An unknown login and a wrong password answer identically (401
`ADMIN_CREDENTIALS_INVALID`), so the endpoint cannot be used to find out who
works here. A deactivated account answers 403 `ADMIN_ACCOUNT_BLOCKED`.

**Render the menu from `user.permissions`, but do not rely on it for anything
else.** Every endpoint checks the permission again on the server, and a hidden
button is a convenience, not a control.

## Permissions, endpoint by endpoint

`I` marks the endpoints that require an `Idempotency-Key` header — any UUID, one
per user action. Without it the answer is 400 `IDEMPOTENCY_KEY_MISSING`;
replaying the same key with the same body returns the original response, and
with a different body 409 `IDEMPOTENCY_KEY_REUSED`.

| Endpoint | Permission | |
|---|---|---|
| `GET /admin/auth/me` | — (any signed-in operator) | |
| `GET /admin/users` | `users.read` | |
| `POST /admin/users` | `users.write` | I |
| `PATCH /admin/users/{id}` | `users.write` | |
| `POST /admin/users/{id}/reset-password` | `users.write` | I |
| `GET /admin/roles` | `roles.read` | |
| `GET /admin/clients` · `GET /admin/clients/{id}` | `clients.read` | |
| `POST /admin/clients/{id}/block` · `/unblock` | `clients.write` | I |
| `GET /admin/postamats` · `GET /admin/postamats/{id}` | `postamats.read` | |
| `POST /admin/postamats` | `postamats.write` | I |
| `PATCH /admin/postamats/{id}` | `postamats.write` | |
| `POST /admin/postamats/{id}/block` · `/unblock` | `postamats.write` | I |
| `PUT /admin/postamats/{id}/schedule` | `postamats.write` | I |
| `POST /admin/postamats/{id}/photos` · `DELETE .../photos/{photo_id}` | `postamats.write` | |
| `GET /admin/cell-types` | `cells.read` | |
| `POST /admin/cell-types` · `/{id}/block` · `/{id}/unblock` | `cells.write` | I |
| `PATCH /admin/cell-types/{id}` | `cells.write` | |
| `GET /admin/cells` · `GET /admin/cells/{id}` | `cells.read` | |
| `POST /admin/cells` · `/bulk` · `/{id}/block` · `/{id}/unblock` · `/{id}/maintenance` | `cells.write` | I |
| `PATCH /admin/cells/{id}` | `cells.write` | |
| `POST /admin/cells/{id}/remote-open` | `cells.remote_open` | I |
| `GET /admin/tariffs` | `tariffs.read` | |
| `PUT /admin/tariffs` | `tariffs.write` | I |
| `GET /admin/custody` · `GET /admin/custody/{id}` | `custody.read` | |
| `POST /admin/custody` · `/{id}/handover` · `/{id}/dispose` | `custody.write` | I |
| `GET /admin/devices` · `GET /admin/devices/{id}` | `devices.read` | |
| `POST /admin/devices/{id}/provisioning-code` | `devices.write` | I |
| `GET /admin/stats/*` · `GET /admin/realtime/stream` | `bookings.read` | |
| `GET /admin/audit-log` · `/export` | `audit.read` | |
| `GET /admin/bookings` · `/{id}` (extension) | `bookings.read` | |
| `POST /admin/bookings/{id}/cancel` (extension) | `bookings.write` | I |

`cells.remote_open` is deliberately separate from `cells.write`: opening a door
from an office is the most dangerous button in the panel, and it should not
arrive with the right to rename a cell.

## Pagination — two kinds, and they are not interchangeable

- **Page-numbered** (`?page=&per_page=`, max 200): postamats, cell types,
  clients, users, admin bookings. The response carries
  `{page, per_page, total, total_pages}`.
- **Cursor** (`?cursor=&limit=`, max 200): cells, custody, the audit log. The
  response carries `{next_cursor, has_more}`. These tables grow while they are
  being read; an offset would show a row twice. Pass `next_cursor` back verbatim
  — it is opaque, and a malformed one answers 422 rather than silently starting
  over.

`?q=` is the one free-text search parameter, on postamats (number, name,
address), cells (their postamat), clients (phone and name) and the audit log.

## Behaviour worth knowing before you wire screens

- **Nothing returns a PIN.** Access codes live as digests; no admin payload
  carries the digits, and no amount of permission changes that.
- **Every state change is in the journal, naming the operator.** Blocking,
  unblocking, price changes, removals, disposals, password resets. Reading the
  audit log is itself audited — once per operator per hour for a page view,
  every time for an export.
- **Freeing a cell is a procedure, not a button.** `POST /admin/custody` files
  the removal act — stated reason, written description — and the cell becomes
  free as part of that call. There is no other way to empty a cell.
- **A parcel at the counter cannot be disposed of early**: 422 `CUSTODY_NOT_DUE`
  before `disposal_due_at`, which is stamped when the act is filed (30 days by
  default) and does not move when the setting does. `POST .../dispose` names its
  `outcome` — `returned_to_sender` or `disposed` — so the two do not blur.
- **The price matrix is replaced whole.** A city that sells a size must price all
  three durations, or the whole `PUT` is refused with 422 `TARIFF_INCOMPLETE`
  before anything is deleted. Prices already charged are untouched: a booking
  keeps the price captured when it was created.
- **Blocking a cell type in use** answers 409 `CELL_TYPE_IN_USE` unless every
  cell of that type is free.
- **A live client session dies at its next refresh** after the client is
  blocked, not only at the next login.
- **`POST /admin/users/{id}/reset-password`** returns a one-time password once
  and sets `must_change_password`. The old password stops working immediately.
  The password itself never reaches the journal.
- **`POST /admin/devices/{id}/provisioning-code`** returns a code once and keeps
  a digest. Reissuing kills the previous one.
- **The audit log has a one-year hot window.** A range reaching past it sets
  `pagination.truncated_at`; older entries come out through the CSV export.
  An export over 50 000 rows answers 422 `EXPORT_TOO_LARGE` rather than building
  a file nobody can open — narrow the period.
- **`GET /admin/realtime/stream` is Server-Sent Events** over the journal, not a
  WebSocket. It polls every 2 s and sends a heartbeat every 20 s so a proxy does
  not kill an idle connection. `?postamat_id=` narrows it; `?last_event_id=`
  resumes, and an unknown id starts from now rather than replaying the journal.
  An event this server has no contract type for arrives as `audit.entry_created`
  rather than being dropped.

## Errors

Every error, from every endpoint, is the same envelope:

```json
{"error": {"code": "CELL_TYPE_IN_USE", "message": "...", "details": {...},
           "trace_id": "..."}}
```

`code` comes from the contract's `ErrorCode` enum — nothing outside it is ever
sent. `trace_id` is worth showing in the panel's error toast: it is what the
backend needs to find the request in the log.

**The contract under-documents error responses.** Most operations list only their
happy path plus one or two refusals, but any endpoint may answer 401 (no or
expired token), 403 (permission or blocked account), 404, and 422 (validation) in
this envelope. Handle those four everywhere rather than only where the YAML
mentions them. Property-based testing flags this on 87 operation/status pairs;
it is a gap in the file, not in the server.

## What is answered, and what is not

Check it yourself at any time:

```bash
cd backend
./.venv/Scripts/python.exe scripts/contract_coverage.py --all   # paths
./.venv/Scripts/python.exe scripts/contract_walk.py             # payload shapes
```

`contract_coverage.py --all` reports **86 of 104** operations answered, and it
exits non-zero if any gap is not one of the two accounted for below.
`contract_walk.py` signs in as an operator and works the panel's own path —
postamats and their schedule, cell types, prices, a cabinet built in bulk, a cell
through maintenance and blocking, a parcel from removal act to
returned-to-sender — validating every 2xx body against `openapi.yaml`. It
currently reports 51 payloads matching and none failing.

**Not built, on purpose:**

- **`POST /admin/bookings/{id}/refund`.** This system does not refund — decided
  by the operator, twice. An endpoint that always fails is worse than an absent
  one: it puts a button on a screen that can never work. Do not draw it. The
  contract reserves `PAYMENT_NOT_REFUNDABLE` and `REFUND_ALREADY_REQUESTED` for
  the day that changes.
- **The whole `/kiosk/*` surface and `POST /qr-sessions/{id}/confirm`** — 17
  operations waiting on the hardware. That is Plan 3, and no panel screen depends
  on them.

**Answers, but not yet for real:**

- **`POST /admin/cells/{id}/remote-open`** checks its permission, writes an audit
  entry, and answers 503 `DEVICE_OFFLINE`. The lock agent it would command
  arrives with the kiosk. Draw the button — the contract makes the panel draw it
  either way — and show the refusal honestly.

**Departures from the contract, deliberate:**

- **A custody act carries no `photo_url`.** The contract marks it required; this
  product photographs the postamat, not parcels, and the act carries a written
  description instead. `POST /admin/custody` takes JSON, not multipart, and has
  no photo field. Generated validation for `CustodyRecord` will need that field
  relaxed. This is the operator's decision, reaffirmed on 21 August 2026.
- **`GET /admin/bookings`, `GET /admin/bookings/{id}` and
  `POST /admin/bookings/{id}/cancel` are extensions**, not in the contract. The
  contract routes an operator's view of bookings through
  `/admin/stats/recent-bookings` and custody, which is thinner than what somebody
  actually needs when a customer telephones. Their shapes are ours and may
  change; do not generate against them, and tell the backend if you come to rely
  on them.
- **`GET /admin/dashboard/attention` is gone.** It is `GET /admin/stats/attention`,
  with the same payload plus `to_remove_bookings` and `blocked_cells`.

## When something looks wrong

`openapi.yaml` is the source of truth. If a response does not match it, that is a
backend bug — send the `trace_id` and the operation id. If the contract itself is
wrong for the panel's needs, say so before generating around it: the file is
checked against the server on every run of the two scripts above, and a private
workaround in the panel is invisible to that check.
