"""Profile pool manager."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

from ..config import ProfileConfig, config
from ..core.errors import NoProfileAvailableError
from ..core.logging import get_logger
from .models import BlockKind, Profile, ProfileState, STRIKE_KINDS
from .signatures import Signature, active_signatures, get_signature
from .store import ProfileStore

log = get_logger(__name__)


@dataclass
class PoolStats:
    """Snapshot for the API and for diagnostics."""

    total: int
    available: int
    leased: int
    cooldown: int
    retired: int
    target_size: int
    signatures_available: int


class ProfileManager:
    """The pool."""

    def __init__(self, cfg: ProfileConfig | None = None, store: ProfileStore | None = None):
        self.cfg = cfg or config.profiles
        self.store = store or ProfileStore(self.cfg.data_dir)

        self._lock = threading.RLock()
        self._available = threading.Condition(self._lock)   # signalled when a profile frees up
        self._profiles: dict[str, Profile] = {p.id: p for p in self.store.load_all()}

        self._reaper_stop = threading.Event()
        self._reaper: threading.Thread | None = None

        with self._lock:
            self._recover_leases_on_startup()
            self._retire_unlaunchable_locked()
            self._ensure_pool_size_locked()
            self._persist_locked()

        log.info("profile pool ready", extra_fields=self.stats().__dict__)

    # ================================================================== public API

    def acquire(self, holder: str | None = None, timeout_s: float | None = None) -> Profile:
        """Lease the healthiest available profile, waiting if the pool is momentarily empty."""
        holder = holder or f"holder-{uuid.uuid4().hex[:8]}"
        timeout_s = self.cfg.acquire_timeout_s if timeout_s is None else timeout_s
        deadline = time.time() + timeout_s

        with self._available:
            while True:
                self._promote_expired_cooldowns_locked()
                self._reap_stale_leases_locked()
                self._ensure_pool_size_locked()

                candidate = self._pick_best_available_locked()
                if candidate is not None:
                    now = time.time()
                    candidate.state = ProfileState.LEASED
                    candidate.leased_at = now
                    candidate.leased_by = holder
                    candidate.last_heartbeat_at = now
                    candidate.last_used_at = now
                    self._persist_locked()
                    log.info(
                        "profile leased",
                        extra_fields={
                            "profile_id": candidate.id,
                            "signature_id": candidate.signature_id,
                            "holder": holder,
                            "health": round(candidate.health_score(), 2),
                        },
                    )
                    return candidate

                remaining = deadline - time.time()
                if remaining <= 0:
                    stats = self.stats()
                    raise NoProfileAvailableError(
                        f"no profile available after {timeout_s:.0f}s "
                        f"(leased={stats.leased} cooldown={stats.cooldown} retired={stats.retired})"
                    )
                # Wake early if someone releases; otherwise re-check periodically so an expiring
                # cooldown is noticed even with no release traffic.
                self._available.wait(timeout=min(remaining, 5.0))

    def release(self, profile_id: str) -> None:
        """Return a leased profile. Idempotent, and safe to call from a finally block."""
        with self._available:
            profile = self._profiles.get(profile_id)
            if profile is None:
                return

            # A profile put into COOLDOWN or RETIRED while leased must not be resurrected here.
            if profile.state is ProfileState.LEASED:
                profile.state = ProfileState.AVAILABLE

            profile.leased_at = None
            profile.leased_by = None
            profile.last_heartbeat_at = None
            self._ensure_pool_size_locked()
            self._persist_locked()
            self._available.notify_all()
            log.debug("profile released", extra_fields={"profile_id": profile_id, "state": profile.state.value})

    def heartbeat(self, profile_id: str) -> None:
        """Signal that the holder is still alive."""
        with self._lock:
            profile = self._profiles.get(profile_id)
            if profile and profile.state is ProfileState.LEASED:
                profile.last_heartbeat_at = time.time()

    def report_success(self, profile_id: str, *, minted: bool = False) -> None:
        """Record a good outcome. `minted` marks that this profile paid for a browser launch."""
        with self._lock:
            profile = self._profiles.get(profile_id)
            if profile is None:
                return
            now = time.time()
            profile.success_events.append(now)
            profile.total_successes += 1
            profile.last_success_at = now
            profile.last_used_at = now
            if minted:
                profile.total_mints += 1
            profile.trim_events()
            self._persist_locked()

    def report_blocked(self, profile_id: str, kind: BlockKind = BlockKind.HARD_403) -> Profile | None:
        """Record a block and apply the health policy."""
        with self._available:
            profile = self._profiles.get(profile_id)
            if profile is None:
                return None

            now = time.time()
            profile.block_events.append((now, kind.value))
            profile.total_blocks += 1
            profile.last_block_at = now
            profile.trim_events()

            if kind not in STRIKE_KINDS:
                # 429: back off, but do not blame the identity.
                log.info(
                    "rate limited, not counted as a strike",
                    extra_fields={"profile_id": profile_id, "kind": kind.value},
                )
                self._persist_locked()
                return profile

            strikes = profile.blocks_in_window(self.cfg.block_window_s, now)
            log.warning(
                "profile blocked",
                extra_fields={
                    "profile_id": profile_id,
                    "kind": kind.value,
                    "strikes_in_window": strikes,
                    "threshold": self.cfg.block_threshold,
                },
            )

            if strikes >= self.cfg.block_threshold:
                self._send_to_cooldown_locked(profile, reason=f"{strikes} strikes in {self.cfg.block_window_s:.0f}s")

            self._persist_locked()
            self._available.notify_all()
            return profile

    def stats(self) -> PoolStats:
        with self._lock:
            by_state: dict[ProfileState, int] = {s: 0 for s in ProfileState}
            for p in self._profiles.values():
                by_state[p.state] += 1
            return PoolStats(
                total=len(self._profiles),
                available=by_state[ProfileState.AVAILABLE],
                leased=by_state[ProfileState.LEASED],
                cooldown=by_state[ProfileState.COOLDOWN],
                retired=by_state[ProfileState.RETIRED],
                target_size=self.cfg.pool_size,
                signatures_available=len(active_signatures()),
            )

    def describe(self) -> list[dict]:
        """Per-profile detail for the diagnostics endpoint."""
        with self._lock:
            now = time.time()
            out = []
            for p in sorted(self._profiles.values(), key=lambda x: x.created_at):
                out.append({
                    "id": p.id,
                    "signature_id": p.signature_id,
                    "state": p.state.value,
                    "age_s": round(p.age_seconds()),
                    "health": round(p.health_score(), 2),
                    "successes": p.total_successes,
                    "blocks": p.total_blocks,
                    "mints": p.total_mints,
                    "strikes_in_window": p.blocks_in_window(self.cfg.block_window_s, now),
                    "cooldown_count": p.cooldown_count,
                    "cooldown_remaining_s": (
                        round(max(0.0, p.cooldown_until - now)) if p.cooldown_until else None
                    ),
                    "leased_by": p.leased_by,
                    "retired_reason": p.retired_reason,
                })
            return out

    # ================================================================== background reaper

    def start_reaper(self) -> None:
        """Start the background thread that reclaims stale leases and expires cooldowns."""
        if self._reaper and self._reaper.is_alive():
            return
        self._reaper_stop.clear()
        self._reaper = threading.Thread(target=self._reaper_loop, name="profile-reaper", daemon=True)
        self._reaper.start()
        log.info("reaper started", extra_fields={"interval_s": self.cfg.reaper_interval_s})

    def stop_reaper(self) -> None:
        self._reaper_stop.set()
        if self._reaper:
            self._reaper.join(timeout=2.0)

    def _reaper_loop(self) -> None:
        while not self._reaper_stop.wait(self.cfg.reaper_interval_s):
            try:
                with self._available:
                    reclaimed = self._reap_stale_leases_locked()
                    promoted = self._promote_expired_cooldowns_locked()
                    created = self._ensure_pool_size_locked()
                    if reclaimed or promoted or created:
                        self._persist_locked()
                        self._available.notify_all()
            except Exception:  # a reaper that dies silently is worse than one that logs
                log.exception("reaper iteration failed")

    # ================================================================== internals (lock held)

    def _pick_best_available_locked(self) -> Profile | None:
        """Highest health score wins."""
        available = [p for p in self._profiles.values() if p.state is ProfileState.AVAILABLE]
        if not available:
            return None
        return max(available, key=lambda p: p.health_score())

    def _send_to_cooldown_locked(self, profile: Profile, *, reason: str) -> None:
        """Rest a profile, or retire it if it has already been rested too often."""
        profile.cooldown_count += 1

        if profile.cooldown_count > self.cfg.max_cooldowns:
            self._retire_locked(profile, reason=f"exceeded {self.cfg.max_cooldowns} cooldowns ({reason})")
            return

        profile.state = ProfileState.COOLDOWN
        profile.cooldown_until = time.time() + self.cfg.cooldown_s
        profile.leased_at = None
        profile.leased_by = None
        profile.last_heartbeat_at = None
        log.warning(
            "profile sent to cooldown",
            extra_fields={
                "profile_id": profile.id,
                "reason": reason,
                "cooldown_s": self.cfg.cooldown_s,
                "cooldown_count": profile.cooldown_count,
            },
        )
        self._ensure_pool_size_locked()

    def _retire_locked(self, profile: Profile, *, reason: str) -> None:
        profile.state = ProfileState.RETIRED
        profile.retired_at = time.time()
        profile.retired_reason = reason
        profile.cooldown_until = None
        profile.leased_at = None
        profile.leased_by = None
        log.warning("profile retired", extra_fields={"profile_id": profile.id, "reason": reason})

        # The Chrome directory is only useful while the profile lives; reclaim the disk.
        self.store.destroy_user_data_dir(profile)
        self._ensure_pool_size_locked()

    def _promote_expired_cooldowns_locked(self) -> int:
        """COOLDOWN -> AVAILABLE once the rest period has elapsed."""
        promoted = 0
        for profile in self._profiles.values():
            if profile.is_cooldown_expired():
                profile.state = ProfileState.AVAILABLE
                profile.cooldown_until = None
                promoted += 1
                log.info(
                    "profile back from cooldown",
                    extra_fields={"profile_id": profile.id, "cooldown_count": profile.cooldown_count},
                )
        return promoted

    def _reap_stale_leases_locked(self) -> int:
        """Reclaim leases whose holder stopped heartbeating."""
        reclaimed = 0
        for profile in self._profiles.values():
            if profile.is_lease_stale(self.cfg.lease_timeout_s):
                log.warning(
                    "reclaiming stale lease",
                    extra_fields={
                        "profile_id": profile.id,
                        "holder": profile.leased_by,
                        "silent_for_s": round(time.time() - (profile.last_heartbeat_at or profile.leased_at or 0)),
                    },
                )
                profile.state = ProfileState.AVAILABLE
                profile.leased_at = None
                profile.leased_by = None
                profile.last_heartbeat_at = None
                reclaimed += 1
        return reclaimed

    def _retire_unlaunchable_locked(self) -> None:
        """Retire profiles whose signature cannot be launched here."""
        available_ids = {s.id for s in active_signatures()}
        for profile in self._profiles.values():
            if profile.state is ProfileState.RETIRED:
                continue
            if profile.signature_id not in available_ids:
                log.warning(
                    "retiring profile: its signature is not launchable on this platform",
                    extra_fields={"profile_id": profile.id, "signature_id": profile.signature_id},
                )
                profile.state = ProfileState.RETIRED
                profile.retired_at = time.time()
                profile.retired_reason = f"signature {profile.signature_id} unavailable here"
                profile.leased_at = None
                profile.leased_by = None

    def _recover_leases_on_startup(self) -> None:
        """Anything still marked LEASED in the registry belonged to a process that no longer exists."""
        for profile in self._profiles.values():
            if profile.state is ProfileState.LEASED:
                log.info("releasing lease held by a previous process", extra_fields={"profile_id": profile.id})
                profile.state = ProfileState.AVAILABLE
                profile.leased_at = None
                profile.leased_by = None
                profile.last_heartbeat_at = None

    def _ensure_pool_size_locked(self) -> int:
        """Top the pool up to `pool_size` *usable* profiles."""
        usable = [
            p for p in self._profiles.values()
            if p.state in (ProfileState.AVAILABLE, ProfileState.LEASED, ProfileState.COOLDOWN)
        ]
        deficit = self.cfg.pool_size - len(usable)
        if deficit <= 0:
            return 0

        signatures = active_signatures()
        if not signatures:
            log.error("no launchable signatures; cannot grow the pool")
            return 0

        created = 0
        for _ in range(deficit):
            signature = self._least_used_signature_locked(signatures)
            self._create_profile_locked(signature)
            created += 1
        return created

    def _least_used_signature_locked(self, signatures: list[Signature]) -> Signature:
        """Spread profiles across signatures."""
        live_by_sig: dict[str, int] = {s.id: 0 for s in signatures}
        for p in self._profiles.values():
            if p.state is not ProfileState.RETIRED and p.signature_id in live_by_sig:
                live_by_sig[p.signature_id] += 1
        return min(signatures, key=lambda s: live_by_sig[s.id])

    def _create_profile_locked(self, signature: Signature) -> Profile:
        profile_id = f"prof-{signature.chrome_version}-{uuid.uuid4().hex[:8]}"
        user_data_dir = self.store.allocate_user_data_dir(profile_id)

        profile = Profile(
            id=profile_id,
            signature_id=signature.id,
            user_data_dir=str(user_data_dir),
        )
        self._profiles[profile.id] = profile
        log.info(
            "profile created",
            extra_fields={"profile_id": profile.id, "signature_id": signature.id, "dir": str(user_data_dir)},
        )
        return profile

    def _persist_locked(self) -> None:
        self.store.save_all(list(self._profiles.values()))

    # ------------------------------------------------------------------ helpers for consumers

    def signature_for(self, profile: Profile) -> Signature:
        """The immutable template behind a profile — needed to launch a browser or pick impersonation."""
        return get_signature(profile.signature_id)
