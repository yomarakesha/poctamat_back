# SDD ledger — plan: docs/superpowers/plans/2026-08-17-postamat-payments-custody.md

Spec: docs/superpowers/specs/2026-08-14-postamat-backend-design.md (§3.4, 4.3, 4.4, 5.1, 5.2, 5.3,
7, 9). Branch: main. Base commit: 6a750f1 (end of Plan 2a plus its hardening pass).

## Rulings, all set by the human partner on 2026-08-17

Q1 — no refunds anywhere. A client who pays and cancels before depositing forfeits the money, and a
success webhook arriving after the hold worker cancelled the booking is money taken for a booking
that no longer exists. Both are made visible as `payment.needs_attention` audit entries at warning
severity rather than silently absorbed, and both surface in the admin work queue.

Q2 — custody acts carry no photograph. The only photo the product wants is of the postamat itself,
so a customer can recognise the machine and an operator can confirm which one they are looking at.
That became Task 11.

Q3 — SMS behind `SmsProvider`, two implementations: a recording stand-in for development and tests,
and the operator's gateway at `sms.post.tm`, whose contract the partner supplied mid-execution:
`POST /api/clients/sms/create`, bearer token, `{"phone": 99362615986, "content": "..."}` — the
number as digits, no leading plus. Token lives in `backend/.env` only.

Q4 — the overdue branch is five statuses (`expired`, `grace`, `overdue`, `removed`, `closed`); the
spec's "to remove" stage is derived from `overdue` plus `remove_after`.

Q5 — `expired`, `grace` and `overdue` hold the cell; the partial unique index is rebuilt to match.

## Progress

Task 1: complete — 29e9065. Settings for payments, SMS, the overdue intervals and the media root.

Task 2: complete — c5d913f. `Notification`, three-language templates, `SmsProvider` with both
implementations, migration `140b256f7029`. A provider failure is stored on the notification rather
than raised: a dropped SMS must not fail the booking that triggered it.

Task 3: complete — 76a0426. `issue_otp` now takes a session and a language and actually sends the
code; the courier resend answers 202 with no body and texts the courier. Until this task the
one-time code was written into the key-value store and carried nowhere, and the courier PIN came
back through the sender's screen — which defeats the point of a separate courier PIN. The `book`
fixture moved to the shared conftest so the notify tests could use it.

Task 4: complete — 75633f9. The notification feed. `/read-all` is declared before
`/{notification_id}/read` so the literal is never parsed as an id.

Tasks 5 and 6: complete — 8c62a1b. `Payment`, `PaymentProvider` with a mock acquirer, the start
endpoint, the read endpoint and the signed webhook; migration `e8013f4bdc62`.

Defect worth recording, because it would have shipped as "the webhook returns 200 and nothing
happens": every status comparison in the payments service used `is`. SQLAlchemy hands back the
stored **string** for a `String`-typed StrEnum column, so `payment.status is PaymentStatus.PENDING`
is false for a row read from the database, and `settle` returned early every time. Now `==`
throughout, with a comment at the top of `settle` saying why. Dict lookups keyed on the enum keep
working because StrEnum hashes as its string, which is exactly why this hid for so long.

Tasks 7 and 8: complete — ccde030. The overdue statuses, `remove_after`, `reminded_at`, the
transitions and `app/workers/overdue.py`; migration `93ff7a72630e`.

The index rebuild in that migration is hand-written and was predicted in the plan: autogenerate
compares index columns, not the text of a partial predicate, so it emitted only the two new columns.
Left alone, `uq_active_booking_per_cell` would still have listed four statuses and a cell holding
somebody's overdue parcel could have been sold to the next customer. Verified in `dev.db` that the
predicate now lists all seven.

Task 9: complete — eeca4b4. `CustodyRecord`, `CustodyHandover`, the service and the admin router;
migration `57b644514458`. The act is written before the cell is released, so there is never a moment
where a cell is free and no record says where its contents went.

Task 10: complete — ed77cfd. `GET /admin/dashboard/attention`.

Task 11: complete — 8755c92. `FileStorage` with a local-disk implementation, `PostamatPhoto`, upload
and delete, `GET /api/v1/media/{key}`, and photo URLs on both postamat details; migration
`33d77c9f2b74`. Storage keys are validated on the way in, and a malformed key answers exactly like a
missing one.

Two harness-shared-session bugs fixed properly rather than papered over: uploads append to
`postamat.photos` and deletes remove from it, instead of adding or deleting the row on its own, so
the object in memory never disagrees with the database about the gallery.

Task 12: complete — 3b63b7a. Smoke walks booking → payment → webhook → notifications; its output is
forced to UTF-8 after a cp1251 console killed a run on the Turkmen city names.

## Final state

Suite: 216 passed, from `backend/` and from the repository root. Smoke: 27 ok, 0 failed, 1 not built
(`GET /me`, which belongs to no plan yet). Eleven migrations, applied to SQLite only.

Left for later: a real bank (one class behind `PaymentProvider`), push notifications and
`/me/push-tokens`, a scheduler for the two workers, and Plan 3 — the kiosk and the device channel,
whose seams here are `find_code`, `mark_deposited` and `mark_collected`.
