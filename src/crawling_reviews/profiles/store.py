"""Profile persistence."""

from __future__ import annotations

import json
import logging
import os
import socket
import tempfile
import threading
from pathlib import Path

from .models import Profile
from .signatures import detect_platform

log = logging.getLogger(__name__)


def clear_stale_singleton_locks(user_data_dir: Path | str) -> bool:
    """Remove a `SingletonLock` left behind by a process that no longer exists."""
    directory = Path(user_data_dir)
    lock = directory / "SingletonLock"

    try:
        target = os.readlink(lock)
    except OSError:
        return False        # absent, or a plain file we should not interpret

    # Target is "hostname-pid"; rpartition so hostnames containing '-' survive.
    host, _, pid_text = target.rpartition("-")
    if host == socket.gethostname() and pid_text.isdigit():
        try:
            os.kill(int(pid_text), 0)
            log.debug("SingletonLock is held by live pid %s on this host; leaving it alone", pid_text)
            return False
        except (OSError, ProcessLookupError):
            pass            # recorded pid is gone, so the lock is stale

    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (directory / name).unlink(missing_ok=True)

    log.info(
        "cleared a stale Chrome SingletonLock (owner %r is not a live process here) in %s",
        target, directory.name,
    )
    return True


class ProfileStore:
    """Thread-safe JSON-backed registry of profiles, scoped per platform."""

    def __init__(self, data_dir: Path, platform: str | None = None):
        self.platform = platform or detect_platform()
        self.root_dir = Path(data_dir)
        self.data_dir = self.root_dir / self.platform
        self.registry_path = self.data_dir / "registry.json"
        self.profiles_dir = self.data_dir / "chrome-profiles"
        self._lock = threading.RLock()

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self._warn_about_legacy_layout()

    def _warn_about_legacy_layout(self) -> None:
        """Flag data left by the earlier flat layout instead of moving it."""
        legacy_registry = self.root_dir / "registry.json"
        legacy_profiles = self.root_dir / "chrome-profiles"
        if legacy_registry.exists() or legacy_profiles.exists():
            import logging
            logging.getLogger(__name__).warning(
                "found profile data in the old un-scoped layout at %s. It is ignored. If those "
                "profiles were created on %s, move them into %s to keep their accumulated trust.",
                self.root_dir, self.platform, self.data_dir,
            )

    # ------------------------------------------------------------------ registry

    def load_all(self) -> list[Profile]:
        with self._lock:
            if not self.registry_path.exists():
                return []
            try:
                raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                # A corrupt registry must not be fatal: profiles are rebuildable, and refusing to
                # start would be a worse outcome than losing accumulated stats.
                return []
            return [self._rebase(Profile.from_dict(item)) for item in raw.get("profiles", [])]

    def _rebase(self, profile: Profile) -> Profile:
        """Point the profile at where its directory belongs *now*, ignoring the stored path."""
        expected = self.profiles_dir / profile.id
        if Path(profile.user_data_dir) != expected:
            log.info("rebased %s: %s -> %s", profile.id, profile.user_data_dir, expected)
            profile.user_data_dir = str(expected)
        return profile

    def save_all(self, profiles: list[Profile]) -> None:
        """Atomic write: serialise to a temp file in the same directory, then rename."""
        with self._lock:
            payload = {"version": 1, "profiles": [p.to_dict() for p in profiles]}
            fd, tmp_path = tempfile.mkstemp(dir=self.data_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
                os.replace(tmp_path, self.registry_path)
            except Exception:
                Path(tmp_path).unlink(missing_ok=True)
                raise

    # ------------------------------------------------------------------ chrome user-data dirs

    def allocate_user_data_dir(self, profile_id: str) -> Path:
        """Create the Chrome user-data directory for a profile."""
        path = self.profiles_dir / profile_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def destroy_user_data_dir(self, profile: Profile) -> None:
        """Remove a retired profile's directory. Best-effort: a leftover directory is harmless."""
        import shutil

        path = Path(profile.user_data_dir)
        if path.exists() and path.is_relative_to(self.profiles_dir):
            shutil.rmtree(path, ignore_errors=True)

    def dir_size_bytes(self, profile: Profile) -> int:
        """Profiles grow (cache, service workers, IndexedDB); surfaced so growth is visible."""
        path = Path(profile.user_data_dir)
        if not path.exists():
            return 0
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
