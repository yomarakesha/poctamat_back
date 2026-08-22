### Task 14: Postamats and opening hours

**Files:**
- Modify: `backend/app/modules/catalog/models.py`, `backend/app/modules/catalog/schemas.py`
- Create: `backend/app/modules/catalog/service.py`, `backend/app/api/admin/postamats.py`, `backend/app/api/public/postamats.py`, `backend/tests/catalog/test_postamats.py`

**Interfaces:**
- Produces: `Postamat`, `PostamatSchedule`, `Device` models; `is_open_at(postamat, moment) -> bool`; `next_opening_after(postamat, moment) -> datetime`.
- Endpoints: `GET /api/v1/postamats`, `GET /api/v1/postamats/{postamat_id}` public; `GET POST PATCH /api/v1/admin/postamats`, `POST /api/v1/admin/postamats/{id}/block`, `/unblock`, `PUT /api/v1/admin/postamats/{id}/schedule`.

`Postamat.status` is the operator's decision (`active`, `maintenance`, `blocked`). Hardware presence lives on `Device` (`online`, `offline`, `degraded`) with `last_seen_at`. Plan 3 fills the device side; the table exists now so the monitor screen has something to read.

- [ ] **Step 1: Add the models**

```python
import uuid
from datetime import datetime, time
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship


class PostamatStatus(StrEnum):
    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    BLOCKED = "blocked"


class DeviceStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"


class Postamat(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "postamats"

    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    city_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cities.id"), index=True)
    address: Mapped[str] = mapped_column(String(500))
    latitude: Mapped[float | None]
    longitude: Mapped[float | None]
    status: Mapped[PostamatStatus] = mapped_column(String(16), default=PostamatStatus.ACTIVE)
    round_the_clock: Mapped[bool] = mapped_column(Boolean, default=False)

    schedule: Mapped[list["PostamatSchedule"]] = relationship(
        back_populates="postamat", lazy="selectin", cascade="all, delete-orphan"
    )


class PostamatSchedule(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "postamat_schedules"

    postamat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("postamats.id"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = Monday
    opens_at: Mapped[time] = mapped_column(Time)
    closes_at: Mapped[time] = mapped_column(Time)

    postamat: Mapped[Postamat] = relationship(back_populates="schedule")


class Device(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "devices"

    postamat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("postamats.id"), unique=True)
    status: Mapped[DeviceStatus] = mapped_column(String(16), default=DeviceStatus.OFFLINE)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_version: Mapped[str | None] = mapped_column(String(32))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    mac_address: Mapped[str | None] = mapped_column(String(17))
```

- [ ] **Step 2: Write the failing test for opening hours**

```python
from datetime import datetime, time, timezone

from app.modules.catalog.models import Postamat, PostamatSchedule
from app.modules.catalog.service import is_open_at, next_opening_after


def build_postamat() -> Postamat:
    postamat = Postamat(number="10042", name="ТП #4", address="ул. Ататюрк",
                        city_id=None, round_the_clock=False)
    postamat.schedule = [
        PostamatSchedule(weekday=day, opens_at=time(8, 0), closes_at=time(20, 0))
        for day in range(7)
    ]
    return postamat


def test_closed_outside_working_hours():
    postamat = build_postamat()
    assert is_open_at(postamat, datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)) is True
    assert is_open_at(postamat, datetime(2026, 8, 14, 21, 0, tzinfo=timezone.utc)) is False


def test_round_the_clock_is_always_open():
    postamat = build_postamat()
    postamat.round_the_clock = True
    assert is_open_at(postamat, datetime(2026, 8, 14, 3, 0, tzinfo=timezone.utc)) is True


def test_next_opening_skips_to_the_morning():
    postamat = build_postamat()
    moment = datetime(2026, 8, 14, 21, 0, tzinfo=timezone.utc)
    assert next_opening_after(postamat, moment) == datetime(
        2026, 8, 15, 8, 0, tzinfo=timezone.utc
    )
```

- [ ] **Step 3: Run it and confirm it fails**

- [ ] **Step 4: Write `backend/app/modules/catalog/service.py`**

```python
from datetime import datetime, timedelta

from app.modules.catalog.models import Postamat


def _slot_for(postamat: Postamat, weekday: int):
    for slot in postamat.schedule:
        if slot.weekday == weekday:
            return slot
    return None


def is_open_at(postamat: Postamat, moment: datetime) -> bool:
    if postamat.round_the_clock:
        return True
    slot = _slot_for(postamat, moment.weekday())
    if slot is None:
        return False
    return slot.opens_at <= moment.time() < slot.closes_at


def next_opening_after(postamat: Postamat, moment: datetime) -> datetime:
    if postamat.round_the_clock:
        return moment
    for offset in range(8):
        day = moment + timedelta(days=offset)
        slot = _slot_for(postamat, day.weekday())
        if slot is None:
            continue
        candidate = day.replace(
            hour=slot.opens_at.hour, minute=slot.opens_at.minute, second=0, microsecond=0
        )
        if candidate > moment:
            return candidate
    return moment
```

- [ ] **Step 5: Run the test and confirm it passes**

- [ ] **Step 6: Write the admin router `backend/app/api/admin/postamats.py`**

```python
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.core.pagination import PageMeta, PageParams, page_params, paginate_page
from app.modules.audit.models import Source
from app.modules.audit.service import record
from app.modules.catalog.models import Postamat, PostamatStatus
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/postamats", tags=["admin-postamats"])


class PostamatIn(BaseModel):
    number: str
    name: str
    city_id: uuid.UUID
    address: str
    latitude: float | None = None
    longitude: float | None = None
    round_the_clock: bool = False


class PostamatOut(BaseModel):
    id: uuid.UUID
    number: str
    name: str
    address: str
    status: PostamatStatus
    round_the_clock: bool


class PostamatPage(BaseModel):
    items: list[PostamatOut]
    pagination: PageMeta


class BlockRequest(BaseModel):
    reason: str


@router.get("", response_model=PostamatPage)
async def list_postamats(
    params: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("postamats.read")),
) -> PostamatPage:
    rows, meta = await paginate_page(session, select(Postamat).order_by(Postamat.number), params)
    return PostamatPage(items=[PostamatOut.model_validate(r, from_attributes=True) for r in rows],
                        pagination=meta)


@router.post("", response_model=PostamatOut, status_code=201)
async def create_postamat(
    payload: PostamatIn,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatOut:
    postamat = Postamat(**payload.model_dump())
    session.add(postamat)
    await session.flush()
    await record(session, event="postamat.created", source=Source.ADMIN,
                 message=f"Postamat {postamat.number} created", actor=admin.login,
                 postamat_id=postamat.id)
    await session.commit()
    return PostamatOut.model_validate(postamat, from_attributes=True)


@router.post("/{postamat_id}/block", response_model=PostamatOut)
async def block_postamat(
    postamat_id: uuid.UUID,
    payload: BlockRequest,
    session: AsyncSession = Depends(get_session),
    admin: AdminUser = Depends(require_permission("postamats.write")),
) -> PostamatOut:
    postamat = await session.get(Postamat, postamat_id)
    if postamat is None:
        raise AppError(ErrorCode.NOT_FOUND, "Postamat not found.", 404)
    postamat.status = PostamatStatus.BLOCKED
    await record(session, event="postamat.blocked", source=Source.ADMIN,
                 message=payload.reason, actor=admin.login, postamat_id=postamat.id)
    await session.commit()
    return PostamatOut.model_validate(postamat, from_attributes=True)
```

- [ ] **Step 7: Write the API test**

```python
async def test_create_and_block_postamat(client, session, admin_token, city):
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = await client.post("/api/v1/admin/postamats", headers=headers, json={
        "number": "10042", "name": "ТП #4", "city_id": str(city.id),
        "address": "ул. Ататюрк, 31", "round_the_clock": False,
    })
    assert created.status_code == 201
    postamat_id = created.json()["id"]

    blocked = await client.post(f"/api/v1/admin/postamats/{postamat_id}/block",
                                headers=headers, json={"reason": "vandalised"})
    assert blocked.json()["status"] == "blocked"
```

Add `admin_token` and `city` fixtures to `backend/tests/conftest.py`, seeding a role with
`["postamats.read", "postamats.write", "cells.write", "tariffs.write", "roles.read"]`.

- [ ] **Step 8: Run the tests, generate the migration, commit**

```bash
./.venv/Scripts/python.exe -m pytest tests/catalog -v
./.venv/Scripts/python.exe -m alembic revision --autogenerate -m "postamats, schedules, devices"
git add <only the files this task created or modified, listed explicitly> && git commit -m "feat: postamats with opening hours and operator status"
```

---

