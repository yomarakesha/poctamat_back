---
name: postamat-ux-facts
description: Non-obvious product rules read out of the postamat Figma UX that the code must honour
metadata: 
  node_type: memory
  type: project
  originSessionId: 4637af53-c3ce-4e80-a20e-2b8f65baaa56
  modified: 2026-08-14T04:15:56.440Z
---

Read from the Figma file `k8TRIKpQ7dZdARfd7bfaFO` (Postamat Full Project) on 2026-08-14.
These are product rules that are easy to get wrong from the screen names alone — verified against
the rendered screens, not just the layer tree.

- The customer picks a **cell size**, never a specific cell. The booking screen shows size cards
  ("Маленький 20×20×40 · 4 доступно") with prices; the cell number appears only after payment.
  So allocation races are over the pool of a size, not over one cell.
- Sizes and prices as drawn: small 20×20×40 = 12 TMT, medium 30×30×40 = 18 TMT,
  large 40×40×60 = 18 TMT. Rental durations are a fixed set: 12 / 24 / 48 h.
- Each postamat carries opening hours (the drawn one is inside a post office, 08:00–20:00).
  Modelled as an optional schedule with a 24/7 option, not as a mandatory rule.
- **Three PINs per booking**, all 5 digits: one for the sender to deposit, one texted to the courier
  (the UI has a "Повторно отправить PIN-код курьера" button), one shown to the sender to pass to the
  recipient. 5 digits means per-device brute-force limits are mandatory, not optional.
- The mobile booking detail screen shows a status timeline ("Забронировано" → "Курьер оставил посылку")
  with timestamps, so booking state transitions need to be stored as events, not just a status column.
- The UX states the overdue policy in plain text: "По истечении срока хранения посылку перемещают на
  стойку, а сотрудники вручную освобождают ячейку."
- Admin screens seen: Логин, Панель, Постаматы, Редактировать постамат, Ячейки, Клиенты, Журнал событий.
  The event log distinguishes sources: `Система`, `Сервер API`, and admin logins like `admin_ivanov`.
- Kiosk (tablet, 1280×800) flow: splash → pick role (владелец / курьер) → PIN entry → 3 attempts
  → "ЯЧЕЙКА ОТКРЫТА" → "Ожидание закрытия датчика...".

Related: [[postamat-backend-decisions]], [[locker-project]]
