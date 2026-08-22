---
name: postamat-2b-decisions
description: "Product decisions the user made for the payments/custody plan — no refunds, no photos on custody acts, post.tm SMS gateway"
metadata: 
  node_type: memory
  type: project
  originSessionId: 128f07a2-82d6-4c5b-a7bf-eae981180a6d
  modified: 2026-08-17T09:59:27.564Z
---

Decided by the user on 2026-08-17 while planning the payments and custody work (Plan 2b). These
were their calls after the trade-offs were laid out — do not re-open without asking.

- **No refunds at all.** No automatic return on cancellation and no admin refund endpoint. Two
  consequences were accepted knowingly: a paid booking cancelled before the parcel is deposited
  leaves the money with the operator, and a payment that succeeds after the hold expired is money
  for a booking that no longer exists. Both are recorded as `payment.needs_attention` audit entries
  at warning severity and appear in `GET /admin/dashboard/attention` for a human to settle.
- **Photographs are of the postamat, not of parcels.** Reaffirmed by the user on 2026-08-21 when
  the admin contract work reached `POST /admin/custody`: the contract marks `photo_url` required
  and takes multipart, and this server deliberately departs from it — JSON body, no photo field.
  The departure is declared in `backend/scripts/contract_walk.py` (printed on every run) and in
  `postbox-contract/HANDOVER-ADMIN.md`. The custody act carries a written description
  only. Postamat photos exist for locating and recognising the machine, are uploaded in the admin
  panel and appear on the public postamat detail. Stored on local disk behind a `FileStorage`
  interface in `backend/media/`.
- **SMS gateway is post.tm**, token supplied by the user:
  `POST https://sms.post.tm/api/clients/sms/create`, `Authorization: Bearer <token>`,
  body `{"phone": 99362615986, "content": "..."}`. The number goes as **digits with no leading
  plus**. Configured by `SMS_PROVIDER=post_tm` and `SMS_API_TOKEN` in `backend/.env`; `log` is the
  default and is forced in tests. The one-time login code and the courier PIN share this channel —
  the user's words: "пин код приходит с отп".
- **Bank still not named.** Payments run against a mock provider behind `PaymentProvider`, with the
  webhook trusted only through an HMAC signature.

Related: [[postamat-backend-decisions]], [[postamat-ux-facts]]
