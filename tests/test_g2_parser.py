"""G2 parser tests against saved fixtures — no network, no browser."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from crawling_reviews.sources.g2.adapter import G2Adapter
from crawling_reviews.sources.g2.parser import parse_reviews_page

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"fixture not captured: {name}")
    return path.read_text(encoding="utf-8")


@pytest.fixture()
def page1():
    return parse_reviews_page(load("g2.frame-page1.html"), 1)


@pytest.fixture()
def page3():
    return parse_reviews_page(load("g2.frame-page3.html"), 3)


# --- extraction ---------------------------------------------------------------------------------

def test_extracts_ten_reviews_per_page(page1):
    assert len(page1.reviews) == 10


def test_every_field_is_populated(page1):
    for review in page1.reviews:
        assert review.review_date is not None
        assert review.rating is not None
        assert review.reviewer_name
        assert review.comment


def test_ratings_are_within_the_documented_scale(page1):
    for review in page1.reviews:
        assert 0 <= review.rating <= 5


def test_review_ids_are_captured_for_dedup(page1):
    ids = [r.review_id for r in page1.reviews]
    assert all(ids), "the article id suffix is G2's own review id and is the natural dedup key"
    assert len(set(ids)) == len(ids)


def test_reads_authoritative_total_from_json_ld(page1):
    assert page1.total_count is not None
    assert page1.total_count > 1000


def test_detects_that_more_pages_exist(page1):
    assert page1.has_more is True


# --- pagination ---------------------------------------------------------------------------------

def test_different_pages_return_different_reviews(page1, page3):
    """The bug this guards: /reviews returns page 1 forever; only the frame endpoint paginates."""
    assert {r.review_id for r in page1.reviews} != {r.review_id for r in page3.reviews}


def test_later_pages_are_generally_older(page1, page3):
    newest_p1 = max(r.review_date for r in page1.reviews if r.review_date)
    newest_p3 = max(r.review_date for r in page3.reviews if r.review_date)
    assert newest_p3 <= newest_p1


def test_sort_is_not_strictly_monotonic_within_a_page(page3):
    """G2 injects a pinned review mid-page despite order=most_recent (id 11131331, 2025-05-06 on page 3)."""
    dates = [r.review_date for r in page3.reviews if r.review_date]
    assert dates != sorted(dates, reverse=True)


# --- adapter url construction --------------------------------------------------------------------

def test_adapter_targets_the_frame_endpoint_not_the_shell():
    adapter = G2Adapter(fetcher=None)  # type: ignore[arg-type]
    url, headers = adapter.page_request("https://www.g2.com/products/asana/reviews", 3)

    assert "/reviews_and_filters" in url, "the /reviews shell never paginates"
    assert "page=3" in url and "order=most_recent" in url
    assert headers["turbo-frame"] == "reviews-and-filters"
    # XHR-shaped, not navigation-shaped: navigations are where challenges get issued.
    assert headers["sec-fetch-mode"] == "cors"


def test_mint_spec_lands_on_the_reviews_page():
    adapter = G2Adapter(fetcher=None)  # type: ignore[arg-type]
    spec = adapter.mint_spec("https://www.g2.com/products/asana/reviews")
    assert spec.url.endswith("order=most_recent")
    assert "article" in spec.wait_selector


# --- aggregation logic (no network: a stub fetcher replays fixtures) -----------------------------

class _FixtureFetcher:
    """Replays saved pages so the template method can be tested without touching the network."""
    name = "fixture"

    def __init__(self, pages: dict[int, str]):
        self.pages = pages
        self.calls: list[str] = []

    def fetch(self, url, *, headers=None, session=None, timeout_s=None):
        from crawling_reviews.crawl.http_fetcher.base import FetchResponse
        import re
        page = int(re.search(r"page=(\d+)", url).group(1))
        self.calls.append(url)
        return FetchResponse(url=url, status=200, body=self.pages[page], elapsed_ms=10)

    def close(self): ...


def test_aggregate_dedupes_and_sorts_newest_first():
    from crawling_reviews.crawl.session.models import Session

    fetcher = _FixtureFetcher({1: load("g2.frame-page1.html"), 3: load("g2.frame-page3.html")})
    adapter = G2Adapter(fetcher=fetcher, max_pages=1)
    session = Session(host="www.g2.com", profile_id="p1", user_agent="ua")

    result = adapter.aggregate("https://www.g2.com/products/asana/reviews", session=session)

    dates = [r.review_date for r in result.reviews if r.review_date]
    assert dates == sorted(dates, reverse=True), "output must be newest-first regardless of input"
    ids = [r.review_id for r in result.reviews]
    assert len(ids) == len(set(ids)), "reviews shift between requests, so dedup is required"


def test_filter_date_excludes_older_reviews():
    from crawling_reviews.crawl.session.models import Session

    fetcher = _FixtureFetcher({1: load("g2.frame-page1.html")})
    adapter = G2Adapter(fetcher=fetcher, max_pages=1)
    session = Session(host="www.g2.com", profile_id="p1", user_agent="ua")

    cutoff = date(2026, 8, 8)
    result = adapter.aggregate(
        "https://www.g2.com/products/asana/reviews", session=session, filter_date=cutoff
    )
    assert result.reviews, "fixture should contain reviews at or after the cutoff"
    assert all(r.review_date >= cutoff for r in result.reviews)


def test_empty_first_page_is_treated_as_a_shadow_block():
    """A 200 with no rows, when the site claims thousands, is a block — not an empty result."""
    from crawling_reviews.core.errors import BlockedError
    from crawling_reviews.crawl.session.models import Session

    fetcher = _FixtureFetcher({1: "<html><body>nothing here</body></html>"})
    adapter = G2Adapter(fetcher=fetcher, max_pages=1)
    session = Session(host="www.g2.com", profile_id="p1", user_agent="ua")

    with pytest.raises(BlockedError) as err:
        adapter.aggregate("https://www.g2.com/products/asana/reviews", session=session)
    assert err.value.kind == "shadow"
