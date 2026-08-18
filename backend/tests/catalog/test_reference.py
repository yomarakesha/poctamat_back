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
                         width_mm=200, height_mm=200, depth_mm=400))
    session.add(CellType(code="huge", name_tk="X", name_ru="X", name_en="X",
                         width_mm=900, height_mm=900, depth_mm=900, is_blocked=True))
    await session.commit()

    response = await client.get("/api/v1/cell-types")
    assert [item["code"] for item in response.json()] == ["small"]
