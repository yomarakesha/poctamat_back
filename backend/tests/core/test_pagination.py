from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.pagination import MAX_PAGE, PageMeta, PageParams, page_meta, page_params


def test_total_pages_rounds_up():
    assert page_meta(page=1, per_page=20, total=124) == PageMeta(
        page=1, per_page=20, total=124, total_pages=7
    )


def test_zero_total_still_reports_one_page():
    assert page_meta(page=1, per_page=20, total=0).total_pages == 1


def _app_using_page_params() -> FastAPI:
    app = FastAPI()

    @app.get("/things")
    async def things(params: PageParams = Depends(page_params)) -> dict[str, int]:
        return {"page": params.page, "per_page": params.per_page}

    return app


async def _get(url: str):
    transport = ASGITransport(app=_app_using_page_params())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(url)


async def test_page_is_bounded_so_offset_cannot_be_made_enormous():
    # Without a ceiling, page=10**9 becomes OFFSET 20000000000 and the database
    # scans and discards every row before it. The list endpoints are public.
    response = await _get(f"/things?page={MAX_PAGE + 1}")
    assert response.status_code == 422


async def test_page_at_the_ceiling_is_still_accepted():
    response = await _get(f"/things?page={MAX_PAGE}")
    assert response.status_code == 200
    assert response.json()["page"] == MAX_PAGE
