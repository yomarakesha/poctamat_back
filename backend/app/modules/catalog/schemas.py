import uuid
from datetime import datetime, time

from pydantic import BaseModel, Field, model_validator

from app.core.pagination import PageMeta
from app.core.types import utc_isoformat
from app.modules.catalog.models import Postamat, PostamatStatus
from app.modules.catalog.service import is_open_at, next_opening_after


class CityOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str


class CellTypeOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    width_mm: int
    height_mm: int
    depth_mm: int


def localized(row, language: str) -> str:
    """Pick the name column for `language`, falling back to Turkmen.

    An unknown language must not raise: the header is caller-supplied and a
    reference list is not the place to reject a request over it.
    """
    return getattr(row, f"name_{language}", row.name_tk)


MEDIA_PREFIX = "/api/v1/media"


class PhotoOut(BaseModel):
    id: uuid.UUID
    url: str
    caption: str | None


def photos_out(postamat: Postamat) -> list["PhotoOut"]:
    return [
        PhotoOut(id=photo.id, url=f"{MEDIA_PREFIX}/{photo.storage_key}",
                 caption=photo.caption)
        for photo in postamat.photos
    ]


class ScheduleSlotIn(BaseModel):
    # 1 = Monday on the wire, as the contract numbers it; storage counts from 0
    # because `datetime.weekday()` does, and the conversion happens in one place.
    weekday: int = Field(ge=1, le=7)
    opens_at: time
    closes_at: time

    @model_validator(mode="after")
    def _window_must_close_after_it_opens(self) -> "ScheduleSlotIn":
        # A window running past midnight is rejected rather than modelled: a
        # machine that serves through the night is `round_the_clock`, and
        # accepting 22:00-06:00 here would silently mean "closed all day"
        # everywhere `is_open_at` is used.
        if self.opens_at >= self.closes_at:
            raise ValueError("closes_at must be later than opens_at on the same day")
        return self


class SchedulePut(BaseModel):
    round_the_clock: bool = False
    # `days` rather than `slots`, and ignored entirely when the machine stands in
    # a lobby that never closes.
    days: list[ScheduleSlotIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_slot_per_weekday(self) -> "SchedulePut":
        weekdays = [slot.weekday for slot in self.days]
        if len(weekdays) != len(set(weekdays)):
            raise ValueError("each weekday may appear at most once")
        return self


class ScheduleSlotOut(BaseModel):
    weekday: int
    # `HH:MM` strings rather than times: Pydantic renders a `time` as
    # `08:00:00`, and the contract's pattern is four digits and a colon.
    opens_at: str
    closes_at: str


class ScheduleOut(BaseModel):
    round_the_clock: bool
    days: list[ScheduleSlotOut]


class GeoPoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class DeviceInline(BaseModel):
    """The IP and MAC the admin form edits inline.

    The device is its own resource with its own endpoints; this exists because
    the postamat form on screen has two fields for it and making the operator
    save twice would be a worse lie about how the two are related.
    """

    ip_address: str | None = Field(default=None, max_length=45)
    mac_address: str | None = Field(
        default=None, pattern=r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"
    )


class PostamatIn(BaseModel):
    number: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    city_id: uuid.UUID
    address: str = Field(min_length=1, max_length=500)
    location: GeoPoint | None = None
    working_hours: str | None = Field(default=None, max_length=64)
    grid_rows: int | None = Field(default=None, ge=1, le=20)
    grid_cols: int | None = Field(default=None, ge=1, le=20)
    device: DeviceInline | None = None
    round_the_clock: bool = False


class PostamatPatch(BaseModel):
    number: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    city_id: uuid.UUID | None = None
    address: str | None = Field(default=None, min_length=1, max_length=500)
    location: GeoPoint | None = None
    working_hours: str | None = Field(default=None, max_length=64)
    grid_rows: int | None = Field(default=None, ge=1, le=20)
    grid_cols: int | None = Field(default=None, ge=1, le=20)
    device: DeviceInline | None = None
    round_the_clock: bool | None = None


class BlockRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class PostamatOut(BaseModel):
    id: uuid.UUID
    number: str
    name: str
    city_id: uuid.UUID
    address: str
    # One `location` object rather than two loose floats, because that is the
    # shape three generated clients read.
    location: GeoPoint | None
    # What the panel prints in the table cell: «24/7», «08:00–20:00», or whatever
    # the operator typed.
    working_hours: str | None
    status: PostamatStatus
    round_the_clock: bool
    schedule: list[ScheduleSlotOut]
    photos: list[PhotoOut]
    occupied_cell_count: int = 0
    total_cell_count: int = 0
    # A string rather than a datetime: Pydantic renders an aware datetime with
    # a +00:00 offset, and the wire format is fixed at trailing Z.
    created_at: str


class DeviceBrief(BaseModel):
    id: uuid.UUID
    postamat_id: uuid.UUID
    status: str
    ip_address: str | None
    mac_address: str | None
    last_seen_at: str | None


class PostamatDetailsOut(PostamatOut):
    device: DeviceBrief | None = None
    grid_rows: int | None = None
    grid_cols: int | None = None
    blocked_reason: str | None = None
    updated_at: str | None = None


class PostamatPage(BaseModel):
    items: list[PostamatOut]
    pagination: PageMeta


class PostamatPublicOut(BaseModel):
    id: uuid.UUID
    number: str
    name: str
    city_id: uuid.UUID
    address: str
    location: GeoPoint | None
    working_hours: str | None
    status: PostamatStatus
    round_the_clock: bool
    schedule: list[ScheduleSlotOut]
    photos: list[PhotoOut]
    is_open_now: bool
    # None when the machine has no schedule at all, which reads as "call us"
    # rather than a time the client can wait for.
    next_opening_at: str | None


class PostamatPublicPage(BaseModel):
    items: list[PostamatPublicOut]
    pagination: PageMeta


def _slots(postamat: Postamat) -> list[ScheduleSlotOut]:
    return [
        ScheduleSlotOut(
            # Storage counts weekdays from 0 because `datetime.weekday()` does;
            # the wire counts from 1 because the contract does.
            weekday=slot.weekday + 1,
            opens_at=slot.opens_at.strftime("%H:%M"),
            closes_at=slot.closes_at.strftime("%H:%M"),
        )
        for slot in sorted(postamat.schedule, key=lambda slot: slot.weekday)
    ]


def schedule_out(postamat: Postamat) -> "ScheduleOut":
    return ScheduleOut(round_the_clock=postamat.round_the_clock,
                       days=_slots(postamat))


def location_of(postamat: Postamat) -> GeoPoint | None:
    if postamat.latitude is None or postamat.longitude is None:
        return None
    return GeoPoint(lat=postamat.latitude, lon=postamat.longitude)


def working_hours_of(postamat: Postamat) -> str | None:
    """The one line the table prints for opening hours.

    What the operator typed wins; otherwise it is read off the schedule, which
    is the fact the rest of the system actually enforces. A machine with neither
    says nothing rather than inventing hours.
    """
    if postamat.working_hours:
        return postamat.working_hours
    if postamat.round_the_clock:
        return "24/7"
    if not postamat.schedule:
        return None
    opens = min(slot.opens_at for slot in postamat.schedule)
    closes = max(slot.closes_at for slot in postamat.schedule)
    return f"{opens.strftime('%H:%M')}–{closes.strftime('%H:%M')}"


def _common(postamat: Postamat, occupied: int = 0, total: int = 0) -> dict:
    return {
        "id": postamat.id, "number": postamat.number, "name": postamat.name,
        "city_id": postamat.city_id, "address": postamat.address,
        "location": location_of(postamat),
        "working_hours": working_hours_of(postamat),
        "status": postamat.status, "round_the_clock": postamat.round_the_clock,
        "schedule": _slots(postamat), "photos": photos_out(postamat),
        "occupied_cell_count": occupied, "total_cell_count": total,
        "created_at": utc_isoformat(postamat.created_at),
    }


def postamat_out(postamat: Postamat, occupied: int = 0,
                 total: int = 0) -> PostamatOut:
    return PostamatOut(**_common(postamat, occupied, total))


def postamat_details_out(
    postamat: Postamat, occupied: int = 0, total: int = 0,
    device: DeviceBrief | None = None,
) -> PostamatDetailsOut:
    return PostamatDetailsOut(
        **_common(postamat, occupied, total), device=device,
        grid_rows=postamat.grid_rows, grid_cols=postamat.grid_cols,
        blocked_reason=postamat.blocked_reason,
        updated_at=utc_isoformat(postamat.updated_at),
    )


class PriceOut(BaseModel):
    duration_hours: int
    amount_minor: int
    currency: str


class AvailabilityItem(BaseModel):
    cell_type_id: uuid.UUID
    code: str
    name: str
    width_mm: int
    height_mm: int
    depth_mm: int
    free: int
    prices: list[PriceOut]


class AvailabilityOut(BaseModel):
    items: list[AvailabilityItem]


def postamat_public_out(postamat: Postamat, moment: datetime) -> PostamatPublicOut:
    opening = next_opening_after(postamat, moment)
    return PostamatPublicOut(
        id=postamat.id, number=postamat.number, name=postamat.name,
        city_id=postamat.city_id, address=postamat.address,
        location=location_of(postamat), working_hours=working_hours_of(postamat),
        status=postamat.status, round_the_clock=postamat.round_the_clock,
        schedule=_slots(postamat), photos=photos_out(postamat),
        is_open_now=is_open_at(postamat, moment),
        next_opening_at=None if opening is None else utc_isoformat(opening),
    )
