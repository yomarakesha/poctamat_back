import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.db import utcnow
from app.modules.audit.models import AuditEntry, Severity, Source

PATH = "/api/v1/admin/audit-log"


@pytest.fixture
async def auditor(session):
    from app.core.security import create_access_token, hash_secret
    from app.modules.identity.models import AdminUser, Role

    role = Role(code="auditor", name="Аудитор", permissions=["audit.read"])
    session.add(role)
    await session.flush()
    admin = AdminUser(login="auditor_test", full_name="Аудитов А.А.",
                      password_hash=hash_secret("secret123"), role_id=role.id)
    session.add(admin)
    await session.commit()
    await session.refresh(admin)
    return admin


@pytest.fixture
def auditor_headers(auditor):
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token('admin', auditor.id)}"}


def _stamp(moment):
    # `+00:00` in a query string is a space unless it is escaped, and the
    # panel sends `Z` anyway.
    return moment.isoformat().replace("+00:00", "Z")


async def _entry(session, **extra):
    row = AuditEntry(
        event=extra.pop("event", "cell.opened"),
        source=extra.pop("source", Source.ADMIN),
        severity=extra.pop("severity", Severity.INFO),
        message=extra.pop("message", "Ячейка открыта."),
        **extra,
    )
    session.add(row)
    await session.commit()
    return row


async def test_the_log_reads_newest_first_with_its_labels(
    client, session, auditor_headers, postamat
):
    await _entry(session, event="cell.blocked", message="Ячейка заблокирована.",
                 actor="admin_test", postamat_id=postamat.id,
                 details={"reason": "Замок заедает"}, trace_id="trace-1")

    response = await client.get(PATH, headers=auditor_headers)
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["event_type"] == "cell.blocked"
    assert item["actor_label"] == "admin_test"
    assert item["postamat_number"] == postamat.number
    assert item["reason"] == "Замок заедает"
    assert item["trace_id"] == "trace-1"
    assert item["occurred_at"].endswith("Z")


async def test_the_log_filters_by_event_severity_and_source(
    client, session, auditor_headers
):
    await _entry(session, event="cell.opened", severity=Severity.INFO)
    await _entry(session, event="pin.failed", severity=Severity.WARNING,
                 source=Source.API, message="Неверный PIN.")

    by_event = await client.get(f"{PATH}?event_type=pin.failed",
                                headers=auditor_headers)
    assert [one["event_type"] for one in by_event.json()["items"]] == ["pin.failed"]

    by_severity = await client.get(f"{PATH}?severity=warning", headers=auditor_headers)
    assert [one["severity"] for one in by_severity.json()["items"]] == ["warning"]

    by_source = await client.get(f"{PATH}?source=api,system", headers=auditor_headers)
    assert [one["source"] for one in by_source.json()["items"]] == ["api"]


async def test_an_actor_filter_is_by_person_not_by_login_string(
    client, session, auditor, auditor_headers, admin_user
):
    await _entry(session, actor=admin_user.login)
    await _entry(session, actor=auditor.login, event="audit.read")

    response = await client.get(f"{PATH}?actor_id={admin_user.id}",
                                headers=auditor_headers)
    assert [one["actor_label"] for one in response.json()["items"]] == [
        admin_user.login
    ]


async def test_an_unknown_actor_matches_nothing_rather_than_everything(
    client, session, auditor_headers
):
    await _entry(session, actor="admin_test")

    response = await client.get(f"{PATH}?actor_id={uuid.uuid4()}",
                                headers=auditor_headers)
    assert response.json()["items"] == []


async def test_the_free_text_search_reads_message_event_and_actor(
    client, session, auditor_headers
):
    await _entry(session, message="Дверь заклинило.", event="lock_agent.error")
    await _entry(session, message="Ячейка открыта.")

    response = await client.get(f"{PATH}?query=заклинило", headers=auditor_headers)
    assert [one["message"] for one in response.json()["items"]] == [
        "Дверь заклинило."
    ]


async def test_the_log_pages_by_cursor(client, session, auditor_headers):
    for index in range(3):
        await _entry(session, message=f"Событие {index}")

    first = await client.get(f"{PATH}?limit=2", headers=auditor_headers)
    body = first.json()
    assert len(body["items"]) == 2
    assert body["pagination"]["has_more"] is True

    second = await client.get(
        f"{PATH}?limit=2&cursor={body['pagination']['next_cursor']}",
        headers=auditor_headers,
    )
    seen = {one["id"] for one in body["items"]} | {
        one["id"] for one in second.json()["items"]
    }
    # Three rows across two pages, with nothing repeated: the point of a keyset
    # cursor over a table that is being written to while it is read.
    assert len(seen) == 3


async def test_a_range_older_than_the_hot_window_says_where_it_was_cut(
    client, session, auditor_headers
):
    await _entry(session)
    since = _stamp(utcnow() - timedelta(days=400))

    response = await client.get(f"{PATH}?from={since}", headers=auditor_headers)
    body = response.json()
    assert body["truncated_at"] is not None
    assert body["truncated_at"].endswith("Z")


async def test_a_range_inside_the_hot_window_is_not_truncated(
    client, session, auditor_headers
):
    since = _stamp(utcnow() - timedelta(days=7))

    response = await client.get(f"{PATH}?from={since}", headers=auditor_headers)
    assert response.json()["truncated_at"] is None


async def test_reading_the_log_is_itself_audited_but_only_once_an_hour(
    client, session, auditor, auditor_headers
):
    await client.get(PATH, headers=auditor_headers)
    await client.get(PATH, headers=auditor_headers)
    await client.get(PATH, headers=auditor_headers)

    rows = list(await session.scalars(
        select(AuditEntry).where(AuditEntry.event == "audit.read")
    ))
    # A screen that refreshes itself must not bury the entries worth reading.
    assert len(rows) == 1
    assert rows[0].actor == auditor.login


async def test_the_export_streams_csv_with_a_bom(client, session, auditor_headers):
    await _entry(session, message="Ячейка открыта.", actor="admin_test")

    response = await client.get(f"{PATH}/export", headers=auditor_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="audit-log-' in \
        response.headers["content-disposition"]
    body = response.text
    # Excel reads a BOM-less UTF-8 CSV as cp1251 and turns every Russian message
    # into mojibake.
    assert body.startswith("﻿")
    assert "occurred_at,source,actor,event_type" in body
    assert "Ячейка открыта." in body


async def test_the_export_obeys_the_same_filters(client, session, auditor_headers):
    await _entry(session, event="pin.failed", message="Неверный PIN.")
    await _entry(session, event="cell.opened", message="Ячейка открыта.")

    response = await client.get(f"{PATH}/export?event_type=pin.failed",
                                headers=auditor_headers)
    assert "Неверный PIN." in response.text
    assert "Ячейка открыта." not in response.text


async def test_an_export_over_the_cap_is_refused(
    client, session, auditor_headers, monkeypatch
):
    from app.api.admin import audit

    await _entry(session)
    monkeypatch.setattr(audit, "EXPORT_MAX_ROWS", 0)

    response = await client.get(f"{PATH}/export", headers=auditor_headers)
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "EXPORT_TOO_LARGE"
    # The caller is told what to do about it, not only that it failed.
    assert body["details"]["matched"] == 1


async def test_every_export_is_audited(client, session, auditor_headers):
    await _entry(session)
    await client.get(f"{PATH}/export", headers=auditor_headers)
    await client.get(f"{PATH}/export", headers=auditor_headers)

    rows = list(await session.scalars(
        select(AuditEntry).where(AuditEntry.event == "audit.exported")
    ))
    # A copy of the journal leaving the building is recorded every time.
    assert len(rows) == 2


async def test_the_log_needs_the_permission(client, admin_token):
    response = await client.get(PATH,
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 403
