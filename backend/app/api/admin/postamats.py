import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_permission
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.storage import ALLOWED_IMAGE_TYPES, get_file_storage
from app.core.pagination import PageParams, page_params, paginate_page
from app.modules.audit.models import Source
from app.modules.audit.service import record
from app.modules.catalog.models import (
    Postamat,
    PostamatPhoto,
    PostamatSchedule,
    PostamatStatus,
)
from app.modules.catalog.schemas import (
    MEDIA_PREFIX,
    BlockRequest,
    PhotoOut,
    PostamatIn,
    PostamatOut,
    PostamatPage,
    PostamatPatch,
    SchedulePut,
    postamat_out,
)
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/postamats", tags=["admin-postamats"])


async def _load(session: AsyncSession, postamat_id: uuid.UUID) -> Postamat:
    postamat = await session.get(Postamat, postamat_id)
    if postamat is None:
        raise AppError(ErrorCode.NOT_FOUND, "Postamat not found.", 404)
    return postamat


@router.get("", response_model=PostamatPage)
async def list_postamats(
    city_id: uuid.UUID | None = Query(default=None),
    status: PostamatStatus | None = Query(default=None),
    params: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("postamats.read")),
) -> PostamatPage:
    stmt = select(Postamat).order_by(Postamat.number)
    if city_id is not None:
        stmt = stmt.where(Postamat.city_id == city_id)
    if status is not None:
        stmt = stmt.where(Postamat.status == status)
    rows, meta = await paginate_page(session, stmt, params)
    return PostamatPage(items=[postamat_out(row) for row in rows], pagination=meta)


@router.get("/{postamat_id}", response_model=PostamatOut)
async def get_postamat(
    postamat_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("postamats.read")),
) -> PostamatOut:
    return postamat_out(await _load(session, postamat_id))


@router.post("", response_model=PostamatOut, status_code=201)
async def create_postamat(
    payload: PostamatIn,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatOut:
    # Checked rather than left to the unique index, so the operator gets the
    # field name back instead of a 500 from an IntegrityError.
    taken = await session.scalar(
        select(Postamat.id).where(Postamat.number == payload.number)
    )
    if taken is not None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "Postamat number already exists.",
                       409, details={"field": "number"})
    postamat = Postamat(**payload.model_dump())
    session.add(postamat)
    await session.flush()
    await record(session, event="postamat.created", source=Source.ADMIN,
                 message=f"Postamat {postamat.number} created", actor=admin.login,
                 postamat_id=postamat.id)
    await session.commit()
    # created_at comes from a server default, so it is unloaded until the row is
    # read back. Refreshing here rather than letting the serializer touch it
    # keeps the load out of async lazy-load territory.
    await session.refresh(postamat)
    return postamat_out(postamat)


@router.patch("/{postamat_id}", response_model=PostamatOut)
async def update_postamat(
    postamat_id: uuid.UUID,
    payload: PostamatPatch,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatOut:
    postamat = await _load(session, postamat_id)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(postamat, field, value)
    # `number` and `status` are deliberately absent from PostamatPatch: the
    # number is what the machine is called on its own label, and a status change
    # has to carry a reason, which is what block/unblock exist for.
    await record(session, event="postamat.updated", source=Source.ADMIN,
                 message=f"Postamat {postamat.number} updated", actor=admin.login,
                 postamat_id=postamat.id, details={"fields": sorted(changes)})
    await session.commit()
    return postamat_out(postamat)


@router.post("/{postamat_id}/block", response_model=PostamatOut)
async def block_postamat(
    postamat_id: uuid.UUID,
    payload: BlockRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatOut:
    postamat = await _load(session, postamat_id)
    postamat.status = PostamatStatus.BLOCKED
    await record(session, event="postamat.blocked", source=Source.ADMIN,
                 message=payload.reason, actor=admin.login, postamat_id=postamat.id)
    await session.commit()
    return postamat_out(postamat)


@router.post("/{postamat_id}/unblock", response_model=PostamatOut)
async def unblock_postamat(
    postamat_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatOut:
    postamat = await _load(session, postamat_id)
    postamat.status = PostamatStatus.ACTIVE
    await record(session, event="postamat.unblocked", source=Source.ADMIN,
                 message=f"Postamat {postamat.number} returned to service",
                 actor=admin.login, postamat_id=postamat.id)
    await session.commit()
    return postamat_out(postamat)


@router.post("/{postamat_id}/photos", response_model=PhotoOut, status_code=201)
async def upload_photo(
    postamat_id: uuid.UUID,
    file: UploadFile = File(...),
    caption: str | None = Form(default=None),
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PhotoOut:
    postamat = await _load(session, postamat_id)
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise AppError(ErrorCode.VALIDATION_ERROR,
                       "Only PNG, JPEG and WebP images are accepted.", 422,
                       details={"content_type": file.content_type})

    data = await file.read()
    if len(data) > get_settings().max_upload_bytes:
        # 413 rather than 422: the file is not malformed, it is too large, and
        # the client can act on that difference.
        raise AppError(ErrorCode.VALIDATION_ERROR, "The file is too large.", 413,
                       details={"max_bytes": get_settings().max_upload_bytes})

    key = get_file_storage().save(data, file.content_type)
    photo = PostamatPhoto(
        storage_key=key, content_type=file.content_type, caption=caption,
        position=len(postamat.photos),
    )
    # Appended to the collection rather than added to the session on its own, so
    # the postamat in memory carries the new photo too — otherwise the very next
    # read of this object in the same session shows a gallery one photo short.
    postamat.photos.append(photo)
    await record(session, event="postamat.photo_added", source=Source.ADMIN,
                 message=f"Photo added to postamat {postamat.number}",
                 actor=admin.login, postamat_id=postamat.id)
    await session.commit()
    return PhotoOut(id=photo.id, url=f"{MEDIA_PREFIX}/{key}", caption=photo.caption)


@router.delete("/{postamat_id}/photos/{photo_id}", status_code=204)
async def delete_photo(
    postamat_id: uuid.UUID,
    photo_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> Response:
    postamat = await _load(session, postamat_id)
    photo = next((row for row in postamat.photos if row.id == photo_id), None)
    if photo is None:
        raise AppError(ErrorCode.NOT_FOUND, "Photo not found.", 404)

    key = photo.storage_key
    # Removed from the collection, not deleted on its own: delete-orphan turns
    # this into the DELETE, and the postamat in memory stops carrying a photo
    # that no longer exists.
    postamat.photos.remove(photo)
    await record(session, event="postamat.photo_removed", source=Source.ADMIN,
                 message="Photo removed", actor=admin.login, postamat_id=postamat_id)
    await session.commit()
    # The file goes after the row, not before: a delete that fails halfway
    # should leave an orphan file rather than a row pointing at nothing.
    get_file_storage().delete(key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{postamat_id}/schedule", response_model=PostamatOut)
async def set_schedule(
    postamat_id: uuid.UUID,
    payload: SchedulePut,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatOut:
    postamat = await _load(session, postamat_id)
    # A whole-week replacement rather than a per-day edit: the operator sees the
    # week as one form, and a partial update would leave a day nobody meant to
    # keep. delete-orphan on the relationship removes the rows that are gone.
    postamat.schedule = [
        PostamatSchedule(weekday=slot.weekday, opens_at=slot.opens_at,
                         closes_at=slot.closes_at)
        for slot in payload.slots
    ]
    await record(session, event="postamat.schedule_set", source=Source.ADMIN,
                 message=f"Schedule set for postamat {postamat.number}",
                 actor=admin.login, postamat_id=postamat.id,
                 details={"days": sorted(slot.weekday for slot in payload.slots)})
    await session.commit()
    return postamat_out(postamat)
