"""Impersonating HTTP transport (curl_cffi over curl-impersonate)."""
from __future__ import annotations

import time

from curl_cffi import requests as curl_requests

from ...config import config
from ...core.errors import NetworkError
from ...core.logging import get_logger
from ..session.models import Session
from .base import Fetcher, FetchResponse
from .classify import classify

log = get_logger(__name__)


class ImpersonateFetcher(Fetcher):
    """HTTP transport that presents a browser's TLS and HTTP/2 fingerprint."""

    name = "impersonate"

    def __init__(self, default_impersonate: str = "chrome146"):
        self.default_impersonate = default_impersonate

    def fetch(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        session: Session | None = None,
        timeout_s: float | None = None,
    ) -> FetchResponse:
        timeout_s = timeout_s or config.fetch.timeout_s
        impersonate = session.impersonate_key if session else self.default_impersonate

        merged = dict(headers or {})
        cookies = None
        if session:
            cookies = session.cookie_dict()
            # The UA must match the one that earned the cookies; everything else is left to the
            # impersonation profile so the identity stays internally consistent.
            merged.setdefault("user-agent", session.user_agent)
            # The platform hint must be stated, not inherited.
            merged.setdefault("sec-ch-ua-platform", session.sec_ch_ua_platform)
            # The language the minting browser advertised, for the same reason the cookies and UA
            # come
            # from here: they were all issued together to one identity.
            if session.accept_language:
                merged.setdefault("accept-language", session.accept_language)
            if session.csrf_token:
                merged.setdefault("x-csrf-token", session.csrf_token)

        started = time.time()
        try:
            resp = curl_requests.get(
                url,
                headers=merged,
                cookies=cookies,
                impersonate=impersonate,
                timeout=timeout_s,
            )
        except Exception as exc:
            raise NetworkError("Network Issue", cause=exc) from exc

        elapsed_ms = int((time.time() - started) * 1000)
        body = resp.text or ""

        log.debug(
            "impersonate fetch",
            extra_fields={
                "url": url, "status": resp.status_code, "bytes": len(body),
                "ms": elapsed_ms, "impersonate": impersonate,
            },
        )

        classify(url, resp.status_code, body)
        return FetchResponse(
            url=url, status=resp.status_code, body=body,
            elapsed_ms=elapsed_ms, headers=dict(resp.headers),
        )
