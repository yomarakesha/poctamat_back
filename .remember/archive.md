# Archive

## Week of 2026-08-03
Phase 1 complete: kiosk APK, server.py proxy, locker board mapped. Diagnosed UART issues (64-bit HANDLE truncation, baud mismatch); RS485/TTL conflict blocked direct UART. Pivoted to HTTP API (/sendData); blockers: USB/ADB dead, vendor WebAPK unavailable.

## Week of 2026-08-11
Validated RS485 requirement; sourced USB-RS485 adapter. Completed backend design: 13-entity domain model, 3 API contracts, atomic cell alloc with 6-stage escalation. Finalized API spec (80→101 endpoints), reviewed FE contract (5 issues). Started 17-task foundation; blocked by Docker constraint, awaiting creds.

## Week of 2026-08-15
Foundation + Plan 2c booking/payment system complete (state machine, SQLite WAL, PIN/SMS gateway, custody tracking, access codes, push tokens, mobile contracts; 268 tests passing). Progressed to Plan 2d: admin routers (auth/users, clients, cell_types), cell endpoints (Task 4), audit-log/SSE/postamat enhancements (Tasks 7-9). Docker-free test infrastructure stabilized; connectivity issues identified and scoped.