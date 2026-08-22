### Task 16: Tariff matrix

**Files:**
- Modify: `backend/app/modules/catalog/models.py`
- Create: `backend/app/api/admin/tariffs.py`, `backend/tests/catalog/test_tariffs.py`

**Interfaces:**
- Produces: `Tariff` model keyed on `(city_id, cell_type_id, duration_hours)`; `GET /api/v1/admin/tariffs`, `PUT /api/v1/admin/tariffs`.

`PUT` replaces the whole matrix in one transaction — half-applied price changes are worse than none. Validation: a city that prices a cell type must price all three durations, otherwise `TARIFF_INCOMPLETE`.

- [ ] **Step 1: Add the model**

```python
class Tariff(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "tariffs"
    __table_args__ = (
        UniqueConstraint("city_id", "cell_type_id", "duration_hours", name="uq_tariff"),
    )

    city_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cities.id"), index=True)
    cell_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cell_types.id"), index=True)
    duration_hours: Mapped[int] = mapped_column(Integer)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="TMT")
```

- [ ] **Step 2: Write the failing test**

```python
async def test_incomplete_matrix_is_rejected(client, admin_token, city, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.put("/api/v1/admin/tariffs", headers=headers, json={
        "entries": [
            {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
             "duration_hours": 12, "amount_minor": 1200},
        ]
    })
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "TARIFF_INCOMPLETE"
    assert body["details"]["missing"] == [24, 48]


async def test_complete_matrix_replaces_previous(client, admin_token, city, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"}
    entries = [
        {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
         "duration_hours": hours, "amount_minor": amount}
        for hours, amount in ((12, 1200), (24, 1800), (48, 2600))
    ]
    first = await client.put("/api/v1/admin/tariffs", headers=headers, json={"entries": entries})
    assert first.status_code == 200

    entries[0]["amount_minor"] = 1400
    second = await client.put("/api/v1/admin/tariffs", headers=headers, json={"entries": entries})
    assert second.status_code == 200

    listed = await client.get("/api/v1/admin/tariffs", headers=headers)
    prices = {item["duration_hours"]: item["amount_minor"] for item in listed.json()["items"]}
    assert prices == {12: 1400, 24: 1800, 48: 2600}
```

- [ ] **Step 3: Run it and confirm it fails**

- [ ] **Step 4: Write `backend/app/api/admin/tariffs.py`**

```python
import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.modules.catalog.models import Tariff
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/tariffs", tags=["admin-tariffs"])


class TariffEntry(BaseModel):
    city_id: uuid.UUID
    cell_type_id: uuid.UUID
    duration_hours: int
    amount_minor: int
    currency: str = "TMT"


class TariffMatrix(BaseModel):
    entries: list[TariffEntry]


class TariffList(BaseModel):
    items: list[TariffEntry]


@router.get("", response_model=TariffList)
async def list_tariffs(
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("tariffs.read")),
) -> TariffList:
    rows = await session.scalars(select(Tariff))
    return TariffList(items=[TariffEntry.model_validate(r, from_attributes=True) for r in rows])


@router.put("", response_model=TariffList)
async def replace_tariffs(
    payload: TariffMatrix,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("tariffs.write")),
) -> TariffList:
    required = set(get_settings().rental_durations)
    seen: dict[tuple[uuid.UUID, uuid.UUID], set[int]] = defaultdict(set)
    for entry in payload.entries:
        seen[(entry.city_id, entry.cell_type_id)].add(entry.duration_hours)

    for (city_id, cell_type_id), durations in seen.items():
        missing = sorted(required - durations)
        if missing:
            raise AppError(
                ErrorCode.TARIFF_INCOMPLETE,
                "Every priced cell type must price all durations.", 422,
                details={"city_id": str(city_id), "cell_type_id": str(cell_type_id),
                         "missing": missing},
            )

    await session.execute(delete(Tariff))
    session.add_all([Tariff(**entry.model_dump()) for entry in payload.entries])
    await session.commit()
    return TariffList(items=payload.entries)
```

- [ ] **Step 5: Run the tests, generate the migration, commit**

```bash
./.venv/Scripts/python.exe -m pytest tests/catalog/test_tariffs.py -v
./.venv/Scripts/python.exe -m alembic revision --autogenerate -m "tariffs"
git add <only the files this task created or modified, listed explicitly> && git commit -m "feat: tariff matrix replaced atomically with completeness check"
```

---

