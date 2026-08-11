"""Transport decorators."""
from __future__ import annotations

import random
import time
from urllib.parse import urlsplit

from ...config import config
from ...core.errors import AppError
from ...core.logging import get_logger
from ..rate_limit import TokenBucket
from .base import Fetcher, FetchResponse

log = get_logger(__name__)


class RateLimitedFetcher(Fetcher):
    """Politeness, keyed per host so one slow site cannot throttle another."""

    def __init__(self, inner: Fetcher, bucket: TokenBucket | None = None):
        self.inner = inner
        self.name = f"ratelimited({inner.name})"
        self.bucket = bucket or TokenBucket(config.fetch.rate_per_second, config.fetch.burst)

    def fetch(self, url: str, **kwargs) -> FetchResponse:
        waited = self.bucket.acquire(urlsplit(url).netloc)
        if waited > 0.05:
            log.debug("rate limited", extra_fields={"url": url, "waited_s": round(waited, 2)})
        return self.inner.fetch(url, **kwargs)

    def close(self) -> None:
        self.inner.close()


class RetryingFetcher(Fetcher):
    """Retry transient failures with exponential backoff and **full jitter**."""

    def __init__(self, inner: Fetcher, max_retries: int | None = None):
        self.inner = inner
        self.name = f"retrying({inner.name})"
        self.max_retries = config.fetch.max_retries if max_retries is None else max_retries

    def fetch(self, url: str, **kwargs) -> FetchResponse:
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return self.inner.fetch(url, **kwargs)
            except AppError as err:
                last = err
                if not err.retryable or attempt == self.max_retries:
                    raise
                cap = min(config.fetch.backoff_max_s, config.fetch.backoff_base_s * (2 ** attempt))
                delay = random.uniform(0, cap)
                log.warning(
                    "retrying fetch",
                    extra_fields={"url": url, "attempt": attempt + 1, "delay_s": round(delay, 2),
                                  "error": err.code},
                )
                time.sleep(delay)
        raise last  # unreachable, but keeps the type checker honest

    def close(self) -> None:
        self.inner.close()


def build_transport(inner: Fetcher) -> Fetcher:
    """Compose the standard stack."""
    return RetryingFetcher(RateLimitedFetcher(inner))
