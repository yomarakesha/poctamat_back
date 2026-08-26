import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.core.config import get_settings
from app.core.provider_cache import ProviderCache

logger = logging.getLogger("app.sms")


@dataclass
class SentMessage:
    phone: str
    text: str


@dataclass(frozen=True)
class DeliveryEvent:
    provider_message_id: str
    delivered: bool
    status: str
    error: str | None = None


class SmsProvider(Protocol):
    name: str

    async def send(self, phone: str, text: str) -> str:
        """Deliver a message and return the provider's id for it."""

    def parse_delivery_report(self, headers: dict[str, str], body: bytes) -> DeliveryEvent:
        """Verify the callback's signature and read the result out of it."""


@dataclass
class LoggingSmsProvider:
    """Records messages instead of sending them.

    What development and the test suite run on. It keeps an outbox so tests can
    assert on delivery, and logs only the number and the length — a one-time
    code in a log file is the same leak as one in the database.
    """

    name: str = "log"
    outbox: list[SentMessage] = field(default_factory=list)

    async def send(self, phone: str, text: str) -> str:
        self.outbox.append(SentMessage(phone=phone, text=text))
        logger.info("sms to %s (%d chars)", phone, len(text))
        return f"log-{len(self.outbox)}"

    def parse_delivery_report(self, headers: dict[str, str], body: bytes) -> DeliveryEvent:
        # No gateway to forge a signature from in development, so none is
        # checked here — this provider is never wired to a public endpoint.
        payload = json.loads(body)
        status = payload["status"]
        return DeliveryEvent(
            provider_message_id=str(payload["message_id"]),
            delivered=status == "delivered",
            status=status,
            error=payload.get("error"),
        )


@dataclass
class PostTmSmsProvider:
    """The operator's gateway.

        POST https://sms.post.tm/api/clients/sms/create
        Authorization: Bearer <token>
        {"phone": 99362615986, "content": "..."}

    Note the shape of `phone`: digits, as a number, with no leading plus. Stored
    numbers always carry the plus, so it is stripped here rather than at every
    call site.
    """

    token: str
    name: str = "post_tm"
    base_url: str = "https://sms.post.tm"
    timeout: float = 10.0

    @staticmethod
    def digits(phone: str) -> int:
        return int(phone.lstrip("+"))

    async def send(self, phone: str, text: str, _transport: Any = None) -> str:
        async with httpx.AsyncClient(timeout=self.timeout,
                                     transport=_transport) as client:
            response = await client.post(
                f"{self.base_url}/api/clients/sms/create",
                headers={"Authorization": f"Bearer {self.token}",
                         "accept": "application/json"},
                json={"phone": self.digits(phone), "content": text},
            )
        # The status decides whether the gateway accepted it; anything else
        # raises and is stored on the notification rather than lost.
        response.raise_for_status()
        try:
            return str(response.json().get("id", ""))
        except ValueError:
            return ""

    def parse_delivery_report(self, headers: dict[str, str], body: bytes) -> DeliveryEvent:
        # post.tm has not documented its delivery-callback shape or signature
        # scheme yet — same open question the payment webhook has for its
        # acquirer. Provisional until the partner confirms: HMAC-SHA256 over
        # the raw body with `SMS_WEBHOOK_SECRET`, in `X-Webhook-Signature`.
        settings = get_settings()
        signature = (
            headers.get("x-webhook-signature")
            or headers.get("X-Webhook-Signature")
            or ""
        )
        expected = hmac.new(
            settings.sms_webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("bad signature")

        payload = json.loads(body)
        status = payload["status"]
        return DeliveryEvent(
            provider_message_id=str(payload["message_id"]),
            delivered=status == "delivered",
            status=status,
            error=payload.get("error"),
        )


def _build_sms_provider() -> SmsProvider:
    settings = get_settings()
    if settings.sms_provider == "log":
        return LoggingSmsProvider()
    if settings.sms_provider == "post_tm":
        if not settings.sms_api_token:
            raise RuntimeError("SMS_API_TOKEN is required for the post_tm provider")
        return PostTmSmsProvider(
            token=settings.sms_api_token, base_url=settings.sms_base_url,
            timeout=settings.sms_timeout_seconds,
        )
    raise RuntimeError(f"unknown SMS provider {settings.sms_provider!r}")


_cache: ProviderCache[SmsProvider] = ProviderCache(_build_sms_provider)


def get_sms_provider() -> SmsProvider:
    return _cache.get()


def reset_sms_provider() -> None:
    """Drop the cached provider. Tests use this; production never does."""
    _cache.reset()
