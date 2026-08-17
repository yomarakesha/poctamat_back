import uuid

from pydantic import BaseModel, Field

from app.core.pagination import PageMeta
from app.core.types import PhoneNumber
from app.modules.booking.models import BookingStatus, CodePurpose, Depositor


class BookingCreate(BaseModel):
    postamat_id: uuid.UUID
    cell_type_id: uuid.UUID
    duration_hours: int
    recipient_phone: PhoneNumber
    recipient_name: str | None = Field(default=None, max_length=200)
    depositor: Depositor = Depositor.OWNER
    courier_phone: PhoneNumber | None = None


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


class CancelRequest(BaseModel):
    reason: str = Field(default="Отменено клиентом", min_length=1, max_length=500)


class CourierCodeOut(BaseModel):
    courier_code: str
