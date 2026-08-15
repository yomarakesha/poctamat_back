import uuid

from pydantic import BaseModel


class CityOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str


class CellTypeOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    width_cm: int
    height_cm: int
    depth_cm: int


def localized(row, language: str) -> str:
    """Pick the name column for `language`, falling back to Turkmen.

    An unknown language must not raise: the header is caller-supplied and a
    reference list is not the place to reject a request over it.
    """
    return getattr(row, f"name_{language}", row.name_tk)
