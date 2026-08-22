---
name: postamat-backend-decisions
description: "Architecture decisions locked in for the postamat backend (scale, stack, offline model, host, payments, overdue policy)"
metadata: 
  node_type: memory
  type: project
  originSessionId: 4637af53-c3ce-4e80-a20e-2b8f65baaa56
  modified: 2026-08-17T05:57:06.182Z
---

Decisions made on 2026-08-14 while designing the postamat backend from the Figma UX
(`https://www.figma.com/design/k8TRIKpQ7dZdARfd7bfaFO/Postamat-Full-Project`, pages "Lane A" and "UI KIT").
These were chosen by the user after weighing alternatives — do not re-open them without asking.

- **Scale:** built for a fleet of postamats from day one, not a pilot on the single 43-cell locker.
- **Connectivity:** each postamat gets a static IP, but the agent still holds an outbound WebSocket
  to the server; direct server-to-postamat HTTP is only the fallback path.
- **Offline:** hybrid. Server is the source of truth; the on-site agent caches active bookings and
  access-code hashes so it can open cells with the network down, then flushes buffered events.
- **Host at the postamat:** the stock Android terminal gets replaced by a Linux box. This is what
  killed the Go-for-the-agent argument (no more 1 GB RAM / static-binary constraint).
- **Stack:** FastAPI + Postgres + Redis on the server, Python agent on the Linux box. One language.
  Go stays a fallback if the agent ever becomes memory-constrained; the WebSocket+JSON contract survives it.
- **Database in practice (decided 2026-08-17):** SQLite, not Postgres, and Redis is not installed.
  The user asked why Postgres was needed for the booking core and chose to stay on SQLite after the
  trade-off was laid out: SQLite serialises writers globally, so cell allocation cannot double-sell
  even without `FOR UPDATE SKIP LOCKED` — that clause buys parallelism, not safety. Allocation runs
  under `BEGIN IMMEDIATE` with WAL and `busy_timeout=5000`, backed by the partial unique index
  `uq_active_booking_per_cell`. Postgres becomes necessary when bookings start queueing; the only
  place to change is `catalog.service.free_cell_ids(...)`. One uvicorn worker only, because OTP codes
  and rate-limit counters live in-process (`app/core/kvstore.py`).
- **Payments:** real acquiring, bank agreed but not yet named. Card PAN must never touch our servers —
  bank-hosted page or SDK, we keep only token and status (PCI DSS SAQ-D otherwise).
- **Business model:** C2C only. Sender books and pays; recipient picks up. Courier is optional.
- **Overdue parcels:** no storage fees, no debt accounting. Free grace period, then staff physically
  remove the parcel to the counter with a photo and a written act, which frees the cell.
- **Architecture:** modular monolith with a seam for later splitting out the device gateway.
  Modules never import each other's models; `booking` calls `devices.gateway.open_cell(...)`.

Related: [[locker-project]]
