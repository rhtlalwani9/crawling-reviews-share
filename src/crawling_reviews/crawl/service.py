"""Crawl service — the only place that knows how a request becomes an answer."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

from ..config import config
from ..core.errors import BlockedError, MintError, ValidationError
from ..core.logging import get_logger
from ..profiles.manager import ProfileManager
from ..profiles.models import BlockKind
from ..sources.base import AggregateResult, SourceAdapter
from ..sources.registry import infer_source, resolve_adapter, supported_sources
from .http_fetcher.decorators import build_transport
from .http_fetcher.httpx_fetcher import HttpxFetcher
from .http_fetcher.impersonate import ImpersonateFetcher
from .session.minter import SessionMinter
from .session.store import SessionStore

log = get_logger(__name__)


@dataclass
class CrawlOutcome:
    source: str
    result: AggregateResult
    profile_id: str
    session_summary: dict
    attempts: int
    minted: bool


class CrawlService:
    def __init__(self, profile_manager: ProfileManager | None = None):
        self.profiles = profile_manager or ProfileManager()
        self.sessions = SessionStore()
        self.minter = SessionMinter()

        # One transport per strategy, shared: both are cheap to hold and thread-safe for our use.
        self._impersonate = build_transport(ImpersonateFetcher())
        self._plain = build_transport(HttpxFetcher())

    # ------------------------------------------------------------------ public

    def aggregate_reviews(
        self,
        *,
        url: str,
        source_name: str | None = None,
        filter_date: date | None = None,
        max_pages: int | None = None,
    ) -> CrawlOutcome:
        resolved = self._resolve_source(url, source_name)
        adapter_cls = resolve_adapter(resolved)
        host = urlsplit(url).netloc

        attempts = config.crawl.max_profile_attempts
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            profile = self.profiles.acquire(holder=f"crawl-{host}-{attempt}")
            minted = False
            try:
                signature = self.profiles.signature_for(profile)
                adapter = adapter_cls(
                    fetcher=self._impersonate if adapter_cls.needs_impersonation else self._plain,
                    max_pages=max_pages or config.crawl.max_pages,
                )

                session = self.sessions.get(host, profile.id)
                first_page_html: str | None = None

                if session is None:
                    # The mint has to load page 1 anyway, so keep its HTML and skip re-fetching it.
                    session, first_page_html = self.minter.mint(profile, signature, adapter.mint_spec(url))
                    self.sessions.put(session)
                    minted = True

                with self._heartbeat(profile.id):
                    result = adapter.aggregate(
                        url,
                        session=session,
                        filter_date=filter_date,
                        first_page_html=first_page_html,
                    )

                self.profiles.report_success(profile.id, minted=minted)
                log.info(
                    "crawl finished",
                    extra_fields={
                        "source": resolved, "returned": len(result.reviews),
                        "site_total": result.site_total, "pages": result.pages_fetched,
                        "avg_ms_per_page": result.avg_ms_per_page(), "attempt": attempt,
                        "profile_id": profile.id, "minted": minted,
                    },
                )
                return CrawlOutcome(
                    source=resolved, result=result, profile_id=profile.id,
                    session_summary=session.summary(), attempts=attempt, minted=minted,
                )

            except BlockedError as err:
                last_error = err
                # Attribute the block to the identity, then take that identity out of play.
                self.profiles.report_blocked(profile.id, BlockKind(err.kind))
                self.sessions.invalidate(host, profile.id)
                log.warning(
                    "crawl blocked",
                    extra_fields={"profile_id": profile.id, "kind": err.kind, "attempt": attempt,
                                  "attempts_left": attempts - attempt},
                )

            except MintError as err:
                last_error = err
                # A failed mint means the challenge was not cleared under this identity, which is
                # the
                # same signal as a block for pool-health purposes.
                self.profiles.report_blocked(profile.id, BlockKind.CHALLENGE)
                log.warning("mint failed", extra_fields={"profile_id": profile.id, "attempt": attempt})

            finally:
                self.profiles.release(profile.id)

        assert last_error is not None
        raise last_error

    def pool_status(self) -> dict:
        return {
            "pool": self.profiles.stats().__dict__,
            "profiles": self.profiles.describe(),
            "sessions": self.sessions.describe(),
        }

    def close(self) -> None:
        self.profiles.stop_reaper()
        self._impersonate.close()
        self._plain.close()

    # ------------------------------------------------------------------ internals

    def _resolve_source(self, url: str, source_name: str | None) -> str:
        if not url:
            raise ValidationError('query param "url" is required')
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValidationError(f'"url" is not a valid http(s) URL: {url}')

        inferred = infer_source(url)
        resolved = source_name or inferred
        if not resolved:
            raise ValidationError(
                f'could not determine the source for "{parts.netloc}". '
                f'Pass source_name (one of: {", ".join(supported_sources())})'
            )
        # A mismatch is a caller error worth surfacing: running the wrong parser would return a
        # confidently empty result rather than an error.
        if source_name and inferred and source_name.lower() != inferred.lower():
            raise ValidationError(
                f'source_name "{source_name}" does not match URL host "{parts.netloc}" '
                f'(looks like "{inferred}")'
            )
        return resolved

    @contextmanager
    def _heartbeat(self, profile_id: str, interval_s: float = 20.0):
        """Prove liveness to the pool while a long crawl runs."""
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(interval_s):
                self.profiles.heartbeat(profile_id)

        thread = threading.Thread(target=beat, name=f"heartbeat-{profile_id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1.0)
