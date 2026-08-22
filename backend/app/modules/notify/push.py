import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import jwt

from app.core.config import get_settings
from app.core.provider_cache import ProviderCache

logger = logging.getLogger("app.push")

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_TOKEN_URL = "https://oauth2.googleapis.com/token"


@dataclass
class SentPush:
    token: str
    title: str
    body: str


class PushProvider(Protocol):
    name: str

    async def send(
        self, token: str, title: str, body: str, data: dict[str, str] | None = None
    ) -> str:
        """Deliver a push to one device token and return the provider's id for it."""


@dataclass
class LoggingPushProvider:
    """Records pushes instead of sending them.

    What development and the test suite run on, mirroring `LoggingSmsProvider`:
    an outbox so tests can assert on delivery, and a log line that carries only
    the token's edges and the title.
    """

    name: str = "log"
    outbox: list[SentPush] = field(default_factory=list)

    async def send(
        self, token: str, title: str, body: str, data: dict[str, str] | None = None
    ) -> str:
        self.outbox.append(SentPush(token=token, title=title, body=body))
        logger.info("push to %s..%s: %s", token[:6], token[-4:], title)
        return f"log-{len(self.outbox)}"


@dataclass
class FcmPushProvider:
    """Firebase Cloud Messaging, HTTP v1 — covers both `PushToken.platform` values.

    Auth is a self-signed RS256 JWT exchanged at Google's token endpoint for a
    short-lived bearer token, the standard service-account flow. The access
    token is cached and reused until it is within a minute of expiring, so a
    burst of sends costs one token mint, not one per push.
    """

    project_id: str
    client_email: str
    private_key: str
    name: str = "fcm"
    timeout: float = 10.0
    _access_token: str | None = field(default=None, init=False, repr=False)
    _expires_at: float = field(default=0.0, init=False, repr=False)
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def _shared_client(self) -> httpx.AsyncClient:
        # Built once and reused: a worker tick can fan a reminder out to every
        # device on a client's account, and a fresh client per push would pay
        # a full TCP+TLS handshake each time even though the bearer token
        # underneath it is already cached.
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _assertion(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self.client_email,
            "scope": FCM_SCOPE,
            "aud": FCM_TOKEN_URL,
            "iat": now,
            "exp": now + 3600,
        }
        return jwt.encode(claims, self.private_key, algorithm="RS256")

    async def _bearer_token(self, client: httpx.AsyncClient) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        response = await client.post(
            FCM_TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self._assertion(),
            },
        )
        response.raise_for_status()
        payload = response.json()
        self._access_token = payload["access_token"]
        self._expires_at = time.time() + payload.get("expires_in", 3600)
        return self._access_token

    async def _post(self, client: httpx.AsyncClient, message: dict[str, Any]) -> str:
        bearer = await self._bearer_token(client)
        response = await client.post(
            f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send",
            headers={"Authorization": f"Bearer {bearer}"},
            json=message,
        )
        response.raise_for_status()
        return str(response.json().get("name", ""))

    async def send(
        self,
        token: str,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
        _transport: Any = None,
    ) -> str:
        message: dict[str, Any] = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
            }
        }
        if data:
            message["message"]["data"] = data

        if _transport is not None:
            # Test hook: an isolated one-off client for this call's
            # MockTransport, never cached, so it cannot leak into a later send
            # that expects the real one.
            async with httpx.AsyncClient(timeout=self.timeout, transport=_transport) as client:
                return await self._post(client, message)
        return await self._post(self._shared_client(), message)


def _build_push_provider() -> PushProvider:
    settings = get_settings()
    if settings.push_provider == "log":
        return LoggingPushProvider()
    if settings.push_provider == "fcm":
        if not (
            settings.fcm_project_id
            and settings.fcm_client_email
            and settings.fcm_private_key
        ):
            raise RuntimeError(
                "FCM_PROJECT_ID, FCM_CLIENT_EMAIL and FCM_PRIVATE_KEY are "
                "required for the fcm provider"
            )
        return FcmPushProvider(
            project_id=settings.fcm_project_id,
            client_email=settings.fcm_client_email,
            private_key=settings.fcm_private_key,
            timeout=settings.fcm_timeout_seconds,
        )
    raise RuntimeError(f"unknown push provider {settings.push_provider!r}")


_cache: ProviderCache[PushProvider] = ProviderCache(_build_push_provider)


def get_push_provider() -> PushProvider:
    return _cache.get()


def reset_push_provider() -> None:
    """Drop the cached provider. Tests use this; production never does."""
    _cache.reset()
