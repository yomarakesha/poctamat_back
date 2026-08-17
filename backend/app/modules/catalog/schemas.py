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
    width_cm: int
    height_cm: int
    depth_cm: int


def localized(row, language: str) -> str:
    """Pick the name column for `language`, falling back to Turkmen.

    An unknown language must not raise: the header is caller-supplied and a
    reference list is not the place to reject a request over it.
    """
    return getattr(row, f"name_{language}", row.name_tk)


class ScheduleSlotIn(BaseModel):
    weekday: int = Field(ge=0, le=6)  # 0 = Monday
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
    slots: list[ScheduleSlotIn]

    @model_validator(mode="after")
    def _one_slot_per_weekday(self) -> "SchedulePut":
        days = [slot.weekday for slot in self.slots]
        if len(days) != len(set(days)):
            raise ValueError("each weekday may appear at most once")
        return self


class ScheduleSlotOut(BaseModel):
    weekday: int
    opens_at: time
    closes_at: time


class PostamatIn(BaseModel):
    number: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    city_id: uuid.UUID
    address: str = Field(min_length=1, max_length=500)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    round_the_clock: bool = False


class PostamatPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    city_id: uuid.UUID | None = None
    address: str | None = Field(default=None, min_length=1, max_length=500)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    round_the_clock: bool | None = None


class BlockRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class PostamatOut(BaseModel):
    id: uuid.UUID
    number: str
    name: str
    city_id: uuid.UUID
    address: str
    latitude: float | None
    longitude: float | None
    status: PostamatStatus
    round_the_clock: bool
    schedule: list[ScheduleSlotOut]
    # A string rather than a datetime: Pydantic renders an aware datetime with
    # a +00:00 offset, and the wire format is fixed at trailing Z.
    created_at: str


class PostamatPage(BaseModel):
    items: list[PostamatOut]
    pagination: PageMeta


class PostamatPublicOut(BaseModel):
    id: uuid.UUID
    number: str
    name: str
    city_id: uuid.UUID
    address: str
    latitude: float | None
    longitude: float | None
    status: PostamatStatus
    round_the_clock: bool
    schedule: list[ScheduleSlotOut]
    is_open_now: bool
    # None when the machine has no schedule at all, which reads as "call us"
    # rather than a time the client can wait for.
    next_opening_at: str | None


class PostamatPublicPage(BaseModel):
    items: list[PostamatPublicOut]
    pagination: PageMeta


def _slots(postamat: Postamat) -> list[ScheduleSlotOut]:
    return [
        ScheduleSlotOut(weekday=slot.weekday, opens_at=slot.opens_at,
                        closes_at=slot.closes_at)
        for slot in sorted(postamat.schedule, key=lambda slot: slot.weekday)
    ]


def postamat_out(postamat: Postamat) -> PostamatOut:
    return PostamatOut(
        id=postamat.id, number=postamat.number, name=postamat.name,
        city_id=postamat.city_id, address=postamat.address,
        latitude=postamat.latitude, longitude=postamat.longitude,
        status=postamat.status, round_the_clock=postamat.round_the_clock,
        schedule=_slots(postamat), created_at=utc_isoformat(postamat.created_at),
    )


def postamat_public_out(postamat: Postamat, moment: datetime) -> PostamatPublicOut:
    opening = next_opening_after(postamat, moment)
    return PostamatPublicOut(
        id=postamat.id, number=postamat.number, name=postamat.name,
        city_id=postamat.city_id, address=postamat.address,
        latitude=postamat.latitude, longitude=postamat.longitude,
        status=postamat.status, round_the_clock=postamat.round_the_clock,
        schedule=_slots(postamat),
        is_open_now=is_open_at(postamat, moment),
        next_opening_at=None if opening is None else utc_isoformat(opening),
    )
