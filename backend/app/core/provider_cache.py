from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class ProviderCache(Generic[T]):
    """Lazily builds one instance from settings and holds onto it.

    Every notification channel (SMS, push, ...) picks its concrete provider
    this way: built on first use, not at import time, so a setting the
    environment supplies late is still picked up. `reset()` drops the cached
    instance — tests call it so one test's provider, and a stray provider
    setting from the environment, never survive into the next.
    """

    def __init__(self, factory: Callable[[], T]) -> None:
        self._factory = factory
        self._value: T | None = None

    def get(self) -> T:
        if self._value is None:
            self._value = self._factory()
        return self._value

    def reset(self) -> None:
        self._value = None
