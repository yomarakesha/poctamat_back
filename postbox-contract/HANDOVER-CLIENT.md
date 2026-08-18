# Client API handover — 18 August 2026

The client half of the backend now answers what `openapi.yaml` in this folder
says it answers. Point the generated client at it instead of the Prism mock.

Generated clients: `orval` / `openapi-typescript` for the web apps,
`openapi-generator` for Dart. Do not hand-edit generated code — edit
`openapi.yaml`, and tell the backend, because that file is what the backend is
checked against.

## Base URL and running it

There is no deployed environment yet. Run it locally:

```bash
cd backend
cp .env.example .env          # fill in JWT_SECRET and, for real SMS, SMS_API_TOKEN
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
cells, a full tariff matrix and one client, and prints a ready-made bearer token
and the seeded ids as JSON on the first line. That is the fastest way to get a
screen rendering against real data.

## Authenticating

1. `POST /auth/otp/request` `{phone}` → `{request_id, code_length, expires_at, resend_after}`.
   The code is **not** in the response; it goes by SMS. In development the SMS
   provider writes to the log instead of sending — read the code there.
2. `POST /auth/otp/verify` `{request_id, code}` → `{access_token, refresh_token,
   token_type: "Bearer", expires_in, profile_complete}`.
   `profile_complete: false` means the client has no name yet: show the
   registration form. Booking refuses with `PROFILE_INCOMPLETE` until it is true.
3. Send `Authorization: Bearer <access_token>` on everything else.
4. `POST /auth/refresh` `{refresh_token}` rotates the pair. The old refresh token
   dies immediately — replaying it answers `REFRESH_TOKEN_INVALID`, so store the
   new one before the next request.
5. `POST /auth/logout` needs the bearer token. With no body it ends every session
   for that client; with `{refresh_token}` it ends only that one, so signing out
   on the phone leaves the tablet signed in. `{push_token}` in the same body
   unregisters the device.

`Idempotency-Key` (any UUID, one per user action) is **required** on every
state-changing POST: booking, cancel, extend-hold, payments, payment cancel, and
all three code operations. Without it the answer is 400 `IDEMPOTENCY_KEY_MISSING`.
Replaying the same key with the same body returns the original response; with a
different body it is 409 `IDEMPOTENCY_KEY_REUSED`.

`Accept-Language: tk | ru | en` picks the language of names in responses and of
outgoing SMS. `tk` is the default.

## What is answered

All 30 client-surface operations in the contract — Auth (client), Profile,
Bookings, Access codes, Payments, Reference and the payments webhook. Check it
yourself at any time:

```bash
cd backend
./.venv/Scripts/python.exe scripts/contract_coverage.py       # paths
./.venv/Scripts/python.exe scripts/contract_walk.py           # payload shapes
```

`contract_walk.py` signs in, fills the profile, books, pays, deposits, rotates
both codes and transfers pickup, validating every 2xx body against the response
schema in `openapi.yaml`. It currently reports 22 payloads matching and none
failing.

## Behaviour worth knowing before you wire screens

- **Codes are returned exactly once.** `deposit_code` is filled in the response
  to `POST /bookings` and to `POST /bookings/{id}/deposit-code/rotate`, and is
  `null` in every later read. The server keeps a keyed hash and genuinely cannot
  produce the digits again. If the user needs the code later and the app did not
  cache it, rotate: a fresh code is issued and the old one stops working.
- **The pickup code does not exist until the parcel is inside.** Before the
  deposit, `pickup_code_sent_at` is `null` and
  `POST /bookings/{id}/pickup-code/rotate` answers 409 `PICKUP_CODE_NOT_ISSUED_YET`.
- **Both rotations are rate-limited per booking**: 429 `CODE_RESEND_TOO_SOON`
  inside `resend_after` seconds (60 by default). The response carries
  `details.resend_after`; render a countdown against it.
- **The hold is ten minutes** and may be extended twice via
  `POST /bookings/{id}/extend-hold`, for the case where the user is still on the
  bank's 3-D Secure page. After that: 409 `HOLD_EXTENSION_LIMIT_EXCEEDED`.
- **Payment is a redirect.** `POST /bookings/{id}/payments` `{bank_code, return_url}`
  answers a `Payment` with `redirect_url` on the acquirer's domain. Open it in a
  WebView; the card is typed there and never travels through this API. The deep
  link back is a hint — poll `GET /payments/{payment_id}` for the truth,
  including on a cold start.
- **There are no refunds anywhere in this API, by design.** Cancelling a paid
  booking releases the cell and keeps the money. Say so on the summary screen
  before the user pays.
- **`GET /bookings?scope=active|history`** and `GET /me/notifications` page by
  cursor: read `pagination.next_cursor` and pass it back as `?cursor=`.
  `pagination.has_more` says whether to keep going. The cursor is opaque.
- **Timeline** is a separate call: `GET /bookings/{id}/timeline` returns all
  seven steps in drawing order, with `occurred_at: null` for the ones that have
  not happened. Grey those out; do not reorder.
- **`cell_number` is a string** (`"14"`), and `price` / `amount` are
  `{amount_minor, currency}` objects. Never a float, never two loose fields.
- **Error codes** come from the contract's `ErrorCode` enum, in the envelope
  `{"error": {code, message, details, trace_id}}`. `message` is for the log, not
  for the user — translate `code`. Quote `trace_id` when reporting a bug.

## Known gaps between server and contract

Behavioural, not shape-level. Nothing here blocks generating a client.

1. **Undocumented 404s.** `POST /bookings/{id}/extend-hold`,
   `POST /bookings/{id}/payments` and `POST /payments/{id}/cancel` answer 404 for
   an id that is not the caller's; the contract does not list 404 on those three.
   The server is right and the contract needs the response added.
2. **422 where the contract documents only 200/401.** A malformed query parameter
   (`?limit=abc`, a corrupt cursor) answers 422 `VALIDATION_ERROR` on
   `GET /bookings` and `GET /me/notifications`. Handle 422 generically.
3. **`GRANT_EXPIRED` is not enforced yet.** Codes now carry an expiry
   (`expires_at` on both rotation responses) but nothing rejects an expired code
   — the kiosk that would check it is Plan 3.
4. **`BOOKING_LIMIT_EXCEEDED` never fires.** The setting exists and is off.
   Handle the code anyway, as the contract asks.
5. **`NO_FREE_CELLS` carries no `alternatives`.** The contract shows nearby
   postamats in `details`; the server sends `details.cell_type_id` only. Do not
   build the "try another postamat" list against it yet.
6. **The acquirer is the mock.** Every `bank_code` is accepted and
   `redirect_url` points at `https://pay.invalid/mock/...`, which does not load.
   To settle a payment in development, post the signed callback yourself — see
   `backend/scripts/smoke.py` for the exact body and signature.

## Not built at all

The kiosk surface (Plan 3, waiting on hardware) and most of the admin surface.
Both are out of scope for the client app. The admin operations that do exist
(postamats, cells, tariffs, bookings, custody, dashboard) do **not** follow the
contract's admin shapes yet — that is the next plan.
