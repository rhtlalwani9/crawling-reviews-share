"""Per-host token bucket."""
from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, rate_per_second: float = 1.0, burst: int = 2):
        self.rate = rate_per_second
        self.burst = burst
        self._state: dict[str, tuple[float, float]] = {}   # key -> (tokens, last_refill)
        self._lock = threading.Lock()

    def acquire(self, key: str) -> float:
        """Block until a token is free for `key`. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                tokens, last = self._state.get(key, (float(self.burst), time.monotonic()))
                now = time.monotonic()
                tokens = min(self.burst, tokens + (now - last) * self.rate)
                if tokens >= 1.0:
                    self._state[key] = (tokens - 1.0, now)
                    return waited
                deficit = 1.0 - tokens
                self._state[key] = (tokens, now)
            sleep_for = deficit / self.rate
            time.sleep(sleep_for)
            waited += sleep_for
