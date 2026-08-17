import uuid

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.config import get_settings
from app.core.pagination import PageMeta
from app.core.types import PhoneNumber, utc_isoformat
from app.modules.booking.models import BookingStatus, CodePurpose, Depositor


class BookingCreate(BaseModel):
    postamat_id: uuid.UUID
    cell_type_id: uuid.UUID
    duration_hours: int
    recipient_phone: PhoneNumber
    recipient_name: str | None = Field(default=None, max_length=200)
    depositor: Depositor = Depositor.OWNER
    courier_phone: PhoneNumber | None = None

    @field_validator("duration_hours")
    @classmethod
    def _known_duration(cls, value: int) -> int:
        durations = get_settings().rental_durations
        if value not in durations:
            raise ValueError(f"duration_hours must be one of {list(durations)}")
        return value

    @model_validator(mode="after")
    def _courier_comes_with_a_number(self) -> "BookingCreate":
        # The courier's PIN is delivered by SMS. Without a number the code is
        # issued to nobody, and the parcel waits for a courier who never got it.
        if self.depositor is Depositor.COURIER and not self.courier_phone:
            raise ValueError("courier_phone is required when the courier deposits")
        if self.depositor is Depositor.OWNER and self.courier_phone:
            raise ValueError("courier_phone is only for a courier deposit")
        return self


class TimelineEntry(BaseModel):
    status: BookingStatus
    message: str
    at: str


class BookingOut(BaseModel):
    id: uuid.UUID
    status: BookingStatus
    postamat_id: uuid.UUID
    cell_number: int
    cell_type_id: uuid.UUID
    duration_hours: int
    amount_minor: int
    currency: str
    recipient_phone: str
    recipient_name: str | None
    depositor: Depositor
    hold_expires_at: str | None
    expires_at: str | None
    created_at: str
    timeline: list[TimelineEntry]


class BookingCreated(BookingOut):
    # The only response that ever carries plaintext PINs. They are shown once,
    # here, and exist as digests everywhere else.
    codes: dict[CodePurpose, str]


class BookingPage(BaseModel):
    items: list[BookingOut]
    pagination: PageMeta


def booking_out(booking, cell_number: int) -> BookingOut:
    """One booking as the wire sees it.

    Shared by the client and the admin surfaces so neither can drift into
    exposing a field the other hides — `codes` in particular lives only on
    BookingCreated, so no list or detail response can carry a plaintext PIN.
    """
    return BookingOut(
        id=booking.id, status=booking.status, postamat_id=booking.postamat_id,
        cell_number=cell_number, cell_type_id=booking.cell_type_id,
        duration_hours=booking.duration_hours, amount_minor=booking.amount_minor,
        currency=booking.currency, recipient_phone=booking.recipient_phone,
        recipient_name=booking.recipient_name, depositor=booking.depositor,
        hold_expires_at=(
            utc_isoformat(booking.hold_expires_at) if booking.hold_expires_at else None
        ),
        expires_at=utc_isoformat(booking.expires_at) if booking.expires_at else None,
        created_at=utc_isoformat(booking.created_at),
        timeline=[
            TimelineEntry(status=event.status, message=event.message,
                          at=utc_isoformat(event.created_at))
            for event in booking.events
        ],
    )


class CancelRequest(BaseModel):
    reason: str = Field(default="Отменено клиентом", min_length=1, max_length=500)


class CourierCodeOut(BaseModel):
    courier_code: str
