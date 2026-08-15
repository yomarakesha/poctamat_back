import time
from dataclasses import dataclass


@dataclass
class _Entry:
    value: dict[str, str]
    expires_at: float


class KeyValueStore:
    """Short-lived keyed values with a TTL.

    In-process stand-in for Redis: correct for a single worker, and the only
    implementation this deployment needs until Redis is introduced. Swapping in a
    Redis-backed implementation means replacing this class, not its callers.

    Single-worker only: with more than one uvicorn worker each process keeps its
    own dictionary, so one-time codes and rate-limit counters diverge. That is
    acceptable now and is the reason Redis returns later.
    """

    def __init__(self) -> None:
        self._data: dict[str, _Entry] = {}

    def _live(self, key: str) -> _Entry | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            del self._data[key]
            return None
        return entry

    async def put(self, key: str, value: dict[str, str], ttl_seconds: int) -> None:
        self._data[key] = _Entry(dict(value), time.monotonic() + ttl_seconds)

    async def get(self, key: str) -> dict[str, str] | None:
        entry = self._live(key)
        return dict(entry.value) if entry else None

    async def set_field(self, key: str, field: str, value: str) -> None:
        entry = self._live(key)
        if entry is not None:
            entry.value[field] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def increment(self, key: str, ttl_seconds: int) -> tuple[int, int]:
        """Return the new counter value and the seconds left in its window."""
        entry = self._live(key)
        if entry is None:
            self._data[key] = _Entry({"n": "1"}, time.monotonic() + ttl_seconds)
            return 1, ttl_seconds
        entry.value["n"] = str(int(entry.value["n"]) + 1)
        return int(entry.value["n"]), max(1, int(entry.expires_at - time.monotonic()))

    def clear(self) -> None:
        self._data.clear()


_store = KeyValueStore()


def get_kvstore() -> KeyValueStore:
    return _store
