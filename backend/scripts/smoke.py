"""Walks the API end to end and prints what every call returned.

Runs the application in-process over an ASGI transport rather than against a
running server. That is deliberate: one-time codes and rate-limit counters live
in an in-process key-value store (see app/core/kvstore.py), so a script talking
to a separate uvicorn process could never read the code it needs to complete the
login flow. In-process, it can.

The database is a throwaway SQLite file, created and dropped on every run, so
smoking the API never touches dev.db.

    cd backend
    ./.venv/Scripts/python.exe -m scripts.smoke

Endpoints that later tasks have not built yet are reported as SKIP rather than
failing the run, so this script stays useful while the plan is still landing.
"""

import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The output is full of Turkmen and Russian; a console that defaults to cp1251
# otherwise kills the run halfway through with a UnicodeEncodeError.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

from app.core.config import BACKEND_DIR, get_settings  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")

_db_file = Path(tempfile.gettempdir()) / "postamat_smoke.db"
_db_file.unlink(missing_ok=True)
# Set before anything imports the engine, so the throwaway file is what gets
# bound rather than the development database named in .env.
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_file.as_posix()}"

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base, get_session  # noqa: E402
from app.main import create_app  # noqa: E402

GREEN, RED, YELLOW, GREY, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[0m"

_passed = 0
_failed = 0
_skipped = 0

# Paths the application actually registered, filled in once the app is built. A
# 404 on a path outside this set means the router belongs to a task that has not
# landed yet, which is a SKIP; a 404 on a path inside it is a real failure.
_known_paths: set[str] = set()


def _import_models() -> None:
    """Register every mapped class on Base.metadata before create_all.

    Importing the packages is not enough — the modules/*/__init__.py files are
    empty, so the model modules themselves have to be imported by name. Modules
    that a later task has not created yet are simply absent, which is why this
    tolerates ImportError instead of exploding.
    """
    for module in (
        "app.core.idempotency",
        "app.modules.audit.models",
        "app.modules.identity.models",
        "app.modules.catalog.models",
        "app.modules.booking.models",
        "app.modules.notify.models",
        "app.modules.payments.models",
        "app.modules.custody.models",
    ):
        try:
            __import__(module)
        except ImportError:
            pass


def _short(body: object, limit: int = 160) -> str:
    text = json.dumps(body, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def call(
    client,
    method: str,
    path: str,
    *,
    expect: int | None = 200,
    skip_if_absent: bool = True,
    **kwargs,
):
    """Issue one request, print the outcome, and return the parsed body.

    `expect=None` means any status is acceptable and only the response is shown.
    A 404 on a path the app never registered counts as SKIP, not as a failure:
    the plan is still being implemented and the missing routers are known. Pass
    `skip_if_absent=False` when the missing route is the point of the check, as
    it is for the probe that inspects the 404 error envelope.
    """
    global _passed, _failed, _skipped

    response = await client.request(method, path, **kwargs)
    try:
        body = response.json()
    except ValueError:
        body = response.text

    if (
        skip_if_absent
        and response.status_code == 404
        and path.split("?")[0] not in _known_paths
    ):
        _skipped += 1
        print(f"{GREY}SKIP {method:6} {path:44} not implemented yet{RESET}")
        return None

    if expect is None or response.status_code == expect:
        _passed += 1
        colour, mark = GREEN, "OK  "
    else:
        _failed += 1
        colour, mark = RED, "FAIL"

    suffix = "" if expect is None or response.status_code == expect else f" (expected {expect})"
    print(f"{colour}{mark} {method:6} {path:44} {response.status_code}{suffix}{RESET}")
    print(f"     {_short(body)}")
    return body


async def _seed_fleet(maker) -> dict:
    """Put one postamat with two small cells and a full tariff matrix in place.

    Written straight through the session rather than through the admin API,
    because the script has no admin account to log in as — seeding is not what
    this script is here to exercise.
    """
    from app.modules.catalog.models import Cell, CellType, City, Postamat, Tariff

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
            for n in (1, 2)
        ])
        session.add_all([
            Tariff(city_id=city.id, cell_type_id=cell_type.id,
                   duration_hours=hours, amount_minor=amount, currency="TMT")
            for hours, amount in ((12, 1200), (24, 1800), (48, 2600))
        ])
        await session.commit()
        return {"postamat_id": str(postamat.id), "cell_type_id": str(cell_type.id)}


async def main() -> int:
    _import_models()

    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = create_app()
    _known_paths.update(app.openapi()["paths"])
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = override

    try:
        fleet = await _seed_fleet(maker)
    except ImportError:
        fleet = {}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://smoke") as client:
        print(f"\n{YELLOW}-- health and schema --{RESET}")
        await call(client, "GET", "/api/v1/health")
        await call(client, "GET", "/openapi.json", expect=200)

        print(f"\n{YELLOW}-- error envelope --{RESET}")
        await call(
            client,
            "GET",
            "/api/v1/definitely-not-a-route",
            expect=404,
            skip_if_absent=False,
        )

        print(f"\n{YELLOW}-- client login by one-time code --{RESET}")
        phone = "+99362123456"
        requested = await call(
            client, "POST", "/api/v1/auth/otp/request", json={"phone": phone}
        )
        await call(
            client,
            "POST",
            "/api/v1/auth/otp/request",
            json={"phone": "+7999123456"},
            expect=422,
        )

        access = None
        if requested is not None:
            from app.modules.identity.service import peek_otp

            code = await peek_otp(requested["request_id"])
            print(f"{GREY}     one-time code read out of the key-value store: {code}{RESET}")
            await call(
                client,
                "POST",
                "/api/v1/auth/otp/verify",
                json={"request_id": requested["request_id"], "code": "000000"},
                expect=401,
            )
            tokens = await call(
                client,
                "POST",
                "/api/v1/auth/otp/verify",
                json={"request_id": requested["request_id"], "code": code},
            )
            if tokens:
                access = tokens.get("access_token")

        print(f"\n{YELLOW}-- public catalogue --{RESET}")
        await call(client, "GET", "/api/v1/cities")
        await call(client, "GET", "/api/v1/cities", headers={"Accept-Language": "ru"})
        await call(client, "GET", "/api/v1/cell-types")
        await call(client, "GET", "/api/v1/postamats")

        print(f"\n{YELLOW}-- admin --{RESET}")
        await call(
            client,
            "POST",
            "/api/v1/admin/auth/login",
            json={"login": "nobody", "password": "wrong"},
            expect=401,
        )

        print(f"\n{YELLOW}-- idempotency --{RESET}")
        # The same key with the same body must replay the first response; the same
        # key with a different body must be refused with 409.
        headers = {"Idempotency-Key": "smoke-key-1"}
        first = await call(
            client,
            "POST",
            "/api/v1/auth/otp/request",
            json={"phone": phone},
            headers=headers,
            expect=None,
        )
        if first is not None:
            await call(
                client,
                "POST",
                "/api/v1/auth/otp/request",
                json={"phone": phone},
                headers=headers,
                expect=None,
            )
            await call(
                client,
                "POST",
                "/api/v1/auth/otp/request",
                json={"phone": "+99362999999"},
                headers=headers,
                expect=409,
            )

        if access:
            print(f"\n{YELLOW}-- authenticated as a client --{RESET}")
            bearer = {"Authorization": f"Bearer {access}"}
            await call(client, "GET", "/api/v1/me", headers=bearer, expect=None)
            # Booking refuses an unnamed client, so the registration form the app
            # shows on `profile_complete: false` is part of the happy path here.
            await call(client, "PATCH", "/api/v1/me", headers=bearer,
                       json={"last_name": "Аннаев", "first_name": "Мырат"})

        if access and fleet:
            print(f"\n{YELLOW}-- booking a cell --{RESET}")
            bearer = {"Authorization": f"Bearer {access}"}
            postamat_id = fleet["postamat_id"]

            before = await call(
                client, "GET", f"/api/v1/postamats/{postamat_id}/availability"
            )
            if before:
                print(f"{GREY}     free before booking: "
                      f"{before['items'][0]['free']}{RESET}")

            created = await call(
                client, "POST", "/api/v1/bookings", expect=201,
                # Booking allocates a real cell, so the endpoint refuses a
                # request it could not safely replay after a dropped connection.
                headers=bearer | {"Idempotency-Key": "smoke-booking-1"},
                json={
                    "postamat_id": postamat_id,
                    "cell_type_id": fleet["cell_type_id"],
                    "duration_hours": 24,
                    "recipient_phone": "+99365000001",
                    "recipient_name": "Получатель",
                },
            )
            if created:
                # Lengths only. This output gets pasted into chats, and those
                # five digits open a physical door.
                lengths = {purpose: len(code)
                           for purpose, code in (created.get("codes") or {}).items()}
                if created.get("deposit_code"):
                    lengths["deposit"] = len(created["deposit_code"])
                print(f"{GREY}     cell {created['cell_number']}, code lengths "
                      f"{lengths}{RESET}")

                await call(client, "GET", "/api/v1/bookings", headers=bearer)
                await call(
                    client, "GET", f"/api/v1/postamats/{postamat_id}/availability"
                )
                await call(
                    client, "POST", f"/api/v1/bookings/{created['id']}/cancel",
                    headers=bearer | {"Idempotency-Key": "smoke-cancel-1"},
                    json={"reason": "smoke run"},
                )
                after = await call(
                    client, "GET", f"/api/v1/postamats/{postamat_id}/availability"
                )
                if after and before:
                    # Plan 2a in one line: cancelling gives the cell back.
                    freed = after["items"][0]["free"] == before["items"][0]["free"]
                    print(f"{GREY}     cell returned to the pool: {freed}{RESET}")

            print(f"\n{YELLOW}-- paying --{RESET}")
            paid_booking = await call(
                client, "POST", "/api/v1/bookings", expect=201,
                headers=bearer | {"Idempotency-Key": "smoke-booking-2"},
                json={
                    "postamat_id": postamat_id,
                    "cell_type_id": fleet["cell_type_id"],
                    "duration_hours": 24,
                    "recipient_phone": "+99365000001",
                },
            )
            if paid_booking:
                started = await call(
                    client, "POST",
                    f"/api/v1/bookings/{paid_booking['id']}/payments", expect=201,
                    headers=bearer | {"Idempotency-Key": "smoke-payment-1"},
                    json={"bank_code": "halk",
                          "return_url": "postamat://payment/result"},
                )
                if started:
                    # Signed the way the acquirer will sign it: the endpoint is
                    # unauthenticated and the signature is the whole of its trust.
                    body = json.dumps({
                        "payment_id": started["id"],
                        "provider_payment_id": f"mock-{started['id']}",
                        "status": "succeeded",
                        "amount_minor": started["amount"]["amount_minor"],
                    }).encode()
                    signature = hmac.new(
                        get_settings().payment_webhook_secret.encode(), body,
                        hashlib.sha256,
                    ).hexdigest()
                    await call(
                        client, "POST", "/api/v1/webhooks/payments/mock",
                        content=body,
                        headers={"X-Signature": signature,
                                 "Content-Type": "application/json"},
                    )
                    detail = await call(
                        client, "GET", f"/api/v1/bookings/{paid_booking['id']}",
                        headers=bearer,
                    )
                    if detail:
                        print(f"{GREY}     status after payment: "
                              f"{detail['status']}{RESET}")
                    await call(
                        client, "GET",
                        f"/api/v1/bookings/{paid_booking['id']}/timeline",
                        headers=bearer,
                    )
                    await call(client, "GET",
                               f"/api/v1/payments/{started['id']}", headers=bearer)

            print(f"\n{YELLOW}-- notifications --{RESET}")
            await call(client, "GET", "/api/v1/me/notifications", headers=bearer)
            await call(client, "POST", "/api/v1/me/notifications/read",
                       headers=bearer, json={})
            await call(client, "GET", "/api/v1/me/notification-settings",
                       headers=bearer)
            await call(client, "PATCH", "/api/v1/me/notification-settings",
                       headers=bearer, json={"push_marketing": True})

            print(f"\n{YELLOW}-- push tokens --{RESET}")
            registered = await call(
                client, "POST", "/api/v1/auth/push-tokens", headers=bearer,
                json={"token": "smoke-device-token", "platform": "android",
                      "app_version": "1.0.0+1"},
                expect=201,
            )
            if registered:
                await call(
                    client, "DELETE",
                    f"/api/v1/auth/push-tokens/{registered['push_token_id']}",
                    headers=bearer, expect=204,
                )

    await engine.dispose()
    _db_file.unlink(missing_ok=True)

    print(f"\n{GREEN}{_passed} ok{RESET}, {RED}{_failed} failed{RESET}, {GREY}{_skipped} not built yet{RESET}\n")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
