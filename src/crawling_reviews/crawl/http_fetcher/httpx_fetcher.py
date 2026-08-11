"""Plain HTTP transport (httpx), no impersonation."""
from __future__ import annotations

import time

import httpx

from ...config import config
from ...core.errors import NetworkError
from ...core.logging import get_logger
from ..session.models import Session
from .base import Fetcher, FetchResponse
from .classify import classify

log = get_logger(__name__)

def chrome_ish_headers(session: Session | None = None) -> dict[str, str]:
    """Baseline headers for a replayed request."""
    if session is not None and session.accept_language:
        accept_language = session.accept_language
    else:
        from ..session.geo import egress_profile

        accept_language = egress_profile()["accept_language"]

    return {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": accept_language,
    }


class HttpxFetcher(Fetcher):
    """Ordinary HTTP/2-capable client. Fine for sources that do not inspect the handshake."""

    name = "httpx"

    def __init__(self, proxy: str | None = None):
        # HTTP/2 is preferred (its frame ordering is part of looking like a browser) but the `h2`
        # extra is optional.
        try:
            self._client = httpx.Client(
                http2=True, follow_redirects=True, timeout=config.fetch.timeout_s, proxy=proxy
            )
        except ImportError:
            log.warning("h2 not installed; falling back to HTTP/1.1 (pip install 'httpx[http2]')")
            self._client = httpx.Client(
                http2=False, follow_redirects=True, timeout=config.fetch.timeout_s, proxy=proxy
            )

    def fetch(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        session: Session | None = None,
        timeout_s: float | None = None,
    ) -> FetchResponse:
        merged = {**chrome_ish_headers(session), **(headers or {})}
        cookies = session.cookie_dict() if session else None
        if session:
            merged.setdefault("user-agent", session.user_agent)

        started = time.time()
        try:
            resp = self._client.get(
                url, headers=merged, cookies=cookies,
                timeout=timeout_s or config.fetch.timeout_s,
            )
        except Exception as exc:
            raise NetworkError("Network Issue", cause=exc) from exc

        elapsed_ms = int((time.time() - started) * 1000)
        body = resp.text or ""
        log.debug("httpx fetch", extra_fields={"url": url, "status": resp.status_code, "ms": elapsed_ms})

        classify(url, resp.status_code, body)
        return FetchResponse(
            url=url, status=resp.status_code, body=body,
            elapsed_ms=elapsed_ms, headers=dict(resp.headers),
        )

    def close(self) -> None:
        self._client.close()
