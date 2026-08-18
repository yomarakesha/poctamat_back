import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Protocol

from app.core.config import get_settings


@dataclass(frozen=True)
class StartedPayment:
    provider_payment_id: str
    redirect_url: str


@dataclass(frozen=True)
class WebhookEvent:
    payment_id: uuid.UUID
    provider_payment_id: str
    succeeded: bool
    reason: str | None = None
    # Checked against what the booking was priced at when the acquirer sends it.
    # None means the callback did not carry one, not that it matched.
    amount_minor: int | None = None


class PaymentProvider(Protocol):
    name: str

    async def start(
        self, payment_id: uuid.UUID, amount_minor: int, currency: str, return_url: str
    ) -> StartedPayment:
        """Open a payment at the acquirer and say where to send the customer."""

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        """Verify the callback's signature and read the result out of it."""


def sign_payload(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class MockProvider:
    """Stands in until the bank is named.

    It behaves like an acquirer in the two ways that matter here: the customer
    leaves for a URL the provider chooses, and the result arrives later as a
    signed webhook rather than as the answer to our own call. Card details never
    pass through this server, which is what keeps the project out of PCI DSS
    SAQ-D.
    """

    # The name the webhook path carries. The contract types that path parameter
    # as a bank code; `mock` is accepted beside them until an acquirer ships,
    # and that is a development affordance, not a fourth bank.
    name = "mock"

    async def start(
        self, payment_id: uuid.UUID, amount_minor: int, currency: str, return_url: str
    ) -> StartedPayment:
        return StartedPayment(
            provider_payment_id=f"mock-{payment_id}",
            redirect_url=(
                f"https://pay.invalid/mock/{payment_id}"
                f"?amount={amount_minor}&currency={currency}&return_url={return_url}"
            ),
        )

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        settings = get_settings()
        signature = headers.get("x-signature") or headers.get("X-Signature") or ""
        expected = sign_payload(body, settings.payment_webhook_secret)
        # compare_digest, not ==: this endpoint is unauthenticated by necessity,
        # and timing on a string comparison is a signature oracle.
        if not hmac.compare_digest(signature, expected):
            raise ValueError("bad signature")

        payload = json.loads(body)
        return WebhookEvent(
            payment_id=uuid.UUID(payload["payment_id"]),
            provider_payment_id=payload.get("provider_payment_id", ""),
            succeeded=payload.get("status") == "succeeded",
            reason=payload.get("reason"),
            amount_minor=payload.get("amount_minor"),
        )


_provider: PaymentProvider | None = None


def get_payment_provider() -> PaymentProvider:
    global _provider
    if _provider is None:
        settings = get_settings()
        if settings.payment_provider != "mock":
            raise RuntimeError(
                f"unknown payment provider {settings.payment_provider!r}"
            )
        _provider = MockProvider()
    return _provider


def reset_payment_provider() -> None:
    global _provider
    _provider = None
