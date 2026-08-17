from pathlib import Path

from fastapi import APIRouter, Response

from app.core.errors import AppError, ErrorCode
from app.core.storage import CONTENT_TYPES, get_file_storage

router = APIRouter(tags=["media"])


@router.get("/media/{key}")
async def get_media(key: str) -> Response:
    """Serve a stored file by its key.

    The key comes from a URL and is therefore attacker-controlled; the storage
    validates its shape before touching the filesystem. A malformed key and a
    missing one answer identically, because the difference between them is what
    a traversal attempt is refined against.
    """
    try:
        data = get_file_storage().open(key)
    except (ValueError, OSError):
        raise AppError(ErrorCode.NOT_FOUND, "Not found.", 404) from None

    media_type = CONTENT_TYPES.get(Path(key).suffix, "application/octet-stream")
    # Cached hard: the key changes whenever the file does, so a stale copy is
    # not a thing that can happen.
    return Response(content=data, media_type=media_type,
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})
