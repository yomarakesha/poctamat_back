# Postamat backend — design

Date: 2026-08-14
Source of truth for the product surface: Figma `k8TRIKpQ7dZdARfd7bfaFO` (Postamat Full Project),
pages "Lane A" (Mobile / Desktop / Tablet sections) and "UI KIT".

## 1. Scope

A parcel-locker (postamat) platform for a fleet of devices, serving three clients:

- **Mobile app** — customers book a cell, pay, and share PINs.
- **Admin panel** — operators manage postamats, cells, clients, bookings, and read the audit log.
- **Kiosk** — the screen on the postamat itself: pick a role, type a PIN, open a cell.

Business model is C2C: the sender books and pays, the recipient collects. A courier may deposit
on the sender's behalf.

## 2. Locked decisions

| Question | Decision |
|---|---|
| Scale | Fleet from day one, multi-postamat schema |
| Connectivity | Postamats have static IPs, but the agent holds an outbound WebSocket; direct HTTP is fallback |
| Offline behaviour | Hybrid — server is source of truth, agent caches active bookings and code hashes |
| Host at the postamat | Stock Android terminal replaced by a Linux box |
| Server stack | FastAPI, Postgres, Redis |
| Agent stack | Python (Go remains a fallback if memory ever constrains) |
| Payments | Real acquiring; bank agreed, not yet named |
| Overdue parcels | No fees. Free grace, then staff remove the parcel to the counter |
| Architecture | Modular monolith with a seam for extracting the device gateway |

## 3. Domain model

### 3.1 Reference data

**City** — names in three languages (RU / TM / EN), active flag.

**CellSize** — code (`small` / `medium` / `large`), dimensions, display name.
As drawn: 20×20×40, 30×30×40, 40×40×60 cm.

**Tariff** — (size, duration) → price, with optional overrides per city or per postamat.
Durations are a fixed set: 12 / 24 / 48 h. Drawn prices: small 12 TMT, medium 18 TMT, large 18 TMT.

### 3.2 Hardware

**Postamat** — external ID, name, city, address, coordinates, photos, status
(`active` / `maintenance` / `offline`), IP, MAC, agent key, `last_seen_at`, timezone.
The "occupied / total" counter shown in the admin table is computed, never stored.

**PostamatSchedule** — opening hours per weekday, plus a `round_the_clock` flag. Optional:
a postamat standing in a 24/7 lobby has no hours; one inside a post office inherits that office's.

**Cell** — postamat, number, display code (`[01150]` in the mockups), size, `is_blocked`,
`is_maintenance`, and the physical address on the lock board (board 1 → cells 1–21,
board 2 → cells 22–43 on the reference hardware).

Occupancy is **not** a column. It is derived from the active booking. Only human decisions —
blocking and maintenance — are stored as flags. Two sources of truth for occupancy is the single
most likely cause of a cell that cannot be opened or a cell that is double-sold.

### 3.3 People

**Client** — phone (the login), full name, registration date, status (`active` / `blocked`),
language, default city.

**AdminUser** — login, full name, role, and the set of postamats in scope. The event log in the
mockups shows logins of the form `admin_ivanov`, `manager_petrov`.

### 3.4 Core

**Booking** — sender, postamat, cell, size, duration, price at the time of booking, status,
`created_at`, `paid_at`, `expires_at`, recipient phone and name, who deposits (`owner` / `courier`),
courier phone.

Price is copied into the booking rather than referenced. Tariffs change; history must stay true.

**AccessCode** — booking, purpose (`deposit` / `pickup` / `courier`), **hash of the code**, attempt
counter, expiry, used-at. The plaintext PIN is never stored; it is shown once when issued.
All three PINs are 5 digits, per the UX.

**BookingEvent** — the timeline the mobile app renders ("Забронировано" → "Курьер оставил посылку"),
one row per transition with a timestamp.

**Payment** — booking, provider, amount, currency, status, provider payment ID, idempotency key.

**CustodyRecord** — a parcel removed from a cell and held at the counter: who removed it, when,
photo, description, and every subsequent handover.

**DeviceCommand** — a command queued for hardware, with status. Present from day one so that
"open a cell" is never a synchronous call inside an HTTP request.

**EventLog** — timestamp, postamat, cell, event type, message, source (`Система` / `Сервер API` /
an admin login), severity, arbitrary detail JSON.

**Notification** — per client, for the notification screen in the app.

## 4. Booking lifecycle

```
draft ──► pending_payment ──► paid ──► awaiting_deposit
                 │                            │
                 │ timeout / decline          │ parcel deposited
                 ▼                            ▼
             cancelled                  awaiting_pickup
                                              │
                                              │ collected
                                              ▼
                                          completed

Side branch: expired ──► grace ──► overdue ──► removed ──► closed
```

### 4.1 Cell allocation

The customer picks a **size**, not a cell. The cell number is assigned after payment and appears
on the confirmation screen. Allocation must therefore be atomic over the pool of a size:

```sql
BEGIN;
  SELECT ... FROM cells
   WHERE postamat_id = ? AND size = ? AND NOT is_blocked AND NOT is_maintenance
     AND NOT EXISTS (active booking on this cell)
   ORDER BY number
   FOR UPDATE SKIP LOCKED
   LIMIT 1;
  -- no row → 409, the size sold out
  INSERT booking (status = 'pending_payment', hold_expires_at = now() + interval '15 minutes');
  INSERT access_codes (hashes);
COMMIT;
```

`SKIP LOCKED` is what makes two concurrent requests take different cells instead of fighting over
one. A partial unique index enforcing "at most one active booking per cell" backs this up at the
database level, in case of a bug in application code.

Payment settlement runs in its own transaction, driven by the bank's webhook. It must not be held
inside the allocation transaction — the bank answers in seconds and locks cannot be held that long.

### 4.2 Expiry and opening hours

If a postamat is not round-the-clock, expiry is pushed to shortly after the next opening. Paying
for 12 h at 19:00 against a location that closes at 20:00 would otherwise expire at 07:00, before
the recipient could physically arrive.

### 4.3 Overdue escalation

| Stage | When | System behaviour |
|---|---|---|
| Reminder | 2 h before expiry | push + SMS to recipient, push to sender |
| Expired | at expiry | status `expired`; PINs still work; cell still held |
| Grace | +2 h (for closed sites: until opening + 2 h) | collection still free, reminders more frequent |
| Overdue | after grace | status `overdue`, enters the admin work queue |
| To remove | +24 h overdue | flagged for removal, staff notified |
| Removed | staff act | cell freed, parcel at the counter |
| Closed | collected, returned, or disposed of | end of life |

Every interval is configuration, not a constant. No storage fees are charged at any stage.

### 4.4 Removal procedure

Removal is a procedure, not a "free the cell" button. Staff open the cell with a stated reason,
take the parcel out, **photograph it**, and enter a short description. The system writes a custody
record: who, when, which postamat and cell, photo, description. Only then does the cell become free.

The parcel then lives at the counter under its own number, with handover recorded on collection.
If it is never collected, after a configured period (default 30 days) it is returned to the sender
or disposed of, again with a record. Without this, a removed parcel disappears from the system and
no dispute can be answered.

## 5. API surface

Base path `/api/v1`. Three audiences with three different trust levels; no shared "generic" API.

### 5.1 Mobile (client JWT)

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/otp/request` | send an OTP to a phone number |
| POST | `/auth/otp/verify` | exchange OTP for tokens; registers on first use |
| POST | `/auth/refresh` | rotate tokens |
| POST | `/auth/logout` | revoke the refresh token |
| GET | `/me` | profile |
| PATCH | `/me` | name, language, default city |
| POST | `/me/push-tokens` | register a device for pushes |
| DELETE | `/me/push-tokens/{id}` | unregister |
| GET | `/cities` | city list |
| GET | `/postamats` | filter by city; coordinates for the map |
| GET | `/postamats/{id}` | details, photos, opening hours |
| GET | `/postamats/{id}/availability` | free count and price per size |
| POST | `/bookings` | postamat + size + duration + recipient → holds a cell for 15 min |
| GET | `/bookings` | active and history |
| GET | `/bookings/{id}` | details, PINs, timeline |
| POST | `/bookings/{id}/cancel` | only before the parcel is deposited |
| POST | `/bookings/{id}/courier/resend` | re-send the courier PIN by SMS |
| POST | `/bookings/{id}/payment` | start payment → bank page or token |
| GET | `/payments/{id}` | payment status |
| GET | `/notifications` | notification list |
| POST | `/notifications/{id}/read` | mark read |
| POST | `/notifications/read-all` | mark all read |

There is deliberately **no** endpoint that opens a cell from the phone. Opening happens only at the
kiosk against a PIN. A stolen token must not be able to open a physical door remotely; unlike a
booking, an opened door cannot be rolled back.

### 5.2 Webhooks (unauthenticated, signature-verified)

| Method | Path | Purpose |
|---|---|---|
| POST | `/webhooks/payments/{provider}` | payment result callback; idempotent by payment ID |
| POST | `/webhooks/sms/{provider}` | delivery reports |

### 5.3 Admin (session + 2FA for the admin role)

**Auth and dashboard**

| Method | Path | Purpose |
|---|---|---|
| POST | `/admin/auth/login` | login and password |
| POST | `/admin/auth/2fa` | second factor |
| POST | `/admin/auth/refresh` | rotate session |
| POST | `/admin/auth/logout` | end session |
| GET | `/admin/me` | current admin, role, scope |
| GET | `/admin/dashboard/stats` | figures for the "Панель" screen |
| GET | `/admin/dashboard/attention` | the work queue: overdue, offline postamats, failed commands |

**Postamats**

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/postamats` | list, filters, pagination |
| POST | `/admin/postamats` | create |
| GET | `/admin/postamats/{id}` | details |
| PATCH | `/admin/postamats/{id}` | edit |
| DELETE | `/admin/postamats/{id}` | decommission |
| POST | `/admin/postamats/{id}/photos` | upload a photo |
| DELETE | `/admin/postamats/{id}/photos/{photo_id}` | remove a photo |
| PUT | `/admin/postamats/{id}/schedule` | opening hours or 24/7 |
| POST | `/admin/postamats/{id}/maintenance` | enter or leave maintenance |
| GET | `/admin/postamats/{id}/health` | agent state, last seen, versions |
| POST | `/admin/postamats/{id}/agent-key/rotate` | rotate the device key |

**Cells**

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/cells` | filters: postamat, status, size |
| POST | `/admin/cells` | bulk creation when commissioning a postamat |
| GET | `/admin/cells/{id}` | details and history |
| PATCH | `/admin/cells/{id}` | size, display code, board address |
| POST | `/admin/cells/{id}/block` | block, with a reason |
| POST | `/admin/cells/{id}/unblock` | unblock |
| POST | `/admin/cells/{id}/open` | manual open, reason required, audited |

**Clients**

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/clients` | search by phone or name |
| GET | `/admin/clients/{id}` | profile and active bookings |
| POST | `/admin/clients/{id}/block` | block, with a reason |
| POST | `/admin/clients/{id}/unblock` | unblock |

Clients are never created from the admin panel — registration is self-service only.

**Bookings and custody**

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/bookings` | filters by status, postamat, period |
| GET | `/admin/bookings/{id}` | details and timeline |
| POST | `/admin/bookings/{id}/cancel` | force-cancel |
| POST | `/admin/bookings/{id}/refund` | refund |
| GET | `/admin/custody` | removal queue and parcels at the counter |
| POST | `/admin/custody` | file a removal act (photo + description) → frees the cell |
| GET | `/admin/custody/{id}` | record and its handovers |
| POST | `/admin/custody/{id}/handover` | handed to recipient or sender |
| POST | `/admin/custody/{id}/dispose` | returned or disposed of |

**Reference data and staff**

| Method | Path | Purpose |
|---|---|---|
| GET POST | `/admin/cities` | list, create |
| PATCH DELETE | `/admin/cities/{id}` | edit, remove |
| GET POST | `/admin/cell-sizes` | list, create |
| PATCH | `/admin/cell-sizes/{id}` | edit |
| GET POST | `/admin/tariffs` | list, create |
| PATCH DELETE | `/admin/tariffs/{id}` | edit, remove |
| GET POST | `/admin/users` | staff accounts |
| GET PATCH DELETE | `/admin/users/{id}` | manage a staff account |
| POST | `/admin/users/{id}/reset-password` | force a password reset |

**Audit**

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/events` | filters: postamat, cell, severity, source, period |
| GET | `/admin/events/export` | CSV export |

The event log is read-only. No PATCH, no DELETE — otherwise it is not an audit trail. Every action
that moves money or physical state writes an entry carrying the acting admin's login.

### 5.4 Device and kiosk (device key)

| Method | Path | Purpose |
|---|---|---|
| POST | `/device/auth` | device key → session token |
| WS | `/device/ws` | persistent channel: commands down, results and telemetry up |
| GET | `/device/sync` | active bookings and code hashes for the offline cache |
| POST | `/device/codes/verify` | PIN + role → which cell to open, or a rejection |
| POST | `/device/cells/{n}/opened` | the lock fired |
| POST | `/device/cells/{n}/closed` | the door sensor closed |
| POST | `/device/events` | batch of events, including ones buffered while offline |
| POST | `/device/heartbeat` | liveness, versions, board status |

`/device/codes/verify` is the most security-sensitive endpoint in the system:

- per-session attempt limit (the kiosk shows "Попытка 1 из 3");
- **and separately** a per-postamat rate limit over a time window. A 5-digit PIN is 100 000
  combinations. With ~20 active codes in a cabinet, a random guess hits roughly 1 in 5 000. Three
  attempts per session costs nothing to bypass by starting a new session — without a per-device
  limit the cabinet is brute-forced in an evening;
- compares hashes, never plaintext;
- burns the code immediately on successful use.

If the 5-digit PIN is a hard design requirement, per-device limiting is mandatory rather than
optional. The alternative is six digits, which is a change to the mockups.

## 6. Code structure

```
app/
  core/          config, database, security, FastAPI dependencies
  modules/
    identity/    clients, admins, OTP, tokens, roles
    catalog/     cities, sizes, tariffs, postamats, schedules
    booking/     bookings, access codes, lifecycle
    payments/    providers, webhooks, idempotency
    devices/     agents, commands, sync, telemetry
    custody/     removals, counter storage, acts
    audit/       event log
    notify/      SMS, pushes, templates in three languages
  api/
    mobile/      mobile routers
    admin/       admin routers
    device/      agent and kiosk routers
  workers/       background jobs
```

Each module has three layers: `models.py` (tables), `service.py` (rules), `schemas.py` (validation).
Routers call services only and never touch SQL directly.

The rule that holds this together: **modules do not import each other's models.** `booking` calls
`devices.gateway.open_cell(...)` rather than reaching into device tables. Without this the device
gateway can never be extracted into its own process later.

## 7. Background jobs

- release unpaid bookings whose hold has expired;
- move bookings through `expired` → `grace` → `overdue` → flagged for removal;
- send reminders, respecting opening hours;
- reconcile stuck payments against the bank;
- watch agent liveness and mark silent postamats `offline`.

## 8. Testing

- Unit tests on booking lifecycle transitions and on code verification — the core, where a bug is
  most expensive.
- Integration tests against a real Postgres in a container.
- Bank and SMS providers behind interfaces, stubbed in tests.
- An explicit concurrency test: parallel bookings for the last cell of a size; exactly one wins.

## 9. Security notes

- Card PANs must never reach our servers. The mockup's payment screen collects card details in-app;
  this has to become a bank-hosted page or SDK, with only a token and status returned to us.
  Otherwise the project falls under PCI DSS SAQ-D.
- PIN codes are stored as hashes only.
- Device keys are per-postamat and rotatable.
- The reference hardware's own web panel authenticates only `/Login` and `/sendData`; everything
  else is open. Any legacy terminal left in the field must be firewalled off from client networks.

## 10. Deliberately out of scope

Ratings, chat, promo codes, referral programmes, and multi-tenancy across several operating
companies. None appear in the UX; they can be added when they do.

## 11. Open questions

1. **Which bank**, and which integration flow — redirect, widget, or SDK. Until this is known the
   payment module is designed against a `PaymentProvider` interface with a mock implementation.
2. **SMS provider** for OTP and courier PINs, including delivery reports and per-number rate limits.
3. **Does the recipient need the app?** The UX implies collection by PIN alone, without registration.
4. **Admin roles** — the log shows `admin_` and `manager_` prefixes; the exact permission matrix is
   not drawn anywhere.
5. **Cell display codes** — the admin table shows `[01150]`, `[01250]`. The encoding of postamat and
   cell inside that string needs confirming before it is parsed anywhere.
