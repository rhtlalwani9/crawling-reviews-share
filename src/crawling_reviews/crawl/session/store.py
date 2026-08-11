"""Session cache, keyed by (host, profile_id)."""
from __future__ import annotations

import threading

from ...core.logging import get_logger
from .models import Session

log = get_logger(__name__)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], Session] = {}
        self._lock = threading.RLock()

    def get(self, host: str, profile_id: str) -> Session | None:
        """Return a live session, or None. Expired entries are evicted on read."""
        with self._lock:
            session = self._sessions.get((host, profile_id))
            if session is None:
                return None
            if session.is_expired():
                del self._sessions[(host, profile_id)]
                log.debug("session expired", extra_fields=session.summary())
                return None
            return session

    def put(self, session: Session) -> None:
        with self._lock:
            self._sessions[session.key()] = session

    def invalidate(self, host: str, profile_id: str) -> None:
        """Drop a session after a block: whatever it held is no longer trusted."""
        with self._lock:
            if self._sessions.pop((host, profile_id), None) is not None:
                log.info("session invalidated", extra_fields={"host": host, "profile_id": profile_id})

    def invalidate_profile(self, profile_id: str) -> None:
        """Drop every session for a profile — used when the profile itself is cooled down."""
        with self._lock:
            for key in [k for k in self._sessions if k[1] == profile_id]:
                del self._sessions[key]

    def describe(self) -> list[dict]:
        with self._lock:
            return [s.summary() for s in self._sessions.values()]
