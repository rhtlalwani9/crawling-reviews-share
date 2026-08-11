"""Session model — the artifact a browser mint produces."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Session:
    host: str
    profile_id: str

    cookies: list[dict[str, str]] = field(default_factory=list)
    csrf_token: str | None = None
    user_agent: str = ""
    impersonate_key: str = "chrome146"

    # The Accept-Language the minting browser advertised.
    accept_language: str = ""
    #: Quoted sec-ch-ua-platform value, e.g. '"Linux"'. Travels with the session because the client
    #: hints must agree with the overridden User-Agent; see ImpersonateFetcher.
    sec_ch_ua_platform: str = '"macOS"' 

    minted_at: float = field(default_factory=time.time)
    ttl_s: float = 900.0

    # How many pages this session has served, so its longevity can be measured rather than guessed.
    uses: int = 0

    def cookie_dict(self) -> dict[str, str]:
        return {c["name"]: c["value"] for c in self.cookies}

    def age_s(self) -> float:
        return time.time() - self.minted_at

    def is_expired(self) -> bool:
        return self.age_s() >= self.ttl_s

    def key(self) -> tuple[str, str]:
        """Cache key. Host alone is not enough: a jar from profile A is void under profile B."""
        return (self.host, self.profile_id)

    def summary(self) -> dict:
        return {
            "host": self.host,
            "profile_id": self.profile_id,
            "cookies": len(self.cookies),
            "has_csrf": bool(self.csrf_token),
            "impersonate": self.impersonate_key,
            "platform": self.sec_ch_ua_platform.strip('"'),
            "age_s": round(self.age_s()),
            "uses": self.uses,
        }
