import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.admin import realtime
from app.core.db import utcnow
from app.modules.audit.models import AuditEntry, Severity, Source

PATH = "/api/v1/admin/realtime/stream"


class FakeRequest:
    """A request that hangs up after a given number of polls.

    The generator's exit is driven by `is_disconnected`, and a test that waited
    for a real socket to close would be a test of httpx.
    """

    def __init__(self, alive_for: int = 1):
        self.alive_for = alive_for
        self.polls = 0

    async def is_disconnected(self) -> bool:
        self.polls += 1
        return self.polls > self.alive_for


@pytest.fixture
def maker(test_engine):
    return async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def fast_stream(monkeypatch):
    # The loop sleeps between polls; a suite that waited two seconds a test
    # would be paying for a constant nobody is testing.
    monkeypatch.setattr(realtime, "POLL_SECONDS", 0.0)


async def _entry(session, **extra):
    row = AuditEntry(
        event=extra.pop("event", "cell.opened"),
        source=extra.pop("source", Source.ADMIN),
        severity=extra.pop("severity", Severity.INFO),
        message=extra.pop("message", "Ячейка открыта."),
        created_at=extra.pop("created_at", utcnow() + timedelta(seconds=1)),
        **extra,
    )
    session.add(row)
    await session.commit()
    return row


def _frames(chunks: list[str]) -> list[dict]:
    return [
        json.loads(line[len("data: "):])
        for chunk in chunks
        for line in chunk.splitlines()
        if line.startswith("data: ")
    ]


async def test_a_new_entry_arrives_as_the_contract_envelope(
    session, maker, postamat
):
    request = FakeRequest(alive_for=1)
    await _entry(session, event="cell.blocked", message="Ячейка заблокирована.",
                 postamat_id=postamat.id, details={"reason": "Замок заедает"})

    chunks = [chunk async for chunk in realtime.events(request, maker)]
    event = _frames(chunks)[0]
    assert set(event) == {"id", "type", "occurred_at", "payload"}
    assert event["type"] == "cell.status_changed"
    assert event["occurred_at"].endswith("Z")
    # The payload is the same shape the log screen already draws.
    assert event["payload"]["event_type"] == "cell.blocked"
    assert event["payload"]["postamat_number"] == postamat.number


async def test_the_frame_carries_an_sse_id_for_resuming(session, maker):
    request = FakeRequest(alive_for=1)
    row = await _entry(session)

    chunks = [chunk async for chunk in realtime.events(request, maker)]
    assert f"id: {row.id}\n" in chunks[0]
    assert "event: cell.door_opened\n" in chunks[0]


async def test_a_stream_starts_from_now_not_from_the_whole_journal(
    session, maker
):
    await _entry(session, created_at=utcnow() - timedelta(hours=1),
                 message="Старое событие.")
    request = FakeRequest(alive_for=1)

    chunks = [chunk async for chunk in realtime.events(request, maker)]
    assert _frames(chunks) == []


async def test_a_resume_replays_what_was_missed(session, maker):
    first = await _entry(session, message="Первое.")
    await _entry(session, message="Второе.",
                 created_at=first.created_at + timedelta(seconds=1))
    request = FakeRequest(alive_for=1)

    chunks = [
        chunk async for chunk
        in realtime.events(request, maker, last_event_id=str(first.id))
    ]
    messages = [one["payload"]["message"] for one in _frames(chunks)]
    # Everything after the id the client last saw, and not that entry again.
    assert messages == ["Второе."]


async def test_an_unknown_resume_token_starts_from_now(session, maker):
    await _entry(session, message="Первое.",
                 created_at=utcnow() - timedelta(hours=1))
    request = FakeRequest(alive_for=1)

    chunks = [
        chunk async for chunk
        in realtime.events(request, maker, last_event_id=str(uuid.uuid4()))
    ]
    # A stale token must not replay the entire journal.
    assert _frames(chunks) == []


async def test_a_malformed_resume_token_is_not_an_error(session, maker):
    request = FakeRequest(alive_for=1)
    chunks = [
        chunk async for chunk
        in realtime.events(request, maker, last_event_id="not-a-uuid")
    ]
    assert _frames(chunks) == []


async def test_one_postamat_can_be_subscribed_to_alone(
    session, maker, postamat, city
):
    from app.modules.catalog.models import Postamat

    other = Postamat(number="10002", name="ТП #2", city_id=city.id, address="ул. 2")
    session.add(other)
    await session.flush()
    await _entry(session, postamat_id=postamat.id, message="Наш.")
    await _entry(session, postamat_id=other.id, message="Чужой.")
    request = FakeRequest(alive_for=1)

    chunks = [
        chunk async for chunk
        in realtime.events(request, maker, postamat_id=postamat.id)
    ]
    assert [one["payload"]["message"] for one in _frames(chunks)] == ["Наш."]


async def test_a_quiet_stream_sends_a_heartbeat(session, maker, monkeypatch):
    monkeypatch.setattr(realtime, "HEARTBEAT_SECONDS", 0.0)
    request = FakeRequest(alive_for=1)

    chunks = [chunk async for chunk in realtime.events(request, maker)]
    # A comment frame: legal SSE that no client dispatches as an event, and
    # enough traffic that a proxy does not kill an idle monitor.
    assert chunks[0].startswith(": ping ")


async def test_the_stream_stops_when_the_client_hangs_up(session, maker):
    request = FakeRequest(alive_for=3)

    chunks = [chunk async for chunk in realtime.events(request, maker)]
    assert chunks == []
    # It stopped because the socket was gone, not because it ran out of rows.
    assert request.polls == 4


def test_unmapped_events_still_reach_the_monitor():
    assert realtime.realtime_type("cell.opened") == "cell.door_opened"
    assert realtime.realtime_type("booking.cancelled") == "booking.status_changed"
    assert realtime.realtime_type("parcel.collected") == "booking.status_changed"
    assert realtime.realtime_type("device.tamper") == "device.alert"
    # An event nobody has mapped yet is still worth showing, so it arrives as a
    # journal entry rather than being dropped.
    assert realtime.realtime_type("something.new") == "audit.entry_created"


async def test_the_endpoint_streams_event_stream(
    client, session, admin_token, monkeypatch
):
    from starlette.requests import Request

    polls = {"n": 0}

    async def hangs_up_after_one(self) -> bool:
        polls["n"] += 1
        return polls["n"] > 1

    # The real socket never closes under an in-process ASGI transport, so the
    # hang-up is simulated. What is being tested here is the wiring — media type,
    # permission, and that a live entry reaches the wire — not the loop, which
    # has its own tests above.
    monkeypatch.setattr(Request, "is_disconnected", hangs_up_after_one)
    await _entry(session, message="Ячейка открыта.")

    response = await client.get(PATH,
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "Ячейка открыта." in response.text


async def test_the_stream_needs_a_permission(client, session):
    from app.core.security import create_access_token
    from app.modules.identity.models import AdminUser, Role

    role = Role(code="nobody_stream", name="Nobody", permissions=[])
    session.add(role)
    await session.flush()
    admin = AdminUser(login="nobody_stream", full_name="Никто Н.Н.",
                      password_hash="x", role_id=role.id)
    session.add(admin)
    await session.commit()
    await session.refresh(admin)

    response = await client.get(
        PATH,
        headers={"Authorization": f"Bearer {create_access_token('admin', admin.id)}"},
    )
    assert response.status_code == 403
