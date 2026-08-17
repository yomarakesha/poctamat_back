# Postamat payments, overdue and custody (Plan 2b) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A booking can be paid for and then lives out its whole life — deposited, collected, or
left behind until staff remove the parcel to the counter — with every step notified to the people
involved and answerable afterwards from the record.

**Architecture:** Two new modules on top of Plan 2a's `booking`: `payments` (a `PaymentProvider`
interface, a mock implementation, a signature-verified webhook) and `custody` (removal acts and
handovers at the counter), plus `notify` (a `Notification` table, an in-app feed, and an
`SmsProvider` interface that the existing OTP flow moves onto). The booking lifecycle gains its
overdue branch, driven by workers rather than by request traffic, because a parcel goes overdue
while nobody is looking at it.

**Tech Stack:** Python 3.14.4, FastAPI, SQLAlchemy 2.0 async, aiosqlite, Alembic, pytest.

**Spec:** `docs/superpowers/specs/2026-08-14-postamat-backend-design.md` (sections 3.4, 4.3, 4.4,
5.1, 5.2, 5.3, 7, 9).

**Predecessors:** Plan 1 (`2026-08-14-postamat-backend-foundation.md`) and Plan 2a
(`2026-08-17-postamat-booking-core.md`), both complete; suite at 151 passed.

## Global Constraints

- **SQLite**, `sqlite+aiosqlite`, dev and tests. `JSON` never `JSONB`, no Postgres-only SQL.
- **Money** is `amount_minor: int` plus `currency: str`. Never a float.
- **Card data never reaches this server.** The provider returns a redirect URL or a token; we store
  a provider payment id and a status, nothing that could put the project under PCI DSS SAQ-D.
- **Timestamps** on the wire are UTC with a trailing `Z` via `app.core.types.utc_isoformat`.
  SQLite returns naive datetimes; normalise with the module-local `_as_utc` before comparing.
- **Modules do not import each other's models.** `payments` and `custody` call
  `booking.service`; `booking` calls `notify.service`. Routers may join modules — they are the
  composition layer.
- **Timeline entries are appended through `booking.service.record_event`**, which assigns `seq`.
  Never construct a `BookingEvent` directly; ordering depends on it.
- **Intervals are configuration.** Every overdue stage reads `Settings`, never a literal.
- **Every task ends green:** `./.venv/Scripts/python.exe -m pytest -q` from `backend/`.
- **Commits stage explicit paths.** Never `git add -A`.

## Rulings taken before execution

**Ruling Q1 — no refunds anywhere in this plan.** Decided by the human partner on 2026-08-17.
There is no `POST /admin/bookings/{id}/refund`, no automatic return on cancellation, and no refund
state on `Payment`. Two consequences are accepted deliberately and must be visible rather than
silent: a client who pays and then cancels before depositing forfeits the money, and a success
webhook arriving after the hold worker has already cancelled the booking is money taken for a
booking that no longer exists. Both write an audit entry with `severity=warning` and the event
`payment.needs_attention`, so the operator can settle them by hand. Cost if wrong: a refund call on
the provider interface and one endpoint.

**Ruling Q2 — custody acts carry no photograph.** Decided by the human partner: the only photograph
the product wants is of the postamat itself, so an operator or a customer can find the machine, and
it belongs to the postamat rather than to a removal act. The act carries who removed the parcel,
when, from which cell, and a written description. Postamat photos are Task 12 of this plan. Cost if
wrong: a `photo_key` column on `CustodyRecord`, reusing the storage Task 12 introduces.

**Ruling Q3 — SMS goes behind `SmsProvider`, with two implementations.** Both the one-time login
code and the courier PIN travel this one path — same channel, same phone — so
`identity.service.issue_otp` moves onto it here rather than continuing to write codes nowhere. The
real provider is post.tm, supplied by the human partner on 2026-08-17:

```
POST https://sms.post.tm/api/clients/sms/create
Authorization: Bearer <token>
Content-Type: application/json
{"phone": 99362615986, "content": "12345"}
```

Note the shape: `phone` is a **number without a leading `+`**, so `+99362615986` is sent as
`99362615986`, and `content` is the whole message. The token lives in `backend/.env` as
`SMS_API_TOKEN` and never in code or in `.env.example`. `SMS_PROVIDER=log` stays the default for
development and is forced in tests — a suite that can send real messages will eventually send one.

**Ruling Q4 — the overdue branch is four statuses, not six.** The spec's stage table lists
Reminder, Expired, Grace, Overdue, To remove, Removed, Closed. Reminder is a notification, not a
state, and "to remove" is `overdue` plus elapsed time. The statuses added are therefore `EXPIRED`,
`GRACE`, `OVERDUE`, `REMOVED`, `CLOSED`, and the admin work queue derives "needs removing" from
`overdue` and `remove_after`. Fewer states, same behaviour, and every one of them is reachable in a
test.

**Ruling Q5 — the cell stays held until the parcel physically leaves it.** `EXPIRED`, `GRACE` and
`OVERDUE` join `CELL_HELD_STATUSES`; `REMOVED` and `CLOSED` do not. The partial unique index is
built from that set, so this task rebuilds the index in its migration. Getting this wrong would
free a cell with a parcel still inside it.

---

## File structure

| File | Responsibility |
|---|---|
| `backend/app/core/config.py` (modify) | overdue intervals, payment and SMS settings |
| `backend/app/modules/notify/models.py` (create) | `Notification`, `NotificationChannel`, `NotificationKind` |
| `backend/app/modules/notify/sms.py` (create) | `SmsProvider` protocol, `LoggingSmsProvider`, `get_sms_provider()` |
| `backend/app/modules/notify/service.py` (create) | `notify(...)`, template rendering in three languages |
| `backend/app/api/mobile/notifications.py` (create) | feed, mark read, mark all read |
| `backend/app/modules/payments/models.py` (create) | `Payment`, `PaymentStatus` |
| `backend/app/modules/payments/provider.py` (create) | `PaymentProvider` protocol, `MockProvider`, signature verification |
| `backend/app/modules/payments/service.py` (create) | start a payment, settle a webhook event |
| `backend/app/api/mobile/payments.py` (create) | `POST /bookings/{id}/payment`, `GET /payments/{id}` |
| `backend/app/api/webhooks/payments.py` (create) | `POST /webhooks/payments/{provider}` |
| `backend/app/modules/booking/models.py` (modify) | the overdue statuses, `remove_after` |
| `backend/app/modules/booking/service.py` (modify) | `expire`, `to_grace`, `to_overdue`, `mark_removed` |
| `backend/app/modules/custody/models.py` (create) | `CustodyRecord`, `CustodyHandover` |
| `backend/app/modules/custody/service.py` (create) | file an act, hand over, dispose |
| `backend/app/api/admin/custody.py` (create) | the counter queue and its acts |
| `backend/app/workers/overdue.py` (create) | the escalation and reminder job |
| `backend/app/core/storage.py` (create) | `FileStorage` interface and a local-disk implementation |
| `backend/app/modules/catalog/models.py` (modify) | `PostamatPhoto` |
| `backend/app/api/admin/postamats.py` (modify) | photo upload and removal |
| `backend/app/api/public/media.py` (create) | serving a stored file by key |
| `backend/tests/{notify,payments,custody}/` (create) | tests per module |

---

### Task 1: Settings for the money and the clock

**Files:**
- Modify: `backend/app/core/config.py`, `backend/.env.example`, `backend/.env`
- Test: `backend/tests/core/test_config.py`

**Interfaces:**
- Produces on `Settings`: `payment_provider: str = "mock"`, `payment_webhook_secret: str`,
  `payment_return_url: str`, `sms_provider: str = "log"`, `sms_api_token: str = ""`,
  `reminder_hours_before: int = 2`, `grace_hours: int = 2`, `removal_after_hours: int = 24`,
  `custody_disposal_days: int = 30`.

- [ ] **Step 1: Write the failing test**

```python
def test_overdue_intervals_are_configuration_not_constants():
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:", jwt_secret="x" * 32,
                        pin_pepper="p", payment_webhook_secret="s")
    assert settings.reminder_hours_before == 2
    assert settings.grace_hours == 2
    assert settings.removal_after_hours == 24
    assert settings.custody_disposal_days == 30


def test_the_payment_and_sms_providers_default_to_the_local_stand_ins():
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:", jwt_secret="x" * 32,
                        pin_pepper="p", payment_webhook_secret="s")
    assert settings.payment_provider == "mock"
    assert settings.sms_provider == "log"
    assert settings.sms_api_token == ""
```

Add both to the existing `backend/tests/core/test_config.py`, importing `Settings` the way that
file already does.

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/core/test_config.py -v`
Expected: FAIL — `Settings` has no `reminder_hours_before`.

- [ ] **Step 3: Add the fields**

In `backend/app/core/config.py`, inside `Settings`, after `offline_ttl_hours`:

```python
    # Payments. The provider is named rather than imported so the mock and the
    # real acquirer are chosen by configuration, not by an edit.
    payment_provider: str = "mock"
    payment_webhook_secret: str
    payment_return_url: str = "https://example.invalid/payment/done"

    # SMS. One channel carries both the login code and the courier PIN.
    # "log" in development and in tests; "post_tm" against the real gateway.
    sms_provider: str = "log"
    sms_api_token: str = ""
    sms_base_url: str = "https://sms.post.tm"
    sms_timeout_seconds: float = 10.0

    # The overdue clock. Every stage is configuration because the product will
    # tune these without a deploy, and because a literal in a worker is a rule
    # nobody can find.
    reminder_hours_before: int = 2
    grace_hours: int = 2
    removal_after_hours: int = 24
    custody_disposal_days: int = 30
```

- [ ] **Step 4: Add the values to `.env` and `.env.example`**

```
PAYMENT_WEBHOOK_SECRET=dev-webhook-secret-change-me
```

- [ ] **Step 5: Run the test and confirm it passes, then commit**

```bash
./.venv/Scripts/python.exe -m pytest tests/core/test_config.py -v
git add app/core/config.py .env.example tests/core/test_config.py
git commit -m "feat: settings for payments, SMS and the overdue clock"
```

---

### Task 2: Notifications and the one SMS channel

**Files:**
- Create: `backend/app/modules/notify/__init__.py`, `models.py`, `sms.py`, `service.py`
- Create: `backend/tests/notify/__init__.py`, `backend/tests/notify/test_notify.py`
- Modify: `backend/alembic/env.py`

**Interfaces:**
- Produces: `Notification`, `NotificationKind`, `NotificationChannel`;
  `SmsProvider` (protocol with `async def send(phone: str, text: str) -> str`),
  `LoggingSmsProvider`, `get_sms_provider()`, `reset_sms_provider()` for tests;
  `async def notify(session, *, client_id, phone, kind, language, channel, **params) -> Notification`.

Templates live in one dict keyed by `(kind, language)`. Three languages, because the client's
language is already stored on `Client` and a Russian-only reminder is unusable at a Turkmen kiosk.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/notify/test_notify.py
from app.modules.notify.models import Notification, NotificationChannel, NotificationKind
from app.modules.notify.service import notify
from app.modules.notify.sms import get_sms_provider


async def test_an_sms_notification_is_recorded_and_sent(session):
    sent = get_sms_provider()
    row = await notify(
        session, client_id=None, phone="+99362123456",
        kind=NotificationKind.OTP, language="ru",
        channel=NotificationChannel.SMS, code="12345",
    )
    await session.commit()

    assert row.channel == NotificationChannel.SMS
    assert row.sent_at is not None
    assert sent.outbox[-1].phone == "+99362123456"
    # The code reaches the phone and nothing else: the stored text must not be
    # the place a five-digit door code is archived in the clear.
    assert "12345" in sent.outbox[-1].text
    assert "12345" not in row.body


async def test_the_language_picks_the_template(session):
    turkmen = await notify(session, client_id=None, phone="+99362123456",
                           kind=NotificationKind.OTP, language="tk",
                           channel=NotificationChannel.SMS, code="12345")
    russian = await notify(session, client_id=None, phone="+99362123456",
                           kind=NotificationKind.OTP, language="ru",
                           channel=NotificationChannel.SMS, code="12345")
    assert turkmen.body != russian.body


async def test_an_unknown_language_falls_back_rather_than_raising(session):
    row = await notify(session, client_id=None, phone="+99362123456",
                       kind=NotificationKind.OTP, language="fr",
                       channel=NotificationChannel.SMS, code="12345")
    assert row.body


async def test_the_gateway_gets_the_number_without_a_plus():
    # post.tm takes `phone` as a number. A stored "+99362615986" sent verbatim
    # is rejected, and the login code silently never arrives.
    import httpx

    from app.modules.notify.sms import PostTmSmsProvider

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={"id": "sms-1"})

    provider = PostTmSmsProvider(token="tok")
    transport = httpx.MockTransport(handler)
    await provider.send("+99362615986", "Postamat: login code 12345",
                        _transport=transport)

    assert captured["json"] == {"phone": 99362615986,
                                "content": "Postamat: login code 12345"}
    assert captured["auth"] == "Bearer tok"


async def test_an_in_app_notification_is_not_sent_anywhere(session):
    before = len(get_sms_provider().outbox)
    row = await notify(session, client_id=None, phone=None,
                       kind=NotificationKind.BOOKING_EXPIRING, language="ru",
                       channel=NotificationChannel.IN_APP, cell_number=7, hours=2)
    assert row.sent_at is None
    assert len(get_sms_provider().outbox) == before
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/notify -v`
Expected: FAIL — no module `app.modules.notify`.

- [ ] **Step 3: Write `backend/app/modules/notify/models.py`**

```python
import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class NotificationChannel(StrEnum):
    IN_APP = "in_app"
    SMS = "sms"


class NotificationKind(StrEnum):
    OTP = "otp"
    COURIER_CODE = "courier_code"
    BOOKING_CREATED = "booking_created"
    BOOKING_PAID = "booking_paid"
    PARCEL_DEPOSITED = "parcel_deposited"
    BOOKING_EXPIRING = "booking_expiring"
    BOOKING_EXPIRED = "booking_expired"
    BOOKING_OVERDUE = "booking_overdue"
    PARCEL_REMOVED = "parcel_removed"


class Notification(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "notifications"

    # Null for a recipient or a courier who has no account: the phone is the
    # only identity they have, and the parcel still has to reach them.
    client_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    booking_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    kind: Mapped[NotificationKind] = mapped_column(String(32), index=True)
    channel: Mapped[NotificationChannel] = mapped_column(String(16))
    phone: Mapped[str | None] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(120))
    # What the app shows. Secrets never land here — see the SMS body instead.
    body: Mapped[str] = mapped_column(String(500))
    details: Mapped[dict | None] = mapped_column(JSON)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(500))
```

- [ ] **Step 4: Write `backend/app/modules/notify/sms.py`**

```python
import logging
from dataclasses import dataclass, field
from typing import Protocol

from app.core.config import get_settings

logger = logging.getLogger("app.sms")


@dataclass
class SentMessage:
    phone: str
    text: str


class SmsProvider(Protocol):
    async def send(self, phone: str, text: str) -> str:
        """Deliver a message and return the provider's id for it."""


@dataclass
class LoggingSmsProvider:
    """Stand-in until the operator supplies a provider and a token.

    It keeps what it "sent" so tests can assert on it, and logs the phone with
    the message elided — an OTP or a door PIN in a log file is the same leak as
    one in the database.
    """

    outbox: list[SentMessage] = field(default_factory=list)

    async def send(self, phone: str, text: str) -> str:
        self.outbox.append(SentMessage(phone=phone, text=text))
        logger.info("sms to %s (%d chars)", phone, len(text))
        return f"log-{len(self.outbox)}"


@dataclass
class PostTmSmsProvider:
    """The operator's gateway.

    The API takes the number as digits without a leading plus and the whole
    message as `content`:

        POST /api/clients/sms/create
        {"phone": 99362615986, "content": "..."}
    """

    token: str
    base_url: str = "https://sms.post.tm"
    timeout: float = 10.0

    @staticmethod
    def _digits(phone: str) -> int:
        # +99362615986 -> 99362615986. Stored numbers always carry the plus;
        # this gateway rejects it.
        return int(phone.lstrip("+"))

    async def send(self, phone: str, text: str) -> str:
        import httpx

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/api/clients/sms/create",
                headers={"Authorization": f"Bearer {self.token}",
                         "accept": "application/json"},
                json={"phone": self._digits(phone), "content": text},
            )
        # The body may carry a message id; the status is what decides delivery
        # was accepted. Anything else raises and is stored on the notification.
        response.raise_for_status()
        try:
            return str(response.json().get("id", ""))
        except ValueError:
            return ""


_provider: SmsProvider | None = None


def get_sms_provider() -> SmsProvider:
    global _provider
    if _provider is None:
        settings = get_settings()
        if settings.sms_provider == "log":
            _provider = LoggingSmsProvider()
        elif settings.sms_provider == "post_tm":
            if not settings.sms_api_token:
                raise RuntimeError("SMS_API_TOKEN is required for the post_tm provider")
            _provider = PostTmSmsProvider(
                token=settings.sms_api_token, base_url=settings.sms_base_url,
                timeout=settings.sms_timeout_seconds,
            )
        else:
            raise RuntimeError(f"unknown SMS provider {settings.sms_provider!r}")
    return _provider


def reset_sms_provider() -> None:
    """Drop the cached provider. Tests use this; production never does."""
    global _provider
    _provider = None
```

- [ ] **Step 5: Write `backend/app/modules/notify/service.py`**

```python
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import utcnow
from app.modules.notify.models import (
    Notification,
    NotificationChannel,
    NotificationKind,
)
from app.modules.notify.sms import get_sms_provider

# (kind, language) -> (title, in-app body, sms text). The in-app body never
# carries a code: the feed is readable by anyone holding the phone, while the
# SMS is the delivery itself.
TEMPLATES: dict[tuple[NotificationKind, str], tuple[str, str, str]] = {
    (NotificationKind.OTP, "tk"): (
        "Giriş kody", "Bir gezeklik kod SMS bilen iberildi.",
        "Postamat: giriş kody {code}",
    ),
    (NotificationKind.OTP, "ru"): (
        "Код входа", "Одноразовый код отправлен по SMS.",
        "Постамат: код входа {code}",
    ),
    (NotificationKind.OTP, "en"): (
        "Login code", "A one-time code has been sent by SMS.",
        "Postamat: login code {code}",
    ),
    (NotificationKind.COURIER_CODE, "tk"): (
        "Kuryer kody", "Kuryere kod iberildi.",
        "Postamat: ýaçeýka {cell_number} üçin kod {code}",
    ),
    (NotificationKind.COURIER_CODE, "ru"): (
        "Код курьера", "Код отправлен курьеру.",
        "Постамат: код {code} для ячейки {cell_number}",
    ),
    (NotificationKind.COURIER_CODE, "en"): (
        "Courier code", "The code has been sent to the courier.",
        "Postamat: code {code} for cell {cell_number}",
    ),
    (NotificationKind.BOOKING_EXPIRING, "tk"): (
        "Möhlet gutarýar", "Ýaçeýka {cell_number}: {hours} sagatdan möhlet gutarýar.", "",
    ),
    (NotificationKind.BOOKING_EXPIRING, "ru"): (
        "Срок истекает", "Ячейка {cell_number}: срок хранения истекает через {hours} ч.", "",
    ),
    (NotificationKind.BOOKING_EXPIRING, "en"): (
        "Expiring soon", "Cell {cell_number}: storage ends in {hours} h.", "",
    ),
    (NotificationKind.BOOKING_EXPIRED, "tk"): (
        "Möhlet gutardy", "Ýaçeýka {cell_number}: möhlet gutardy, ibermäni alyň.", "",
    ),
    (NotificationKind.BOOKING_EXPIRED, "ru"): (
        "Срок истёк", "Ячейка {cell_number}: срок хранения истёк, заберите посылку.", "",
    ),
    (NotificationKind.BOOKING_EXPIRED, "en"): (
        "Storage ended", "Cell {cell_number}: storage has ended, please collect.", "",
    ),
    (NotificationKind.BOOKING_OVERDUE, "tk"): (
        "Gijä galdy", "Ýaçeýka {cell_number}: ibermäni gyssagly alyň.", "",
    ),
    (NotificationKind.BOOKING_OVERDUE, "ru"): (
        "Просрочено", "Ячейка {cell_number}: заберите посылку, иначе её снимут на стойку.", "",
    ),
    (NotificationKind.BOOKING_OVERDUE, "en"): (
        "Overdue", "Cell {cell_number}: collect the parcel or staff will move it.", "",
    ),
    (NotificationKind.PARCEL_REMOVED, "tk"): (
        "Iberme stoýkada", "Ibermäňiz stoýka geçirildi.", "",
    ),
    (NotificationKind.PARCEL_REMOVED, "ru"): (
        "Посылка на стойке", "Посылку переместили на стойку выдачи.", "",
    ),
    (NotificationKind.PARCEL_REMOVED, "en"): (
        "Parcel at the counter", "The parcel has been moved to the counter.", "",
    ),
}


def _template(kind: NotificationKind, language: str) -> tuple[str, str, str]:
    settings = get_settings()
    # An unknown language must never lose a message: the fallback is the default
    # language, not an exception in the middle of a worker.
    return TEMPLATES.get((kind, language)) or TEMPLATES[(kind, settings.default_language)]


async def notify(
    session: AsyncSession,
    *,
    client_id: uuid.UUID | None,
    phone: str | None,
    kind: NotificationKind,
    language: str,
    channel: NotificationChannel,
    booking_id: uuid.UUID | None = None,
    **params,
) -> Notification:
    """Record a notification and, for SMS, hand it to the provider."""
    title, body, sms = _template(kind, language)
    row = Notification(
        client_id=client_id, booking_id=booking_id, kind=kind, channel=channel,
        phone=phone, title=title, body=body.format(**params),
        details=params or None,
    )
    session.add(row)

    if channel is NotificationChannel.SMS:
        if not phone:
            row.error = "no phone number"
        else:
            try:
                await get_sms_provider().send(phone, sms.format(**params))
                row.sent_at = utcnow()
            except Exception as error:  # noqa: BLE001 - provider failure is data
                # A failed SMS must not fail the booking that triggered it. The
                # error is stored so the operator can see what never arrived.
                row.error = str(error)[:500]
    return row
```

- [ ] **Step 6: Register the models and reset the provider between tests**

`backend/alembic/env.py`:

```python
from app.modules.notify import models as notify_models  # noqa: F401  register models
```

`backend/tests/conftest.py`, beside `reset_kvstore`:

```python
@pytest.fixture(autouse=True)
def reset_sms():
    from app.modules.notify.sms import reset_sms_provider

    reset_sms_provider()
    yield
    reset_sms_provider()
```

- [ ] **Step 7: Run the tests, generate the migration, commit**

```bash
./.venv/Scripts/python.exe -m pytest tests/notify -v
./.venv/Scripts/python.exe -m alembic revision --autogenerate -m "notifications"
./.venv/Scripts/python.exe -m alembic upgrade head
git add app/modules/notify tests/notify tests/conftest.py alembic/env.py alembic/versions/<rev>_notifications.py
git commit -m "feat: notifications with one SMS channel behind an interface"
```

---

### Task 3: The login code actually goes somewhere

**Files:**
- Modify: `backend/app/modules/identity/service.py`, `backend/app/api/mobile/auth.py`
- Modify: `backend/app/api/mobile/bookings.py`
- Test: `backend/tests/notify/test_delivery.py`

**Interfaces:**
- Consumes: `notify(...)`, `NotificationKind`, `NotificationChannel` (Task 2).
- Changes: `issue_otp(phone)` becomes `issue_otp(session, phone, language)` and sends the code;
  the courier resend endpoint sends the PIN instead of returning it.

Until now `issue_otp` wrote a code into the key-value store and nothing carried it anywhere, and
the courier PIN was returned in the HTTP response because there was no channel. Both are the same
channel, and it now exists.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/notify/test_delivery.py
from app.modules.notify.sms import get_sms_provider


async def test_the_login_code_is_sent_by_sms(client):
    phone = "+99362123456"
    response = await client.post("/api/v1/auth/otp/request", json={"phone": phone})
    assert response.status_code == 200

    from app.modules.identity.service import peek_otp

    code = await peek_otp(phone)
    outbox = get_sms_provider().outbox
    assert outbox[-1].phone == phone
    assert code in outbox[-1].text
    # The response still refuses to carry the code itself.
    assert code not in response.text


async def test_the_courier_pin_is_sent_and_no_longer_returned(
    client, session, book, admin_token, city, cell_type, postamat
):
    from app.modules.catalog.models import Cell

    session.add(Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=1,
                     board=1, output=1))
    await session.commit()
    await client.put("/api/v1/admin/tariffs",
                     headers={"Authorization": f"Bearer {admin_token}"},
                     json={"entries": [
                         {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
                          "duration_hours": hours, "amount_minor": 1800}
                         for hours in (12, 24, 48)
                     ]})
    created = await book({
        "postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
        "duration_hours": 24, "recipient_phone": "+99365000001",
        "depositor": "courier", "courier_phone": "+99366000002",
    })
    booking_id = created.json()["id"]

    resent = await client.post(f"/api/v1/bookings/{booking_id}/courier/resend",
                               headers=created.request.headers)
    assert resent.status_code == 202
    assert "courier_code" not in resent.json()

    last = get_sms_provider().outbox[-1]
    assert last.phone == "+99366000002"
    assert len(last.text) > 0
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/notify/test_delivery.py -v`
Expected: FAIL — the outbox is empty and the resend endpoint still returns the code.

- [ ] **Step 3: Send the login code from `identity.service`**

Replace `issue_otp` in `backend/app/modules/identity/service.py`:

```python
async def issue_otp(session: AsyncSession, phone: str, language: str | None = None) -> int:
    settings = get_settings()
    code = "".join(secrets.choice("0123456789") for _ in range(settings.otp_length))
    await get_kvstore().put(
        _otp_key(phone), {"code": code, "attempts": "0"}, settings.otp_ttl_seconds
    )
    # The client may not exist yet — this is the registration path — so the
    # language comes from the request rather than from a profile.
    await notify(
        session, client_id=None, phone=phone, kind=NotificationKind.OTP,
        language=language or settings.default_language,
        channel=NotificationChannel.SMS, code=code,
    )
    await session.commit()
    return settings.otp_ttl_seconds
```

Imports to add there: `from app.modules.notify.models import NotificationChannel, NotificationKind`
and `from app.modules.notify.service import notify`.

- [ ] **Step 4: Update the route**

In `backend/app/api/mobile/auth.py`:

```python
@router.post(
    "/auth/otp/request", response_model=OtpRequestResult,
    dependencies=[Depends(rate_limit("otp", limit=3, window_seconds=600))],
)
async def request_otp(
    payload: OtpRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> OtpRequestResult:
    ttl = await service.issue_otp(session, payload.phone, get_language(request))
    return OtpRequestResult(code_length=get_settings().otp_length, expires_in_seconds=ttl)
```

Add `from fastapi import Request` and `from app.core.context import get_language`.

- [ ] **Step 5: Send the courier PIN instead of returning it**

In `backend/app/api/mobile/bookings.py`, the resend handler ends:

```python
    code = reissue_code(booking, CodePurpose.COURIER)
    numbers = await cell_numbers(session, [booking.cell_id])
    await notify(
        session, client_id=None, phone=booking.courier_phone,
        kind=NotificationKind.COURIER_CODE, language=client.language,
        channel=NotificationChannel.SMS, booking_id=booking.id,
        code=code, cell_number=numbers.get(booking.cell_id, 0),
    )
    await service.record_event(session, booking, booking.status,
                               "PIN курьера отправлен повторно")
    await session.commit()
    # 202, and no body: the code goes to the courier's phone and never back
    # through the sender's screen, which is the whole point of a courier PIN.
    return Response(status_code=status.HTTP_202_ACCEPTED)
```

Change the route decorator to `@router.post("/{booking_id}/courier/resend", status_code=202)`,
drop `response_model`, and delete `CourierCodeOut` from the schemas — it has no user left.

- [ ] **Step 6: Fix the callers the change breaks**

`backend/tests/booking/test_client_routes.py` asserts on `courier_code`; rewrite those two tests to
read the outbox instead. `backend/scripts/smoke.py` prints code lengths from the booking response,
which still works, and its OTP section reads `peek_otp`, which still works.

- [ ] **Step 7: Run the whole suite and commit**

```bash
./.venv/Scripts/python.exe -m pytest -q
git add app/modules/identity/service.py app/api/mobile/auth.py app/api/mobile/bookings.py \
        app/modules/booking/schemas.py tests/notify/test_delivery.py tests/booking/test_client_routes.py
git commit -m "feat: deliver the login code and the courier PIN over SMS"
```

---

### Task 4: The notification feed

**Files:**
- Create: `backend/app/api/mobile/notifications.py`, `backend/tests/notify/test_feed.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces: `GET /api/v1/notifications` (paged, newest first, `unread=true` filter),
  `POST /api/v1/notifications/{id}/read`, `POST /api/v1/notifications/read-all`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/notify/test_feed.py
from app.modules.notify.models import Notification, NotificationChannel, NotificationKind


async def _seed(session, client_id, count):
    for index in range(count):
        session.add(Notification(
            client_id=client_id, kind=NotificationKind.BOOKING_EXPIRED,
            channel=NotificationChannel.IN_APP, title=f"t{index}", body=f"b{index}",
        ))
    await session.commit()


async def test_the_feed_shows_only_this_clients_notifications(
    client, session, booking_client, client_token
):
    import uuid

    await _seed(session, booking_client.id, 2)
    await _seed(session, uuid.uuid4(), 3)

    response = await client.get("/api/v1/notifications",
                                headers={"Authorization": f"Bearer {client_token}"})
    assert response.status_code == 200
    assert response.json()["pagination"]["total"] == 2


async def test_marking_one_read_leaves_the_others(
    client, session, booking_client, client_token
):
    await _seed(session, booking_client.id, 2)
    headers = {"Authorization": f"Bearer {client_token}"}
    listed = await client.get("/api/v1/notifications?unread=true", headers=headers)
    first = listed.json()["items"][0]["id"]

    await client.post(f"/api/v1/notifications/{first}/read", headers=headers)
    unread = await client.get("/api/v1/notifications?unread=true", headers=headers)
    assert [item["id"] for item in unread.json()["items"]] == [
        item["id"] for item in listed.json()["items"][1:]
    ]


async def test_read_all_empties_the_unread_list(
    client, session, booking_client, client_token
):
    await _seed(session, booking_client.id, 3)
    headers = {"Authorization": f"Bearer {client_token}"}

    await client.post("/api/v1/notifications/read-all", headers=headers)
    unread = await client.get("/api/v1/notifications?unread=true", headers=headers)
    assert unread.json()["items"] == []


async def test_another_clients_notification_cannot_be_marked_read(
    client, session, booking_client, client_token
):
    import uuid

    other = uuid.uuid4()
    await _seed(session, other, 1)
    from sqlalchemy import select

    row = await session.scalar(select(Notification).where(Notification.client_id == other))
    response = await client.post(f"/api/v1/notifications/{row.id}/read",
                                 headers={"Authorization": f"Bearer {client_token}"})
    assert response.status_code == 404
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/notify/test_feed.py -v`
Expected: FAIL — 404, the routes do not exist.

- [ ] **Step 3: Write `backend/app/api/mobile/notifications.py`**

```python
import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_client
from app.core.errors import AppError, ErrorCode
from app.core.pagination import PageMeta, PageParams, page_params, paginate_page
from app.core.types import utc_isoformat
from app.modules.identity.models import Client
from app.modules.notify.models import Notification, NotificationKind

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: uuid.UUID
    kind: NotificationKind
    title: str
    body: str
    booking_id: uuid.UUID | None
    is_read: bool
    created_at: str


class NotificationPage(BaseModel):
    items: list[NotificationOut]
    pagination: PageMeta


def _out(row: Notification) -> NotificationOut:
    return NotificationOut(
        id=row.id, kind=row.kind, title=row.title, body=row.body,
        booking_id=row.booking_id, is_read=row.is_read,
        created_at=utc_isoformat(row.created_at),
    )


@router.get("", response_model=NotificationPage)
async def list_notifications(
    unread: bool = Query(default=False),
    params: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> NotificationPage:
    stmt = (
        select(Notification).where(Notification.client_id == client.id)
        .order_by(Notification.created_at.desc())
    )
    if unread:
        stmt = stmt.where(Notification.is_read.is_(False))
    rows, meta = await paginate_page(session, stmt, params)
    return NotificationPage(items=[_out(row) for row in rows], pagination=meta)


@router.post("/{notification_id}/read", status_code=204)
async def mark_read(
    notification_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> Response:
    row = await session.get(Notification, notification_id)
    # Someone else's notification is not found, not forbidden: its existence is
    # not a fact this caller gets to confirm.
    if row is None or row.client_id != client.id:
        raise AppError(ErrorCode.NOT_FOUND, "Notification not found.", 404)
    row.is_read = True
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/read-all", status_code=204)
async def mark_all_read(
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> Response:
    await session.execute(
        update(Notification)
        .where(Notification.client_id == client.id, Notification.is_read.is_(False))
        .values(is_read=True)
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

- [ ] **Step 4: Register the router and run the tests**

In `backend/app/main.py`, beside the other mobile routers:

```python
from app.api.mobile import notifications as mobile_notifications
...
    app.include_router(mobile_notifications.router, prefix=API_PREFIX)
```

Note for the implementer: `/read-all` must be declared after `/{notification_id}/read` only if the
router matched greedily — FastAPI matches in declaration order, and `read-all` is not a UUID, so
the path converter rejects it and falls through. Keep the order above and the test proves it.

- [ ] **Step 5: Commit**

```bash
./.venv/Scripts/python.exe -m pytest tests/notify -v
git add app/api/mobile/notifications.py app/main.py tests/notify/test_feed.py
git commit -m "feat: the client notification feed"
```

---

### Task 5: Payments — the model and the provider seam

**Files:**
- Create: `backend/app/modules/payments/__init__.py`, `models.py`, `provider.py`
- Create: `backend/tests/payments/__init__.py`, `backend/tests/payments/test_provider.py`
- Modify: `backend/alembic/env.py`

**Interfaces:**
- Produces: `Payment`, `PaymentStatus`; `PaymentProvider` protocol with
  `async def start(payment_id, amount_minor, currency, return_url) -> StartedPayment` and
  `def parse_webhook(headers, body) -> WebhookEvent`; `MockProvider`; `get_payment_provider()`,
  `reset_payment_provider()`.
- `StartedPayment(provider_payment_id: str, redirect_url: str)`.
- `WebhookEvent(provider_payment_id: str, payment_id: uuid.UUID, succeeded: bool)`.

Card data never touches this server: `start` returns a URL the app opens, and the result comes back
as a webhook. That is the whole reason the interface has this shape (spec §9).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/payments/test_provider.py
import json
import uuid

import pytest

from app.core.config import get_settings
from app.modules.payments.provider import (
    MockProvider,
    get_payment_provider,
    sign_payload,
)


def test_starting_a_payment_returns_a_redirect_and_an_id():
    provider = MockProvider()
    payment_id = uuid.uuid4()
    started = await_sync(provider.start(payment_id, 1800, "TMT", "https://x.invalid/done"))
    assert started.provider_payment_id
    assert str(payment_id) in started.redirect_url


def test_a_webhook_without_a_valid_signature_is_refused():
    provider = MockProvider()
    body = json.dumps({"payment_id": str(uuid.uuid4()), "status": "succeeded"}).encode()
    with pytest.raises(ValueError):
        provider.parse_webhook({"X-Signature": "nonsense"}, body)


def test_a_signed_webhook_parses():
    provider = MockProvider()
    payment_id = uuid.uuid4()
    body = json.dumps({
        "payment_id": str(payment_id), "provider_payment_id": "mock-1",
        "status": "succeeded",
    }).encode()
    signature = sign_payload(body, get_settings().payment_webhook_secret)

    event = provider.parse_webhook({"X-Signature": signature}, body)
    assert event.payment_id == payment_id
    assert event.succeeded is True


def test_the_provider_is_chosen_by_configuration():
    assert isinstance(get_payment_provider(), MockProvider)
```

`await_sync` is a two-line helper at the top of the test module:

```python
import asyncio


def await_sync(coro):
    return asyncio.get_event_loop().run_until_complete(coro)
```

Simpler: mark the first test `async def` and drop the helper. Do that — the suite is already
`asyncio_mode = auto`.

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/payments -v`
Expected: FAIL — no module `app.modules.payments`.

- [ ] **Step 3: Write `backend/app/modules/payments/models.py`**

```python
import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Payment(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "payments"

    booking_id: Mapped[uuid.UUID] = mapped_column(index=True)
    client_id: Mapped[uuid.UUID] = mapped_column(index=True)
    provider: Mapped[str] = mapped_column(String(32))
    # Unique so a webhook delivered twice — which every acquirer does — settles
    # once. There is no refund state on purpose: see Ruling Q1.
    provider_payment_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="TMT")
    status: Mapped[PaymentStatus] = mapped_column(
        String(16), index=True, default=PaymentStatus.PENDING
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(200))
    details: Mapped[dict | None] = mapped_column(JSON)
```

- [ ] **Step 4: Write `backend/app/modules/payments/provider.py`**

```python
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Protocol

from app.core.config import get_settings


@dataclass(frozen=True)
class StartedPayment:
    provider_payment_id: str
    redirect_url: str


@dataclass(frozen=True)
class WebhookEvent:
    payment_id: uuid.UUID
    provider_payment_id: str
    succeeded: bool
    reason: str | None = None


class PaymentProvider(Protocol):
    name: str

    async def start(
        self, payment_id: uuid.UUID, amount_minor: int, currency: str, return_url: str
    ) -> StartedPayment:
        """Open a payment at the acquirer and return where to send the customer."""

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        """Verify the callback's signature and read the result out of it."""


def sign_payload(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class MockProvider:
    """Stands in until the bank is named.

    It behaves like an acquirer in the two ways that matter to our code: the
    customer leaves for a URL of the provider's choosing, and the result comes
    back later as a signed webhook rather than as the response to our call.
    """

    name = "mock"

    async def start(
        self, payment_id: uuid.UUID, amount_minor: int, currency: str, return_url: str
    ) -> StartedPayment:
        return StartedPayment(
            provider_payment_id=f"mock-{payment_id}",
            redirect_url=(
                f"https://pay.invalid/mock/{payment_id}"
                f"?amount={amount_minor}&currency={currency}&return_url={return_url}"
            ),
        )

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        settings = get_settings()
        signature = headers.get("x-signature") or headers.get("X-Signature") or ""
        expected = sign_payload(body, settings.payment_webhook_secret)
        # compare_digest, not ==: a webhook endpoint is unauthenticated and
        # timing on a string compare is a signature oracle.
        if not hmac.compare_digest(signature, expected):
            raise ValueError("bad signature")

        payload = json.loads(body)
        return WebhookEvent(
            payment_id=uuid.UUID(payload["payment_id"]),
            provider_payment_id=payload.get("provider_payment_id", ""),
            succeeded=payload.get("status") == "succeeded",
            reason=payload.get("reason"),
        )


_provider: PaymentProvider | None = None


def get_payment_provider() -> PaymentProvider:
    global _provider
    if _provider is None:
        settings = get_settings()
        if settings.payment_provider != "mock":
            raise RuntimeError(f"unknown payment provider {settings.payment_provider!r}")
        _provider = MockProvider()
    return _provider


def reset_payment_provider() -> None:
    global _provider
    _provider = None
```

- [ ] **Step 5: Register the model, run the tests, migrate, commit**

```bash
./.venv/Scripts/python.exe -m pytest tests/payments -v
./.venv/Scripts/python.exe -m alembic revision --autogenerate -m "payments"
./.venv/Scripts/python.exe -m alembic upgrade head
git add app/modules/payments tests/payments alembic/env.py alembic/versions/<rev>_payments.py
git commit -m "feat: payment records behind a provider interface"
```

---

### Task 6: Starting a payment and settling the webhook

**Files:**
- Create: `backend/app/modules/payments/service.py`, `backend/app/api/mobile/payments.py`,
  `backend/app/api/webhooks/__init__.py`, `backend/app/api/webhooks/payments.py`
- Create: `backend/tests/payments/test_flow.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces: `async def start_payment(session, booking, client) -> tuple[Payment, str]`;
  `async def settle(session, event) -> Payment`;
  `POST /api/v1/bookings/{booking_id}/payment` → `{"payment_id", "redirect_url", "amount_minor", "currency"}`;
  `GET /api/v1/payments/{payment_id}`; `POST /api/v1/webhooks/payments/{provider}`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/payments/test_flow.py
import json
import uuid

from sqlalchemy import select

from app.core.config import get_settings
from app.modules.booking.models import Booking, BookingStatus
from app.modules.catalog.models import Cell
from app.modules.payments.models import Payment, PaymentStatus
from app.modules.payments.provider import sign_payload


async def _booked(client, session, book, admin_token, city, cell_type, postamat):
    session.add(Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=1,
                     board=1, output=1))
    await session.commit()
    await client.put("/api/v1/admin/tariffs",
                     headers={"Authorization": f"Bearer {admin_token}"},
                     json={"entries": [
                         {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
                          "duration_hours": hours, "amount_minor": 1800}
                         for hours in (12, 24, 48)
                     ]})
    created = await book({"postamat_id": str(postamat.id),
                          "cell_type_id": str(cell_type.id), "duration_hours": 24,
                          "recipient_phone": "+99365000001"})
    return created.json()


def _webhook(payment_id, succeeded=True):
    body = json.dumps({
        "payment_id": str(payment_id), "provider_payment_id": f"mock-{payment_id}",
        "status": "succeeded" if succeeded else "failed",
        "reason": None if succeeded else "declined",
    }).encode()
    signature = sign_payload(body, get_settings().payment_webhook_secret)
    return body, {"X-Signature": signature, "Content-Type": "application/json"}


async def test_paying_moves_the_booking_and_clears_the_hold(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    booking = await _booked(client, session, book, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}

    started = await client.post(f"/api/v1/bookings/{booking['id']}/payment",
                                headers=headers | {"Idempotency-Key": "pay-1"})
    assert started.status_code == 201
    assert started.json()["amount_minor"] == 1800
    assert started.json()["redirect_url"].startswith("https://")
    payment_id = started.json()["payment_id"]

    body, hook_headers = _webhook(payment_id)
    delivered = await client.post("/api/v1/webhooks/payments/mock", content=body,
                                  headers=hook_headers)
    assert delivered.status_code == 200

    detail = await client.get(f"/api/v1/bookings/{booking['id']}", headers=headers)
    assert detail.json()["status"] == BookingStatus.AWAITING_DEPOSIT
    assert detail.json()["hold_expires_at"] is None
    assert [event["status"] for event in detail.json()["timeline"]] == [
        "pending_payment", "paid", "awaiting_deposit",
    ]


async def test_the_same_webhook_twice_settles_once(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    booking = await _booked(client, session, book, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    started = await client.post(f"/api/v1/bookings/{booking['id']}/payment",
                                headers=headers | {"Idempotency-Key": "pay-2"})
    body, hook_headers = _webhook(started.json()["payment_id"])

    first = await client.post("/api/v1/webhooks/payments/mock", content=body,
                              headers=hook_headers)
    second = await client.post("/api/v1/webhooks/payments/mock", content=body,
                               headers=hook_headers)
    assert (first.status_code, second.status_code) == (200, 200)

    detail = await client.get(f"/api/v1/bookings/{booking['id']}", headers=headers)
    # Not five entries: the second delivery must not walk the booking through
    # paid a second time.
    assert len(detail.json()["timeline"]) == 3


async def test_an_unsigned_webhook_is_refused(client):
    body = json.dumps({"payment_id": str(uuid.uuid4()), "status": "succeeded"}).encode()
    response = await client.post("/api/v1/webhooks/payments/mock", content=body,
                                 headers={"X-Signature": "nope"})
    assert response.status_code == 400


async def test_a_declined_payment_releases_the_cell(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    booking = await _booked(client, session, book, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    started = await client.post(f"/api/v1/bookings/{booking['id']}/payment",
                                headers=headers | {"Idempotency-Key": "pay-3"})
    body, hook_headers = _webhook(started.json()["payment_id"], succeeded=False)
    await client.post("/api/v1/webhooks/payments/mock", content=body, headers=hook_headers)

    detail = await client.get(f"/api/v1/bookings/{booking['id']}", headers=headers)
    assert detail.json()["status"] == BookingStatus.CANCELLED

    availability = await client.get(f"/api/v1/postamats/{postamat.id}/availability")
    assert availability.json()["items"][0]["free"] == 1


async def test_money_arriving_after_the_hold_expired_is_flagged_for_a_human(
    client, session, book, client_token, admin_token, city, cell_type, postamat
):
    # No refunds (Ruling Q1), so this is money taken for a booking that no
    # longer exists. It must never disappear quietly.
    from app.modules.audit.models import AuditEntry, Severity
    from app.workers.holds import release_expired_holds
    from app.core.db import utcnow
    from datetime import timedelta

    booking = await _booked(client, session, book, admin_token, city, cell_type, postamat)
    headers = {"Authorization": f"Bearer {client_token}"}
    started = await client.post(f"/api/v1/bookings/{booking['id']}/payment",
                                headers=headers | {"Idempotency-Key": "pay-4"})

    row = await session.get(Booking, uuid.UUID(booking["id"]))
    row.hold_expires_at = utcnow() - timedelta(minutes=1)
    await session.commit()
    await release_expired_holds(session)

    body, hook_headers = _webhook(started.json()["payment_id"])
    response = await client.post("/api/v1/webhooks/payments/mock", content=body,
                                 headers=hook_headers)
    assert response.status_code == 200

    payment = await session.scalar(select(Payment))
    assert payment.status == PaymentStatus.SUCCEEDED

    entry = await session.scalar(
        select(AuditEntry).where(AuditEntry.event == "payment.needs_attention")
    )
    assert entry is not None
    assert entry.severity == Severity.WARNING
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/payments/test_flow.py -v`
Expected: FAIL — 404 on both new paths.

- [ ] **Step 3: Write `backend/app/modules/payments/service.py`**

```python
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import utcnow
from app.core.errors import AppError, ErrorCode
from app.modules.audit.models import Severity, Source
from app.modules.audit.service import record
from app.modules.booking import service as booking_service
from app.modules.booking.models import Booking, BookingStatus
from app.modules.payments.models import Payment, PaymentStatus
from app.modules.payments.provider import WebhookEvent, get_payment_provider


async def start_payment(
    session: AsyncSession, booking: Booking, client_id: uuid.UUID
) -> tuple[Payment, str]:
    if booking.status is not BookingStatus.PENDING_PAYMENT:
        raise AppError(ErrorCode.BOOKING_INVALID_STATE,
                       "This booking is not awaiting payment.", 409)

    provider = get_payment_provider()
    payment = Payment(
        booking_id=booking.id, client_id=client_id, provider=provider.name,
        amount_minor=booking.amount_minor, currency=booking.currency,
    )
    session.add(payment)
    await session.flush()

    started = await provider.start(
        payment.id, payment.amount_minor, payment.currency,
        get_settings().payment_return_url,
    )
    payment.provider_payment_id = started.provider_payment_id
    await session.commit()
    return payment, started.redirect_url


async def settle(session: AsyncSession, event: WebhookEvent) -> Payment:
    """Apply a provider callback exactly once.

    Acquirers redeliver: the same event arrives two or five times, and each
    extra delivery must be a no-op rather than a second walk through the
    lifecycle.
    """
    payment = await session.get(Payment, event.payment_id)
    if payment is None:
        raise AppError(ErrorCode.NOT_FOUND, "Unknown payment.", 404)
    if payment.status is not PaymentStatus.PENDING:
        return payment

    booking = await session.get(Booking, payment.booking_id)
    payment.settled_at = utcnow()
    payment.provider_payment_id = (
        event.provider_payment_id or payment.provider_payment_id
    )

    if not event.succeeded:
        payment.status = PaymentStatus.FAILED
        payment.failure_reason = event.reason
        if booking is not None and booking.status is BookingStatus.PENDING_PAYMENT:
            await booking_service.cancel(
                session, booking, reason="Оплата не прошла", actor="system"
            )
        await session.commit()
        return payment

    payment.status = PaymentStatus.SUCCEEDED
    if booking is None or booking.status is not BookingStatus.PENDING_PAYMENT:
        # Money for a booking that is gone — the hold ran out while the customer
        # was on the bank's page. There is no refund in this system (Ruling Q1),
        # so the only honest thing is to make it visible and let a person settle
        # it; failing the webhook would just make the acquirer retry forever.
        await record(
            session, event="payment.needs_attention", source=Source.SYSTEM,
            severity=Severity.WARNING,
            message="Оплата поступила по брони, которая уже не активна.",
            details={"payment_id": str(payment.id),
                     "booking_id": str(payment.booking_id),
                     "amount_minor": payment.amount_minor},
        )
        await session.commit()
        return payment

    await booking_service.mark_paid(session, booking)
    await session.commit()
    return payment
```

- [ ] **Step 4: Write the two routers**

`backend/app/api/mobile/payments.py`:

```python
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_client
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import require_idempotency_key
from app.core.types import utc_isoformat
from app.modules.booking.models import Booking
from app.modules.identity.models import Client
from app.modules.payments import service
from app.modules.payments.models import Payment, PaymentStatus

router = APIRouter(tags=["payments"])


class PaymentStarted(BaseModel):
    payment_id: uuid.UUID
    redirect_url: str
    amount_minor: int
    currency: str


class PaymentOut(BaseModel):
    id: uuid.UUID
    booking_id: uuid.UUID
    status: PaymentStatus
    amount_minor: int
    currency: str
    settled_at: str | None
    failure_reason: str | None


@router.post("/bookings/{booking_id}/payment", response_model=PaymentStarted,
             status_code=201, dependencies=[Depends(require_idempotency_key)])
async def start_payment(
    booking_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> PaymentStarted:
    booking = await session.get(Booking, booking_id)
    if booking is None or booking.client_id != client.id:
        raise AppError(ErrorCode.NOT_FOUND, "Booking not found.", 404)

    payment, redirect_url = await service.start_payment(session, booking, client.id)
    return PaymentStarted(
        payment_id=payment.id, redirect_url=redirect_url,
        amount_minor=payment.amount_minor, currency=payment.currency,
    )


@router.get("/payments/{payment_id}", response_model=PaymentOut)
async def get_payment(
    payment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> PaymentOut:
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.client_id != client.id:
        raise AppError(ErrorCode.NOT_FOUND, "Payment not found.", 404)
    return PaymentOut(
        id=payment.id, booking_id=payment.booking_id, status=payment.status,
        amount_minor=payment.amount_minor, currency=payment.currency,
        settled_at=utc_isoformat(payment.settled_at) if payment.settled_at else None,
        failure_reason=payment.failure_reason,
    )
```

`backend/app/api/webhooks/payments.py`:

```python
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.errors import AppError, ErrorCode
from app.modules.payments import service
from app.modules.payments.provider import get_payment_provider

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/payments/{provider}")
async def payment_callback(
    provider: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Unauthenticated by necessity, trusted only through its signature."""
    handler = get_payment_provider()
    if provider != handler.name:
        raise AppError(ErrorCode.NOT_FOUND, "Unknown payment provider.", 404)

    body = await request.body()
    try:
        event = handler.parse_webhook(dict(request.headers), body)
    except ValueError:
        # Deliberately terse: an attacker probing the endpoint learns only that
        # the signature was wrong, never which part of the payload was read.
        raise AppError(ErrorCode.VALIDATION_FAILED, "Invalid webhook signature.",
                       400) from None

    payment = await service.settle(session, event)
    return {"status": payment.status}
```

Register both in `backend/app/main.py`.

- [ ] **Step 5: Run the tests and commit**

```bash
./.venv/Scripts/python.exe -m pytest tests/payments -v
./.venv/Scripts/python.exe -m pytest -q
git add app/modules/payments/service.py app/api/mobile/payments.py app/api/webhooks \
        app/main.py tests/payments/test_flow.py
git commit -m "feat: start a payment and settle its webhook exactly once"
```

---

### Task 7: The overdue statuses

**Files:**
- Modify: `backend/app/modules/booking/models.py`, `backend/app/modules/booking/service.py`
- Test: `backend/tests/booking/test_overdue_states.py`

**Interfaces:**
- Produces: `BookingStatus.EXPIRED`, `.GRACE`, `.OVERDUE`, `.REMOVED`, `.CLOSED`;
  `Booking.remove_after`; transitions `expire`, `to_grace`, `to_overdue`, `mark_removed`,
  `close_custody`.
- `CELL_HELD_STATUSES` gains `EXPIRED`, `GRACE`, `OVERDUE`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/booking/test_overdue_states.py
import uuid

import pytest

from app.core.errors import AppError
from app.modules.booking.models import Booking, BookingStatus, CELL_HELD_STATUSES
from app.modules.booking.service import (
    close_custody,
    expire,
    mark_removed,
    to_grace,
    to_overdue,
)


def _booking(status=BookingStatus.AWAITING_PICKUP) -> Booking:
    return Booking(
        client_id=uuid.uuid4(), postamat_id=uuid.uuid4(), cell_id=uuid.uuid4(),
        cell_type_id=uuid.uuid4(), duration_hours=24, amount_minor=1800,
        status=status, recipient_phone="+99362123456",
    )


def test_an_overdue_parcel_still_holds_its_cell():
    for status in (BookingStatus.EXPIRED, BookingStatus.GRACE, BookingStatus.OVERDUE):
        assert status in CELL_HELD_STATUSES
    # Removal is what frees it, and only after the act is filed.
    assert BookingStatus.REMOVED not in CELL_HELD_STATUSES
    assert BookingStatus.CLOSED not in CELL_HELD_STATUSES


async def test_the_escalation_walks_one_stage_at_a_time(session):
    booking = _booking()
    session.add(booking)
    await session.flush()

    await expire(session, booking)
    assert booking.status is BookingStatus.EXPIRED
    await to_grace(session, booking)
    assert booking.status is BookingStatus.GRACE
    await to_overdue(session, booking)
    assert booking.status is BookingStatus.OVERDUE
    assert booking.remove_after is not None
    await mark_removed(session, booking)
    assert booking.status is BookingStatus.REMOVED
    await close_custody(session, booking)
    assert booking.status is BookingStatus.CLOSED

    assert [event.seq for event in booking.events] == list(range(5))


async def test_a_collected_parcel_cannot_go_overdue(session):
    booking = _booking(status=BookingStatus.COMPLETED)
    session.add(booking)
    await session.flush()
    with pytest.raises(AppError):
        await expire(session, booking)


async def test_pickup_still_works_while_expired_and_overdue(session):
    from app.modules.booking.service import mark_collected

    for status in (BookingStatus.EXPIRED, BookingStatus.GRACE, BookingStatus.OVERDUE):
        booking = _booking(status=status)
        session.add(booking)
        await session.flush()
        # Collection stays free at every stage — the product charges nothing for
        # being late, so the door must keep opening.
        await mark_collected(session, booking)
        assert booking.status is BookingStatus.COMPLETED
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_overdue_states.py -v`
Expected: FAIL — `BookingStatus` has no `EXPIRED`.

- [ ] **Step 3: Extend the model**

In `backend/app/modules/booking/models.py`, add to `BookingStatus`:

```python
    EXPIRED = "expired"
    GRACE = "grace"
    OVERDUE = "overdue"
    REMOVED = "removed"
    CLOSED = "closed"
```

Extend `CELL_HELD_STATUSES` with `EXPIRED`, `GRACE`, `OVERDUE` — and note in the comment there
that the partial index must be rebuilt whenever this set changes. Add to `Booking`:

```python
    # When staff may take the parcel out. Stored rather than computed so the
    # work queue can be a plain query and the interval can change without
    # retroactively moving parcels that are already overdue.
    remove_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 4: Extend the state machine**

In `ALLOWED_TRANSITIONS`:

```python
    BookingStatus.AWAITING_PICKUP: frozenset(
        {BookingStatus.COMPLETED, BookingStatus.EXPIRED}
    ),
    BookingStatus.EXPIRED: frozenset({BookingStatus.COMPLETED, BookingStatus.GRACE}),
    BookingStatus.GRACE: frozenset({BookingStatus.COMPLETED, BookingStatus.OVERDUE}),
    BookingStatus.OVERDUE: frozenset({BookingStatus.COMPLETED, BookingStatus.REMOVED}),
    BookingStatus.REMOVED: frozenset({BookingStatus.CLOSED}),
    BookingStatus.CLOSED: frozenset(),
```

Collection remains reachable from every overdue stage: nothing is charged for being late, so the
recipient can still take their parcel until staff physically remove it.

Then the transitions:

```python
async def expire(session: AsyncSession, booking: Booking) -> Booking:
    return await _move(session, booking, BookingStatus.EXPIRED, "Срок хранения истёк")


async def to_grace(session: AsyncSession, booking: Booking) -> Booking:
    return await _move(session, booking, BookingStatus.GRACE, "Льготный период")


async def to_overdue(session: AsyncSession, booking: Booking) -> Booking:
    settings = get_settings()
    booking.remove_after = utcnow() + timedelta(hours=settings.removal_after_hours)
    return await _move(session, booking, BookingStatus.OVERDUE, "Просрочено")


async def mark_removed(session: AsyncSession, booking: Booking) -> Booking:
    # This is what frees the cell, and it may only be called by custody, after
    # an act exists: a cell freed without a record is a parcel nobody can trace.
    return await _move(session, booking, BookingStatus.REMOVED, "Посылка изъята")


async def close_custody(session: AsyncSession, booking: Booking) -> Booking:
    return await _move(session, booking, BookingStatus.CLOSED, "Дело закрыто")
```

- [ ] **Step 5: Migration — the index has to be rebuilt**

```bash
./.venv/Scripts/python.exe -m alembic revision --autogenerate -m "overdue statuses"
```

Autogenerate will add `remove_after` but will **not** notice that
`uq_active_booking_per_cell`'s `WHERE` clause changed, because its predicate is a text literal. Add
by hand, inside `upgrade()`:

```python
    op.drop_index('uq_active_booking_per_cell', table_name='bookings')
    op.create_index(
        'uq_active_booking_per_cell', 'bookings', ['cell_id'], unique=True,
        sqlite_where=sa.text(
            "status IN ('awaiting_deposit', 'awaiting_pickup', 'expired', 'grace', "
            "'overdue', 'paid', 'pending_payment')"
        ),
    )
```

and the reverse in `downgrade()`. Verify afterwards:

```bash
./.venv/Scripts/python.exe -m alembic upgrade head
./.venv/Scripts/python.exe -c "import sqlite3; print(sqlite3.connect('dev.db').execute(\"SELECT sql FROM sqlite_master WHERE name='uq_active_booking_per_cell'\").fetchone()[0])"
```

Expected: the printed predicate lists all seven statuses. If it lists four, an overdue parcel's
cell can be sold to somebody else while the parcel is still inside it.

- [ ] **Step 6: Run the tests and commit**

```bash
./.venv/Scripts/python.exe -m pytest -q
git add app/modules/booking/models.py app/modules/booking/service.py \
        alembic/versions/<rev>_overdue_statuses.py tests/booking/test_overdue_states.py
git commit -m "feat: the overdue branch of the booking lifecycle"
```

---

### Task 8: The worker that walks the clock

**Files:**
- Create: `backend/app/workers/overdue.py`, `backend/tests/booking/test_overdue_worker.py`

**Interfaces:**
- Produces: `async def run_escalation(session, now=None) -> dict[str, int]` returning counts per
  stage, and `async def run_once() -> dict[str, int]`.

Stages, each reading `Settings`: remind `reminder_hours_before` hours before `expires_at`; expire at
`expires_at`; enter grace `grace_hours` later, or — for a postamat that is closed at that moment —
at the next opening plus `grace_hours`; go overdue when grace ends, stamping `remove_after`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/booking/test_overdue_worker.py
import uuid
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select

from app.modules.booking.models import Booking, BookingStatus
from app.modules.catalog.models import PostamatSchedule
from app.modules.notify.models import Notification, NotificationKind
from app.workers.overdue import run_escalation


async def _parcel(session, postamat, expires_at, status=BookingStatus.AWAITING_PICKUP):
    booking = Booking(
        client_id=uuid.uuid4(), postamat_id=postamat.id, cell_id=uuid.uuid4(),
        cell_type_id=uuid.uuid4(), duration_hours=24, amount_minor=1800,
        status=status, recipient_phone="+99365000001", expires_at=expires_at,
    )
    session.add(booking)
    await session.commit()
    return booking


async def test_a_reminder_goes_out_before_expiry(session, postamat):
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    booking = await _parcel(session, postamat, now + timedelta(hours=1))

    counts = await run_escalation(session, now=now)
    assert counts["reminded"] == 1
    assert booking.status is BookingStatus.AWAITING_PICKUP

    kinds = await session.scalars(select(Notification.kind))
    assert NotificationKind.BOOKING_EXPIRING in set(kinds)


async def test_a_reminder_is_not_repeated_on_the_next_run(session, postamat):
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    await _parcel(session, postamat, now + timedelta(hours=1))

    await run_escalation(session, now=now)
    second = await run_escalation(session, now=now + timedelta(minutes=5))
    assert second["reminded"] == 0


async def test_expiry_grace_and_overdue_happen_in_order(session, postamat):
    postamat.round_the_clock = True
    await session.commit()
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    booking = await _parcel(session, postamat, now - timedelta(minutes=1))

    await run_escalation(session, now=now)
    assert booking.status is BookingStatus.EXPIRED

    await run_escalation(session, now=now + timedelta(hours=2, minutes=1))
    assert booking.status is BookingStatus.GRACE

    await run_escalation(session, now=now + timedelta(hours=4, minutes=1))
    assert booking.status is BookingStatus.OVERDUE
    assert booking.remove_after is not None


async def test_grace_waits_for_the_doors_to_open(session, postamat):
    # Expiry lands at 21:00 at a site that closed at 20:00. Grace is the chance
    # to still collect, so it cannot burn down while the door is locked.
    postamat.round_the_clock = False
    postamat.schedule = [
        PostamatSchedule(weekday=day, opens_at=time(8, 0), closes_at=time(20, 0))
        for day in range(7)
    ]
    await session.commit()

    expiry = datetime(2026, 8, 17, 21, 0, tzinfo=timezone.utc)
    booking = await _parcel(session, postamat, expiry)
    await run_escalation(session, now=expiry + timedelta(minutes=1))
    assert booking.status is BookingStatus.EXPIRED

    # Two hours later it is still the middle of the night: nothing moves.
    await run_escalation(session, now=expiry + timedelta(hours=2, minutes=1))
    assert booking.status is BookingStatus.EXPIRED

    # Two hours after the doors open, grace has had its chance.
    await run_escalation(session, now=datetime(2026, 8, 18, 10, 1, tzinfo=timezone.utc))
    assert booking.status is BookingStatus.GRACE


async def test_a_collected_parcel_is_never_escalated(session, postamat):
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    booking = await _parcel(session, postamat, now - timedelta(hours=10),
                            status=BookingStatus.COMPLETED)
    counts = await run_escalation(session, now=now)
    assert counts == {"reminded": 0, "expired": 0, "grace": 0, "overdue": 0}
    assert booking.status is BookingStatus.COMPLETED
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/booking/test_overdue_worker.py -v`
Expected: FAIL — no module `app.workers.overdue`.

- [ ] **Step 3: Write `backend/app/workers/overdue.py`**

```python
"""Walk parcels through the overdue stages.

Run it by hand until a scheduler exists:

    ./.venv/Scripts/python.exe -m app.workers.overdue

Nothing here charges for being late — the product deliberately has no storage
fees. The stages exist so staff know when a cell may be emptied, and so the
recipient is told before it happens.
"""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import session_scope, utcnow
from app.modules.booking import service as booking_service
from app.modules.booking.models import Booking, BookingStatus
from app.modules.catalog.models import Postamat
from app.modules.catalog.service import cell_numbers, is_open_at, next_opening_after
from app.modules.notify.models import NotificationChannel, NotificationKind
from app.modules.notify.service import notify

REMINDED_FLAG = "expiry_reminded"


async def _postamats(session: AsyncSession, ids) -> dict:
    if not ids:
        return {}
    rows = await session.scalars(select(Postamat).where(Postamat.id.in_(list(ids))))
    return {row.id: row for row in rows}


def _grace_ends(postamat, expired_at: datetime, grace_hours: int) -> datetime:
    """When grace runs out, never while the doors are locked.

    A parcel that expires at 21:00 at a site closing at 20:00 would otherwise
    burn its whole grace period overnight, and the recipient would find it
    overdue before they could physically reach it.
    """
    plain = expired_at + timedelta(hours=grace_hours)
    if is_open_at(postamat, plain):
        return plain
    opening = next_opening_after(postamat, plain)
    return plain if opening is None else opening + timedelta(hours=grace_hours)


async def run_escalation(
    session: AsyncSession, now: datetime | None = None
) -> dict[str, int]:
    settings = get_settings()
    moment = now or utcnow()
    counts = {"reminded": 0, "expired": 0, "grace": 0, "overdue": 0}

    live = list(await session.scalars(
        select(Booking).where(
            Booking.status.in_((
                BookingStatus.AWAITING_PICKUP, BookingStatus.EXPIRED,
                BookingStatus.GRACE,
            )),
            Booking.expires_at.is_not(None),
        )
    ))
    if not live:
        return counts

    postamats = await _postamats(session, {row.postamat_id for row in live})
    numbers = await cell_numbers(session, [row.cell_id for row in live])

    for booking in live:
        expires_at = booking_service.as_utc(booking.expires_at)
        postamat = postamats.get(booking.postamat_id)
        cell_number = numbers.get(booking.cell_id, 0)

        if booking.status is BookingStatus.AWAITING_PICKUP:
            reminder_at = expires_at - timedelta(hours=settings.reminder_hours_before)
            already = (booking.details or {}).get(REMINDED_FLAG) if hasattr(
                booking, "details") else None
            if moment >= expires_at:
                await booking_service.expire(session, booking)
                await notify(
                    session, client_id=booking.client_id,
                    phone=booking.recipient_phone,
                    kind=NotificationKind.BOOKING_EXPIRED, language="ru",
                    channel=NotificationChannel.IN_APP, booking_id=booking.id,
                    cell_number=cell_number,
                )
                counts["expired"] += 1
            elif moment >= reminder_at and not already:
                await notify(
                    session, client_id=booking.client_id,
                    phone=booking.recipient_phone,
                    kind=NotificationKind.BOOKING_EXPIRING, language="ru",
                    channel=NotificationChannel.IN_APP, booking_id=booking.id,
                    cell_number=cell_number,
                    hours=settings.reminder_hours_before,
                )
                counts["reminded"] += 1
        elif booking.status is BookingStatus.EXPIRED:
            if postamat is not None and moment >= _grace_ends(
                postamat, expires_at, settings.grace_hours
            ):
                await booking_service.to_grace(session, booking)
                counts["grace"] += 1
        elif booking.status is BookingStatus.GRACE:
            if postamat is not None and moment >= _grace_ends(
                postamat, expires_at, settings.grace_hours * 2
            ):
                await booking_service.to_overdue(session, booking)
                await notify(
                    session, client_id=booking.client_id,
                    phone=booking.recipient_phone,
                    kind=NotificationKind.BOOKING_OVERDUE, language="ru",
                    channel=NotificationChannel.IN_APP, booking_id=booking.id,
                    cell_number=cell_number,
                )
                counts["overdue"] += 1

    await session.commit()
    return counts


async def run_once() -> dict[str, int]:
    async with session_scope() as session:
        return await run_escalation(session)


if __name__ == "__main__":
    import asyncio

    print(asyncio.run(run_once()))
```

Two things the implementer must settle while writing this, because the sketch above leaves them
half-formed:

1. **The reminder must not repeat.** The sketch gestures at a `details` field that `Booking` does
   not have. Add a real column instead — `reminded_at: Mapped[datetime | None]` on `Booking`, set
   when the reminder goes out and checked instead of the flag. Fold it into Task 7's migration if
   that task has not been committed yet; otherwise give it its own.
2. **`booking_service.as_utc`** is the existing private `_as_utc`; rename it without the underscore
   and export it, since a second module now needs it. One rename, one import.

- [ ] **Step 4: Run the tests, fix what they catch, commit**

```bash
./.venv/Scripts/python.exe -m pytest tests/booking/test_overdue_worker.py -v
./.venv/Scripts/python.exe -m pytest -q
git add app/workers/overdue.py app/modules/booking/models.py app/modules/booking/service.py \
        alembic/versions/<rev>_reminded_at.py tests/booking/test_overdue_worker.py
git commit -m "feat: escalate expired parcels through grace to overdue"
```

---

### Task 9: Custody — the act that frees a cell

**Files:**
- Create: `backend/app/modules/custody/__init__.py`, `models.py`, `service.py`
- Create: `backend/app/api/admin/custody.py`, `backend/tests/custody/__init__.py`,
  `backend/tests/custody/test_custody.py`
- Modify: `backend/app/main.py`, `backend/alembic/env.py`, `backend/tests/conftest.py`

**Interfaces:**
- Produces: `CustodyRecord`, `CustodyStatus`, `CustodyHandover`;
  `async def file_removal(session, booking, admin_login, description) -> CustodyRecord`;
  `async def hand_over(session, record, admin_login, to_whom, note) -> CustodyHandover`;
  `async def dispose(session, record, admin_login, reason) -> CustodyRecord`.
- Endpoints: `GET /api/v1/admin/custody` (queue and counter), `POST /api/v1/admin/custody`,
  `GET /api/v1/admin/custody/{id}`, `POST /api/v1/admin/custody/{id}/handover`,
  `POST /api/v1/admin/custody/{id}/dispose`. Permission: `custody.read` / `custody.write`.

Per Ruling Q2 the act carries a written description and no photograph.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/custody/test_custody.py
import uuid
from datetime import timedelta

from sqlalchemy import select

from app.core.db import utcnow
from app.modules.booking.models import Booking, BookingStatus
from app.modules.catalog.models import Cell
from app.modules.custody.models import CustodyRecord, CustodyStatus


async def _overdue_parcel(session, postamat, cell_type):
    cell = Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=1,
                board=1, output=1)
    session.add(cell)
    await session.flush()
    booking = Booking(
        client_id=uuid.uuid4(), postamat_id=postamat.id, cell_id=cell.id,
        cell_type_id=cell_type.id, duration_hours=24, amount_minor=1800,
        status=BookingStatus.OVERDUE, recipient_phone="+99365000001",
        expires_at=utcnow() - timedelta(hours=30),
        remove_after=utcnow() - timedelta(hours=1),
    )
    session.add(booking)
    await session.commit()
    return booking, cell


async def test_filing_an_act_frees_the_cell(
    client, session, admin_token, cell_type, postamat
):
    booking, cell = await _overdue_parcel(session, postamat, cell_type)
    headers = {"Authorization": f"Bearer {admin_token}"}

    response = await client.post("/api/v1/admin/custody", headers=headers, json={
        "booking_id": str(booking.id),
        "description": "Коробка 30×20, скотч, без повреждений",
    })
    assert response.status_code == 201
    assert response.json()["status"] == CustodyStatus.AT_COUNTER
    assert booking.status is BookingStatus.REMOVED

    availability = await client.get(f"/api/v1/postamats/{postamat.id}/availability")
    assert availability.json()["items"][0]["free"] == 1


async def test_an_act_without_a_description_is_refused(
    client, session, admin_token, cell_type, postamat
):
    # The description is the whole evidentiary value of the act: without it the
    # record says a parcel existed and nothing about what it was.
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    response = await client.post("/api/v1/admin/custody",
                                 headers={"Authorization": f"Bearer {admin_token}"},
                                 json={"booking_id": str(booking.id), "description": ""})
    assert response.status_code == 422


async def test_a_parcel_that_is_not_overdue_cannot_be_removed(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    booking.status = BookingStatus.AWAITING_PICKUP
    await session.commit()

    response = await client.post("/api/v1/admin/custody",
                                 headers={"Authorization": f"Bearer {admin_token}"},
                                 json={"booking_id": str(booking.id),
                                       "description": "рано"})
    assert response.status_code == 409


async def test_handing_the_parcel_over_closes_the_booking(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    headers = {"Authorization": f"Bearer {admin_token}"}
    act = await client.post("/api/v1/admin/custody", headers=headers, json={
        "booking_id": str(booking.id), "description": "Коробка",
    })

    handed = await client.post(f"/api/v1/admin/custody/{act.json()['id']}/handover",
                               headers=headers, json={
                                   "to_whom": "recipient", "note": "паспорт проверен",
                               })
    assert handed.status_code == 200
    assert handed.json()["status"] == CustodyStatus.HANDED_OVER
    assert booking.status is BookingStatus.CLOSED


async def test_disposal_records_who_and_why(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    headers = {"Authorization": f"Bearer {admin_token}"}
    act = await client.post("/api/v1/admin/custody", headers=headers, json={
        "booking_id": str(booking.id), "description": "Коробка",
    })

    disposed = await client.post(f"/api/v1/admin/custody/{act.json()['id']}/dispose",
                                 headers=headers, json={"reason": "30 дней не забрали"})
    assert disposed.status_code == 200
    assert disposed.json()["status"] == CustodyStatus.DISPOSED

    record = await session.scalar(select(CustodyRecord))
    assert record.closed_by
    assert record.closing_reason == "30 дней не забрали"


async def test_the_queue_lists_what_is_waiting_at_the_counter(
    client, session, admin_token, cell_type, postamat
):
    booking, _ = await _overdue_parcel(session, postamat, cell_type)
    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post("/api/v1/admin/custody", headers=headers,
                      json={"booking_id": str(booking.id), "description": "Коробка"})

    listed = await client.get("/api/v1/admin/custody?status=at_counter", headers=headers)
    assert len(listed.json()["items"]) == 1
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/custody -v`
Expected: FAIL — no module `app.modules.custody`.

- [ ] **Step 3: Write `backend/app/modules/custody/models.py`**

```python
import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class CustodyStatus(StrEnum):
    AT_COUNTER = "at_counter"
    HANDED_OVER = "handed_over"
    DISPOSED = "disposed"


class CustodyRecord(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "custody_records"

    booking_id: Mapped[uuid.UUID] = mapped_column(index=True, unique=True)
    postamat_id: Mapped[uuid.UUID] = mapped_column(index=True)
    cell_id: Mapped[uuid.UUID] = mapped_column(index=True)
    cell_number: Mapped[int]
    # Who emptied the cell and what they found in it. Ruling Q2: no photograph
    # until there is somewhere to put files.
    removed_by: Mapped[str] = mapped_column(String(64))
    removed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    description: Mapped[str] = mapped_column(String(500))
    status: Mapped[CustodyStatus] = mapped_column(
        String(16), index=True, default=CustodyStatus.AT_COUNTER
    )
    closed_by: Mapped[str | None] = mapped_column(String(64))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closing_reason: Mapped[str | None] = mapped_column(String(500))

    handovers: Mapped[list["CustodyHandover"]] = relationship(
        back_populates="record", lazy="selectin", cascade="all, delete-orphan"
    )


class CustodyHandover(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "custody_handovers"

    record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("custody_records.id"), index=True
    )
    to_whom: Mapped[str] = mapped_column(String(16))  # recipient | sender
    by_admin: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(500))

    record: Mapped[CustodyRecord] = relationship(back_populates="handovers")
```

- [ ] **Step 4: Write `backend/app/modules/custody/service.py`**

```python
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import utcnow
from app.core.errors import AppError, ErrorCode
from app.modules.audit.models import Severity, Source
from app.modules.audit.service import record as audit
from app.modules.booking import service as booking_service
from app.modules.booking.models import Booking, BookingStatus
from app.modules.custody.models import CustodyHandover, CustodyRecord, CustodyStatus

REMOVABLE = frozenset({BookingStatus.OVERDUE})


async def file_removal(
    session: AsyncSession, booking: Booking, cell_number: int, admin_login: str,
    description: str,
) -> CustodyRecord:
    """Record that a parcel was taken out, and only then free the cell.

    The order matters: the act exists before the cell is released, so there is
    no moment where a cell is free and no record says where its contents went.
    """
    if booking.status not in REMOVABLE:
        raise AppError(ErrorCode.BOOKING_INVALID_STATE,
                       "Only an overdue parcel may be removed.", 409,
                       details={"status": booking.status.value})

    record = CustodyRecord(
        booking_id=booking.id, postamat_id=booking.postamat_id,
        cell_id=booking.cell_id, cell_number=cell_number,
        removed_by=admin_login, removed_at=utcnow(), description=description,
    )
    session.add(record)
    await booking_service.mark_removed(session, booking)
    await audit(session, event="custody.removed", source=Source.ADMIN,
                severity=Severity.WARNING, message=description, actor=admin_login,
                postamat_id=booking.postamat_id, cell_id=booking.cell_id,
                details={"booking_id": str(booking.id)})
    return record


async def hand_over(
    session: AsyncSession, record: CustodyRecord, booking: Booking, admin_login: str,
    to_whom: str, note: str | None,
) -> CustodyHandover:
    if record.status is not CustodyStatus.AT_COUNTER:
        raise AppError(ErrorCode.BOOKING_INVALID_STATE,
                       "This parcel is no longer at the counter.", 409)

    handover = CustodyHandover(to_whom=to_whom, by_admin=admin_login, note=note)
    record.handovers.append(handover)
    record.status = CustodyStatus.HANDED_OVER
    record.closed_by = admin_login
    record.closed_at = utcnow()
    record.closing_reason = f"Выдано: {to_whom}"
    await booking_service.close_custody(session, booking)
    await audit(session, event="custody.handed_over", source=Source.ADMIN,
                message=note or f"Выдано: {to_whom}", actor=admin_login,
                postamat_id=record.postamat_id, cell_id=record.cell_id,
                details={"booking_id": str(record.booking_id), "to_whom": to_whom})
    return handover


async def dispose(
    session: AsyncSession, record: CustodyRecord, booking: Booking, admin_login: str,
    reason: str,
) -> CustodyRecord:
    if record.status is not CustodyStatus.AT_COUNTER:
        raise AppError(ErrorCode.BOOKING_INVALID_STATE,
                       "This parcel is no longer at the counter.", 409)

    record.status = CustodyStatus.DISPOSED
    record.closed_by = admin_login
    record.closed_at = utcnow()
    record.closing_reason = reason
    await booking_service.close_custody(session, booking)
    await audit(session, event="custody.disposed", source=Source.ADMIN,
                severity=Severity.WARNING, message=reason, actor=admin_login,
                postamat_id=record.postamat_id, cell_id=record.cell_id,
                details={"booking_id": str(record.booking_id)})
    return record
```

- [ ] **Step 5: Write `backend/app/api/admin/custody.py`**

The router mirrors `app/api/admin/bookings.py` in shape: a `CustodyOut` model, a paged list with a
`status` filter, a detail, and the three POSTs. `POST /admin/custody` loads the booking, resolves
its cell number through `catalog.service.cell_numbers`, calls `file_removal`, notifies the client
with `NotificationKind.PARCEL_REMOVED`, commits, and returns 201. The handover and disposal
endpoints load the record, load its booking, call the service, commit, and return the record. All
five sit behind `require_permission("custody.read")` or `("custody.write")`.

Add `"custody.read"` and `"custody.write"` to the `admin_user` fixture's permission list in
`backend/tests/conftest.py`.

- [ ] **Step 6: Register, migrate, commit**

```bash
./.venv/Scripts/python.exe -m pytest tests/custody -v
./.venv/Scripts/python.exe -m alembic revision --autogenerate -m "custody"
./.venv/Scripts/python.exe -m alembic upgrade head
./.venv/Scripts/python.exe -m pytest -q
git add app/modules/custody app/api/admin/custody.py app/main.py alembic/env.py \
        alembic/versions/<rev>_custody.py tests/custody tests/conftest.py
git commit -m "feat: custody acts that free a cell and account for the parcel"
```

---

### Task 10: The work queue the operator opens in the morning

**Files:**
- Create: `backend/app/api/admin/dashboard.py`, `backend/tests/custody/test_dashboard.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces: `GET /api/v1/admin/dashboard/attention` →
  `{"overdue": [...], "to_remove": [...], "payments_needing_attention": [...], "counts": {...}}`,
  behind `require_permission("bookings.read")`.

This is the screen the spec calls "the work queue": what a person has to do something about today.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/custody/test_dashboard.py
import uuid
from datetime import timedelta

from app.core.db import utcnow
from app.modules.booking.models import Booking, BookingStatus


async def _booking(session, postamat, status, remove_after=None):
    row = Booking(
        client_id=uuid.uuid4(), postamat_id=postamat.id, cell_id=uuid.uuid4(),
        cell_type_id=uuid.uuid4(), duration_hours=24, amount_minor=1800,
        status=status, recipient_phone="+99365000001",
        expires_at=utcnow() - timedelta(hours=5), remove_after=remove_after,
    )
    session.add(row)
    await session.commit()
    return row


async def test_the_queue_separates_overdue_from_ready_to_remove(
    client, session, admin_token, postamat
):
    await _booking(session, postamat, BookingStatus.OVERDUE,
                   remove_after=utcnow() + timedelta(hours=10))
    await _booking(session, postamat, BookingStatus.OVERDUE,
                   remove_after=utcnow() - timedelta(hours=1))

    response = await client.get("/api/v1/admin/dashboard/attention",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200
    assert response.json()["counts"]["overdue"] == 2
    assert response.json()["counts"]["to_remove"] == 1


async def test_payments_needing_attention_reach_the_queue(
    client, session, admin_token, postamat
):
    from app.modules.audit.models import AuditEntry, Severity, Source

    session.add(AuditEntry(
        event="payment.needs_attention", source=Source.SYSTEM,
        severity=Severity.WARNING, message="Оплата по неактивной брони",
        details={"amount_minor": 1800},
    ))
    await session.commit()

    response = await client.get("/api/v1/admin/dashboard/attention",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.json()["counts"]["payments_needing_attention"] == 1


async def test_a_quiet_morning_is_all_zeroes(client, admin_token):
    response = await client.get("/api/v1/admin/dashboard/attention",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.json()["counts"] == {
        "overdue": 0, "to_remove": 0, "payments_needing_attention": 0,
    }
```

- [ ] **Step 2: Run it, write the router, run it again**

The router runs three counted queries — overdue bookings, the subset whose `remove_after` has
passed, and unresolved `payment.needs_attention` audit entries — and returns the first twenty rows
of each alongside the counts. No new models.

- [ ] **Step 3: Commit**

```bash
./.venv/Scripts/python.exe -m pytest tests/custody -v
git add app/api/admin/dashboard.py app/main.py tests/custody/test_dashboard.py
git commit -m "feat: the admin work queue"
```

---

### Task 11: Photographs of the postamat

**Files:**
- Create: `backend/app/core/storage.py`, `backend/app/api/public/media.py`,
  `backend/tests/catalog/test_photos.py`
- Modify: `backend/app/modules/catalog/models.py`, `backend/app/modules/catalog/schemas.py`,
  `backend/app/api/admin/postamats.py`, `backend/app/api/public/postamats.py`,
  `backend/app/main.py`, `backend/app/core/config.py`, `backend/alembic/env.py`

**Interfaces:**
- Produces: `FileStorage` protocol (`save(data, content_type) -> key`, `open(key) -> bytes`,
  `delete(key)`), `LocalFileStorage`, `get_file_storage()`; `PostamatPhoto`;
  `POST /api/v1/admin/postamats/{id}/photos` (multipart), `DELETE .../photos/{photo_id}`,
  `GET /api/v1/media/{key}`; photo URLs on both the admin and the public postamat detail.

The photograph is of the machine, not of a parcel: it is how a customer recognises the postamat in
a lobby and how an operator confirms which one they are looking at. That is why it hangs off
`Postamat` and why the public detail carries it.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/catalog/test_photos.py
import io


def _png() -> bytes:
    # The smallest thing browsers agree is a PNG: a 1x1 transparent pixel.
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
        "05575bcecd0000000049454e44ae426082"
    )


async def test_uploading_a_photo_puts_it_on_the_postamat(
    client, admin_token, postamat
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.post(
        f"/api/v1/admin/postamats/{postamat.id}/photos", headers=headers,
        files={"file": ("front.png", io.BytesIO(_png()), "image/png")},
    )
    assert response.status_code == 201
    url = response.json()["url"]

    fetched = await client.get(url)
    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "image/png"

    public = await client.get(f"/api/v1/postamats/{postamat.id}")
    assert [photo["url"] for photo in public.json()["photos"]] == [url]


async def test_a_file_that_is_not_an_image_is_refused(client, admin_token, postamat):
    response = await client.post(
        f"/api/v1/admin/postamats/{postamat.id}/photos",
        headers={"Authorization": f"Bearer {admin_token}"},
        files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert response.status_code == 422


async def test_an_oversized_file_is_refused(client, admin_token, postamat):
    big = _png() + b"\0" * (6 * 1024 * 1024)
    response = await client.post(
        f"/api/v1/admin/postamats/{postamat.id}/photos",
        headers={"Authorization": f"Bearer {admin_token}"},
        files={"file": ("huge.png", io.BytesIO(big), "image/png")},
    )
    assert response.status_code == 413


async def test_deleting_a_photo_removes_it_from_the_listing(
    client, admin_token, postamat
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = await client.post(
        f"/api/v1/admin/postamats/{postamat.id}/photos", headers=headers,
        files={"file": ("front.png", io.BytesIO(_png()), "image/png")},
    )
    photo_id = created.json()["id"]

    deleted = await client.delete(
        f"/api/v1/admin/postamats/{postamat.id}/photos/{photo_id}", headers=headers
    )
    assert deleted.status_code == 204

    public = await client.get(f"/api/v1/postamats/{postamat.id}")
    assert public.json()["photos"] == []


async def test_a_traversal_key_cannot_reach_outside_the_media_directory(client):
    response = await client.get("/api/v1/media/..%2F..%2F.env")
    assert response.status_code in (400, 404)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/catalog/test_photos.py -v`
Expected: FAIL — 404/405, the routes do not exist.

- [ ] **Step 3: Write `backend/app/core/storage.py`**

```python
import re
import uuid
from pathlib import Path
from typing import Protocol

from app.core.config import BACKEND_DIR, get_settings

ALLOWED_IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_KEY = re.compile(r"^[0-9a-f]{32}\.(png|jpg|webp)$")


class FileStorage(Protocol):
    def save(self, data: bytes, content_type: str) -> str: ...
    def open(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class LocalFileStorage:
    """Files on disk beside the application.

    Keys are generated here and validated on the way back in: a key that came
    from a URL is attacker-controlled, and joining one straight onto a path is
    how `../../.env` gets served.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not _KEY.match(key):
            raise ValueError("bad storage key")
        return self.root / key

    def save(self, data: bytes, content_type: str) -> str:
        suffix = ALLOWED_IMAGE_TYPES[content_type]
        key = f"{uuid.uuid4().hex}{suffix}"
        (self.root / key).write_bytes(data)
        return key

    def open(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


_storage: FileStorage | None = None


def get_file_storage() -> FileStorage:
    global _storage
    if _storage is None:
        _storage = LocalFileStorage(BACKEND_DIR / get_settings().media_root)
    return _storage


def reset_file_storage() -> None:
    global _storage
    _storage = None
```

Settings gain `media_root: str = "media"` and `max_upload_bytes: int = 5 * 1024 * 1024`.

- [ ] **Step 4: Add the model**

In `backend/app/modules/catalog/models.py`:

```python
class PostamatPhoto(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "postamat_photos"

    postamat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("postamats.id"), index=True
    )
    storage_key: Mapped[str] = mapped_column(String(64))
    content_type: Mapped[str] = mapped_column(String(32))
    # What the photo is of, for an operator scrolling a list: "фасад", "вход".
    caption: Mapped[str | None] = mapped_column(String(200))
    position: Mapped[int] = mapped_column(Integer, default=0)
```

and a `photos` relationship on `Postamat` ordered by `position`, `lazy="selectin"`,
`cascade="all, delete-orphan"`.

- [ ] **Step 5: Write the routes**

`backend/app/api/public/media.py` serves a key:

```python
@router.get("/media/{key}")
async def get_media(key: str) -> Response:
    try:
        data = get_file_storage().open(key)
    except (ValueError, FileNotFoundError):
        # One answer for a malformed key and a missing one: probing the
        # difference is how a traversal attempt gets refined.
        raise AppError(ErrorCode.NOT_FOUND, "Not found.", 404) from None
    return Response(content=data, media_type=_CONTENT_TYPES[Path(key).suffix])
```

`backend/app/api/admin/postamats.py` gains the upload behind `postamats.write`: read the
`UploadFile`, refuse a content type outside `ALLOWED_IMAGE_TYPES` with 422, refuse a body over
`max_upload_bytes` with 413, save through the storage, add the row, audit it, and return
`{"id", "url", "caption"}` where `url` is `/api/v1/media/{key}`. Deletion removes the row and the
file, and answers 204.

Both `PostamatOut` and `PostamatPublicOut` gain `photos: list[PhotoOut]`.

- [ ] **Step 6: Migration, suite, commit**

```bash
./.venv/Scripts/python.exe -m alembic revision --autogenerate -m "postamat photos"
./.venv/Scripts/python.exe -m alembic upgrade head
./.venv/Scripts/python.exe -m pytest -q
git add app/core/storage.py app/core/config.py app/api/public/media.py \
        app/api/admin/postamats.py app/api/public/postamats.py \
        app/modules/catalog/models.py app/modules/catalog/schemas.py app/main.py \
        alembic/env.py alembic/versions/<rev>_postamat_photos.py tests/catalog/test_photos.py
git commit -m "feat: photographs of a postamat, stored on disk behind an interface"
```

Add `media/` to `.gitignore` — uploaded files are data, not source.

---

### Task 12: Smoke the money and the clock

**Files:**
- Modify: `backend/scripts/smoke.py`, `README.md`

- [ ] **Step 1: Extend the smoke script**

After the booking section, add a payment walk-through:

```python
        if access and fleet and created:
            print(f"\n{YELLOW}-- paying --{RESET}")
            started = await call(
                client, "POST", f"/api/v1/bookings/{created['id']}/payment",
                expect=201,
                headers=bearer | {"Idempotency-Key": "smoke-payment-1"},
            )
            if started:
                import hashlib
                import hmac as _hmac
                import json as _json

                from app.core.config import get_settings

                body = _json.dumps({
                    "payment_id": started["payment_id"],
                    "provider_payment_id": f"mock-{started['payment_id']}",
                    "status": "succeeded",
                }).encode()
                signature = _hmac.new(
                    get_settings().payment_webhook_secret.encode(), body,
                    hashlib.sha256,
                ).hexdigest()
                await call(
                    client, "POST", "/api/v1/webhooks/payments/mock", content=body,
                    headers={"X-Signature": signature,
                             "Content-Type": "application/json"},
                )
                detail = await call(
                    client, "GET", f"/api/v1/bookings/{created['id']}", headers=bearer
                )
                if detail:
                    print(f"{GREY}     status after payment: "
                          f"{detail['status']}{RESET}")
```

Note that the booking cancellation block that currently follows must move above this, or be
dropped: a paid booking can no longer be cancelled by the client, which is the point.

- [ ] **Step 2: Update the README**

Document: payments against the mock provider and how the webhook is signed; that there are no
refunds and why (Ruling Q1) and that `payment.needs_attention` in the audit log is where the
exceptions land; the two workers (`app.workers.holds`, `app.workers.overdue`) and what each does;
the notification feed; that SMS goes to a logging provider until a token exists.

- [ ] **Step 3: Run everything and commit**

```bash
./.venv/Scripts/python.exe -m pytest -q
./.venv/Scripts/python.exe scripts/smoke.py
git add scripts/smoke.py ../README.md
git commit -m "chore: smoke the payment flow and document the workers"
```

---

## Definition of done for this plan

- `./.venv/Scripts/python.exe -m pytest` green from `backend/` and from the repository root.
- A client pays a booking against the mock provider, the signed webhook settles it exactly once,
  and the booking reaches `awaiting_deposit` with a three-entry timeline.
- A declined payment releases the cell; a payment that lands after the hold expired is recorded and
  raises `payment.needs_attention` in the audit log.
- A deposited parcel walks `awaiting_pickup → expired → grace → overdue` on the worker's clock,
  with grace never burning down while the postamat is closed, and collection still possible at
  every stage.
- Removal requires a written act; filing one frees the cell and moves the booking to `removed`;
  handover or disposal closes it.
- `uq_active_booking_per_cell` lists all seven holding statuses.
- The login code and the courier PIN are delivered through `SmsProvider`; neither is returned in an
  HTTP response any more. `SMS_PROVIDER=log` in development and tests; `post_tm` sends for real, and
  the payload is `{"phone": 99362615986, "content": "..."}` with no plus.
- An operator uploads a photograph of a postamat, it appears on the public detail, and a key from a
  URL cannot escape the media directory.

## Deliberately not in this plan

- **Refunds**, per Ruling Q1. The exceptions are visible in the audit log for a human to settle.
- **Photographs on custody acts**, per Ruling Q2 — they return with a storage decision.
- **A real bank and a real SMS provider**: both are one class each, behind interfaces that exist
  now. The webhook signature scheme will need revisiting against the bank's actual scheme.
- **Push notifications and `/me/push-tokens`** — no mobile client to register a token yet.
- **The kiosk and the device channel** — Plan 3. `find_code` and `mark_deposited` /
  `mark_collected` are the seams it calls.
- **A scheduler.** Both workers are run by hand or by an external timer; picking one is a
  deployment decision, not a code one.
