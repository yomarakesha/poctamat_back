from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamped, UUIDPrimaryKey


class City(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "cities"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # The three names are product data rather than interface strings: a city is
    # spelled differently in each language and an operator edits all three.
    name_tk: Mapped[str] = mapped_column(String(100))
    name_ru: Mapped[str] = mapped_column(String(100))
    name_en: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class CellType(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "cell_types"

    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name_tk: Mapped[str] = mapped_column(String(50))
    name_ru: Mapped[str] = mapped_column(String(50))
    name_en: Mapped[str] = mapped_column(String(50))
    width_cm: Mapped[int] = mapped_column(Integer)
    height_cm: Mapped[int] = mapped_column(Integer)
    depth_cm: Mapped[int] = mapped_column(Integer)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
