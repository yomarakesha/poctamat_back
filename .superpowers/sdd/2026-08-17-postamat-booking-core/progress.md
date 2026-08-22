# SDD ledger — plan: docs/superpowers/plans/2026-08-17-postamat-booking-core.md

Spec: docs/superpowers/specs/2026-08-14-postamat-backend-design.md (sections 3.4, 4.1, 4.2, 5.1,
6, 7, 8). Branch: main, continuing Plan 1's history. Base commit: 4a13dd1.

Plan 2a covers the booking core; payments, custody, overdue escalation and notifications are
Plan 2b, and the kiosk is Plan 3. The split was the human partner's call.

## Rulings taken before execution

Ruling P1: SQLite stays; `FOR UPDATE SKIP LOCKED` is not used. Plan 1's ruling R7 said Plan 2 could
not start without Postgres. That was wrong about correctness and right about throughput — SQLite
permits one write transaction at a time, so two allocations cannot both see the same cell as free.
`SKIP LOCKED` buys parallelism, not safety. The dialect-dependent part is confined to
`catalog.service.free_cell_ids(...)`.

Ruling P2: the partial unique index `uq_active_booking_per_cell` is the real guarantee. SQLite
supports partial indexes, so it survives P1.

Ruling P3: no cursor pagination yet — its first consumers (audit feed, notifications) are not in
this plan.

Ruling P4: `expires_at` starts at deposit, not at payment; `hold_expires_at` governs until then.

Ruling P5: `mark_deposited` and `mark_collected` land here without their HTTP surface, because they
are what frees the cell and the spec names the lifecycle as the place a bug is most expensive.

## Progress

Task 1: complete — a8c0d6a. Pragmas (WAL, `busy_timeout=5000`, `foreign_keys=ON`) and
`write_transaction`.

Two things the plan did not foresee, both fixed in the code rather than worked around:

1. `await session.execute(text("BEGIN IMMEDIATE"))` fails with "cannot start a transaction within a
   transaction" — pysqlite opens one of its own first. The driver is now put in autocommit
   (`isolation_level = None`) and SQLAlchemy emits the BEGIN itself through a `begin` event listener,
   DEFERRED by default and IMMEDIATE when the caller passes the `sqlite_txn` execution option. That
   also removes a second latent bug: `PRAGMA journal_mode=WAL` cannot run inside a transaction.
2. The suite's `test_engine` is `StaticPool` over `:memory:`, so every session shares one connection
   and cannot contend for a lock at all. A `file_engine` fixture (temp file, real pool) was added
   for anything testing concurrent writers.

Task 2: complete — 268fe89. `Booking`, `BookingEvent`, `AccessCode` and migration `12e9e5575047`.
Autogenerate emitted `sqlite_where` on the partial index without help; verified in `dev.db` that the
index carries its `WHERE status IN (...)` clause.

Task 3: complete — fd74ff6. `ALLOWED_TRANSITIONS` as a table, plus `can_transition` and
`assert_transition`. `ErrorCode` gained `BOOKING_INVALID_STATE` and `SIZE_SOLD_OUT`.

Task 4: complete — e79e8a5. `issue_codes`, `reissue_code`, `find_code`. Lookup is scoped to one
postamat because five digits repeat across a fleet.

Task 5: complete — 00d98c7. `cell_ids_by_type`, `free_cell_ids`, `storage_expiry`. `free_cell_ids`
takes the held-cell set as an argument so `catalog` never imports `booking`.

Task 6: complete — 0c0d4d4. `GET /postamats/{id}/availability`. Occupancy is derived from bookings
in the holding statuses, never stored on the cell.

Task 7: complete — 2bda5d9. `POST /bookings`, `GET /bookings/{id}`, `create_booking`.

Deviation, necessary: the router's own lookups (postamat, tariff) open a deferred transaction before
allocation starts, and `BEGIN IMMEDIATE` cannot upgrade a running transaction. `create_booking`
therefore commits an in-flight transaction before opening its own. Those lookups are reads, so
nothing is lost. Without this the write lock would silently never be taken in production, while
every test still passed.

Task 8: complete — 3adb17f. The spec's named concurrency test.

Worth recording: the test passed on the first run **and** kept passing with `write_transaction`
replaced by a plain deferred context — ten `asyncio.gather` attempts simply finished one after
another on this machine, so the test proved nothing. A 50 ms delay was injected between reading the
pool and inserting (monkeypatching `free_cell_ids` in the service module). With it, the deferred
version fails with `database is locked` — exactly the failure mode P1 predicts — and the real
implementation passes. A concurrency test that cannot fail is worse than none, because it reads as
evidence.

Task 9: complete — f751568. `mark_paid`, `mark_deposited`, `mark_collected`, `cancel`.

Defect found by the tests: `record_event` appended to `booking.events`, and appending to an unloaded
collection makes SQLAlchemy load it first — a lazy load inside async code, which raises
`MissingGreenlet`. It now refreshes the collection explicitly when the booking is persistent and the
relationship is unloaded, which also keeps the response's timeline from being one entry stale.

Task 10: complete — 9e5c95f. `GET /bookings`, `POST /bookings/{id}/cancel`,
`POST /bookings/{id}/courier/resend`.

Second defect, found the same way: raising `SIZE_SOLD_OUT` inside `write_transaction` rolled the
transaction back, and a rollback expires every object in the session — so the next attribute read on
a fixture object died with `MissingGreenlet` in the shared-session harness, and in production would
have expired the caller's objects for a request that wrote nothing. Sold out is now answered after
the transaction closes normally.

Task 11: complete — 99781d9. `app/workers/holds.py`. Only `pending_payment` is in scope: a paid
booking holds its cell because it was paid for, not because of a timer.

Task 12: complete — 7b8c3b6. Admin list, detail and force-cancel behind `bookings.read` /
`bookings.write`, with an audit entry naming the operator. Admin responses reuse `BookingOut`, which
has no `codes` field, so no admin response can carry a plaintext PIN.

Task 13: complete — 855e8e0. Smoke script walks availability → booking → list → cancel →
availability, ending on the assertion that a cancelled booking returns its cell. It prints code
lengths, never codes. README documents the booking surface, the hold worker and why SQLite suffices.

## Hardening pass after the plan finished

Asked to fix the three defects reported at the end and to carry the implementation further. The
three were already fixed in the commits that introduced them (a8c0d6a, 3adb17f, 9e5c95f) — verified
in the tree before doing anything else — so the work became a hardening pass over what the plan had
left thin.

3939124 — the timeline was ordered by `created_at`, and SQLite's `CURRENT_TIMESTAMP` has second
resolution and gives every row of one statement the same value. `mark_paid` writes two entries in
one transaction, so the app could have shown "Ожидает отправителя" above "Оплачено" — a history
that never happened — and two bookings made in the same second reshuffled between list requests.
`BookingEvent.seq` now carries position, `Booking.created_at` is filled in Python, migration
`ed2858d11271`. The migration adds `seq` with a `server_default` because a NOT NULL column without
one fails on a non-empty table.

f79f2cf — two guards on allocation. An `IntegrityError` from `uq_active_booking_per_cell` surfaced
as a 500; it means the cell was held by a booking this transaction could not see, which from the
customer's side is exactly sold out, and is now answered that way. It becomes reachable the moment
there is more than one worker. Second, `create_booking` ends the caller's transaction to take the
write lock, which is safe for reads and destructive for unflushed writes — a session carrying
pending changes now raises instead of having them committed on its behalf.

675fce1 — `POST /bookings` now requires `Idempotency-Key`. Booking allocates a physical cell for ten
minutes, so a double tap or a retry after a dropped connection quietly took a second cell. Two
things fell out of this: the middleware's `_owner` relied on `request.state.subject_id`, which the
original comment expected auth to set — but middleware runs before dependencies resolve, so every
key in practice belonged to an IP address, and a building behind one NAT shares it. The token is now
decoded in the middleware. Tests moved onto a `book` fixture that mints a fresh key per call, since
reusing one is what makes the second request replay.

6a750f1 — `booking_out` moved into the booking schemas and both surfaces share it, and
`catalog.service.cell_numbers` reads a page of door numbers in one query. Listing bookings had been
fetching the cell row per booking; a test now counts statements against the `cells` table and holds
it at one. Validation added alongside: `duration_hours` must be one of the product durations, and a
courier deposit must carry the courier's phone, because the PIN is delivered by SMS and a booking
without a number issues a code that cannot reach the person it is for.

## Final state

Suite: 151 passed, from `backend/` and from the repository root. Smoke: 21 ok, 0 failed, 1 not built
(`GET /me`, which belongs to no plan yet). Seven migrations, ordered
`c5094fa66f11 -> f1115e0c48e5 -> 6d13cda6dc34 -> afb57d4e3c91 -> 49fa8bc984a9 -> 12e9e5575047 ->
ed2858d11271`, applied to SQLite only.

Deliberately left for Plan 2b: payments and the bank webhook (`mark_paid` is their seam), custody
and the overdue escalation, notifications carrying the courier PIN by SMS. Plan 3 owns the kiosk and
`/device/codes/verify`, whose seam is `find_code`; the per-postamat brute-force limit a 5-digit PIN
makes mandatory belongs there.
