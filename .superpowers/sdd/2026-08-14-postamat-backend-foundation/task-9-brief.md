### Task 9: Audit log

**Files:**
- Create: `backend/app/modules/audit/models.py`, `backend/app/modules/audit/service.py`, `backend/tests/audit/test_audit.py`

**Interfaces:**
- Produces: `AuditEntry` model, and `record(session, *, event, source, severity="info", message, actor=None, postamat_id=None, cell_id=None, details=None) -> AuditEntry`.

Sources match the mockups: `system`, `api`, or an admin login. Nothing ever updates or deletes an entry.

- [ ] **Step 1: Write `backend/app/modules/audit/models.py`**

```python
import uuid
from enum import StrEnum

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Source(StrEnum):
    SYSTEM = "system"
    API = "api"
    ADMIN = "admin"


class AuditEntry(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "audit_entries"

    event: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[Source] = mapped_column(String(16), index=True)
    severity: Mapped[Severity] = mapped_column(String(16), index=True, default=Severity.INFO)
    message: Mapped[str] = mapped_column(String(500))
    actor: Mapped[str | None] = mapped_column(String(64), index=True)
    postamat_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    cell_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    details: Mapped[dict | None] = mapped_column(JSON)
```

- [ ] **Step 2: Write `backend/app/modules/audit/service.py`**

```python
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditEntry, Severity, Source


async def record(
    session: AsyncSession,
    *,
    event: str,
    source: Source,
    message: str,
    severity: Severity = Severity.INFO,
    actor: str | None = None,
    postamat_id: uuid.UUID | None = None,
    cell_id: uuid.UUID | None = None,
    details: dict | None = None,
) -> AuditEntry:
    entry = AuditEntry(
        event=event,
        source=source,
        severity=severity,
        message=message,
        actor=actor,
        postamat_id=postamat_id,
        cell_id=cell_id,
        details=details,
    )
    session.add(entry)
    await session.flush()
    return entry
```

- [ ] **Step 3: Write the test**

```python
from sqlalchemy import select

from app.modules.audit.models import AuditEntry, Severity, Source
from app.modules.audit.service import record


async def test_record_persists_an_entry(session):
    await record(session, event="cell.remote_open", source=Source.ADMIN,
                 message="Manual open of cell 12", actor="admin_ivanov",
                 severity=Severity.WARNING, details={"reason": "stuck door"})
    await session.commit()

    entry = await session.scalar(select(AuditEntry))
    assert entry.event == "cell.remote_open"
    assert entry.actor == "admin_ivanov"
    assert entry.details == {"reason": "stuck door"}
```

- [ ] **Step 4: Run the test and confirm it passes**

```bash
docker compose run --rm api pytest tests/audit -v
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: append-only audit log"
```

---

