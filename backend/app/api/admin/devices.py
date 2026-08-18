import secrets
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session, utcnow
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import require_idempotency_key
from app.core.pagination import PageMeta, PageParams, page_params, paginate_page
from app.core.security import hash_token
from app.core.types import utc_isoformat
from app.modules.audit.models import Source
from app.modules.audit.service import record
from app.modules.catalog.models import Device
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/devices", tags=["admin-devices"])

# Long enough to survive being read out over the telephone to whoever is
# standing at the cabinet, short enough that a stale note is useless.
PROVISIONING_CODE_HOURS = 2
PROVISIONING_CODE_BYTES = 6


class DeviceOut(BaseModel):
    id: uuid.UUID
    postamat_id: uuid.UUID
    status: str
    ip_address: str | None
    mac_address: str | None
    os_version: str | None
    app_version: str | None
    last_seen_at: str | None
    certificate_expires_at: str | None
    on_battery: bool | None
    tamper_detected: bool | None
    jammed_cell_ids: list[uuid.UUID]


class DevicePage(BaseModel):
    items: list[DeviceOut]
    pagination: PageMeta


class ProvisioningCode(BaseModel):
    # Returned once. The digest is what the server keeps, so a code lost before
    # the technician arrives is reissued rather than looked up.
    provisioning_code: str
    expires_at: str


def _out(device: Device) -> DeviceOut:
    return DeviceOut(
        id=device.id, postamat_id=device.postamat_id, status=str(device.status),
        ip_address=device.ip_address, mac_address=device.mac_address,
        os_version=device.os_version,
        # `agent_version` in storage: the contract calls it the app's version,
        # and it is the same number.
        app_version=device.agent_version,
        last_seen_at=(
            utc_isoformat(device.last_seen_at) if device.last_seen_at else None
        ),
        certificate_expires_at=(
            utc_isoformat(device.certificate_expires_at)
            if device.certificate_expires_at else None
        ),
        on_battery=device.on_battery, tamper_detected=device.tamper_detected,
        jammed_cell_ids=[uuid.UUID(str(one))
                         for one in (device.jammed_cell_ids or [])],
    )


async def _device_or_404(session: AsyncSession, device_id: uuid.UUID) -> Device:
    device = await session.get(Device, device_id)
    if device is None:
        raise AppError(ErrorCode.NOT_FOUND, "No such device.", 404)
    return device


@router.get("", response_model=DevicePage)
async def list_devices(
    status: str | None = Query(default=None,
                               pattern="^(online|offline|degraded)$"),
    postamat_id: uuid.UUID | None = Query(default=None),
    params: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("devices.read")),
) -> DevicePage:
    stmt = select(Device).order_by(Device.last_seen_at.desc())
    if status:
        stmt = stmt.where(Device.status == status)
    if postamat_id:
        stmt = stmt.where(Device.postamat_id == postamat_id)
    rows, meta = await paginate_page(session, stmt, params)
    return DevicePage(items=[_out(row) for row in rows], pagination=meta)


@router.get("/{device_id}", response_model=DeviceOut)
async def get_device(
    device_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("devices.read")),
) -> DeviceOut:
    return _out(await _device_or_404(session, device_id))


@router.post("/{device_id}/provisioning-code", response_model=ProvisioningCode,
             dependencies=[Depends(require_idempotency_key)])
async def issue_provisioning_code(
    device_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: AdminUser = Depends(require_permission("devices.write")),
) -> ProvisioningCode:
    device = await _device_or_404(session, device_id)
    code = secrets.token_hex(PROVISIONING_CODE_BYTES).upper()
    # Issuing a new one kills the old, for the same reason code rotation does:
    # a credential that was read to the wrong person must stop working.
    device.provisioning_code_hash = hash_token(code)
    expires_at = utcnow() + timedelta(hours=PROVISIONING_CODE_HOURS)
    device.provisioning_expires_at = expires_at

    # The code itself never reaches the journal; that one was issued does.
    await record(
        session, event="device.provisioning_code_issued", source=Source.ADMIN,
        actor=actor.login, message="Выпущен код привязки терминала.",
        postamat_id=device.postamat_id, details={"device_id": str(device.id)},
    )
    await session.commit()
    return ProvisioningCode(provisioning_code=code,
                            expires_at=utc_isoformat(expires_at))
