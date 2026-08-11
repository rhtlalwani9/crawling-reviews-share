"""G2 adapter."""
from __future__ import annotations

from urllib.parse import urlencode, urlsplit, urlunsplit

from ...crawl.session.minter import MintSpec
from ..base import PageResult, SourceAdapter
from .parser import parse_reviews_page

FRAME_ID = "reviews-and-filters"
ORDER = "most_recent"          # newest-first, as the assignment requires


class G2Adapter(SourceAdapter):
    source_name = "g2"
    host_patterns = ("g2.com",)
    needs_impersonation = True          # refuses OpenSSL-based clients at the handshake

    # ------------------------------------------------------------------ hooks

    def mint_spec(self, url: str) -> MintSpec:
        # Land on the reviews page itself: one navigation yields the cookie jar, the CSRF token and
        # page 1 of reviews. The homepage would cost the same challenge and give less.
        return MintSpec(
            url=self._with_order(url),
            wait_selector='article[id*="review-"]',
            settle_s=2.0,
            extract_csrf=True,
        )

    def page_request(self, url: str, page: int) -> tuple[str, dict[str, str]]:
        return self._frame_url(url, page), {
            "accept": "text/html, application/xhtml+xml",
            # accept-language is deliberately absent: the fetcher sets it from the session, which
            # recorded what the minting browser advertised.
            "turbo-frame": FRAME_ID,
            "referer": self._with_order(url),
            # XHR-shaped, not navigation-shaped: a fresh top-level navigation is where challenges
            # get issued, while an in-page request from a cleared session is scored leniently.
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }

    def parse_page(self, html: str, page: int) -> PageResult:
        return parse_reviews_page(html, page)

    # ------------------------------------------------------------------ url helpers

    @staticmethod
    def _with_order(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode({"order": ORDER}), ""))

    @staticmethod
    def _frame_url(url: str, page: int) -> str:
        parts = urlsplit(url)
        path = parts.path.rstrip("/")
        if path.endswith("/reviews"):
            path = path[: -len("/reviews")] + "/reviews_and_filters"
        query = urlencode({"order": ORDER, "page": page})
        return urlunsplit((parts.scheme, parts.netloc, path, query, ""))
