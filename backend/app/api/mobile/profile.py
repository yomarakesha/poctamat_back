import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import require_client
from app.core.errors import AppError, ErrorCode
from app.modules.catalog.models import City
from app.modules.identity.models import Client

router = APIRouter(tags=["profile"])


class ClientOut(BaseModel):
    id: uuid.UUID
    phone: str
    last_name: str | None
    first_name: str | None
    middle_name: str | None
    city_id: uuid.UUID | None
    language: str
    status: str
    profile_complete: bool


class ClientPatch(BaseModel):
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    middle_name: str | None = Field(default=None, max_length=100)
    city_id: uuid.UUID | None = None
    language: str | None = Field(default=None, pattern="^(tk|ru|en)$")


def client_out(client: Client) -> ClientOut:
    return ClientOut(
        id=client.id, phone=client.phone, last_name=client.last_name,
        first_name=client.first_name, middle_name=client.middle_name,
        city_id=client.city_id, language=client.language,
        # Derived rather than stored: `is_blocked` is the fact, `blocked` is how
        # the contract spells it.
        status="blocked" if client.is_blocked else "active",
        profile_complete=client.profile_complete,
    )


@router.get("/me", response_model=ClientOut)
async def get_me(client: Client = Depends(require_client)) -> ClientOut:
    return client_out(client)


@router.patch("/me", response_model=ClientOut)
async def update_me(
    payload: ClientPatch,
    session: AsyncSession = Depends(get_session),
    client: Client = Depends(require_client),
) -> ClientOut:
    changes = payload.model_dump(exclude_unset=True)

    if changes.get("city_id") is not None:
        city = await session.get(City, changes["city_id"])
        # A city marked «скоро» is in the list but cannot be chosen yet; picking
        # one has to fail with a code the app can render, not silently stick.
        if city is None or not city.is_active:
            raise AppError(ErrorCode.CITY_NOT_AVAILABLE,
                           "This city is not available yet.", 422,
                           details={"city_id": str(changes["city_id"])})

    for field, value in changes.items():
        setattr(client, field, value)
    await session.commit()
    return client_out(client)
