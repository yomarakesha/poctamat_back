# Changes against "Postamat backend — design" (2026-08-14)

Reply to your design document. Written in English to match it.

**Source of truth for everything below:** [`contracts/openapi.yaml`](../contracts/openapi.yaml)
— OpenAPI 3.1, 101 operations, 58 schemas, 102 error codes, passes `npx @redocly/cli lint`.
Prose version with rationale: [`docs/API-01.md`](API-01.md). Where this file and the YAML
disagree, the YAML wins.

Three clients generate their HTTP layer from that file (`orval` for the two web apps,
`openapi-generator` for Flutter). That is why path and field names matter more than usual here:
renaming one later is a regeneration plus edits across three codebases.

The document is good. Most of what follows is agreement; the disagreements are in §2 and the
gaps in §3.

---

## 1. Accepted from your design — no action needed

These went into the contract as you wrote them, in some cases replacing what I had:

- **`Booking` as a single object.** I had `Reservation` + a separate `Shipment`. You are right
  and the mockups agree: the «Курьер» screen is a step inside booking, not another entity.
  Courier delivery is `deposited_by: owner|courier` plus `courier_phone`. Shipment is gone.
- **Structured opening hours, and expiry that respects them.** I had `working_hours` as a
  display string. Your example — 12 h paid at 19:00 at a site closing at 20:00 expiring at
  07:00 — is exactly right, and it is a data-model problem, not a UI one.
- **Six-step overdue ladder** `expired → grace → overdue → to_remove → removed → closed`, all
  intervals configuration, no fees at any stage.
- **`CustodyRecord`.** I had `{destination, note}`. Yours is an act with a photograph, a
  description and a handover chain. Adopted whole.
- **Occupancy derived, never stored.** Agreed, and stated as an invariant in the contract.
- **`FOR UPDATE SKIP LOCKED` allocation plus the partial unique index**, and the concurrency
  test for the last cell of a size.
- **Per-postamat PIN rate limiting.** Your arithmetic is right: 100 000 combinations, ~20 live
  codes, ~1 in 5 000 per guess, and a per-session limit is bypassed by starting a new session.
  With the 5-digit PIN confirmed as a product decision, this is mandatory.
- **`DeviceCommand` queue** so "open a cell" is never synchronous inside an HTTP request.
- **No endpoint that opens a cell from the phone.** Same conclusion, same reason.
- **Card data never reaching the server.** Confirmed by the client: the bank supplies its API
  and payment happens on the bank's page; we get a redirect URL and a status.

---

## 2. Changes to make

### 2.1 Access codes — `resend` becomes `rotate`

You store hashes only, which is correct. It also means **there is no such thing as re-sending
the same code** — the server cannot reproduce the digits. So the operation becomes: mint a new
code, revoke the old one, send the SMS.

| Your design | Contract |
|---|---|
| `POST /bookings/{id}/courier/resend` | `POST /bookings/{booking_id}/deposit-code/rotate` |
| — | `POST /bookings/{booking_id}/pickup-code/rotate` |
| — | `POST /bookings/{booking_id}/pickup-code/transfer` |

Rules:

- `deposit-code/rotate` returns the new plaintext **in the response** (the app shows and copies
  it) and SMSes it to `courier_phone`. Optional `phone` in the body redirects it to a different
  courier and stores that number.
- `pickup-code/rotate` returns **no digits at all**. The only copy in the world is the SMS on
  the recipient's phone. Not to the sender, not to an admin, not in a log line.
- `pickup-code/transfer` is the «передать получение» feature: revoke the current pickup grant,
  issue a fresh one to a different number. This is why the offline rules in §2.4 matter.
- Plaintext of the deposit code appears **only** in the response that issues it: booking
  creation and rotate. Every other read returns `null`.
- Both rotate operations are rate-limited: `CODE_RESEND_TOO_SOON`, `CODE_RESEND_LIMIT_EXCEEDED`.

### 2.2 Access code purposes: three → two

Your `AccessCode.purpose` is `deposit | pickup | courier`. The contract has two grants per
booking: **deposit** and **pickup**. Who physically deposits is `Booking.deposited_by`, not a
third code.

Reason: a third live code is a third row in the guessing space, and it buys nothing — the
courier uses the deposit code, which is the point of handing it to them.

### 2.3 PIN storage — a plain hash is not enough

"Stored as a hash" is the right instinct but insufficient at this length. A 5-digit PIN has
100 000 candidates; an attacker with the database enumerates all of them against a fast hash
instantly, and against bcrypt in about an hour per code.

Required: **keyed HMAC, or a slow KDF with a pepper held outside the database** (KMS, env, HSM —
anywhere that a database dump does not include). Per-code salt as well.

### 2.4 Offline behaviour — narrower than "hybrid"

Your version: the agent caches active bookings and code hashes and keeps working. My original
version: no core, no opening. The agreed middle:

| Rule | Value |
|---|---|
| Cached at the agent | Active bookings of its own postamat + code **hashes**. Never plaintext |
| Allowed offline | **Pickup only** — the parcel is already inside, the code is already issued |
| Forbidden offline | Deposit, staff force-open, any custody action |
| Cache validity | `offline_ttl`. Synced longer ago than that → the agent refuses and shows the connectivity banner |
| On reconnect | Buffered events flush to the core |

Why pickup only: a deposit creates new state (cell occupied, pickup code to be issued **and
SMSed**), and the SMS cannot be sent without the core. The recipient would end up with a parcel
in a cabinet and no code.

Why the TTL: revocation does not reach a disconnected agent. Someone transfers pickup rights,
the server kills the old code, and a cabinet that lost its uplink still accepts it. The TTL
turns that from an unbounded hole into a bounded one. The cost is explicit and accepted:
while an agent is offline, `pickup-code/transfer` does not take effect at that postamat until
the TTL expires.

**Please propose a value for `offline_ttl`.** It is the one number in this section I do not have.

### 2.5 No `DELETE`

Your §5.3 has `DELETE` for postamats, cities, tariffs and users. The contract has exactly one
delete in the whole surface: removing an uploaded photo.

Everything else is blocking / deactivation: `POST /admin/postamats/{id}/block`,
`/unblock`, `POST /admin/cell-types/{id}/block`, `is_active` on users. The bin icon in the
admin table is wired to block.

Reason: the audit log, the statistics and past bookings must stay readable. A deleted postamat
takes last quarter's revenue chart with it.

### 2.6 Postamat status vs device presence

Your `Postamat.status` is `active | maintenance | offline`. Split it:

- `Postamat.status` = `active | maintenance | blocked` — an operator's decision.
- `Device.status` = `online | offline | degraded`, plus `last_seen_at` — the state of the
  hardware.

A postamat can be blocked by an operator *and* offline at the same time; with one field you
cannot express that, and the monitor screen needs both.

### 2.7 Cell status is a read model with a fixed precedence

Agreed that occupancy is derived. The API still exposes a single `status` for the monitor and
for the client, computed in this order:

```
blocked > maintenance > occupied > booked > free
```

`blocked` (three wrong PINs or an operator decision) and `maintenance` (planned work) are
different states, different filters in the panel and different reasons in the log. Please do not
collapse them.

### 2.8 Identifiers

- **Postamat number**: alphanumeric, unique across the network (`10042`, `ТП-4`).
- **Cell number**: a plain number `1`–`42`, unique **within its postamat** — the label on the
  door.
- `[01150]` from the mockups is a **display/search composite**, not a key. Nothing parses it.
- `Cell.number`, `Cell.row`/`col` and `Cell.hardware_address {board, output}` are three
  independent fields. In the pilot cabinet their values happen to line up; that is a
  coincidence, not a rule, and code must not rely on it.

### 2.9 Tariffs — keyed on city × size × duration

You have (size, duration) with optional overrides per city or per postamat. Confirmed with the
client: **prices vary by city, not by individual postamat.** Per-postamat override is dropped.

| Your design | Contract |
|---|---|
| `GET POST /admin/tariffs`, `PATCH DELETE /admin/tariffs/{id}` | `GET /admin/tariffs`, `PUT /admin/tariffs` |

`PUT` replaces the whole matrix in one transaction. Half-applied price changes are worse than
none, and it lets the panel show a diff before saving. Validation: a city that sells a size must
price all three durations → `TARIFF_INCOMPLETE`.

Price is copied into the booking at creation — you already have this, and it stays.

### 2.10 Refunds — operator only

Product decision: **cancelling never returns money.** The warning goes on the summary screen
before the pay button.

Your `POST /admin/bookings/{id}/refund` stays, because broken cabinets and double charges happen
regardless of policy — but it is admin-only, requires a reason of at least 10 characters,
answers `202` with the payment object, and **does not exist in the mobile API in any form**.

### 2.11 Hold duration: 15 → 10 minutes

The countdown drawn on the summary screen is 10 minutes. Make it configuration with a default of
10 rather than a constant.

### 2.12 Freeing a cell goes through custody

There is no "free the cell" operation. The only path from `overdue` to a free cell is
`POST /admin/custody` (multipart: `booking_id`, `reason`, `description`, `photo`), and the photo
is mandatory — `PHOTO_REQUIRED` otherwise. Freeing the cell is a side effect of filing the act,
in the same transaction.

### 2.13 OTP and PIN are different lengths — keep them apart

- Login OTP: **6 digits** (`/auth/otp/request` returns `code_length: 6`).
- Cell PIN: **5 digits**.

They look alike in the UI and this has already caused confusion in the mockups. Do not unify
them, and do not share a validator.

### 2.14 Public endpoints

These take no token at all — a person sees the map and the prices before handing over a phone
number:

`GET /cities`, `GET /postamats`, `GET /postamats/{id}`, `GET /postamats/{id}/availability`,
`GET /cell-types`.

Rate-limit them by IP; they are the only unauthenticated read surface.

### 2.15 Dropped

- `tenant_id` / multi-tenancy — confirmed as never happening. You had already put it out of
  scope; noting it so it stays out.
- Client-facing refunds (§2.10).
- Shipment as a separate object (§1).

---

## 3. Missing from your document — required by the contract

None of this is optional: each item is something the three clients cannot work around.

### 3.1 One error envelope, 102 machine codes

```json
{
  "error": {
    "code": "PIN_INVALID",
    "message": "Wrong code.",
    "details": { "attempts_left": 2, "attempt": 2, "max_attempts": 3 },
    "trace_id": "0190f2c1-6b7a-7c3e-9a11-2f0c9a5d4e61"
  }
}
```

- `code` is a stable machine constant; the full enum is `ErrorCode` in the YAML. Adding a code
  is not a breaking change, renaming one is.
- `message` is English, for developers and logs. It is **never shown to a user** — the clients
  translate by `code` into tk/ru/en.
- `details` carries the structured extras the UI actually renders: `attempts_left` for the kiosk
  keypad, `fields` for form validation, `alternatives` for `NO_FREE_CELLS`.
- `trace_id` correlates with your logs and the audit trail.

**`401` vs `403` must not be confused.** `401` = expired or missing token, the client refreshes
and retries. `403` = insufficient rights, retrying is pointless. Getting this backwards puts the
clients into a refresh loop that looks like a hang.

### 3.2 `Idempotency-Key` on every state-changing POST

You have idempotency for payments only. It is required on all of them — the parameter is marked
`required: true` in the YAML.

- Client-generated UUID, scoped to endpoint + subject.
- Same key + same body → the original response, including its status code. No second action.
- Same key + different body → `409 IDEMPOTENCY_KEY_REUSED`.
- Keys retained at least 24 h. `5xx` responses are **not** memoized — those must stay retryable.

Connectivity inside post offices drops mid-request. Without this, a retried `POST /bookings`
books and charges a second cell, and a retried rotate sends a second SMS with a different code.

### 3.3 Money

`{"amount_minor": 1200, "currency": "TMT"}` — integer minor units plus a currency code. No
floats anywhere near money, including in statistics aggregates.

### 3.4 Pagination

- **Cursor** for anything that grows while being read: audit log, notifications, bookings,
  cells monitor, custody. Response carries `{next_cursor, has_more}`.
- **Pages** for reference data: cities, postamats, cell types, clients, users. Response carries
  `{page, per_page, total, total_pages}` — the admin footer «Показано 1-3 из 124» needs `total`.

### 3.5 Time, phone, language

UTC ISO-8601 with `Z` everywhere (clients render `Asia/Ashgabat`). Phones E.164
`^\+993[0-9]{8}$`. `Accept-Language` of `tk|ru|en`, **default `tk`** — it is preselected on first
launch and it is what a kiosk resets to after every session.

### 3.6 Realtime for the admin monitor

You have a WebSocket for devices only. The panel needs its own feed:
`GET /admin/realtime/stream`, `text/event-stream`, resumable via `last_event_id`.

Event types: `cell.status_changed`, `cell.door_opened`, `cell.door_closed`, `booking.created`,
`booking.status_changed`, `device.presence_changed`, `device.alert`, `audit.entry_created`.
Clients ignore unknown types silently, so new ones can be added without a version bump.

The panel ships on 5-second polling first; the stream is the drop-in upgrade.

### 3.7 Audit log retention and export

- One year in the hot table; older entries move to cold storage **on the server**. A query
  reaching past the hot window returns what it has and sets `pagination.truncated_at`.
- `GET /admin/audit-log/export` streams CSV with `Transfer-Encoding: chunked`, UTF-8 with BOM.
  The browser must never assemble it — the log runs to millions of rows.

### 3.8 Notification feed

`GET /me/notifications` (cursor, `unread_only`, returns `unread_count`) and
`POST /me/notifications/read`. Push delivery is not guaranteed and a dismissed banner is gone
forever, so this list is the durable copy. It never contains a pickup code in any field.

### 3.9 Booking status enum

Eleven values, replacing your six-plus-branch: `pending_payment`, `awaiting_deposit`,
`awaiting_pickup`, `completed`, `expired`, `grace`, `overdue`, `removed`, `closed`, `cancelled`,
`payment_failed`. Semantics are yours; this is just the wire spelling.

### 3.10 Kiosk configuration comes from the server

`GET /kiosk/context` returns `pin_length: 5`, `max_pin_attempts: 3`,
`door_close_timeout_seconds: 60`, `idle_timeout_seconds: 60`, `default_language: tk`, plus the
support phone numbers for the «ЯЧЕЙКА ЗАБЛОКИРОВАНО» screen. The terminal hard-codes none of it.

---

## 4. Path mapping

Your naming on the left, the contract on the right. Where only the name differs, the contract
name wins for the reason at the top of this file.

| Your design | Contract |
|---|---|
| `POST /me/push-tokens` | `POST /auth/push-tokens` |
| `DELETE /me/push-tokens/{id}` | `DELETE /auth/push-tokens/{push_token_id}` |
| `GET /notifications`, `POST /notifications/{id}/read`, `POST /notifications/read-all` | `GET /me/notifications`, `POST /me/notifications/read` (empty body = all) |
| `POST /bookings/{id}/payment` | `POST /bookings/{booking_id}/payments` |
| `POST /bookings/{id}/courier/resend` | `POST /bookings/{booking_id}/deposit-code/rotate` |
| `GET /admin/dashboard/stats` | `GET /admin/stats/overview` + `/bookings` + `/utilization` + `/peak-hours` + `/revenue` |
| `GET /admin/dashboard/attention` | not in the contract yet — good idea, no screen for it. Say the word and I will add it |
| `GET POST /admin/cell-sizes` | `GET POST /admin/cell-types` (`CellType` is the term used across all three clients) |
| `GET /admin/events`, `GET /admin/events/export` | `GET /admin/audit-log`, `GET /admin/audit-log/export` |
| `POST /admin/cells/{id}/open` | `POST /admin/cells/{cell_id}/remote-open` — `202` + `command_id`, reason ≥ 10 chars, mandatory audit entry |
| `GET /admin/postamats/{id}/health` | `GET /admin/devices/{device_id}` |
| `POST /admin/postamats/{id}/agent-key/rotate` | `POST /admin/devices/{device_id}/provisioning-code` |
| `GET POST /admin/cities`, `PATCH DELETE /admin/cities/{id}` | `GET /cities` public; admin CRUD not in the contract yet — cities are near-static, tell me if you want the endpoints |
| `POST /admin/users/{id}/reset-password` | not in the contract yet; add it if the panel needs it |

### The three channels — please keep them distinct

Your document folds the kiosk and the device into one "device" audience. There are three
separate links and they are not interchangeable:

1. **Kiosk UI ↔ core** — `/kiosk/*` in the contract. PIN verification, opening authorisation,
   door-sensor reporting, staff mode. This is a browser app on the terminal.
2. **Agent ↔ core** — `/device/*` and the outbound WebSocket from your §5.4. Yours to define;
   I do not consume it.
3. **Kiosk UI ↔ local agent** — a WebSocket on `ws://127.0.0.1:8765`, loopback only. Schemas
   `LockAgentCommand` and `LockAgentEvent` live in the YAML purely so the kiosk and its
   TypeScript mock generate from the same types. Commands: `open`, `status`, `diagnostics`,
   `ping`. Events: `agent_ready`, `ack`, `opened`, `open_failed`, `door_opened`, `door_closed`,
   `door_timeout`, `sensor_fault`, `jammed`, `tamper`, `power_state`, `pong`.

`door_closed` is the hinge of the whole system: on a deposit it is what flips the cell to
`occupied` **and releases the pickup SMS** — not a button, not a timer, not a successful `open`.

---

## 5. Answers to your open questions

1. **Which bank, and which flow.** Not chosen yet. The client confirmed the bank supplies its
   API and payment happens on the bank's page — so: redirect, and `PaymentProvider` behind an
   interface is the right call. All three banks stay in the `BankCode` enum; the ones without a
   live integration answer `BANK_NOT_SUPPORTED` and grey out in the UI.
2. **SMS provider.** Still open, and it is now the most valuable unknown on your side: the pickup
   PIN is SMS-only, so delivery reports are the difference between "not delivered" and "not read".
   Your `POST /webhooks/sms/{provider}` is the right place for them.
3. **Does the recipient need the app?** No. Collection is by PIN from an SMS, no registration.
   The recipient never authenticates anywhere.
4. **Admin roles.** Roles are a mechanism, not a fixed list: `GET /admin/roles` returns roles with
   their permission sets, and `GET /admin/auth/me` returns the current user's `permissions[]`.
   The panel hides what is not in that list; the server checks it again on every call. One
   superadmin exists at launch. Your `admin_ivanov` / `manager_petrov` become two roles whose
   permission matrix we can fill in once someone from Türkmenpoçta describes the job.
5. **Cell display codes `[01150]`.** Display and search only. The door carries `1`–`42`, unique
   within its postamat; the postamat carries an alphanumeric number unique across the network.
   Nothing parses the composite.

**Accepted from you, needs a screen that does not exist yet:** admin 2FA (`/admin/auth/2fa`).
It is in the drawing queue, not in the contract, and I will add the endpoint once the flow is
drawn — SMS, TOTP or email changes the request body.

---

## 6. Still unanswered, and blocking nobody yet

| Question | Owner | Default until answered |
|---|---|---|
| `offline_ttl` value | you | needs a proposal |
| Storage grace period after expiry | Türkmenpoçta | server setting, default 24 h |
| Cold archive retention for the audit log | infrastructure | on the server, term unnamed |
| Concurrent booking limit per client | product | no limit, but `BOOKING_LIMIT_EXCEEDED` exists and clients handle it from day one |
