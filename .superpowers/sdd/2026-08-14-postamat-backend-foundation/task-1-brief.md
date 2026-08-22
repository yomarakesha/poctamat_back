### Task 1: Repository skeleton and a running container

**Files:**
- Create: `.gitignore`, `backend/requirements.txt`, `backend/pytest.ini`, `backend/.env.example`, `backend/app/__init__.py`, `backend/app/main.py`, `backend/tests/__init__.py`, `backend/tests/core/__init__.py`, `backend/tests/core/test_health.py`
- Delete: `backend/Dockerfile`, `backend/docker-compose.yml` (left behind by an aborted first attempt)

**Interfaces:**
- Produces: `app.main.create_app() -> FastAPI`, and a `GET /api/v1/health` route returning `{"status": "ok"}`.

- [ ] **Step 1: Initialise the repository**

```bash
cd /c/Users/yomarakesha/Desktop/projects/poctamat
git init
git add -A
git commit -m "chore: snapshot existing reverse-engineering material"
```

- [ ] **Step 2: Write `.gitignore` at the repository root**

```gitignore
__pycache__/
*.py[cod]
.venv/
.env
.pytest_cache/
.ruff_cache/
*.egg-info/
```

- [ ] **Step 3: Remove the containers left by the aborted first attempt**

```bash
cd backend
rm -f Dockerfile docker-compose.yml
```

- [ ] **Step 4: Write `backend/requirements.txt`**

Version floors, not exact pins. The floors are the versions that actually carry Python 3.14
wheels — `asyncpg` below 0.31 and `pydantic` below 2.13 have none, and pip would fall back to
building from source against a compiler this machine does not have.

```
fastapi>=0.121
uvicorn>=0.38
sqlalchemy[asyncio]>=2.0.52
asyncpg>=0.31.0
aiosqlite>=0.20.0
alembic>=1.19.1
pydantic>=2.13.4
pydantic-settings>=2.7.0
pyjwt>=2.13.0
argon2-cffi>=23.1.0
python-multipart>=0.0.20
pytest>=8.3.4
pytest-asyncio>=0.25.0
httpx>=0.28.1
```

Do not add `uvicorn[standard]` — it pulls `uvloop`, which does not build on Windows.

- [ ] **Step 5: Create the virtualenv and install**

```bash
cd backend
py -m venv .venv
./.venv/Scripts/python.exe -m pip install --upgrade pip
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

Every later command in every task runs through `./.venv/Scripts/python.exe`, never through a
bare `python` or `pytest`.

- [ ] **Step 6: Write `backend/.env.example` and `backend/.env`**

Postgres credentials are not configured on this machine yet. Until they are, the development
database is a SQLite file — Alembic needs a reachable database to autogenerate against, and this
keeps every task runnable today.

```
DATABASE_URL=sqlite+aiosqlite:///./dev.db
TEST_DATABASE_URL=sqlite+aiosqlite:///:memory:
JWT_SECRET=dev-secret-change-me
PIN_PEPPER=dev-pepper-change-me
```

`.env.example` carries the same keys plus the Postgres form as a comment, so the switch is one
line when the service is ready:

```
# Production / once postgresql-x64-18 is configured:
# DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/postamat
```

`.env` and `dev.db` are git-ignored; `.env.example` is committed.

- [ ] **Step 7: Write `backend/pytest.ini`**

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

- [ ] **Step 8: Write the failing test in `backend/tests/core/test_health.py`**

```python
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_returns_ok(client):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 9: Run the test and confirm it fails**

```bash
cd backend
./.venv/Scripts/python.exe -m pytest tests/core/test_health.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.main'`.

- [ ] **Step 10: Write `backend/app/main.py`**

```python
from fastapi import APIRouter, FastAPI

API_PREFIX = "/api/v1"

health_router = APIRouter()


@health_router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def create_app() -> FastAPI:
    app = FastAPI(title="Postamat API", version="1.0.0")
    app.include_router(health_router, prefix=API_PREFIX)
    return app


app = create_app()
```

Also create empty `backend/app/__init__.py`, `backend/tests/__init__.py` and `backend/tests/core/__init__.py`.

- [ ] **Step 11: Run the test and confirm it passes**

```bash
./.venv/Scripts/python.exe -m pytest tests/core/test_health.py -v
```

Expected: PASS.

- [ ] **Step 12: Add `backend/.venv/` and `backend/.env` to the root `.gitignore`, then commit**

```bash
git add -A
git commit -m "feat: backend skeleton with health endpoint"
```

Verify with `git status --short` that no `.venv` content was staged.

---

