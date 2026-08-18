# Mobile contract conformance (Plan 2c) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** The client-facing half of this server answers exactly what
`postbox-contract/openapi.yaml` says it answers, so the Flutter app can point its generated client
at our base URL instead of a Prism mock.

**Architecture:** No new subsystems. Paths, field names, enum values and response envelopes move to
the contract; the parts of the contract we never built (profile, push tokens, notification
settings, hold extension, code rotation, payment sessions) get built to its shapes.

**Spec:** `postbox-contract/openapi.yaml` — **the source of truth for every shape in this plan**.
Where this plan and the YAML disagree, the YAML wins. Prose with rationale:
`postbox-contract/API-01.md`.

**Predecessors:** Plans 1, 2a and 2b, all complete. Suite at 216.

## Why the contract wins

Three clients generate their HTTP layer from that file — `orval` for the two web apps,
`openapi-generator` for Flutter. A name that differs is not a cosmetic difference: it is a compile
error in three codebases, and a generated Dart enum throws on a value it has never heard of. That
is why `paid` has to leave our status set and why `depositor` becomes `deposited_by` on the wire.

## Global Constraints

- **SQLite**, `sqlite+aiosqlite`. No Postgres-only SQL.
- **Money on the wire is `{"amount_minor": int, "currency": "TMT"}`** — an object, not two fields.
- **Timestamps** are UTC with `Z` (`utc_isoformat`).
- **Phones** are `+993XXXXXXXX` on the wire, always with the plus. The SMS gateway strips it.
- **`Idempotency-Key` is required** on every state-changing client POST the contract marks
  `IdempotencyKeyRequired`.
- **Codes are returned exactly once**, in the response that issues them. Every later read is `null`.
- **The database keeps its own names.** `depositor`, `is_blocked`, `kind` stay as columns; the
  serializer maps them to `deposited_by`, `status`, `type`. A rename on the wire is not a reason to
  rewrite storage.
- Every task ends with `pytest -q` green from `backend/`.
- Commits stage explicit paths.

## Rulings

**C1 — `paid` leaves `BookingStatus`.** The contract's main line is
`pending_payment → awaiting_deposit → awaiting_pickup → completed`. `paid` was ours alone; a
generated client that meets it throws. Payment still writes a `paid` **timeline step**, which the
contract does define (`TimelineStep.step`). Cost if wrong: one enum value and its transition.

**C2 — `to_remove` becomes a real status.** Plan 2b's Ruling Q4 derived it from `remove_after`; the
contract makes it a status and the admin queue distinguishes it from `overdue`. The overdue worker
gains one more stage. It holds the cell, so the partial unique index is rebuilt again.

**C3 — a declined payment ends the booking in `payment_failed`, not `cancelled`.** Both are dead
ends; the contract keeps them apart because the app shows different screens.

**C4 — the pickup code exists from the deposit onward.** Plan 2a issued three codes at booking
time. The contract issues the deposit code at creation, and the pickup code when the parcel is
actually inside — `pickup_code_sent_at` is null until then, and `POST /bookings/{id}/pickup-code/rotate`
answers `PICKUP_CODE_NOT_ISSUED_YET` before it. The `courier` purpose disappears: the deposit code
*is* the courier's code, sent to `courier_phone` when there is one.

**C5 — cursor pagination arrives.** Plan 1's Ruling R3 deferred it for want of a consumer. The
contract paginates `/bookings` and `/me/notifications` by cursor, so it has two. Page pagination
stays where the contract uses it (`/postamats`, admin lists).

**C6 — error codes follow the contract's vocabulary on the client surface.** `SIZE_SOLD_OUT` becomes
`NO_FREE_CELLS`, `TOKEN_INVALID` splits into `REFRESH_TOKEN_INVALID` / `REFRESH_TOKEN_EXPIRED` on
the refresh path, `BOOKING_INVALID_STATE` splits into the specific codes each endpoint documents.
The envelope itself already matches.

**C7 — the webhook accepts a bank code as its provider, and `mock` alongside.** The contract types
`provider` as `BankCode` (`halk|senagat|rysgal`). Until an acquirer is chosen the mock must stay
callable, so both are accepted and the extra value is documented as a development affordance.

---

### Task 1: Cursor pagination and Money

**Files:** modify `backend/app/core/pagination.py`, `backend/app/core/types.py`;
create `backend/tests/core/test_cursor.py`

**Produces:** `CursorParams`, `cursor_params`, `encode_cursor`, `decode_cursor`, `CursorPage`,
`paginate_cursor(session, stmt, params, order_column)`; `Money` (Pydantic model with `amount_minor`,
`currency`) in `core/types.py`.

The cursor is opaque to the client and stable across inserts: it encodes the ordering column's
value and the row id, so a row added between two pages cannot make one item appear twice.

- [x] Write tests: a page of 3 out of 5 returns `has_more: true` and a cursor; feeding that cursor
      back returns the remaining 2 with `has_more: false`; an unparseable cursor is a 422; inserting
      a newer row between pages does not duplicate or skip.
- [x] Implement, run, commit.

### Task 2: The status set the contract defines

**Files:** modify `backend/app/modules/booking/models.py`, `service.py`,
`app/modules/payments/service.py`, `app/workers/overdue.py`; migration; touch every test asserting
`paid`.

- [x] `BookingStatus` loses `PAID`, gains `TO_REMOVE` and `PAYMENT_FAILED`.
- [x] `mark_paid` moves `pending_payment → awaiting_deposit` in one transition and writes two
      timeline entries (`paid`, then the state change), so the checklist screen still shows payment.
- [x] A declined payment moves the booking to `payment_failed` (C3); the hold worker keeps using
      `cancelled` for an unpaid hold that simply ran out.
- [x] `CELL_HELD_STATUSES` gains `TO_REMOVE`; the partial unique index is rebuilt **by hand** in the
      migration — autogenerate does not compare predicate text.
- [x] The overdue worker adds the `overdue → to_remove` stage at `remove_after`.
- [x] Suite green, commit.

### Task 3: The client profile

**Files:** modify `backend/app/modules/identity/models.py`; create
`backend/app/api/mobile/profile.py`, `backend/tests/identity/test_profile.py`; migration.

- [x] `Client` gains `last_name`, `first_name`, `middle_name`; `default_city_id` is renamed
      `city_id` in the migration; `full_name` is dropped after the data is moved into `last_name`.
- [x] `GET /me` and `PATCH /me` returning the contract's `Client`: `id, phone, last_name,
      first_name, middle_name, city_id, language, status, profile_complete`. `status` is derived
      from `is_blocked`; `profile_complete` is `last_name and first_name`.
- [x] `PATCH /me` answers 422 `CITY_NOT_AVAILABLE` for an unknown or inactive city.
- [x] Booking creation answers 422 `PROFILE_INCOMPLETE` when the profile is not complete — the
      contract lists that code and the app renders the registration form on it.
- [x] Suite green, commit.

### Task 4: Auth as the contract states it

**Files:** modify `backend/app/modules/identity/service.py`, `schemas.py`,
`backend/app/api/mobile/auth.py`, `backend/app/core/kvstore.py` if needed; tests.

- [x] `POST /auth/otp/request` answers `{request_id, code_length, expires_at, resend_after}`. The
      code is stored under the request id as well as the phone, so verify can find it by id.
- [x] `POST /auth/otp/verify` takes `{request_id, code}` and answers `TokenPair + profile_complete`.
- [x] `TokenPair` everywhere is `{access_token, refresh_token, token_type: "Bearer", expires_in}`.
      `is_new_client` is gone — `profile_complete` replaced it.
- [x] Refresh failures answer `REFRESH_TOKEN_INVALID` / `REFRESH_TOKEN_EXPIRED`; OTP failures answer
      `OTP_INVALID` with `attempts_left`, `OTP_EXPIRED`, `OTP_NOT_FOUND`, `OTP_ATTEMPTS_EXCEEDED`.
- [x] A blocked client answers 403 `CLIENT_BLOCKED` on request and verify.
- [x] Suite green, commit.

### Task 5: Push tokens

**Files:** create `backend/app/modules/identity/push.py` or extend models; routes in
`app/api/mobile/auth.py`; migration; tests.

- [x] `PushToken` model: client, token (≤512), platform (`android|ios`), app_version, revoked_at.
- [x] `POST /auth/push-tokens` → 201 `{push_token_id}`; re-registering the same token for the same
      client returns the existing id rather than duplicating.
- [x] `DELETE /auth/push-tokens/{id}` → 204, 404 for someone else's token.
- [x] `POST /auth/logout` also revokes the push token bound to that session, which the contract
      states in prose.
- [x] Suite green, commit.

### Task 6: Notification settings and the feed at /me

**Files:** create `backend/app/modules/notify/settings.py` (model) and move the feed router into
`backend/app/api/mobile/profile.py`; delete `app/api/mobile/notifications.py`; migration; tests.

- [x] `NotificationSettings` per client: `push_parcel_deposited`, `push_cell_opened`,
      `push_expiring_soon`, `push_marketing`, all defaulting to true, plus a read-only
      `sms_always_on: true` on the wire. Created on demand for a client that has none.
- [x] `GET/PATCH /me/notification-settings`.
- [x] `GET /me/notifications` — cursor pagination, `unread_only`, and `{items, pagination,
      unread_count}`. Items carry `{id, type, title, body, booking_id, created_at, read_at}`;
      `type` is the stored `kind` and the contract's known values are used for it.
- [x] `POST /me/notifications/read` with optional `notification_ids` (omitted means all) →
      `{unread_count}`.
- [x] `Notification.is_read` becomes `read_at` in the migration.
- [x] Suite green, commit.

### Task 7: Booking payloads

**Files:** modify `backend/app/modules/booking/schemas.py`, `app/api/mobile/bookings.py`; tests.

- [x] `BookingListItem`: `{id, status, postamat_id, postamat_name, cell_number (string),
      cell_type_name, created_at, expires_at}`.
- [x] `Booking` adds `{postamat_address, cell_id, duration_hours, price: Money, recipient_phone,
      recipient_name, deposited_by, courier_phone, hold_expires_at, deposit_code, pickup_code_sent_at,
      qr_payload, payment}`.
- [x] `GET /bookings?scope=active|history` with cursor pagination.
- [x] `POST /bookings` answers `Booking` with `deposit_code` filled (C4) and no `codes` map.
      `recipient_phone` is optional; equal to the sender's phone answers 422
      `RECIPIENT_PHONE_SAME_AS_SENDER`; an unpriced duration answers 422 `DURATION_NOT_SUPPORTED`;
      a sold-out size answers 409 `NO_FREE_CELLS`.
- [x] `POST /bookings/{id}/cancel` answers the `Booking`, with 409 `BOOKING_NOT_CANCELLABLE` /
      `BOOKING_ALREADY_CANCELLED`.
- [x] `POST /bookings/{id}/extend-hold`: pushes `hold_expires_at` by `hold_minutes`, at most
      `hold_extensions_max` times (a new setting, default 2), answering 409
      `HOLD_EXTENSION_LIMIT_EXCEEDED`, `BOOKING_HOLD_EXPIRED`, `BOOKING_ALREADY_PAID`.
- [x] `GET /bookings/{id}/timeline` returns every step of
      `booked, paid, parcel_deposited, pickup_code_sent, collected, expired, cancelled` in that
      order, with `occurred_at: null` for the ones that have not happened.
- [x] Suite green, commit.

### Task 8: Access codes

**Files:** modify `backend/app/modules/booking/codes.py`, `models.py`, `app/api/mobile/bookings.py`;
migration if `CodePurpose` changes; tests.

- [x] `CodePurpose` keeps `deposit` and `pickup`; `courier` is gone (C4) — the deposit code is what
      a courier gets, sent to `courier_phone` when one is set.
- [x] Booking creation issues only the deposit code. The pickup code is issued when the parcel is
      deposited (`mark_deposited`) and SMS'd to the recipient, stamping `pickup_code_sent_at`.
- [x] `POST /bookings/{id}/deposit-code/rotate` → `{code, expires_at, resend_after}`, optional
      `phone` override which also updates `courier_phone`; 409 `GRANT_ALREADY_USED` once the parcel
      is in.
- [x] `POST /bookings/{id}/pickup-code/rotate` → same shape; 409 `PICKUP_CODE_NOT_ISSUED_YET`
      before the deposit.
- [x] `POST /bookings/{id}/pickup-code/transfer` `{new_phone, new_name}` → `Booking`; 422
      `TRANSFER_PHONE_SAME`.
- [x] Both rotations are rate-limited per booking: 429 `CODE_RESEND_TOO_SOON` inside
      `resend_after` seconds.
- [x] `/bookings/{id}/courier/resend` is deleted.
- [x] Suite green, commit.

### Task 9: Payment sessions

**Files:** modify `backend/app/modules/payments/models.py`, `provider.py`, `service.py`,
`app/api/mobile/payments.py`, `app/api/webhooks/payments.py`; migration; tests.

- [x] `PaymentStatus` becomes `pending, authorized, succeeded, failed, cancelled, expired`.
- [x] `Payment` gains `bank_code`, `redirect_url`, `expires_at`, `failure_code`.
- [x] `POST /bookings/{id}/payments` takes `{bank_code, return_url}` and answers the contract's
      `Payment`; a second live session answers 409 `PAYMENT_ALREADY_EXISTS`; an unsupported bank
      answers 422 `BANK_NOT_SUPPORTED`.
- [x] `POST /payments/{id}/cancel` → `Payment`, 409 `PAYMENT_NOT_CANCELLABLE` once authorised.
- [x] The webhook accepts `halk|senagat|rysgal|mock` (C7) and answers 400
      `CALLBACK_SIGNATURE_INVALID` / `AMOUNT_MISMATCH`.
- [x] Suite green, commit.

### Task 10: Check it against their file

**Files:** `backend/scripts/smoke.py`, `README.md`, this ledger.

- [ ] Update the smoke script to the new paths and shapes.
- [ ] Run the contract's own check:
      `schemathesis run ../postbox-contract/openapi.yaml --base-url http://localhost:8000/api/v1`
      against a live server with a seeded database, and fix what it finds on the client surface.
      Admin and kiosk failures are expected and out of scope for this plan.
- [ ] Write the handover note for the front-end developer: base URL, which operations conform,
      which are still missing, and how to authenticate.

## Definition of done

- Every client-surface path in the contract either exists with the contract's shape, or is listed
  in the handover note as not built.
- No response carries a status, field name or enum value the contract does not define.
- `pytest -q` green; the smoke script walks the new mobile flow end to end.

## Deliberately not in this plan

- The admin surface: clients, staff, devices, cell-type CRUD, audit log, statistics, SSE.
- The kiosk surface — Plan 3, with the hardware.
- Real acquirer integration; `mock` stays until a bank is chosen.
