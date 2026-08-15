from pydantic import BaseModel

from app.core.types import PhoneNumber


class OtpRequest(BaseModel):
    phone: PhoneNumber


class OtpRequestResult(BaseModel):
    code_length: int
    expires_in_seconds: int


class OtpVerify(BaseModel):
    phone: PhoneNumber
    code: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    is_new_client: bool = False


class RefreshRequest(BaseModel):
    refresh_token: str
