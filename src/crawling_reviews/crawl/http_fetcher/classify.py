"""Response classification: transport signal -> typed outcome."""
from __future__ import annotations

import re

from ...core.errors import BlockedError, NetworkError
from ...profiles.models import BlockKind

# Markers that mean a challenge or interstitial was served, whatever the status code says.
CHALLENGE_MARKERS = re.compile(
    r"just a moment"
    r"|cf-chl|challenge-platform"
    r"|enable javascript and cookies to continue"
    r"|verifying you are human"
    r"|attention required!\s*\|\s*cloudflare"
    r"|captcha-delivery\.com",
    re.IGNORECASE,
)

# Only the head of the body is inspected: a challenge page announces itself immediately, and
# scanning a 1.4MB document for every request is wasted work.
HEAD_BYTES = 4000


def classify(url: str, status: int, body: str) -> None:
    """Raise the appropriate error, or return None when the response looks usable."""
    if CHALLENGE_MARKERS.search(body[:HEAD_BYTES] or ""):
        raise BlockedError(
            f"anti-bot challenge served for {url}",
            kind=BlockKind.CHALLENGE.value,
        )

    if status == 429:
        # Cooperative signal: slow down. Not evidence the identity is burned, which is why it maps
        # to RATE_LIMITED and never counts as a strike against the profile.
        raise BlockedError(f"rate limited by {url}", kind=BlockKind.RATE_LIMITED.value)

    if status == 403:
        raise BlockedError(f"refused with HTTP 403 by {url}", kind=BlockKind.HARD_403.value)

    if status >= 500:
        raise NetworkError(f"upstream returned HTTP {status}")

    if status >= 400:
        # 404 and friends: real answers, just not useful ones. Not retryable.
        raise NetworkError(f"upstream returned HTTP {status}")
