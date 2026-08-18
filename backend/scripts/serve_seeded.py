"""Run the API against a throwaway database seeded with enough to answer.

Exists for the contract check: schemathesis talks HTTP to a real server, and a
server with an empty database answers 404 to nearly everything, which tells us
nothing about whether our payloads match `postbox-contract/openapi.yaml`.

    cd backend
    ./.venv/Scripts/python.exe scripts/serve_seeded.py

It prints a client bearer token and the ids it seeded, then serves on :8100
until interrupted. The database file is recreated on every run.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

from app.core.config import BACKEND_DIR  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")

DB_FILE = Path(tempfile.gettempdir()) / "postamat_conformance.db"
DB_FILE.unlink(missing_ok=True)
# Set before the engine is imported, so the throwaway file is what gets bound.
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_FILE.as_posix()}"

import uvicorn  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.main import app  # noqa: E402

PORT = 8100


async def seed() -> dict:
    import app.core.idempotency  # noqa: F401
    import app.modules.audit.models  # noqa: F401
    import app.modules.booking.models  # noqa: F401
    import app.modules.custody.models  # noqa: F401
    import app.modules.identity.models  # noqa: F401
    import app.modules.notify.models  # noqa: F401
    import app.modules.payments.models  # noqa: F401
    from app.modules.booking import service as booking_service
    from app.modules.catalog.models import Cell, CellType, City, Postamat, Tariff
    from app.modules.identity.models import Client

    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        city = City(code="ashgabat", name_tk="Aşgabat", name_ru="Ашхабад",
                    name_en="Ashgabat")
        cell_type = CellType(code="small", name_tk="Kiçi", name_ru="Маленький",
                             name_en="Small", width_cm=20, height_cm=20, depth_cm=40)
        client = Client(phone="+99362123456", last_name="Аннаев", first_name="Мырат")
        session.add_all([city, cell_type, client])
        await session.flush()

        postamat = Postamat(number="10042", name="ТП #4", city_id=city.id,
                            address="ул. Ататюрк, 31", round_the_clock=True)
        session.add(postamat)
        await session.flush()
        session.add_all([
            Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
                 board=1, output=n)
            for n in range(1, 11)
        ])
        session.add_all([
            Tariff(city_id=city.id, cell_type_id=cell_type.id, duration_hours=hours,
                   amount_minor=amount, currency="TMT")
            for hours, amount in ((12, 1200), (24, 1800), (48, 2600))
        ])
        await session.commit()

        booking, _ = await booking_service.create_booking(
            session, client_id=client.id, postamat_id=postamat.id,
            cell_type_id=cell_type.id, duration_hours=24, amount_minor=1800,
            currency="TMT", recipient_phone="+99365000001",
            recipient_name="Получатель", depositor="owner", courier_phone=None,
        )
        await session.commit()
        seeded = {
            "token": create_access_token("client", client.id),
            "client_id": str(client.id),
            "postamat_id": str(postamat.id),
            "cell_type_id": str(cell_type.id),
            "city_id": str(city.id),
            "booking_id": str(booking.id),
        }

    await engine.dispose()
    return seeded


if __name__ == "__main__":
    facts = asyncio.run(seed())
    print(json.dumps(facts, ensure_ascii=False))
    sys.stdout.flush()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
