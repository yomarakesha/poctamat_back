import uuid

from pydantic import BaseModel, Field

from app.core.pagination import PageMeta


class RoleOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    permissions: list[str]


class AdminUserOut(BaseModel):
    id: uuid.UUID
    login: str
    full_name: str
    role: RoleOut
    # Flattened from the role as well as nested inside it: the panel hides
    # actions against this list, and reaching through `role` for it on every
    # render is what the contract avoids by repeating it here.
    permissions: list[str]
    postamat_ids: list[uuid.UUID]
    is_active: bool
    last_login_at: str | None


class AdminUserPage(BaseModel):
    items: list[AdminUserOut]
    pagination: PageMeta


class AdminUserCreate(BaseModel):
    login: str = Field(min_length=3, max_length=128)
    # Twelve characters minimum, as the contract states. An admin password opens
    # every cell in the fleet through the remote-open button.
    password: str = Field(min_length=12, max_length=256)
    role_id: uuid.UUID
    full_name: str = Field(min_length=1, max_length=200)
    postamat_ids: list[uuid.UUID] = Field(default_factory=list)


class AdminUserPatch(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    role_id: uuid.UUID | None = None
    is_active: bool | None = None
    postamat_ids: list[uuid.UUID] | None = None


class TemporaryPassword(BaseModel):
    # Returned once, read to the person, and useless afterwards: the account is
    # flagged `must_change_password`.
    temporary_password: str
    expires_at: str
