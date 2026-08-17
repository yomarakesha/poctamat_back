from datetime import datetime, timedelta

from app.modules.catalog.models import Postamat, PostamatSchedule

# Every slot is validated as opens_at < closes_at before it reaches the
# database (see `ScheduleSlotIn`), so nothing here has to reason about a window
# that runs past midnight. A machine that serves through the night is
# `round_the_clock`, which is the only form of that the product has.


def _slot_for(postamat: Postamat, weekday: int) -> PostamatSchedule | None:
    for slot in postamat.schedule:
        if slot.weekday == weekday:
            return slot
    return None


def is_open_at(postamat: Postamat, moment: datetime) -> bool:
    if postamat.round_the_clock:
        return True
    slot = _slot_for(postamat, moment.weekday())
    if slot is None:
        return False
    # Half-open: the opening minute counts as open, the closing minute does not.
    return slot.opens_at <= moment.time() < slot.closes_at


def next_opening_after(postamat: Postamat, moment: datetime) -> datetime | None:
    """The first instant at or after `moment` when the postamat is open.

    Returns None when it never opens again on the weekly cycle — no schedule at
    all — because handing back `moment` would read as "open now" to the caller.
    """
    if postamat.round_the_clock:
        return moment
    # Eight days rather than seven: today's own slot may already have passed, so
    # the same weekday has to be reachable a second time.
    for offset in range(8):
        day = moment + timedelta(days=offset)
        slot = _slot_for(postamat, day.weekday())
        if slot is None:
            continue
        candidate = day.replace(
            hour=slot.opens_at.hour, minute=slot.opens_at.minute,
            second=0, microsecond=0,
        )
        if candidate > moment:
            return candidate
    return None
