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
from sqlalchemy import func, select
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
    Cell,
    Device,
    Postamat,
    PostamatPhoto,
    PostamatSchedule,
    PostamatStatus,
)
from app.modules.catalog.schemas import (
    MEDIA_PREFIX,
    BlockRequest,
    DeviceBrief,
    DeviceInline,
    PhotoOut,
    PostamatDetailsOut,
    PostamatIn,
    PostamatPage,
    PostamatPatch,
    ScheduleOut,
    SchedulePut,
    postamat_details_out,
    postamat_out,
    schedule_out,
)
from app.modules.booking.models import Booking, BookingStatus
from app.modules.identity.models import AdminUser
from app.core.types import utc_isoformat

router = APIRouter(prefix="/admin/postamats", tags=["admin-postamats"])

# A cell counts as occupied while one of these bookings is sitting in it. Paid
# for and still empty is a different fact and the panel counts it apart.
OCCUPYING_STATUSES = (
    BookingStatus.AWAITING_PICKUP, BookingStatus.EXPIRED, BookingStatus.GRACE,
    BookingStatus.OVERDUE, BookingStatus.TO_REMOVE,
)


async def _load(session: AsyncSession, postamat_id: uuid.UUID) -> Postamat:
    postamat = await session.get(Postamat, postamat_id)
    if postamat is None:
        raise AppError(ErrorCode.NOT_FOUND, "Postamat not found.", 404)
    return postamat


async def _counts(
    session: AsyncSession, postamat_ids: list[uuid.UUID]
) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, int]]:
    """Total and occupied cells per postamat, in two queries for the whole page.

    The admin table prints «12 / 42» on every row; a count per row would turn one
    screen into eighty round trips.
    """
    if not postamat_ids:
        return {}, {}
    totals = {
        row_id: count
        for row_id, count in await session.execute(
            select(Cell.postamat_id, func.count())
            .where(Cell.postamat_id.in_(postamat_ids))
            .group_by(Cell.postamat_id)
        )
    }
    occupied = {
        row_id: count
        for row_id, count in await session.execute(
            select(Booking.postamat_id, func.count())
            .where(Booking.postamat_id.in_(postamat_ids),
                   Booking.status.in_(OCCUPYING_STATUSES))
            .group_by(Booking.postamat_id)
        )
    }
    return totals, occupied


async def _device_brief(
    session: AsyncSession, postamat_id: uuid.UUID
) -> DeviceBrief | None:
    device = await session.scalar(
        select(Device).where(Device.postamat_id == postamat_id)
    )
    if device is None:
        return None
    return DeviceBrief(
        id=device.id, postamat_id=device.postamat_id, status=str(device.status),
        ip_address=device.ip_address, mac_address=device.mac_address,
        last_seen_at=(utc_isoformat(device.last_seen_at)
                      if device.last_seen_at else None),
    )


async def _apply_device(
    session: AsyncSession, postamat: Postamat, inline: DeviceInline | None
) -> None:
    """Write the two fields the postamat form edits inline.

    A terminal that has never been provisioned still gets a row here, so the
    address an engineer typed survives until the device itself checks in.
    """
    if inline is None:
        return
    device = await session.scalar(
        select(Device).where(Device.postamat_id == postamat.id)
    )
    if device is None:
        device = Device(postamat_id=postamat.id)
        session.add(device)
    if inline.ip_address is not None:
        device.ip_address = inline.ip_address
    if inline.mac_address is not None:
        device.mac_address = inline.mac_address


async def _details(session: AsyncSession, postamat: Postamat) -> PostamatDetailsOut:
    totals, occupied = await _counts(session, [postamat.id])
    return postamat_details_out(
        postamat, occupied=occupied.get(postamat.id, 0),
        total=totals.get(postamat.id, 0),
        device=await _device_brief(session, postamat.id),
    )


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
    totals, occupied = await _counts(session, [row.id for row in rows])
    return PostamatPage(
        items=[
            postamat_out(row, occupied=occupied.get(row.id, 0),
                         total=totals.get(row.id, 0))
            for row in rows
        ],
        pagination=meta,
    )


@router.get("/{postamat_id}", response_model=PostamatDetailsOut)
async def get_postamat(
    postamat_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("postamats.read")),
) -> PostamatDetailsOut:
    return await _details(session, await _load(session, postamat_id))


def _write_fields(payload: PostamatIn | PostamatPatch) -> dict:
    """Turn the contract's write shape into columns.

    `location` arrives as one object and is stored as two floats; `device` is a
    separate resource and is handled apart.
    """
    changes = payload.model_dump(exclude_unset=True, exclude={"location", "device"})
    if payload.location is not None:
        changes["latitude"] = payload.location.lat
        changes["longitude"] = payload.location.lon
    return changes


@router.post("", response_model=PostamatDetailsOut, status_code=201)
async def create_postamat(
    payload: PostamatIn,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatDetailsOut:
    # Checked rather than left to the unique index, so the operator gets the
    # field name back instead of a 500 from an IntegrityError.
    taken = await session.scalar(
        select(Postamat.id).where(Postamat.number == payload.number)
    )
    if taken is not None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "Postamat number already exists.",
                       409, details={"field": "number"})
    postamat = Postamat(**_write_fields(payload))
    session.add(postamat)
    await session.flush()
    await _apply_device(session, postamat, payload.device)
    await record(session, event="postamat.created", source=Source.ADMIN,
                 message=f"Postamat {postamat.number} created", actor=admin.login,
                 postamat_id=postamat.id)
    await session.commit()
    # created_at comes from a server default, so it is unloaded until the row is
    # read back. Refreshing here rather than letting the serializer touch it
    # keeps the load out of async lazy-load territory.
    await session.refresh(postamat)
    return await _details(session, postamat)


@router.patch("/{postamat_id}", response_model=PostamatDetailsOut)
async def update_postamat(
    postamat_id: uuid.UUID,
    payload: PostamatPatch,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatDetailsOut:
    postamat = await _load(session, postamat_id)
    changes = _write_fields(payload)
    for field, value in changes.items():
        setattr(postamat, field, value)
    await _apply_device(session, postamat, payload.device)
    # `status` is deliberately absent: a status change has to carry a reason,
    # which is what block and unblock exist for.
    await record(session, event="postamat.updated", source=Source.ADMIN,
                 message=f"Postamat {postamat.number} updated", actor=admin.login,
                 postamat_id=postamat.id, details={"fields": sorted(changes)})
    await session.commit()
    return await _details(session, postamat)


@router.post("/{postamat_id}/block", response_model=PostamatDetailsOut)
async def block_postamat(
    postamat_id: uuid.UUID,
    payload: BlockRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatDetailsOut:
    postamat = await _load(session, postamat_id)
    postamat.status = PostamatStatus.BLOCKED
    # Kept on the row as well as in the journal: the panel prints it on the
    # blocked card, and reading the journal to draw a card is a query per row.
    postamat.blocked_reason = payload.reason
    await record(session, event="postamat.blocked", source=Source.ADMIN,
                 message=payload.reason, actor=admin.login, postamat_id=postamat.id)
    await session.commit()
    return await _details(session, postamat)


@router.post("/{postamat_id}/unblock", response_model=PostamatDetailsOut)
async def unblock_postamat(
    postamat_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatDetailsOut:
    postamat = await _load(session, postamat_id)
    postamat.status = PostamatStatus.ACTIVE
    # Cleared with the block: a stale reason on a working machine reads as a
    # warning that no longer applies.
    postamat.blocked_reason = None
    await record(session, event="postamat.unblocked", source=Source.ADMIN,
                 message=f"Postamat {postamat.number} returned to service",
                 actor=admin.login, postamat_id=postamat.id)
    await session.commit()
    return await _details(session, postamat)


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


@router.put("/{postamat_id}/schedule", response_model=ScheduleOut)
async def set_schedule(
    postamat_id: uuid.UUID,
    payload: SchedulePut,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> ScheduleOut:
    postamat = await _load(session, postamat_id)
    postamat.round_the_clock = payload.round_the_clock
    # A whole-week replacement rather than a per-day edit: the operator sees the
    # week as one form, and a partial update would leave a day nobody meant to
    # keep. delete-orphan on the relationship removes the rows that are gone.
    # A round-the-clock machine keeps no days at all, so the two can never
    # disagree about when it is open.
    postamat.schedule = [] if payload.round_the_clock else [
        PostamatSchedule(weekday=slot.weekday - 1, opens_at=slot.opens_at,
                         closes_at=slot.closes_at)
        for slot in payload.days
    ]
    await record(session, event="postamat.schedule_set", source=Source.ADMIN,
                 message=f"Schedule set for postamat {postamat.number}",
                 actor=admin.login, postamat_id=postamat.id,
                 details={"round_the_clock": payload.round_the_clock,
                          "days": sorted(slot.weekday for slot in payload.days)})
    await session.commit()
    return schedule_out(postamat)
