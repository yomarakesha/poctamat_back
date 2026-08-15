from app.core.pagination import PageMeta, page_meta


def test_total_pages_rounds_up():
    assert page_meta(page=1, per_page=20, total=124) == PageMeta(
        page=1, per_page=20, total=124, total_pages=7
    )


def test_zero_total_still_reports_one_page():
    assert page_meta(page=1, per_page=20, total=0).total_pages == 1
