from app.modules.catalog.models import CellType, City


async def test_cities_are_localized(client, session):
    session.add(City(code="ashgabat", name_tk="Aşgabat", name_ru="Ашхабад", name_en="Ashgabat"))
    await session.commit()

    default = await client.get("/api/v1/cities")
    assert default.json()[0]["name"] == "Aşgabat"

    russian = await client.get("/api/v1/cities", headers={"Accept-Language": "ru"})
    assert russian.json()[0]["name"] == "Ашхабад"


async def test_blocked_cell_types_are_hidden(client, session):
    session.add(CellType(code="small", name_tk="Kiçi", name_ru="Маленький", name_en="Small",
                         width_cm=20, height_cm=20, depth_cm=40))
    session.add(CellType(code="huge", name_tk="X", name_ru="X", name_en="X",
                         width_cm=90, height_cm=90, depth_cm=90, is_blocked=True))
    await session.commit()

    response = await client.get("/api/v1/cell-types")
    assert [item["code"] for item in response.json()] == ["small"]
