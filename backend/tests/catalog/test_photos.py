import io

import pytest


@pytest.fixture(autouse=True)
def media_root(tmp_path, monkeypatch):
    """Point the storage at a throwaway directory for the whole test.

    Uploads are real files; without this the suite would litter backend/media
    and one run could read another's leftovers.
    """
    from app.core import storage

    monkeypatch.setattr(storage, "_storage", storage.LocalFileStorage(tmp_path))
    yield tmp_path
    storage.reset_file_storage()


def _png() -> bytes:
    # The smallest thing browsers agree is a PNG: a 1x1 transparent pixel.
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
        "05575bcecd0000000049454e44ae426082"
    )


async def test_uploading_a_photo_puts_it_on_the_postamat(client, admin_token, postamat):
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.post(
        f"/api/v1/admin/postamats/{postamat.id}/photos", headers=headers,
        files={"file": ("front.png", io.BytesIO(_png()), "image/png")},
        data={"caption": "фасад"},
    )
    assert response.status_code == 201
    url = response.json()["url"]
    assert response.json()["caption"] == "фасад"

    fetched = await client.get(url)
    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "image/png"
    assert fetched.content == _png()

    public = await client.get(f"/api/v1/postamats/{postamat.id}")
    assert [photo["url"] for photo in public.json()["photos"]] == [url]


async def test_a_file_that_is_not_an_image_is_refused(client, admin_token, postamat):
    response = await client.post(
        f"/api/v1/admin/postamats/{postamat.id}/photos",
        headers={"Authorization": f"Bearer {admin_token}"},
        files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert response.status_code == 422


async def test_an_oversized_file_is_refused(client, admin_token, postamat):
    big = _png() + b"\0" * (6 * 1024 * 1024)
    response = await client.post(
        f"/api/v1/admin/postamats/{postamat.id}/photos",
        headers={"Authorization": f"Bearer {admin_token}"},
        files={"file": ("huge.png", io.BytesIO(big), "image/png")},
    )
    assert response.status_code == 413


async def test_deleting_a_photo_removes_the_row_and_the_file(
    client, admin_token, postamat, media_root
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = await client.post(
        f"/api/v1/admin/postamats/{postamat.id}/photos", headers=headers,
        files={"file": ("front.png", io.BytesIO(_png()), "image/png")},
    )
    photo_id = created.json()["id"]
    assert len(list(media_root.iterdir())) == 1

    deleted = await client.delete(
        f"/api/v1/admin/postamats/{postamat.id}/photos/{photo_id}", headers=headers
    )
    assert deleted.status_code == 204
    assert list(media_root.iterdir()) == []

    public = await client.get(f"/api/v1/postamats/{postamat.id}")
    assert public.json()["photos"] == []


async def test_a_photo_of_another_postamat_cannot_be_deleted(
    client, session, admin_token, postamat, city
):
    from app.modules.catalog.models import Postamat

    other = Postamat(number="10002", name="ТП #2", city_id=city.id, address="ул. Мира")
    session.add(other)
    await session.commit()
    # The suite shares one session with the request, so a row created here comes
    # back from the identity map with its photos collection unloaded — and a
    # lazy load inside the request raises instead of loading.
    await session.refresh(other)

    headers = {"Authorization": f"Bearer {admin_token}"}
    created = await client.post(
        f"/api/v1/admin/postamats/{postamat.id}/photos", headers=headers,
        files={"file": ("front.png", io.BytesIO(_png()), "image/png")},
    )
    response = await client.delete(
        f"/api/v1/admin/postamats/{other.id}/photos/{created.json()['id']}",
        headers=headers,
    )
    assert response.status_code == 404


async def test_a_traversal_key_cannot_reach_outside_the_media_directory(client):
    for key in ("../.env", "..%2F.env", "not-a-key.png", "0" * 32 + ".exe"):
        response = await client.get(f"/api/v1/media/{key}")
        assert response.status_code in (400, 404), key


async def test_uploading_needs_the_write_permission(client, session, postamat):
    from app.core.security import create_access_token
    from app.modules.identity.models import AdminUser, Role

    role = Role(code="reader", name="Reader", permissions=["postamats.read"])
    session.add(role)
    await session.flush()
    admin = AdminUser(login="reader_test", full_name="Читаев Ч.Ч.",
                      password_hash="x", role_id=role.id)
    session.add(admin)
    await session.commit()
    await session.refresh(admin)

    response = await client.post(
        f"/api/v1/admin/postamats/{postamat.id}/photos",
        headers={"Authorization": f"Bearer {create_access_token('admin', admin.id)}"},
        files={"file": ("front.png", io.BytesIO(_png()), "image/png")},
    )
    assert response.status_code == 403
