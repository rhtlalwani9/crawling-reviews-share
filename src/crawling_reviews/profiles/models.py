"""Profile model and lifecycle."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any


class ProfileState(str, Enum):
    AVAILABLE = "available"
    LEASED = "leased"
    COOLDOWN = "cooldown"
    RETIRED = "retired"


class BlockKind(str, Enum):
    """Why a request failed, because the response should differ."""

    HARD_403 = "hard_403"          # explicit refusal
    CHALLENGE = "challenge"        # interstitial / JS challenge served
    SHADOW = "shadow"              # 200 with wrong or empty content
    RATE_LIMITED = "rate_limited"  # 429 — back off, do not strike


STRIKE_KINDS = {BlockKind.HARD_403, BlockKind.CHALLENGE, BlockKind.SHADOW}


@dataclass
class Profile:
    """One leasable browser identity. Mutable: this is state, unlike Signature."""

    id: str
    signature_id: str
    user_data_dir: str

    state: ProfileState = ProfileState.AVAILABLE
    created_at: float = field(default_factory=time.time)

    # --- lease bookkeeping ---------------------------------------------------------------------
    leased_at: float | None = None
    leased_by: str | None = None          # opaque holder id, for diagnostics
    last_heartbeat_at: float | None = None

    # --- health, as event timestamps ------------------------------------------------------------
    success_events: list[float] = field(default_factory=list)
    block_events: list[tuple[float, str]] = field(default_factory=list)

    # --- cooldown -------------------------------------------------------------------------------
    cooldown_until: float | None = None
    cooldown_count: int = 0                # how many times this profile has been rested
    retired_at: float | None = None
    retired_reason: str | None = None

    # --- lifetime totals (cheap to read, survive window trimming) -------------------------------
    total_successes: int = 0
    total_blocks: int = 0
    total_mints: int = 0                   # browser launches this profile has paid for
    last_success_at: float | None = None
    last_block_at: float | None = None
    last_used_at: float | None = None

    # ------------------------------------------------------------------ derived state

    def age_seconds(self) -> float:
        return time.time() - self.created_at

    def blocks_in_window(self, window_s: float, now: float | None = None) -> int:
        """Strike-worthy blocks inside the sliding window. Rate limits are excluded by design."""
        now = now or time.time()
        cutoff = now - window_s
        return sum(1 for ts, kind in self.block_events if ts >= cutoff and kind in {k.value for k in STRIKE_KINDS})

    def successes_in_window(self, window_s: float, now: float | None = None) -> int:
        now = now or time.time()
        cutoff = now - window_s
        return sum(1 for ts in self.success_events if ts >= cutoff)

    def is_cooldown_expired(self, now: float | None = None) -> bool:
        if self.state is not ProfileState.COOLDOWN or self.cooldown_until is None:
            return False
        return (now or time.time()) >= self.cooldown_until

    def is_lease_stale(self, lease_timeout_s: float, now: float | None = None) -> bool:
        """A lease is stale when the holder has gone quiet for longer than the timeout."""
        if self.state is not ProfileState.LEASED:
            return False
        now = now or time.time()
        last_signal = self.last_heartbeat_at or self.leased_at or 0.0
        return (now - last_signal) > lease_timeout_s

    def health_score(self) -> float:
        """Rough desirability, used to pick the best available profile."""
        recent_blocks = self.blocks_in_window(600)
        recent_ok = self.successes_in_window(600)
        age_bonus = min(self.age_seconds() / 3600.0, 5.0)      # up to +5 for a 5h-old profile
        return age_bonus + recent_ok - (recent_blocks * 3.0) - (self.cooldown_count * 1.5)

    # ------------------------------------------------------------------ mutation helpers

    def trim_events(self, keep_window_s: float = 3600.0, max_events: int = 200) -> None:
        """Keep event lists bounded. Called on every write."""
        cutoff = time.time() - keep_window_s
        self.success_events = [t for t in self.success_events if t >= cutoff][-max_events:]
        self.block_events = [(t, k) for t, k in self.block_events if t >= cutoff][-max_events:]

    # ------------------------------------------------------------------ persistence

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        # tuples do not survive a JSON round-trip as tuples
        data["block_events"] = [[t, k] for t, k in self.block_events]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        data = dict(data)
        data["state"] = ProfileState(data["state"])
        data["block_events"] = [(t, k) for t, k in data.get("block_events", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def data_dir_path(self) -> Path:
        return Path(self.user_data_dir)
