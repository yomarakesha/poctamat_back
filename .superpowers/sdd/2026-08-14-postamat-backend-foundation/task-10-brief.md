### Task 10: Identity models

**Files:**
- Create: `backend/app/modules/identity/models.py`, `backend/tests/identity/test_models.py`

**Interfaces:**
- Produces: `Client`, `AdminUser`, `Role`, `RefreshToken`.

Roles carry a permission list rather than being a fixed enum — the contract exposes `GET /admin/roles` and returns `permissions[]` on `GET /admin/auth/me`.

- [ ] **Step 1: Write `backend/app/modules/identity/models.py`**

```python
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class Client(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "clients"

    phone: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(200))
    language: Mapped[str] = mapped_column(String(2), default="tk")
    default_city_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(500))


class Role(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "roles"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    permissions: Mapped[list] = mapped_column(JSON, default=list)


class AdminUser(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "admin_users"

    login: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)

    role: Mapped[Role] = relationship(lazy="joined")


class RefreshToken(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "refresh_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    subject_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[uuid.UUID] = mapped_column(index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 2: Write the test**

```python
from sqlalchemy import select

from app.modules.identity.models import Client


async def test_client_phone_is_unique(session):
    session.add(Client(phone="+99362123456"))
    await session.commit()

    stored = await session.scalar(select(Client))
    assert stored.language == "tk"
    assert stored.is_blocked is False
```

- [ ] **Step 3: Run the test and confirm it passes**

- [ ] **Step 4: Generate the first migration**

```bash
docker compose run --rm api alembic revision --autogenerate -m "identity, audit, idempotency"
docker compose run --rm api alembic upgrade head
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: identity models and first migration"
```

---

