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
    assert booking.status == BookingStatus.AWAITING_PICKUP

    kinds = set(await session.scalars(select(Notification.kind)))
    assert NotificationKind.BOOKING_EXPIRING in kinds


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
    assert booking.status == BookingStatus.EXPIRED

    await run_escalation(session, now=now + timedelta(hours=2, minutes=1))
    assert booking.status == BookingStatus.GRACE

    await run_escalation(session, now=now + timedelta(hours=4, minutes=1))
    assert booking.status == BookingStatus.OVERDUE
    assert booking.remove_after is not None

    kinds = set(await session.scalars(select(Notification.kind)))
    assert {NotificationKind.BOOKING_EXPIRED, NotificationKind.BOOKING_OVERDUE} <= kinds


async def test_grace_waits_for_the_doors_to_open(session, postamat):
    # Storage ends at 21:00 at a site that closed at 20:00. Grace is the chance
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
    assert booking.status == BookingStatus.EXPIRED

    # Two hours later it is still the middle of the night: nothing moves.
    await run_escalation(session, now=expiry + timedelta(hours=2, minutes=1))
    assert booking.status == BookingStatus.EXPIRED

    # Once the doors are open, grace has had its chance.
    await run_escalation(session, now=datetime(2026, 8, 18, 8, 1, tzinfo=timezone.utc))
    assert booking.status == BookingStatus.GRACE


async def test_a_collected_parcel_is_never_escalated(session, postamat):
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    booking = await _parcel(session, postamat, now - timedelta(hours=10),
                            status=BookingStatus.COMPLETED)
    counts = await run_escalation(session, now=now)
    assert counts == {"reminded": 0, "expired": 0, "grace": 0, "overdue": 0}
    assert booking.status == BookingStatus.COMPLETED


async def test_a_parcel_without_a_storage_clock_is_left_alone(session, postamat):
    # Nothing has been deposited yet: expires_at is null and the hold worker,
    # not this one, is what looks after such bookings.
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    booking = await _parcel(session, postamat, None,
                            status=BookingStatus.AWAITING_DEPOSIT)
    counts = await run_escalation(session, now=now)
    assert counts["expired"] == 0
    assert booking.status == BookingStatus.AWAITING_DEPOSIT


async def test_the_worker_is_idempotent_within_one_stage(session, postamat):
    postamat.round_the_clock = True
    await session.commit()
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    booking = await _parcel(session, postamat, now - timedelta(minutes=1))

    await run_escalation(session, now=now)
    second = await run_escalation(session, now=now)
    assert second["expired"] == 0
    assert len(booking.events) == 1
