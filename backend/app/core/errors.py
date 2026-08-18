from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    REFRESH_TOKEN_INVALID = "REFRESH_TOKEN_INVALID"
    REFRESH_TOKEN_EXPIRED = "REFRESH_TOKEN_EXPIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    FORBIDDEN = "FORBIDDEN"

    OTP_INVALID = "OTP_INVALID"
    OTP_EXPIRED = "OTP_EXPIRED"
    OTP_ATTEMPTS_EXCEEDED = "OTP_ATTEMPTS_EXCEEDED"
    OTP_NOT_FOUND = "OTP_NOT_FOUND"
    OTP_REQUEST_TOO_SOON = "OTP_REQUEST_TOO_SOON"

    CLIENT_BLOCKED = "CLIENT_BLOCKED"
    ADMIN_ACCOUNT_BLOCKED = "ADMIN_ACCOUNT_BLOCKED"
    ADMIN_CREDENTIALS_INVALID = "ADMIN_CREDENTIALS_INVALID"
    USER_LOGIN_TAKEN = "USER_LOGIN_TAKEN"
    ROLE_NOT_FOUND = "ROLE_NOT_FOUND"
    PASSWORD_TOO_WEAK = "PASSWORD_TOO_WEAK"
    CANNOT_MODIFY_SELF = "CANNOT_MODIFY_SELF"
    REASON_REQUIRED = "REASON_REQUIRED"

    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    IDEMPOTENCY_KEY_MISSING = "IDEMPOTENCY_KEY_MISSING"
    RATE_LIMITED = "RATE_LIMITED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"

    TARIFF_INCOMPLETE = "TARIFF_INCOMPLETE"
    POSTAMAT_BLOCKED = "POSTAMAT_BLOCKED"
    CELL_NUMBER_DUPLICATE = "CELL_NUMBER_DUPLICATE"
    # Kept for transitions no endpoint documents individually. Where the contract
    # names a specific refusal, the specific code below is the one to raise.
    CONFLICT = "CONFLICT"
    PROFILE_INCOMPLETE = "PROFILE_INCOMPLETE"
    CITY_NOT_AVAILABLE = "CITY_NOT_AVAILABLE"
    NAME_INVALID = "NAME_INVALID"

    NO_FREE_CELLS = "NO_FREE_CELLS"
    CELL_TYPE_UNAVAILABLE = "CELL_TYPE_UNAVAILABLE"
    DURATION_NOT_SUPPORTED = "DURATION_NOT_SUPPORTED"
    RECIPIENT_PHONE_SAME_AS_SENDER = "RECIPIENT_PHONE_SAME_AS_SENDER"
    # Declared from day one though no limit is switched on yet: the app has to
    # render it before the setting is ever raised above zero.
    BOOKING_LIMIT_EXCEEDED = "BOOKING_LIMIT_EXCEEDED"
    BOOKING_NOT_CANCELLABLE = "BOOKING_NOT_CANCELLABLE"
    BOOKING_ALREADY_CANCELLED = "BOOKING_ALREADY_CANCELLED"
    BOOKING_ALREADY_PAID = "BOOKING_ALREADY_PAID"
    BOOKING_HOLD_EXPIRED = "BOOKING_HOLD_EXPIRED"
    HOLD_EXTENSION_LIMIT_EXCEEDED = "HOLD_EXTENSION_LIMIT_EXCEEDED"

    GRANT_ALREADY_USED = "GRANT_ALREADY_USED"
    GRANT_EXPIRED = "GRANT_EXPIRED"
    PICKUP_CODE_NOT_ISSUED_YET = "PICKUP_CODE_NOT_ISSUED_YET"
    CODE_RESEND_TOO_SOON = "CODE_RESEND_TOO_SOON"
    CODE_RESEND_LIMIT_EXCEEDED = "CODE_RESEND_LIMIT_EXCEEDED"
    TRANSFER_NOT_ALLOWED = "TRANSFER_NOT_ALLOWED"
    TRANSFER_PHONE_SAME = "TRANSFER_PHONE_SAME"

    PAYMENT_ALREADY_EXISTS = "PAYMENT_ALREADY_EXISTS"
    PAYMENT_NOT_CANCELLABLE = "PAYMENT_NOT_CANCELLABLE"
    PAYMENT_PROVIDER_UNAVAILABLE = "PAYMENT_PROVIDER_UNAVAILABLE"
    BANK_NOT_SUPPORTED = "BANK_NOT_SUPPORTED"
    CALLBACK_SIGNATURE_INVALID = "CALLBACK_SIGNATURE_INVALID"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"


class AppError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


def _envelope(request: Request, code: str, message: str,
              details: dict[str, Any] | None, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "trace_id": getattr(request.state, "trace_id", None),
            }
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return _envelope(request, exc.code, exc.message, exc.details, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {"field": ".".join(str(p) for p in err["loc"][1:]), "reason": err["msg"]}
            for err in exc.errors()
        ]
        return _envelope(request, ErrorCode.VALIDATION_ERROR,
                         "Request body failed validation.", {"fields": fields}, 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # A 400 from Starlette is a body it could not parse at all — malformed
        # JSON, a truncated upload. That is the caller's request being wrong,
        # not this server breaking, so it must not answer INTERNAL_ERROR.
        code = {
            400: ErrorCode.VALIDATION_ERROR,
            401: ErrorCode.TOKEN_INVALID,
            403: ErrorCode.FORBIDDEN,
            404: ErrorCode.NOT_FOUND,
            405: ErrorCode.VALIDATION_ERROR,
            409: ErrorCode.CONFLICT,
            413: ErrorCode.FILE_TOO_LARGE,
            415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            429: ErrorCode.RATE_LIMITED,
        }.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        return _envelope(request, code, str(exc.detail), None, exc.status_code)
