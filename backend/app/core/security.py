import hashlib
import hmac
import uuid
from datetime import timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import get_settings
from app.core.db import utcnow

_hasher = PasswordHasher()


def hash_secret(value: str) -> str:
    return _hasher.hash(value)


def verify_secret(value: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, value)
    except VerifyMismatchError:
        return False


def hash_pin(pin: str) -> str:
    # Keyed rather than salted: access codes are five digits, so an unkeyed
    # digest of one is trivially reversed by enumeration. The pepper lives in
    # settings and never in the database.
    settings = get_settings()
    return hmac.new(settings.pin_pepper.encode(), pin.encode(), hashlib.sha256).hexdigest()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_access_token(subject_type: str, subject_id: uuid.UUID,
                        extra: dict | None = None) -> str:
    settings = get_settings()
    payload = {
        "sub": str(subject_id),
        "typ": subject_type,
        "exp": utcnow() + timedelta(minutes=settings.access_token_ttl_minutes),
        "iat": utcnow(),
        **(extra or {}),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
