from sqlalchemy import select

from app.modules.identity.models import Client


async def test_client_phone_is_unique(session):
    session.add(Client(phone="+99362123456"))
    await session.commit()

    stored = await session.scalar(select(Client))
    assert stored.language == "tk"
    assert stored.is_blocked is False
