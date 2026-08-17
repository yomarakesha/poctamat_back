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

# (kind, language) -> (title, in-app body, SMS text). The in-app body never
# carries a code: the feed is readable by anyone holding an unlocked phone,
# while the SMS is the delivery itself and exists only in transit.
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
    (NotificationKind.BOOKING_PAID, "tk"): (
        "Töleg kabul edildi", "Ýaçeýka {cell_number}: ibermäni goýup bilersiňiz.", "",
    ),
    (NotificationKind.BOOKING_PAID, "ru"): (
        "Оплата принята", "Ячейка {cell_number}: можно класть посылку.", "",
    ),
    (NotificationKind.BOOKING_PAID, "en"): (
        "Payment received", "Cell {cell_number}: you can deposit the parcel.", "",
    ),
    (NotificationKind.BOOKING_EXPIRING, "tk"): (
        "Möhlet gutarýar", "Ýaçeýka {cell_number}: {hours} sagatdan möhlet gutarýar.", "",
    ),
    (NotificationKind.BOOKING_EXPIRING, "ru"): (
        "Срок истекает", "Ячейка {cell_number}: срок хранения истекает через {hours} ч.",
        "",
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
        "Просрочено", "Ячейка {cell_number}: заберите посылку, иначе её снимут на стойку.",
        "",
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
    return TEMPLATES.get((kind, language)) or TEMPLATES[
        (kind, settings.default_language)
    ]


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

    if channel == NotificationChannel.SMS:
        if not phone:
            row.error = "no phone number"
        else:
            try:
                await get_sms_provider().send(phone, sms.format(**params))
                row.sent_at = utcnow()
            except Exception as error:  # noqa: BLE001 - a provider failure is data
                # A failed SMS must not fail the booking that triggered it. The
                # error is stored so an operator can see what never arrived.
                row.error = str(error)[:500]
    return row
