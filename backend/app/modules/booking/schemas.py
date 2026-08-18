import uuid

from pydantic import BaseModel, Field, model_validator

from app.core.pagination import CursorMeta, PageMeta
from app.core.types import Money, PhoneNumber, utc_isoformat
from app.modules.booking.models import BookingStatus, Depositor
from app.modules.payments.models import PaymentStatus


class BookingCreate(BaseModel):
    postamat_id: uuid.UUID
    cell_type_id: uuid.UUID
    # Not validated against the tariff list here: an unpriced duration is
    # answered with DURATION_NOT_SUPPORTED by the endpoint, which is the code the
    # contract names and the app renders.
    duration_hours: int
    # Optional: omitting it is the «Пропустить» path, and means the sender will
    # collect the parcel themselves.
    recipient_phone: PhoneNumber | None = None
    recipient_name: str | None = Field(default=None, max_length=200)
    deposited_by: Depositor = Depositor.OWNER
    courier_phone: PhoneNumber | None = None

    @model_validator(mode="after")
    def _courier_comes_with_a_number(self) -> "BookingCreate":
        # The deposit code is delivered by SMS. Without a number it is issued to
        # nobody, and the parcel waits for a courier who never got it.
        if self.deposited_by is Depositor.COURIER and not self.courier_phone:
            raise ValueError("courier_phone is required when the courier deposits")
        if self.deposited_by is Depositor.OWNER and self.courier_phone:
            raise ValueError("courier_phone is only for a courier deposit")
        return self


class TimelineEntry(BaseModel):
    status: BookingStatus
    # The contract's checklist vocabulary: booked, paid, parcel_deposited,
    # pickup_code_sent, collected, expired, cancelled. Null for transitions the
    # checklist does not draw.
    step: str | None
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
            TimelineEntry(status=event.status, step=event.step,
                          message=event.message, at=utc_isoformat(event.created_at))
            for event in booking.events
        ],
    )


class CancelRequest(BaseModel):
    reason: str = Field(default="Отменено клиентом", min_length=1, max_length=500)



# ── the client surface, in the front-end contract's names ────────────────────


class PaymentBrief(BaseModel):
    id: uuid.UUID
    booking_id: uuid.UUID
    status: PaymentStatus
    amount: Money
    bank_code: str
    redirect_url: str | None
    expires_at: str | None
    failure_code: str | None
    created_at: str


class BookingListItem(BaseModel):
    id: uuid.UUID
    status: BookingStatus
    postamat_id: uuid.UUID
    postamat_name: str
    # A string, because a door label is not arithmetic.
    cell_number: str
    cell_type_name: str
    created_at: str
    expires_at: str | None


class BookingDetail(BookingListItem):
    postamat_address: str
    cell_id: uuid.UUID
    duration_hours: int
    price: Money
    recipient_phone: str | None
    recipient_name: str | None
    # `depositor` in the database, `deposited_by` on the wire. The rename stops
    # here rather than in a migration.
    deposited_by: Depositor
    courier_phone: str | None
    hold_expires_at: str | None
    # Filled only by the response that issues it — creation and rotation. Every
    # other read is null: the server keeps a keyed hash and cannot produce the
    # digits again.
    deposit_code: str | None
    pickup_code_sent_at: str | None
    qr_payload: str | None
    payment: PaymentBrief | None
    # Server time this payload was produced. The app stores it with the offline
    # copy and shows «данные на такое-то время» when there is no network.
    cached_at: str


class BookingCursorPage(BaseModel):
    items: list[BookingListItem]
    pagination: CursorMeta


class TimelineStep(BaseModel):
    step: str
    # Null means it has not happened; the app greys the row out and keeps the
    # order, which is why every step is returned whether or not it occurred.
    occurred_at: str | None
    actor: str | None


class Timeline(BaseModel):
    items: list[TimelineStep]


class CodeIssued(BaseModel):
    code: str
    # Always present: a grant with no end is a grant somebody finds in an old
    # SMS a month later.
    expires_at: str
    # Seconds before this booking may ask for another code.
    resend_after: int


class TransferRequest(BaseModel):
    new_phone: PhoneNumber
    new_name: str | None = Field(default=None, max_length=200)
