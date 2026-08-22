### Task 15: Cells

**Files:**
- Modify: `backend/app/modules/catalog/models.py`
- Create: `backend/app/api/admin/cells.py`, `backend/tests/catalog/test_cells.py`

**Interfaces:**
- Produces: `Cell` model with independent `number`, `row`, `col`, `board`, `output`; `GET POST PATCH /api/v1/admin/cells`, `POST /api/v1/admin/cells/{cell_id}/block`, `/unblock`.

`number` is the label on the door and is unique within its postamat. `board` and `output` are the lock-board address. On the pilot cabinet their values happen to line up; nothing in the code may rely on that.

- [ ] **Step 1: Add the model**

```python
class Cell(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "cells"
    __table_args__ = (UniqueConstraint("postamat_id", "number", name="uq_cell_number"),)

    postamat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("postamats.id"), index=True)
    cell_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cell_types.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    row: Mapped[int | None] = mapped_column(Integer)
    col: Mapped[int | None] = mapped_column(Integer)
    board: Mapped[int] = mapped_column(Integer)
    output: Mapped[int] = mapped_column(Integer)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    is_maintenance: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(500))
```

Import `UniqueConstraint` at the top of the module.

- [ ] **Step 2: Write the failing test**

```python
async def test_duplicate_cell_number_within_postamat_is_rejected(client, admin_token, postamat, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"}
    body = {"postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
            "cells": [{"number": 1, "board": 1, "output": 1}]}

    first = await client.post("/api/v1/admin/cells", headers=headers, json=body)
    assert first.status_code == 201

    duplicate = await client.post("/api/v1/admin/cells", headers=headers, json=body)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "CELL_NUMBER_TAKEN"


async def test_bulk_create_accepts_a_full_cabinet(client, admin_token, postamat, cell_type):
    headers = {"Authorization": f"Bearer {admin_token}"}
    cells = [{"number": n, "board": 1 if n <= 21 else 2,
              "output": n if n <= 21 else n - 21} for n in range(1, 44)]
    response = await client.post("/api/v1/admin/cells", headers=headers, json={
        "postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id), "cells": cells,
    })
    assert response.status_code == 201
    assert len(response.json()["items"]) == 43
```

- [ ] **Step 3: Run it and confirm it fails**

- [ ] **Step 4: Write `backend/app/api/admin/cells.py`**

```python
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_permission
from app.core.errors import AppError, ErrorCode
from app.modules.catalog.models import Cell
from app.modules.identity.models import AdminUser

router = APIRouter(prefix="/admin/cells", tags=["admin-cells"])


class CellSpec(BaseModel):
    number: int
    board: int
    output: int
    row: int | None = None
    col: int | None = None


class BulkCreate(BaseModel):
    postamat_id: uuid.UUID
    cell_type_id: uuid.UUID
    cells: list[CellSpec]


class CellOut(BaseModel):
    id: uuid.UUID
    number: int
    board: int
    output: int
    is_blocked: bool
    is_maintenance: bool


class CellList(BaseModel):
    items: list[CellOut]


@router.post("", response_model=CellList, status_code=201)
async def create_cells(
    payload: BulkCreate,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("cells.write")),
) -> CellList:
    rows = [
        Cell(postamat_id=payload.postamat_id, cell_type_id=payload.cell_type_id,
             **spec.model_dump())
        for spec in payload.cells
    ]
    session.add_all(rows)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError(ErrorCode.CELL_NUMBER_TAKEN,
                       "A cell with this number already exists in the postamat.", 409) from None

    return CellList(items=[CellOut.model_validate(r, from_attributes=True) for r in rows])


@router.get("", response_model=CellList)
async def list_cells(
    postamat_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AdminUser = Depends(require_permission("cells.read")),
) -> CellList:
    rows = await session.scalars(
        select(Cell).where(Cell.postamat_id == postamat_id).order_by(Cell.number)
    )
    return CellList(items=[CellOut.model_validate(r, from_attributes=True) for r in rows])
```

- [ ] **Step 5: Run the tests, generate the migration, commit**

```bash
./.venv/Scripts/python.exe -m pytest tests/catalog/test_cells.py -v
./.venv/Scripts/python.exe -m alembic revision --autogenerate -m "cells"
git add <only the files this task created or modified, listed explicitly> && git commit -m "feat: cells with independent door and board addressing"
```

---

