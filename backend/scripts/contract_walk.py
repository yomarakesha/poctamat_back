"""Walk the happy path and validate every success payload against the contract.

Schemathesis fuzzes with generated ids and therefore spends most of its budget
on 404s. This walks the flow a real app walks — sign in, fill the profile, book,
pay, deposit, rotate, transfer — and checks each 2xx body against the response
schema in `postbox-contract/openapi.yaml`.

    cd backend
    ./.venv/Scripts/python.exe scripts/contract_walk.py

Runs in-process against a throwaway database, for the same reason the smoke
script does: one-time codes live in an in-process store.
"""

import asyncio
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

from app.core.config import BACKEND_DIR  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")

DB_FILE = Path(tempfile.gettempdir()) / "postamat_walk.db"
DB_FILE.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_FILE.as_posix()}"

import yaml  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402
from referencing import Registry, Resource  # noqa: E402
from referencing.jsonschema import DRAFT202012  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base, get_session  # noqa: E402
from app.main import create_app  # noqa: E402

GREEN, RED, YELLOW, GREY, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[0m"
)

CONTRACT = Path(__file__).resolve().parents[2] / "postbox-contract" / "openapi.yaml"
SPEC = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
# The whole contract is registered as one document and every response schema is
# validated through a $ref into it, so `#/components/schemas/...` resolves the
# way it does inside the file instead of against a lifted-out fragment.
CONTRACT_URI = "urn:postbox-contract"
REGISTRY = Registry().with_resource(CONTRACT_URI, DRAFT202012.create_resource(SPEC))


# Deliberate departures from the contract, with the decision behind each. The
# walk reports them on every run instead of failing on them: a departure that
# stops being visible is one that quietly becomes the way things are, and the
# admin handover has to keep naming them.
DEVIATIONS = {
    "/admin/custody": {
        "photo_url": "Ruling Q2 — the only photograph this product takes is of "
                     "the postamat, so a removal act carries a written "
                     "description and no image.",
    },
    "/admin/custody/{custody_id}": {
        "photo_url": "Ruling Q2 — see POST /admin/custody.",
    },
    "/admin/custody/{custody_id}/handover": {
        "photo_url": "Ruling Q2 — see POST /admin/custody.",
    },
    "/admin/custody/{custody_id}/dispose": {
        "photo_url": "Ruling Q2 — see POST /admin/custody.",
    },
}

_deviations_seen: dict[str, str] = {}


def _waived(template: str, error) -> str | None:
    """The reason this error is a known departure, or None if it is a failure."""
    if error.validator != "required":
        return None
    for name, reason in DEVIATIONS.get(template, {}).items():
        if f"'{name}' is a required property" in error.message:
            _deviations_seen[f"{template}:{name}"] = reason
            return reason
    return None


def pointer(*parts: str) -> str:
    escaped = "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)
    return f"{CONTRACT_URI}#/{escaped}"

_checked = 0
_failed = 0

ADMIN_PASSWORD = "walker-secret-1"
# Everything the admin routers ask for. The walk is about payload shapes, and a
# missing permission would read as a contract failure when it is an ACL gap.
PERMISSIONS = (
    "audit.read", "bookings.read", "bookings.write", "cells.read",
    "cells.remote_open", "cells.write", "clients.read", "clients.write",
    "custody.read", "custody.write", "devices.read", "devices.write",
    "postamats.read", "postamats.write", "roles.read", "tariffs.read",
    "tariffs.write", "users.read", "users.write",
)


def _template(path: str) -> str:
    """Find the contract path whose shape matches this concrete URL."""
    for candidate in SPEC["paths"]:
        pattern = "^" + re.sub(r"\{[^}]+\}", "[^/]+", candidate) + "$"
        if re.match(pattern, path):
            return candidate
    raise KeyError(path)


def check(method: str, path: str, status: int, body: object) -> None:
    """Validate one response against the schema the contract gives it."""
    global _checked, _failed

    template = _template(path)
    operation = SPEC["paths"][template][method.lower()]
    responses = operation["responses"]
    documented_schema = (
        responses.get(str(status), {}).get("content", {})
        .get("application/json", {}).get("schema")
    )
    schema = {"$ref": pointer("paths", template, method.lower(), "responses",
                              str(status), "content", "application/json", "schema")}
    if documented_schema is None:
        if str(status) in responses:
            # Documented, but with no body to check — a 204, for instance.
            print(f"{GREY}OK   {method:6} {path:46} {status} (no body){RESET}")
            return
        documented = ", ".join(sorted(responses))
        print(f"{YELLOW}NOTE {method:6} {path:46} {status} not documented "
              f"(documented: {documented}){RESET}")
        return

    _checked += 1
    found = sorted(
        Draft202012Validator(schema, registry=REGISTRY).iter_errors(body),
        key=lambda error: list(error.path),
    )
    errors = [error for error in found if _waived(template, error) is None]
    if not errors and found:
        print(f"{YELLOW}DEV  {method:6} {path:46} {status} "
              f"(known departure){RESET}")
        return
    if errors:
        _failed += 1
        print(f"{RED}FAIL {method:6} {path:46} {status}{RESET}")
        for error in errors[:4]:
            where = "/".join(str(part) for part in error.path) or "(root)"
            print(f"{RED}     {where}: {error.message}{RESET}")
    else:
        print(f"{GREEN}OK   {method:6} {path:46} {status}{RESET}")


async def seed(maker) -> dict:
    from app.core.security import hash_secret
    from app.modules.catalog.models import Cell, CellType, City, Postamat, Tariff
    from app.modules.identity.models import AdminUser, Role

    async with maker() as session:
        city = City(code="ashgabat", name_tk="Aşgabat", name_ru="Ашхабад",
                    name_en="Ashgabat")
        cell_type = CellType(code="small", name_tk="Kiçi", name_ru="Маленький",
                             name_en="Small", width_mm=200, height_mm=200, depth_mm=400)
        session.add_all([city, cell_type])
        await session.flush()

        postamat = Postamat(number="10042", name="ТП #4", city_id=city.id,
                            address="ул. Ататюрк, 31", round_the_clock=True)
        session.add(postamat)
        await session.flush()
        session.add_all([
            Cell(postamat_id=postamat.id, cell_type_id=cell_type.id, number=n,
                 board=1, output=n)
            for n in range(1, 6)
        ])
        session.add_all([
            Tariff(city_id=city.id, cell_type_id=cell_type.id, duration_hours=hours,
                   amount_minor=amount, currency="TMT")
            for hours, amount in ((12, 1200), (24, 1800), (48, 2600))
        ])
        role = Role(code="root", name="Root", permissions=list(PERMISSIONS))
        session.add(role)
        await session.flush()
        session.add(AdminUser(login="walker", full_name="Ходоков Х.Х.",
                              password_hash=hash_secret(ADMIN_PASSWORD),
                              role_id=role.id))
        await session.commit()
        return {"postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id),
                "city_id": str(city.id)}


async def parcel_to_remove(maker, fleet: dict) -> str:
    """Put a parcel in the removal queue, the way the overdue worker does.

    The custody walk needs a booking a removal act may be filed against, and
    getting there through the API would mean waiting out a rental.
    """
    from app.core.db import utcnow
    from app.modules.booking.models import Booking, BookingStatus
    from app.modules.catalog.models import Cell

    async with maker() as session:
        cell = await session.scalar(
            select(Cell).where(Cell.postamat_id == uuid.UUID(fleet["postamat_id"]))
            .order_by(Cell.number.desc())
        )
        booking = Booking(
            client_id=uuid.uuid4(), postamat_id=uuid.UUID(fleet["postamat_id"]),
            cell_id=cell.id, cell_type_id=uuid.UUID(fleet["cell_type_id"]),
            duration_hours=24, amount_minor=1800, status=BookingStatus.TO_REMOVE,
            recipient_phone="+99365000007",
            expires_at=utcnow() - timedelta(hours=30),
            remove_after=utcnow() - timedelta(hours=1),
        )
        session.add(booking)
        await session.commit()
        return str(booking.id)


async def custody_is_due(maker) -> None:
    """Move the disposal date into the past, the way thirty days would."""
    from app.core.db import utcnow
    from app.modules.custody.models import CustodyRecord

    async with maker() as session:
        record = await session.scalar(select(CustodyRecord))
        record.disposal_due_at = utcnow() - timedelta(minutes=1)
        await session.commit()


async def deposit(maker, booking_id: str) -> None:
    """Put the parcel in, the way the kiosk will once it exists."""
    from app.modules.booking import service
    from app.modules.booking.models import Booking, BookingStatus
    from app.modules.catalog.models import Postamat

    async with maker() as session:
        booking = await session.get(Booking, uuid.UUID(booking_id))
        booking.status = BookingStatus.AWAITING_DEPOSIT
        await session.flush()
        postamat = await session.get(Postamat, booking.postamat_id)
        await service.mark_deposited(session, booking, postamat)
        await session.commit()


async def main() -> int:
    for module in ("app.core.idempotency", "app.modules.audit.models",
                   "app.modules.identity.models", "app.modules.catalog.models",
                   "app.modules.booking.models", "app.modules.notify.models",
                   "app.modules.payments.models", "app.modules.custody.models"):
        __import__(module)

    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    app = create_app()

    async def override():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = override
    fleet = await seed(maker)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://walk") as http:
        from app.modules.identity.service import peek_otp

        async def call(method, path, expect=200, **kwargs):
            response = await http.request(method, path, **kwargs)
            body = response.json() if response.content else None
            check(method, path.split("?")[0].removeprefix("/api/v1"),
                  response.status_code, body)
            assert response.status_code == expect, (path, response.status_code,
                                                    response.text)
            return body

        print(f"\n{YELLOW}-- sign in --{RESET}")
        requested = await call("POST", "/api/v1/auth/otp/request",
                               json={"phone": "+99362123456"})
        tokens = await call("POST", "/api/v1/auth/otp/verify", json={
            "request_id": requested["request_id"],
            "code": await peek_otp(requested["request_id"]),
        })
        bearer = {"Authorization": f"Bearer {tokens['access_token']}"}

        def key():
            return {"Idempotency-Key": str(uuid.uuid4())}

        print(f"\n{YELLOW}-- profile --{RESET}")
        await call("GET", "/api/v1/me", headers=bearer)
        await call("PATCH", "/api/v1/me", headers=bearer,
                   json={"last_name": "Аннаев", "first_name": "Мырат",
                         "city_id": fleet["city_id"]})
        await call("GET", "/api/v1/me/notification-settings", headers=bearer)
        await call("PATCH", "/api/v1/me/notification-settings", headers=bearer,
                   json={"push_marketing": True})
        await call("GET", "/api/v1/me/notifications", headers=bearer)
        await call("POST", "/api/v1/me/notifications/read", headers=bearer, json={})

        print(f"\n{YELLOW}-- push tokens --{RESET}")
        push = await call("POST", "/api/v1/auth/push-tokens", expect=201,
                          headers=bearer | key(),
                          json={"token": "walk-device", "platform": "android"})
        await call("DELETE", f"/api/v1/auth/push-tokens/{push['push_token_id']}",
                   expect=204, headers=bearer)

        print(f"\n{YELLOW}-- booking --{RESET}")
        booking = await call("POST", "/api/v1/bookings", expect=201,
                             headers=bearer | key(),
                             json={"postamat_id": fleet["postamat_id"],
                                   "cell_type_id": fleet["cell_type_id"],
                                   "duration_hours": 24,
                                   "recipient_phone": "+99365000001",
                                   "recipient_name": "Получатель"})
        await call("GET", "/api/v1/bookings?scope=active", headers=bearer)
        await call("GET", f"/api/v1/bookings/{booking['id']}", headers=bearer)
        await call("GET", f"/api/v1/bookings/{booking['id']}/timeline", headers=bearer)
        await call("POST", f"/api/v1/bookings/{booking['id']}/extend-hold",
                   headers=bearer | key())
        await call("POST", f"/api/v1/bookings/{booking['id']}/deposit-code/rotate",
                   headers=bearer | key())

        print(f"\n{YELLOW}-- payment --{RESET}")
        payment = await call("POST", f"/api/v1/bookings/{booking['id']}/payments",
                             expect=201, headers=bearer | key(),
                             json={"bank_code": "halk",
                                   "return_url": "postamat://payment/result"})
        await call("GET", f"/api/v1/payments/{payment['id']}", headers=bearer)
        await call("POST", f"/api/v1/payments/{payment['id']}/cancel",
                   headers=bearer | key())

        print(f"\n{YELLOW}-- the parcel goes in --{RESET}")
        await deposit(maker, booking["id"])
        await call("GET", f"/api/v1/bookings/{booking['id']}", headers=bearer)
        await call("POST", f"/api/v1/bookings/{booking['id']}/pickup-code/rotate",
                   headers=bearer | key())
        await call("POST", f"/api/v1/bookings/{booking['id']}/pickup-code/transfer",
                   headers=bearer | key(),
                   json={"new_phone": "+99365000009", "new_name": "Новый"})
        await call("GET", "/api/v1/bookings?scope=history", headers=bearer)

        print(f"\n{YELLOW}-- the panel signs in --{RESET}")
        staff = await call("POST", "/api/v1/admin/auth/login",
                           json={"login": "walker", "password": ADMIN_PASSWORD})
        panel = {"Authorization": f"Bearer {staff['access_token']}"}
        await call("GET", "/api/v1/admin/auth/me", headers=panel)

        print(f"\n{YELLOW}-- postamats --{RESET}")
        await call("GET", "/api/v1/admin/postamats", headers=panel)
        made = await call("POST", "/api/v1/admin/postamats", expect=201,
                          headers=panel | key(),
                          json={"number": "10043", "name": "ТП #5",
                                "city_id": fleet["city_id"],
                                "address": "ул. Гарашсызлык, 12",
                                "location": {"lat": 37.95, "lon": 58.38},
                                "grid_rows": 3, "grid_cols": 3,
                                "round_the_clock": True})
        await call("GET", f"/api/v1/admin/postamats/{made['id']}", headers=panel)
        await call("PATCH", f"/api/v1/admin/postamats/{made['id']}", headers=panel,
                   json={"name": "ТП #5, вход со двора"})
        await call("PUT", f"/api/v1/admin/postamats/{made['id']}/schedule",
                   headers=panel, json={"round_the_clock": False, "days": [
                       {"weekday": day, "opens_at": "08:00", "closes_at": "20:00"}
                       for day in range(1, 8)
                   ]})
        await call("POST", f"/api/v1/admin/postamats/{made['id']}/block",
                   headers=panel | key(), json={"reason": "Плановые работы"})
        await call("POST", f"/api/v1/admin/postamats/{made['id']}/unblock",
                   headers=panel | key())

        print(f"\n{YELLOW}-- cell types and prices --{RESET}")
        await call("GET", "/api/v1/admin/cell-types", headers=panel)
        kind = await call("POST", "/api/v1/admin/cell-types", expect=201,
                          headers=panel | key(),
                          json={"code": "medium", "name": "Средний",
                                "width_mm": 300, "height_mm": 300, "depth_mm": 500})
        await call("PATCH", f"/api/v1/admin/cell-types/{kind['id']}", headers=panel,
                   json={"name": "Средний+"})
        await call("GET", "/api/v1/admin/tariffs", headers=panel)
        await call("PUT", "/api/v1/admin/tariffs", headers=panel | key(), json={
            "items": [
                {"city_id": fleet["city_id"], "cell_type_id": cell_type_id,
                 "duration_hours": hours,
                 "price": {"amount_minor": amount, "currency": "TMT"}}
                for cell_type_id in (fleet["cell_type_id"], kind["id"])
                for hours, amount in ((12, 1200), (24, 1800), (48, 2600))
            ]
        })
        await call("GET", f"/api/v1/admin/tariffs?city_id={fleet['city_id']}",
                   headers=panel)

        print(f"\n{YELLOW}-- cells --{RESET}")
        await call("GET", f"/api/v1/admin/cells?postamat_id={made['id']}",
                   headers=panel)
        await call("POST", "/api/v1/admin/cells/bulk", expect=201,
                   headers=panel | key(),
                   json={"postamat_id": made["id"], "grid_rows": 2, "grid_cols": 2,
                         "layout": [{"cell_type_id": fleet["cell_type_id"],
                                     "count": 4}],
                         "hardware_start": {"board": 2, "output": 1}})
        cell = await call("POST", "/api/v1/admin/cells", expect=201,
                          headers=panel | key(),
                          json={"postamat_id": made["id"], "number": "99",
                                "cell_type_id": kind["id"], "row": 3, "col": 1,
                                "hardware_address": {"board": 3, "output": 1}})
        await call("GET", f"/api/v1/admin/cells/{cell['id']}", headers=panel)
        await call("PATCH", f"/api/v1/admin/cells/{cell['id']}", headers=panel,
                   json={"row": 3, "col": 2})
        await call("POST", f"/api/v1/admin/cells/{cell['id']}/maintenance",
                   headers=panel | key(),
                   json={"enabled": True, "reason": "Замок заедает"})
        await call("POST", f"/api/v1/admin/cells/{cell['id']}/maintenance",
                   headers=panel | key(), json={"enabled": False})
        await call("POST", f"/api/v1/admin/cells/{cell['id']}/block",
                   headers=panel | key(), json={"reason": "Дверь не закрывается"})
        await call("POST", f"/api/v1/admin/cells/{cell['id']}/unblock",
                   headers=panel | key(), json={"parcel_fate": "cell_was_empty"})
        # Declared but not yet real: the lock agent it would command arrives with
        # the kiosk, and until then the endpoint refuses honestly.
        await call("POST", f"/api/v1/admin/cells/{cell['id']}/remote-open",
                   expect=503, headers=panel | key(),
                   json={"reason": "Клиент забыл посылку, звонит оператору"})

        print(f"\n{YELLOW}-- custody --{RESET}")
        removable = await parcel_to_remove(maker, fleet)
        act = await call("POST", "/api/v1/admin/custody", expect=201,
                         headers=panel | key(),
                         json={"booking_id": removable,
                               "reason": "Срок вышел, ячейка нужна",
                               "description": "Коробка 30×20, скотч"})
        await call("GET", "/api/v1/admin/custody?status=at_counter", headers=panel)
        await call("GET", f"/api/v1/admin/custody/{act['id']}", headers=panel)
        await custody_is_due(maker)
        await call("POST", f"/api/v1/admin/custody/{act['id']}/dispose",
                   headers=panel | key(),
                   json={"outcome": "returned_to_sender",
                         "reason": "Отправитель забрал на пункте"})

        print(f"\n{YELLOW}-- sign out --{RESET}")
        await call("POST", "/api/v1/auth/logout", expect=204, headers=bearer,
                   json={"refresh_token": tokens["refresh_token"]})
        await call("POST", "/api/v1/admin/auth/logout", expect=204, headers=panel,
                   json={"refresh_token": staff["refresh_token"]})

    await engine.dispose()
    DB_FILE.unlink(missing_ok=True)
    print(f"\n{GREEN}{_checked - _failed} payloads match the contract{RESET}, "
          f"{RED}{_failed} do not{RESET}\n")
    for where, reason in sorted(_deviations_seen.items()):
        print(f"{YELLOW}departure {where}{RESET}: {reason}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
