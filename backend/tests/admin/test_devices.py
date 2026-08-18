import uuid

import pytest
from sqlalchemy import select

from app.core.db import utcnow
from app.modules.catalog.models import Device, DeviceStatus

PATH = "/api/v1/admin/devices"


@pytest.fixture
async def fleet_token(session):
    from app.core.security import create_access_token, hash_secret
    from app.modules.identity.models import AdminUser, Role

    role = Role(code="engineer", name="Инженер",
                permissions=["devices.read", "devices.write"])
    session.add(role)
    await session.flush()
    admin = AdminUser(login="engineer_test", full_name="Инженеров И.И.",
                      password_hash=hash_secret("secret123"), role_id=role.id)
    session.add(admin)
    await session.commit()
    await session.refresh(admin)
    return create_access_token("admin", admin.id)


@pytest.fixture
async def device(session, postamat):
    row = Device(postamat_id=postamat.id, status=DeviceStatus.ONLINE,
                 ip_address="10.0.0.7", mac_address="b8:27:eb:00:00:01",
                 agent_version="1.2.0", os_version="Raspbian 12",
                 last_seen_at=utcnow())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def _key():
    return {"Idempotency-Key": str(uuid.uuid4())}


async def test_the_fleet_lists_what_the_screen_shows(client, fleet_token, device):
    response = await client.get(PATH,
                                headers={"Authorization": f"Bearer {fleet_token}"})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["status"] == "online"
    assert item["ip_address"] == "10.0.0.7"
    # `agent_version` in storage, `app_version` on the wire: the same number.
    assert item["app_version"] == "1.2.0"
    assert item["last_seen_at"].endswith("Z")
    assert item["jammed_cell_ids"] == []


async def test_the_fleet_can_be_filtered_by_status(
    client, session, fleet_token, device, city
):
    from app.modules.catalog.models import Postamat

    other = Postamat(number="10002", name="ТП #2", city_id=city.id, address="ул. 2")
    session.add(other)
    await session.flush()
    session.add(Device(postamat_id=other.id, status=DeviceStatus.OFFLINE))
    await session.commit()

    headers = {"Authorization": f"Bearer {fleet_token}"}
    offline = await client.get(f"{PATH}?status=offline", headers=headers)
    assert [item["status"] for item in offline.json()["items"]] == ["offline"]


async def test_one_device_is_readable(client, fleet_token, device):
    response = await client.get(f"{PATH}/{device.id}",
                                headers={"Authorization": f"Bearer {fleet_token}"})
    assert response.status_code == 200
    assert response.json()["id"] == str(device.id)


async def test_an_unknown_device_is_404(client, fleet_token):
    response = await client.get(f"{PATH}/{uuid.uuid4()}",
                                headers={"Authorization": f"Bearer {fleet_token}"})
    assert response.status_code == 404


async def test_a_provisioning_code_is_returned_once_and_stored_hashed(
    client, session, fleet_token, device
):
    from app.core.security import hash_token
    from app.modules.audit.models import AuditEntry

    response = await client.post(f"{PATH}/{device.id}/provisioning-code",
                                 headers={"Authorization": f"Bearer {fleet_token}"}
                                 | _key())
    assert response.status_code == 200
    code = response.json()["provisioning_code"]
    assert response.json()["expires_at"].endswith("Z")

    await session.refresh(device)
    # A provisioning code is a credential, so only its digest is kept.
    assert device.provisioning_code_hash == hash_token(code)
    assert code not in str(device.provisioning_code_hash)

    entry = await session.scalar(
        select(AuditEntry).where(
            AuditEntry.event == "device.provisioning_code_issued")
    )
    assert entry.actor == "engineer_test"
    # The code itself never reaches the journal.
    assert code not in str(entry.details)


async def test_issuing_again_kills_the_previous_code(
    client, session, fleet_token, device
):
    headers = {"Authorization": f"Bearer {fleet_token}"}
    first = await client.post(f"{PATH}/{device.id}/provisioning-code",
                              headers=headers | _key())
    second = await client.post(f"{PATH}/{device.id}/provisioning-code",
                               headers=headers | _key())

    from app.core.security import hash_token

    await session.refresh(device)
    assert first.json()["provisioning_code"] != second.json()["provisioning_code"]
    assert device.provisioning_code_hash == hash_token(
        second.json()["provisioning_code"]
    )


async def test_devices_need_the_permission(client, admin_token, device):
    response = await client.get(PATH,
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 403
