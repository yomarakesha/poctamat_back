import json
import uuid

import pytest

from app.core.config import get_settings
from app.modules.payments.provider import (
    MockProvider,
    get_payment_provider,
    sign_payload,
)


async def test_starting_a_payment_returns_a_redirect_and_an_id():
    provider = MockProvider()
    payment_id = uuid.uuid4()
    started = await provider.start(payment_id, 1800, "TMT", "https://x.invalid/done")

    assert started.provider_payment_id
    # The customer leaves for the provider: card details never reach us.
    assert str(payment_id) in started.redirect_url


def test_a_webhook_without_a_valid_signature_is_refused():
    provider = MockProvider()
    body = json.dumps({"payment_id": str(uuid.uuid4()), "status": "succeeded"}).encode()
    with pytest.raises(ValueError):
        provider.parse_webhook({"X-Signature": "nonsense"}, body)


def test_a_webhook_with_no_signature_at_all_is_refused():
    provider = MockProvider()
    body = json.dumps({"payment_id": str(uuid.uuid4()), "status": "succeeded"}).encode()
    with pytest.raises(ValueError):
        provider.parse_webhook({}, body)


def test_a_tampered_body_no_longer_matches_its_signature():
    provider = MockProvider()
    payment_id = uuid.uuid4()
    body = json.dumps({"payment_id": str(payment_id), "status": "failed"}).encode()
    signature = sign_payload(body, get_settings().payment_webhook_secret)

    tampered = json.dumps({"payment_id": str(payment_id), "status": "succeeded"}).encode()
    with pytest.raises(ValueError):
        provider.parse_webhook({"X-Signature": signature}, tampered)


def test_a_signed_webhook_parses():
    provider = MockProvider()
    payment_id = uuid.uuid4()
    body = json.dumps({
        "payment_id": str(payment_id), "provider_payment_id": "mock-1",
        "status": "succeeded",
    }).encode()
    signature = sign_payload(body, get_settings().payment_webhook_secret)

    event = provider.parse_webhook({"X-Signature": signature}, body)
    assert event.payment_id == payment_id
    assert event.provider_payment_id == "mock-1"
    assert event.succeeded is True


def test_the_provider_is_chosen_by_configuration():
    assert isinstance(get_payment_provider(), MockProvider)
