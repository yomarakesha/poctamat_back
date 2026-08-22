### Task 13: Cities and cell types

**Files:**
- Create: `backend/app/modules/catalog/models.py`, `backend/app/modules/catalog/schemas.py`, `backend/app/api/public/catalog.py`, `backend/tests/catalog/test_reference.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces: `City`, `CellType` models; `GET /api/v1/cities`, `GET /api/v1/cell-types` — both public.

Names are stored per language rather than translated at runtime: the three languages are product data, not UI strings.

- [ ] **Step 1: Write `backend/app/modules/catalog/models.py`**

```python
from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class City(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "cities"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name_tk: Mapped[str] = mapped_column(String(100))
    name_ru: Mapped[str] = mapped_column(String(100))
    name_en: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class CellType(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "cell_types"

    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name_tk: Mapped[str] = mapped_column(String(50))
    name_ru: Mapped[str] = mapped_column(String(50))
    name_en: Mapped[str] = mapped_column(String(50))
    width_cm: Mapped[int] = mapped_column(Integer)
    height_cm: Mapped[int] = mapped_column(Integer)
    depth_cm: Mapped[int] = mapped_column(Integer)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
```

- [ ] **Step 2: Write `backend/app/modules/catalog/schemas.py`**

```python
import uuid

from pydantic import BaseModel


class CityOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str


class CellTypeOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    width_cm: int
    height_cm: int
    depth_cm: int


def localized(row, language: str) -> str:
    return getattr(row, f"name_{language}", row.name_tk)
```

- [ ] **Step 3: Write `backend/app/api/public/catalog.py`**

```python
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_language
from app.core.db import get_session
from app.modules.catalog.models import CellType, City
from app.modules.catalog.schemas import CellTypeOut, CityOut, localized

router = APIRouter(tags=["catalog"])


@router.get("/cities", response_model=list[CityOut])
async def list_cities(
    request: Request, session: AsyncSession = Depends(get_session)
) -> list[CityOut]:
    language = get_language(request)
    rows = await session.scalars(
        select(City).where(City.is_active.is_(True)).order_by(City.code)
    )
    return [
        CityOut(id=row.id, code=row.code, name=localized(row, language)) for row in rows
    ]


@router.get("/cell-types", response_model=list[CellTypeOut])
async def list_cell_types(
    request: Request, session: AsyncSession = Depends(get_session)
) -> list[CellTypeOut]:
    language = get_language(request)
    rows = await session.scalars(
        select(CellType).where(CellType.is_blocked.is_(False)).order_by(CellType.width_cm)
    )
    return [
        CellTypeOut(
            id=row.id, code=row.code, name=localized(row, language),
            width_cm=row.width_cm, height_cm=row.height_cm, depth_cm=row.depth_cm,
        )
        for row in rows
    ]
```

- [ ] **Step 4: Write the test**

```python
from app.modules.catalog.models import CellType, City


async def test_cities_are_localized(client, session):
    session.add(City(code="ashgabat", name_tk="Aşgabat", name_ru="Ашхабад", name_en="Ashgabat"))
    await session.commit()

    default = await client.get("/api/v1/cities")
    assert default.json()[0]["name"] == "Aşgabat"

    russian = await client.get("/api/v1/cities", headers={"Accept-Language": "ru"})
    assert russian.json()[0]["name"] == "Ашхабад"


async def test_blocked_cell_types_are_hidden(client, session):
    session.add(CellType(code="small", name_tk="Kiçi", name_ru="Маленький", name_en="Small",
                         width_cm=20, height_cm=20, depth_cm=40))
    session.add(CellType(code="huge", name_tk="X", name_ru="X", name_en="X",
                         width_cm=90, height_cm=90, depth_cm=90, is_blocked=True))
    await session.commit()

    response = await client.get("/api/v1/cell-types")
    assert [item["code"] for item in response.json()] == ["small"]
```

- [ ] **Step 5: Run the tests, generate the migration, commit**

```bash
./.venv/Scripts/python.exe -m pytest tests/catalog -v
./.venv/Scripts/python.exe -m alembic revision --autogenerate -m "cities and cell types"
git add <only the files this task created or modified, listed explicitly> && git commit -m "feat: public city and cell type reference data"
```

---

