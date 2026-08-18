from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorCode(StrEnum):
    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    REFRESH_TOKEN_INVALID = "REFRESH_TOKEN_INVALID"
    REFRESH_TOKEN_EXPIRED = "REFRESH_TOKEN_EXPIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    PERMISSION_DENIED = "PERMISSION_DENIED"

    OTP_INVALID = "OTP_INVALID"
    OTP_EXPIRED = "OTP_EXPIRED"
    OTP_ATTEMPTS_EXCEEDED = "OTP_ATTEMPTS_EXCEEDED"
    OTP_NOT_FOUND = "OTP_NOT_FOUND"
    OTP_REQUEST_TOO_SOON = "OTP_REQUEST_TOO_SOON"

    CLIENT_BLOCKED = "CLIENT_BLOCKED"
    ADMIN_INACTIVE = "ADMIN_INACTIVE"
    CREDENTIALS_INVALID = "CREDENTIALS_INVALID"

    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"

    TARIFF_INCOMPLETE = "TARIFF_INCOMPLETE"
    POSTAMAT_BLOCKED = "POSTAMAT_BLOCKED"
    CELL_NUMBER_TAKEN = "CELL_NUMBER_TAKEN"
    # Kept for transitions no endpoint documents individually. Where the contract
    # names a specific refusal, the specific code below is the one to raise.
    BOOKING_INVALID_STATE = "BOOKING_INVALID_STATE"
    PROFILE_INCOMPLETE = "PROFILE_INCOMPLETE"
    CITY_NOT_AVAILABLE = "CITY_NOT_AVAILABLE"
    NAME_INVALID = "NAME_INVALID"
    CITY_IN_USE = "CITY_IN_USE"

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
        return _envelope(request, ErrorCode.VALIDATION_FAILED,
                         "Request body failed validation.", {"fields": fields}, 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.INTERNAL_ERROR
        return _envelope(request, code, str(exc.detail), None, exc.status_code)
