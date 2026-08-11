"""Profile-pool policy tests."""

from __future__ import annotations

import time

from pathlib import Path

import pytest

from crawling_reviews.config import ProfileConfig
from crawling_reviews.core.errors import NoProfileAvailableError
from crawling_reviews.profiles.manager import ProfileManager
from crawling_reviews.profiles.models import BlockKind, Profile, ProfileState
from crawling_reviews.profiles.store import ProfileStore, clear_stale_singleton_locks


@pytest.fixture()
def manager(tmp_path):
    """A pool of 2 with a fast, deterministic policy."""
    cfg = ProfileConfig(
        pool_size=2,
        data_dir=tmp_path / "profiles",
        block_window_s=60.0,
        block_threshold=3,
        cooldown_s=5.0,
        max_cooldowns=2,
        lease_timeout_s=1.0,
        reaper_interval_s=999.0,      # reaper driven manually in tests
        acquire_timeout_s=0.2,        # fail fast instead of blocking the suite
    )
    return ProfileManager(cfg=cfg, store=ProfileStore(cfg.data_dir))


# --- pool construction --------------------------------------------------------------------------

def test_pool_fills_to_target_size(manager):
    assert manager.stats().total == 2
    assert manager.stats().available == 2


def test_profiles_are_spread_across_signatures(manager):
    sig_ids = {p["signature_id"] for p in manager.describe()}
    # Two signatures are active, so a pool of two should use one each rather than doubling up:
    # two profiles on the same signature are near-identical to a detector.
    assert len(sig_ids) == 2


def test_each_profile_gets_its_own_user_data_dir(manager):
    dirs = {manager._profiles[p["id"]].user_data_dir for p in manager.describe()}
    assert len(dirs) == 2, "Chrome takes a SingletonLock per directory; sharing is impossible"


# --- leasing ------------------------------------------------------------------------------------

def test_acquire_marks_leased_and_release_returns_it(manager):
    profile = manager.acquire(holder="test")
    assert profile.state is ProfileState.LEASED
    assert manager.stats().leased == 1

    manager.release(profile.id)
    assert manager.stats().leased == 0
    assert manager.stats().available == 2


def test_a_leased_profile_is_not_handed_out_twice(manager):
    first = manager.acquire(holder="a")
    second = manager.acquire(holder="b")
    assert first.id != second.id


def test_exhausted_pool_raises_rather_than_hanging(manager):
    manager.acquire(holder="a")
    manager.acquire(holder="b")
    with pytest.raises(NoProfileAvailableError):
        manager.acquire(holder="c")


def test_release_is_idempotent(manager):
    profile = manager.acquire(holder="a")
    manager.release(profile.id)
    manager.release(profile.id)          # must not raise or corrupt the count
    assert manager.stats().available == 2


# --- health policy ------------------------------------------------------------------------------

def test_single_block_does_not_cool_down_a_profile(manager):
    profile = manager.acquire(holder="a")
    manager.report_blocked(profile.id, BlockKind.HARD_403)
    assert manager._profiles[profile.id].state is ProfileState.LEASED
    assert manager.stats().cooldown == 0


def test_threshold_strikes_in_window_trigger_cooldown(manager):
    profile = manager.acquire(holder="a")
    for _ in range(3):
        manager.report_blocked(profile.id, BlockKind.HARD_403)

    stored = manager._profiles[profile.id]
    assert stored.state is ProfileState.COOLDOWN
    assert stored.cooldown_count == 1
    assert stored.cooldown_until is not None


def test_rate_limiting_never_counts_as_a_strike(manager):
    profile = manager.acquire(holder="a")
    for _ in range(10):
        manager.report_blocked(profile.id, BlockKind.RATE_LIMITED)

    stored = manager._profiles[profile.id]
    assert stored.state is ProfileState.LEASED, "429 is a scheduler problem, not a burned identity"
    assert stored.total_blocks == 10        # still recorded
    assert stored.blocks_in_window(60.0) == 0


def test_blocks_outside_the_window_do_not_accumulate(manager):
    profile = manager.acquire(holder="a")
    stored = manager._profiles[profile.id]

    # Two strikes, but long enough ago to have aged out of the 60s window.
    old = time.time() - 120
    stored.block_events.extend([(old, BlockKind.HARD_403.value), (old, BlockKind.HARD_403.value)])

    manager.report_blocked(profile.id, BlockKind.HARD_403)   # only 1 strike inside the window
    assert manager._profiles[profile.id].state is ProfileState.LEASED


def test_cooldown_expires_back_to_available(manager):
    profile = manager.acquire(holder="a")
    for _ in range(3):
        manager.report_blocked(profile.id, BlockKind.HARD_403)
    assert manager._profiles[profile.id].state is ProfileState.COOLDOWN

    # Expire the rest period rather than sleeping for it.
    manager._profiles[profile.id].cooldown_until = time.time() - 1
    with manager._lock:
        promoted = manager._promote_expired_cooldowns_locked()

    assert promoted == 1
    assert manager._profiles[profile.id].state is ProfileState.AVAILABLE


def test_profile_retires_after_too_many_cooldowns(manager):
    profile = manager.acquire(holder="a")
    stored = manager._profiles[profile.id]

    # max_cooldowns=2, so the third cooldown attempt should retire it instead.
    for cycle in range(3):
        stored.block_events.clear()
        for _ in range(3):
            manager.report_blocked(profile.id, BlockKind.HARD_403)
        if stored.state is ProfileState.COOLDOWN:
            stored.cooldown_until = time.time() - 1
            with manager._lock:
                manager._promote_expired_cooldowns_locked()

    assert stored.state is ProfileState.RETIRED
    assert stored.retired_reason is not None


def test_retiring_a_profile_triggers_a_replacement(manager):
    before = manager.stats().total
    profile = manager.acquire(holder="a")
    stored = manager._profiles[profile.id]

    with manager._lock:
        manager._retire_locked(stored, reason="test")

    # Usable count is restored, so a retired identity does not shrink capacity.
    stats = manager.stats()
    assert stats.total == before + 1
    assert stats.available + stats.leased + stats.cooldown == manager.cfg.pool_size


# --- lease reclamation --------------------------------------------------------------------------

def test_stale_lease_is_reclaimed_when_heartbeat_stops(manager):
    profile = manager.acquire(holder="dead-worker")
    manager._profiles[profile.id].last_heartbeat_at = time.time() - 10   # silent > 1s timeout

    with manager._lock:
        reclaimed = manager._reap_stale_leases_locked()

    assert reclaimed == 1
    assert manager._profiles[profile.id].state is ProfileState.AVAILABLE


def test_heartbeat_keeps_a_long_lease_alive(manager):
    profile = manager.acquire(holder="slow-but-alive")
    manager._profiles[profile.id].leased_at = time.time() - 600   # a 10-minute crawl
    manager.heartbeat(profile.id)                                  # ...still checking in

    with manager._lock:
        reclaimed = manager._reap_stale_leases_locked()

    assert reclaimed == 0, "lease age must not be mistaken for a dead holder"


def test_reclaiming_a_lease_does_not_penalise_health(manager):
    profile = manager.acquire(holder="crashed")
    manager._profiles[profile.id].last_heartbeat_at = time.time() - 10
    with manager._lock:
        manager._reap_stale_leases_locked()

    stored = manager._profiles[profile.id]
    assert stored.total_blocks == 0, "a crashed holder says nothing about the identity"
    assert stored.cooldown_count == 0


# --- selection ----------------------------------------------------------------------------------

def test_healthier_profile_is_preferred(manager):
    a = manager.acquire(holder="a")
    b = manager.acquire(holder="b")
    manager.release(a.id)
    manager.release(b.id)

    # Give b a recent strike; a should then be chosen.
    manager.report_blocked(b.id, BlockKind.HARD_403)
    chosen = manager.acquire(holder="c")
    assert chosen.id == a.id


# --- persistence --------------------------------------------------------------------------------

def test_registry_survives_a_restart(manager, tmp_path):
    profile = manager.acquire(holder="a")
    manager.report_success(profile.id, minted=True)
    manager.release(profile.id)

    reloaded = ProfileManager(cfg=manager.cfg, store=ProfileStore(manager.cfg.data_dir))
    revived = reloaded._profiles[profile.id]

    assert revived.total_successes == 1
    assert revived.total_mints == 1
    assert revived.state is ProfileState.AVAILABLE


def test_leases_held_by_a_dead_process_are_released_on_startup(manager):
    profile = manager.acquire(holder="process-that-died")
    assert manager._profiles[profile.id].state is ProfileState.LEASED

    reloaded = ProfileManager(cfg=manager.cfg, store=ProfileStore(manager.cfg.data_dir))
    assert reloaded._profiles[profile.id].state is ProfileState.AVAILABLE


# --- storage layout and Chrome's singleton lock --------------------------------------------------


def test_stored_path_is_ignored_in_favour_of_the_scoped_location(tmp_path):
    """A registry written before storage was scoped per platform names directories outside the scoped…"""
    store = ProfileStore(tmp_path, platform="Linux")
    stale = Profile(id="prof-1", signature_id="sig-x",
                    user_data_dir="/app/somewhere/old-layout/chrome-profiles/prof-1")
    store.save_all([stale])

    loaded = store.load_all()[0]

    assert Path(loaded.user_data_dir) == tmp_path / "Linux" / "chrome-profiles" / "prof-1"


def test_a_lock_from_another_host_is_cleared(tmp_path):
    """Docker gives each container a new hostname, so a lock written by the previous container names…"""
    profile_dir = tmp_path / "prof-1"
    profile_dir.mkdir()
    (profile_dir / "SingletonLock").symlink_to("a9d347f52332-292")   # a dead container
    (profile_dir / "SingletonCookie").symlink_to("12345")

    assert clear_stale_singleton_locks(profile_dir) is True
    assert not (profile_dir / "SingletonLock").exists()
    assert not (profile_dir / "SingletonCookie").exists()


def test_a_lock_held_by_a_live_local_process_is_left_alone(tmp_path):
    """The paranoia in Chrome's lock is warranted — two writers really can corrupt a profile."""
    import os
    import socket

    profile_dir = tmp_path / "prof-1"
    profile_dir.mkdir()
    (profile_dir / "SingletonLock").symlink_to(f"{socket.gethostname()}-{os.getpid()}")

    assert clear_stale_singleton_locks(profile_dir) is False
    assert (profile_dir / "SingletonLock").is_symlink()


def test_no_lock_present_is_not_an_error(tmp_path):
    profile_dir = tmp_path / "prof-1"
    profile_dir.mkdir()
    assert clear_stale_singleton_locks(profile_dir) is False
