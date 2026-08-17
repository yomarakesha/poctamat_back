import re
import uuid
from pathlib import Path
from typing import Protocol

from app.core.config import BACKEND_DIR, get_settings

# The only kinds a browser will render and the only ones an operator needs.
ALLOWED_IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
CONTENT_TYPES = {suffix: media for media, suffix in ALLOWED_IMAGE_TYPES.items()}
_KEY = re.compile(r"^[0-9a-f]{32}\.(png|jpg|webp)$")


class FileStorage(Protocol):
    def save(self, data: bytes, content_type: str) -> str: ...
    def open(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class LocalFileStorage:
    """Files on disk beside the application.

    Keys are generated here and validated on the way back in: a key that came
    out of a URL is attacker-controlled, and joining one straight onto a path is
    exactly how `../../.env` ends up being served.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not _KEY.match(key):
            raise ValueError("bad storage key")
        return self.root / key

    def save(self, data: bytes, content_type: str) -> str:
        suffix = ALLOWED_IMAGE_TYPES[content_type]
        key = f"{uuid.uuid4().hex}{suffix}"
        (self.root / key).write_bytes(data)
        return key

    def open(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


_storage: FileStorage | None = None


def get_file_storage() -> FileStorage:
    global _storage
    if _storage is None:
        _storage = LocalFileStorage(BACKEND_DIR / get_settings().media_root)
    return _storage


def reset_file_storage() -> None:
    global _storage
    _storage = None
