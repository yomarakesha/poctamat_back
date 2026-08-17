import uuid

from pydantic import BaseModel, Field

from app.core.types import PhoneNumber


class OtpRequest(BaseModel):
    phone: PhoneNumber


class OtpRequestResult(BaseModel):
    request_id: uuid.UUID
    code_length: int
    expires_at: str
    # Seconds before this phone may ask for another code. The app renders a
    # countdown against it rather than counting locally.
    resend_after: int


class OtpVerify(BaseModel):
    request_id: uuid.UUID
    code: str = Field(pattern=r"^[0-9]{6}$")


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    # Access token lifetime in seconds, so a client can refresh before it dies
    # rather than after a request has already failed.
    expires_in: int


class ClientTokenPair(TokenPair):
    # False for a brand-new client: the app shows the registration form before
    # anything else.
    profile_complete: bool


class RefreshRequest(BaseModel):
    refresh_token: str
