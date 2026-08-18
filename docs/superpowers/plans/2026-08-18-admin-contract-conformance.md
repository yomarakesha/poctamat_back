# Admin contract conformance (Plan 2d) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** The admin half of this server answers what `postbox-contract/openapi.yaml` says it
answers, so the admin panel can generate its client from that file instead of hand-writing calls
against whatever the backend happened to expose.

**Architecture:** No new subsystems. Two new modules — `staff` (admin users, roles, clients as an
operator sees them) and `stats` (read-only aggregates) — plus the missing CRUD on cell types,
devices and the audit log. Existing admin routers keep their storage and move their wire shapes.

**Spec:** `postbox-contract/openapi.yaml` — **the source of truth for every shape in this plan.**
Where this plan and the YAML disagree, the YAML wins.

**Predecessors:** Plans 1, 2a, 2b and 2c, all complete. Suite at 268. Client surface: 30/30
operations, payloads checked by `scripts/contract_walk.py`.

**Where this starts:** `scripts/contract_coverage.py --all` reports 56/104. Of the 48 not built,
23 are kiosk (Plan 3, waiting on hardware) and 25 are admin. This plan closes those 25.

## Global Constraints

- **SQLite**, `sqlite+aiosqlite`. No Postgres-only SQL. Aggregates are plain `GROUP BY`; nothing
  here needs a window function that SQLite lacks.
- **Money on the wire is `{"amount_minor": int, "currency": "TMT"}`.**
- **Timestamps** are UTC with `Z` (`utc_isoformat`).
- **Error codes come from the contract's `ErrorCode` enum.** A code outside it fails the admin
  panel's own generated validation — that is how the client-surface drift was found.
- **Every admin route carries a permission**, checked with `require_permission`. A new endpoint
  without one is a hole, not a shortcut.
- **Reads never leak a PIN.** Access codes are digests; no admin payload carries plaintext.
- Every task ends with `pytest -q` green from `backend/`.
- Commits stage explicit paths.

## Rulings

**D1 — no refund endpoint.** The contract defines `POST /admin/bookings/{id}/refund`, and this
system does not refund (Plan 2b, Ruling Q1 — reaffirmed by the operator). Building an endpoint that
always fails is worse than not building it: it puts a button on a screen that can never work. It
stays unbuilt and is listed as such in the admin handover, together with the two error codes the
contract reserves for it. Cost if wrong: one endpoint, once an actual refund policy exists.

**D2 — `GET /admin/dashboard/attention` moves to `GET /admin/stats/attention`.** Same payload plus
`to_remove_bookings` and `blocked_cells`. The old path goes; nothing generated calls it yet.

**D3 — our `/admin/bookings` list, detail and cancel stay, and are documented as extensions.** The
contract routes the operator's view of bookings through `/admin/stats/recent-bookings` and custody,
which is thinner than what an operator actually needs when a customer telephones. They keep their
own shapes and are named in the handover as not part of the contract, so nobody generates against
them and then wonders why the file does not mention them.

**D4 — `POST /admin/cells/{id}/remote-open` answers 503 `DEVICE_OFFLINE` until Plan 3.** The
endpoint exists, checks its permission, writes an audit entry, and refuses honestly: the lock agent
it would command does not exist yet. This is the one place a not-yet-real capability is better
declared than absent, because the contract makes the panel draw the button either way.

**D5 — `GET /admin/realtime/stream` is Server-Sent Events over the audit log.** No message broker.
The endpoint tails `audit_entries` on a short poll and emits the contract's event envelope. It is a
read of something already stored, so a restart loses nothing and a second worker duplicates nothing.

**D6 — roles are read-only in this plan.** The contract has `GET /admin/users` with a role per user
and no role CRUD; roles stay seeded fixtures. Assigning an existing role to a user is in scope,
inventing one is not.

**D7 — statistics are computed live, not materialised.** A fleet of 43 cells and a few thousand
bookings does not need a rollup table, and a stale tile is worse than a slow one. Revisit when a
query on real data crosses ~200 ms.

---

### Task 1: Admin users and roles

**Files:** create `backend/app/modules/staff/{__init__,service,schemas}.py`,
`backend/app/api/admin/users.py`, `backend/tests/admin/test_users.py`; modify
`backend/app/modules/identity/models.py`, `app/main.py`; migration.

- [x] `GET /admin/users` — the contract's `AdminUser` list, page-paginated, with role code and name.
- [x] `POST /admin/users` `{login, full_name, role_id, password}` → 201; 409 `USER_LOGIN_TAKEN`,
      422 `PASSWORD_TOO_WEAK`, 404 `ROLE_NOT_FOUND`.
- [x] `PATCH /admin/users/{id}` — name, role, `is_active`. Answers 409 `CANNOT_MODIFY_SELF` when an
      administrator tries to deactivate or demote their own account.
- [x] `POST /admin/users/{id}/reset-password` → a one-time password in the response, `must_change_password`
      set. The old password stops working immediately.
- [x] Permissions: `users.read` / `users.write`, added to the operator role fixture only for read.
- [x] Suite green, commit.

### Task 2: Clients as the operator sees them

**Files:** create `backend/app/api/admin/clients.py`, `backend/tests/admin/test_clients.py`;
modify `backend/app/modules/staff/service.py`.

- [x] `GET /admin/clients` — `AdminClientListItem`: `{id, full_name, phone, status, registered_at,
      active_bookings}`, with `?query=` over phone and name and `?status=`.
- [x] `GET /admin/clients/{id}` — adds the client's recent bookings and totals.
- [x] `POST /admin/clients/{id}/block` `{reason}` and `/unblock`. Blocking is already enforced at
      login and on refresh; the tests must show a blocked client's live session dying at its next
      refresh rather than only at the next login.
- [x] Permissions `clients.read` / `clients.write`; every block and unblock writes an audit entry
      naming the operator.
- [x] Suite green, commit.

### Task 3: Cell types

**Files:** create `backend/app/api/admin/cell_types.py`, `backend/tests/catalog/test_cell_types.py`;
modify `backend/app/modules/catalog/service.py`.

- [ ] `GET /admin/cell-types`, `POST /admin/cell-types`, `PATCH /admin/cell-types/{id}`,
      `POST /admin/cell-types/{id}/block` — the contract's shapes, three names per type.
- [ ] Blocking a type that cells still use answers 409 `CELL_TYPE_IN_USE` unless every cell of that
      type is free; a type behind a live booking cannot be taken out from under it.
- [ ] Suite green, commit.

### Task 4: Cells — bulk, detail, maintenance, remote open

**Files:** modify `backend/app/api/admin/cells.py`; tests.

- [ ] `GET /admin/cells/{id}` — one cell with its current booking, if any.
- [ ] `POST /admin/cells/bulk` at the contract's path and shape (ours lives elsewhere), answering
      409 `CELL_NUMBER_DUPLICATE` / `HARDWARE_ADDRESS_DUPLICATE` / `LAYOUT_EXCEEDS_GRID`.
- [ ] `POST /admin/cells/{id}/maintenance` `{is_maintenance, reason}`.
- [ ] `POST /admin/cells/{id}/remote-open` — permission checked, audit entry written, 503
      `DEVICE_OFFLINE` (D4).
- [ ] Suite green, commit.

### Task 5: Devices

**Files:** create `backend/app/api/admin/devices.py`, `backend/tests/admin/test_devices.py`;
modify `backend/app/modules/catalog/models.py` if the provisioning code needs storing; migration.

- [ ] `GET /admin/devices`, `GET /admin/devices/{id}` — the contract's `Device`, with
      `last_seen_at` and a derived `status`.
- [ ] `POST /admin/devices/{id}/provisioning-code` → a short-lived code, returned once, stored as a
      digest. Reissuing kills the previous one, for the same reason code rotation does.
- [ ] Suite green, commit.

### Task 6: Statistics

**Files:** create `backend/app/modules/stats/service.py`, `backend/app/api/admin/stats.py`,
`backend/tests/admin/test_stats.py`; delete `backend/app/api/admin/dashboard.py`.

- [ ] `GET /admin/stats/attention` — D2's payload, replacing `/admin/dashboard/attention`.
- [ ] `GET /admin/stats/overview` — postamat, cell, occupied, booked, blocked, maintenance counts.
- [ ] `GET /admin/stats/bookings?period=` — the histogram behind the «7 дней» selector.
- [ ] `GET /admin/stats/utilization`, `/peak-hours`, `/revenue` — the contract's shapes. Revenue
      counts settled payments only; a pending session is not money.
- [ ] `GET /admin/stats/recent-events`, `/recent-bookings`.
- [ ] Every aggregate is one query, and a test asserts the count of statements for the dashboard's
      full set — six screens' worth of tiles must not become sixty round trips.
- [ ] Suite green, commit.

### Task 7: Audit log

**Files:** create `backend/app/api/admin/audit.py`, `backend/tests/admin/test_audit.py`.

- [ ] `GET /admin/audit-log` — filters by actor, event, severity and date range, page-paginated.
- [ ] `GET /admin/audit-log/export` — CSV, streamed, capped; over the cap answers 422
      `EXPORT_TOO_LARGE` rather than building a file nobody can open.
- [ ] Permission `audit.read`, and reading the audit log is itself audited.
- [ ] Suite green, commit.

### Task 8: Realtime stream

**Files:** create `backend/app/api/admin/realtime.py`, `backend/tests/admin/test_realtime.py`.

- [ ] `GET /admin/realtime/stream` — SSE over the audit log (D5), with the contract's event
      envelope and a heartbeat so a proxy does not kill an idle connection.
- [ ] The stream ends cleanly when the client disconnects; a test asserts the generator stops rather
      than polling a dead socket forever.
- [ ] Suite green, commit.

### Task 9: The admin shapes that already exist

**Files:** modify `backend/app/api/admin/{postamats,cells,tariffs,custody}.py`; tests.

- [ ] Postamats, cells, tariffs and custody answer the contract's schemas — field names, enum
      values, pagination envelopes, `Money` objects.
- [ ] `scripts/contract_walk.py` grows an admin walk covering these, and it passes.
- [ ] Suite green, commit.

### Task 10: Check it and hand it over

**Files:** `backend/scripts/contract_coverage.py`, `backend/scripts/contract_walk.py`,
`postbox-contract/HANDOVER-ADMIN.md`, `README.md`, this ledger.

- [ ] `contract_coverage.py --all` shows every admin operation answered except the refund (D1), and
      the kiosk 23 named as Plan 3.
- [ ] Schemathesis over the admin tags against a seeded server, fixing what it finds.
- [ ] `HANDOVER-ADMIN.md` for the panel developer: base URL, how to authenticate, permissions per
      endpoint, what is an extension rather than contract (D3), and what is not built (D1, D4).
- [ ] Suite green, commit.

## Definition of done

- Every admin operation in the contract either exists with the contract's shape, or is named in the
  handover with the reason it does not.
- No admin response carries a status, field name or enum value the contract does not define.
- Every admin route checks a permission, and every state change writes an audit entry naming the
  operator.
- `pytest -q` green; `contract_walk.py` passes both walks.

## Deliberately not in this plan

- The kiosk surface — Plan 3, with the hardware.
- Refunds (D1) and role CRUD (D6).
- Materialised statistics (D7).
