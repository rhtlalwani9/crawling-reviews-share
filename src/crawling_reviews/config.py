"""Environment-driven configuration. Every value has a working default."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SRC_ROOT.parent.parent


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return raw.lower() in ("1", "true", "yes") if raw not in (None, "") else default


@dataclass(frozen=True)
class ProfileConfig:
    """Pool sizing and the health policy that decides when a profile rests or retires."""

    pool_size: int = _int("PROFILE_POOL_SIZE", 2)
    data_dir: Path = SRC_ROOT / "profiles" / "_data"

    # Sliding window used to judge health. Blocks are only meaningful relative to a recent window:
    # three blocks in ten minutes is a burned profile, three blocks over two days is normal wear.
    block_window_s: float = _float("PROFILE_BLOCK_WINDOW_S", 600.0)
    block_threshold: int = _int("PROFILE_BLOCK_THRESHOLD", 3)

    # Rest duration, and how many rests a profile gets before it is retired for good.
    cooldown_s: float = _float("PROFILE_COOLDOWN_S", 900.0)
    max_cooldowns: int = _int("PROFILE_MAX_COOLDOWNS", 3)

    # A lease is stale when the holder stops sending heartbeats for this long.
    lease_timeout_s: float = _float("PROFILE_LEASE_TIMEOUT_S", 300.0)
    reaper_interval_s: float = _float("PROFILE_REAPER_INTERVAL_S", 30.0)

    # How long acquire() waits for a profile before giving up.
    acquire_timeout_s: float = _float("PROFILE_ACQUIRE_TIMEOUT_S", 60.0)


@dataclass(frozen=True)
class FetchConfig:
    timeout_s: float = _float("FETCH_TIMEOUT_S", 30.0)
    max_retries: int = _int("FETCH_MAX_RETRIES", 3)
    backoff_base_s: float = _float("FETCH_BACKOFF_BASE_S", 0.5)
    backoff_max_s: float = _float("FETCH_BACKOFF_MAX_S", 8.0)
    rate_per_second: float = _float("FETCH_RATE_PER_SECOND", 1.0)
    burst: int = _int("FETCH_BURST", 2)


@dataclass(frozen=True)
class SessionConfig:
    ttl_s: float = _float("SESSION_TTL_S", 900.0)          # stay inside cf_clearance's lifetime
    mint_settle_s: float = _float("SESSION_MINT_SETTLE_S", 6.0)
    headless: bool = _bool("BROWSER_HEADLESS", False)      # headless is a signal on protected sites

    # How long to wait for the challenge to resolve.
    wait_timeout_s: float = _float("SESSION_WAIT_TIMEOUT_S", 60.0)

    # Human-like pointer activity during the mint, and how many interact-then-reload cycles to try.
    humanize: bool = _bool("SESSION_HUMANIZE", True)
    challenge_attempts: int = _int("SESSION_CHALLENGE_ATTEMPTS", 3)

    # On a failed mint, save the rendered page and a screenshot. Cheap, and the difference between
    # "we were blocked" and "the challenge was still running" is only visible in the page itself.
    capture_failures: bool = _bool("SESSION_CAPTURE_FAILURES", True)


@dataclass(frozen=True)
class CrawlConfig:
    max_pages: int = _int("CRAWL_MAX_PAGES", 200)
    max_profile_attempts: int = _int("CRAWL_MAX_PROFILE_ATTEMPTS", 2)


@dataclass(frozen=True)
class Config:
    profiles: ProfileConfig = ProfileConfig()
    fetch: FetchConfig = FetchConfig()
    session: SessionConfig = SessionConfig()
    crawl: CrawlConfig = CrawlConfig()
    port: int = _int("PORT", 8080)
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


config = Config()
