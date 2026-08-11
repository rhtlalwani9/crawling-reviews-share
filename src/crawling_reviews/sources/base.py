"""SourceAdapter — the contract every review source implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

from ..core.errors import BlockedError, ParseError
from ..core.logging import get_logger
from ..crawl.http_fetcher.base import Fetcher, FetchResponse
from ..crawl.session.minter import MintSpec
from ..crawl.session.models import Session
from ..profiles.models import BlockKind

log = get_logger(__name__)


@dataclass
class Review:
    """Normalised review. One internal shape regardless of how many source formats exist."""
    rating: float | None
    review_date: date | None
    reviewer_name: str | None
    comment: str | None
    review_id: str | None = None


@dataclass
class PageResult:
    reviews: list[Review]
    has_more: bool
    total_count: int | None = None


@dataclass
class AggregateResult:
    reviews: list[Review]
    site_total: int | None
    pages_fetched: int
    stopped_early: bool = False
    page_timings_ms: list[int] = field(default_factory=list)

    def avg_ms_per_page(self) -> int | None:
        if not self.page_timings_ms:
            return None
        return round(sum(self.page_timings_ms) / len(self.page_timings_ms))


class SourceAdapter(ABC):
    """Base for all sources. Construct with a fetcher; the session is passed per aggregate()."""

    source_name: str = ""
    host_patterns: tuple[str, ...] = ()
    #: Sites that fingerprint the TLS handshake need the impersonating transport.
    needs_impersonation: bool = True

    def __init__(self, fetcher: Fetcher, max_pages: int = 200):
        self.fetcher = fetcher
        self.max_pages = max_pages

    # ------------------------------------------------------------------ abstract hooks

    @abstractmethod
    def mint_spec(self, url: str) -> MintSpec:
        """What the browser must land on, and what proves the challenge cleared."""

    @abstractmethod
    def page_request(self, url: str, page: int) -> tuple[str, dict[str, str]]:
        """(request_url, extra_headers) for page N."""

    @abstractmethod
    def parse_page(self, html: str, page: int) -> PageResult:
        """Turn one response body into reviews."""

    # ------------------------------------------------------------------ template method

    def aggregate(
        self,
        url: str,
        *,
        session: Session,
        filter_date: date | None = None,
        first_page_html: str | None = None,
    ) -> AggregateResult:
        """Walk the source's pagination and return normalised, sorted reviews."""
        collected: list[Review] = []
        seen: set[str] = set()
        site_total: int | None = None
        timings: list[int] = []
        stopped_early = False
        page = 1

        while page <= self.max_pages:
            if page == 1 and first_page_html is not None:
                html, elapsed = first_page_html, 0
            else:
                request_url, headers = self.page_request(url, page)
                response: FetchResponse = self.fetcher.fetch(request_url, headers=headers, session=session)
                html, elapsed = response.body, response.elapsed_ms
                session.uses += 1

            result = self.parse_page(html, page)
            if not isinstance(result, PageResult):
                raise ParseError(f"{self.source_name}: parse_page did not return a PageResult")

            # Content assertion.
            if page == 1 and not result.reviews:
                raise BlockedError(
                    f"{self.source_name}: page 1 returned no reviews (shadow block or layout change)",
                    kind=BlockKind.SHADOW.value,
                )

            if result.total_count is not None and site_total is None:
                site_total = result.total_count

            added = 0
            for review in result.reviews:
                key = self.identity(review)
                if key in seen:
                    continue
                seen.add(key)
                collected.append(review)
                added += 1

            if elapsed:
                timings.append(elapsed)

            log.info(
                "page parsed",
                extra_fields={
                    "source": self.source_name, "page": page, "found": len(result.reviews),
                    "new": added, "total": len(collected), "ms": elapsed,
                },
            )

            # Pages arrive newest-first, so once an entire page predates the filter every later page
            # does too. This is the difference between fetching 3 pages and 1,385.
            if filter_date and self._page_entirely_before(result.reviews, filter_date):
                stopped_early = True
                break

            if not result.has_more or not result.reviews:
                break
            page += 1

        if filter_date:
            collected = [r for r in collected if r.review_date and r.review_date >= filter_date]

        # Undated reviews sink to the bottom rather than being dropped: a date-parsing miss should
        # not silently lose data.
        collected.sort(key=lambda r: (r.review_date is not None, r.review_date), reverse=True)

        return AggregateResult(
            reviews=collected,
            site_total=site_total if site_total is not None else len(collected),
            pages_fetched=min(page, self.max_pages),
            stopped_early=stopped_early,
            page_timings_ms=timings,
        )

    # ------------------------------------------------------------------ helpers

    def identity(self, review: Review) -> str:
        """Dedup key."""
        if review.review_id:
            return f"id:{review.review_id}"
        return "|".join([
            review.reviewer_name or "",
            review.review_date.isoformat() if review.review_date else "",
            (review.comment or "")[:120],
        ])

    @staticmethod
    def _page_entirely_before(reviews: list[Review], cutoff: date) -> bool:
        dated = [r for r in reviews if r.review_date]
        return bool(dated) and all(r.review_date < cutoff for r in dated)
