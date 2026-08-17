from fastapi import Request

from app.core.errors import AppError, ErrorCode
from app.core.kvstore import get_kvstore


def rate_limit(bucket: str, limit: int, window_seconds: int):
    """A per-IP fixed-window counter, one window per bucket.

    Keyed on the caller's address because these routes are the unauthenticated
    surface — there is no subject to key on yet, and an OTP request costs money
    per message. Single-worker only, for the same reason the key-value store is:
    each process counts its own window until Redis replaces the store.
    """

    async def dependency(request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        key = f"rl:{bucket}:{client_ip}"

        current, ttl = await get_kvstore().increment(key, window_seconds)
        if current > limit:
            raise AppError(
                ErrorCode.RATE_LIMITED, "Too many requests.", 429,
                details={"retry_after_seconds": max(ttl, 1)},
            )

    return dependency
