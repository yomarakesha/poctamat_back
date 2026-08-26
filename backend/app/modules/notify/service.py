import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import utcnow
from app.modules.identity.models import PushToken
from app.modules.notify.models import (
    Notification,
    NotificationChannel,
    NotificationKind,
)
from app.modules.notify.push import get_push_provider
from app.modules.notify.sms import DeliveryEvent, get_sms_provider

logger = logging.getLogger("app.notify")

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
    (NotificationKind.DEPOSIT_CODE, "tk"): (
        "Kuryer kody", "Kuryere kod iberildi.",
        "Postamat: ýaçeýka {cell_number} üçin kod {code}",
    ),
    (NotificationKind.DEPOSIT_CODE, "ru"): (
        "Код курьера", "Код отправлен курьеру.",
        "Постамат: код {code} для ячейки {cell_number}",
    ),
    (NotificationKind.DEPOSIT_CODE, "en"): (
        "Deposit code", "The deposit code has been sent.",
        "Postamat: code {code} for cell {cell_number}",
    ),
    (NotificationKind.PICKUP_CODE_SENT, "tk"): (
        "Almak kody", "Almak kody SMS bilen iberildi.",
        "Postamat: ýaçeýka {cell_number} açmak üçin kod {code}",
    ),
    (NotificationKind.PICKUP_CODE_SENT, "ru"): (
        "Код получения", "Код получения отправлен по SMS.",
        "Постамат: код {code} для получения из ячейки {cell_number}",
    ),
    (NotificationKind.PICKUP_CODE_SENT, "en"): (
        "Pickup code", "The pickup code has been sent by SMS.",
        "Postamat: code {code} to collect from cell {cell_number}",
    ),
    (NotificationKind.PICKUP_TRANSFERRED, "tk"): (
        "Almak hukugy geçirildi", "Ýaçeýka {cell_number}: almak hukugy geçirildi.", "",
    ),
    (NotificationKind.PICKUP_TRANSFERRED, "ru"): (
        "Право получения передано", "Ячейка {cell_number}: получатель изменён.", "",
    ),
    (NotificationKind.PICKUP_TRANSFERRED, "en"): (
        "Pickup transferred", "Cell {cell_number}: the recipient has changed.", "",
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
                row.provider_message_id = await get_sms_provider().send(
                    phone, sms.format(**params)
                )
                row.sent_at = utcnow()
            except Exception as error:  # noqa: BLE001 - a provider failure is data
                # A failed SMS must not fail the booking that triggered it. The
                # error is stored so an operator can see what never arrived.
                row.error = str(error)[:500]

    elif channel == NotificationChannel.PUSH:
        if not client_id:
            row.error = "no client"
        else:
            devices = list(await session.scalars(
                select(PushToken).where(
                    PushToken.client_id == client_id, PushToken.revoked_at.is_(None),
                )
            ))
            if not devices:
                row.error = "no push tokens"
            else:
                errors: list[str] = []
                for device in devices:
                    try:
                        await get_push_provider().send(device.token, title, row.body)
                        row.sent_at = utcnow()
                    except Exception as error:  # noqa: BLE001 - one bad device
                        # must not stop delivery to the client's other devices.
                        errors.append(str(error)[:200])
                # `error` means nobody got this. A client with two devices
                # where only one delivery failed still has the notification,
                # so that failure is not recorded on the row — `sent_at`
                # already tells the true story, and a caller branching on
                # `error is not None` to decide whether to retry must not
                # re-push to the device that already succeeded. The failure
                # is logged instead: a token the provider rejects is a token
                # to revoke, and dropping it silently means every later
                # notification keeps fanning out to a device that is gone.
                if errors:
                    if row.sent_at is None:
                        row.error = "; ".join(errors)[:500]
                    else:
                        logger.warning(
                            "push %s: %d of %d device(s) failed: %s",
                            kind.value, len(errors), len(devices),
                            "; ".join(errors),
                        )
    return row


async def record_delivery(
    session: AsyncSession, event: DeliveryEvent
) -> Notification | None:
    """Match a gateway's delivery report back to the SMS it describes.

    Returns None for an id the gateway names that we never sent — most likely
    a retried callback for a notification purged by retention, not an attack
    worth surfacing to the caller as an error.
    """
    row = await session.scalar(
        select(Notification).where(
            Notification.provider_message_id == event.provider_message_id,
            Notification.channel == NotificationChannel.SMS,
        )
    )
    if row is None:
        logger.warning(
            "delivery report for unknown message id %s", event.provider_message_id
        )
        return None

    row.delivery_status = event.status
    if event.delivered:
        row.delivered_at = utcnow()
    elif event.error:
        row.error = event.error[:500]
    return row
