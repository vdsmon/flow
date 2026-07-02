"""Contract tests for lease.py.

The lease is a per-ticket mutex, not a liveness checker: identity is run_id + boot_id + hostname
compared under a flock. All logic tests inject current_boot/hostname/cwd/now explicitly so nothing
touches real platform state. The contention test uses multiprocessing("spawn") (threads can't show
POSIX flock; the GIL hides it) with a fixed large TTL so exactly one of two foreign-run_id acquirers
wins.
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

import lease

# ─── Helpers ─────────────────────────────────────────────────────────────────

NOW = "2026-05-28T12:00:00Z"
LATER = "2026-05-28T12:10:00Z"  # 10 min after NOW
TTL = 300  # expiry = NOW + 5 min = 12:05:00Z


def _acquire(
    ticket_dir: Path,
    run_id: str,
    *,
    now: str = NOW,
    ttl: int = TTL,
    stage: str | None = None,
    boot: str = "boot-A",
    host: str = "host-1",
    cwd: str = "/work",
    session_nonce: str | None = None,
    force: bool = False,
) -> lease.Lease:
    return lease.acquire(
        ticket_dir,
        run_id,
        ttl,
        now,
        stage=stage,
        current_boot=boot,
        hostname=host,
        cwd=cwd,
        session_nonce=session_nonce,
        force=force,
    )


# ─── acquire: free dir ─────────────────────────────────────────────────────────


def test_acquire_on_free_dir_writes_lease(tmp_path: Path) -> None:
    ls = _acquire(tmp_path, "run-1", stage="implement")
    assert ls.run_id == "run-1"
    assert ls.stage == "implement"
    assert ls.acquired_at == NOW
    assert ls.lease_expires_at == "2026-05-28T12:05:00Z"
    assert lease.run_lock_path(tmp_path).exists()
    on_disk = lease.read_lease(tmp_path)
    assert on_disk is not None
    assert on_disk.run_id == "run-1"


# ─── acquire: owner re-acquire refreshes ───────────────────────────────────────


def test_same_run_id_reacquire_refreshes(tmp_path: Path) -> None:
    first = _acquire(tmp_path, "run-1", stage="plan")
    second = _acquire(tmp_path, "run-1", now=LATER, stage="implement")
    assert second.run_id == "run-1"
    # acquired_at preserved across owner re-acquire; expiry/stage move forward.
    assert second.acquired_at == first.acquired_at == NOW
    assert second.lease_expires_at == "2026-05-28T12:15:00Z"
    assert second.stage == "implement"


def test_owner_reacquire_succeeds_even_when_expired(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", now=NOW)  # expires 12:05
    after_expiry = "2026-05-28T13:00:00Z"
    ls = _acquire(tmp_path, "run-1", now=after_expiry)
    assert ls.run_id == "run-1"
    assert ls.acquired_at == NOW  # original acquired_at kept


# ─── acquire: foreign live ─────────────────────────────────────────────────────


def test_foreign_live_raises_lease_held(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1")
    with pytest.raises(lease.LeaseHeld) as exc:
        _acquire(tmp_path, "run-2", now=NOW)
    assert exc.value.holder.run_id == "run-1"


# ─── acquire: foreign expired, same boot ───────────────────────────────────────


def test_foreign_expired_same_boot_raises_expired_foreign(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A")  # expires 12:05
    after = "2026-05-28T13:00:00Z"
    with pytest.raises(lease.LeaseExpiredForeign) as exc:
        _acquire(tmp_path, "run-2", now=after, boot="boot-A")
    assert exc.value.holder.run_id == "run-1"


def test_foreign_expired_empty_boot_is_not_reboot_clearable(tmp_path: Path) -> None:
    # empty boot ids must fall through to force/else, never silently steal.
    _acquire(tmp_path, "run-1", boot="")
    after = "2026-05-28T13:00:00Z"
    with pytest.raises(lease.LeaseExpiredForeign):
        _acquire(tmp_path, "run-2", now=after, boot="")


# ─── acquire: foreign expired, different boot (reboot-clearable) ────────────────


def test_foreign_expired_different_boot_overwrites(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A")  # expires 12:05
    after = "2026-05-28T13:00:00Z"
    ls = _acquire(tmp_path, "run-2", now=after, boot="boot-B")
    assert ls.run_id == "run-2"
    assert ls.boot_id == "boot-B"
    assert ls.acquired_at == after  # fresh acquired_at on overwrite


def test_foreign_expired_different_boot_different_host_raises_expired_foreign(
    tmp_path: Path,
) -> None:
    # a live foreign host (different hostname AND boot) on shared .flow storage is
    # NOT a same-host reboot; its expired lease must need human takeover.
    _acquire(tmp_path, "run-1", boot="boot-A", host="host-1")  # expires 12:05
    after = "2026-05-28T13:00:00Z"
    with pytest.raises(lease.LeaseExpiredForeign) as exc:
        _acquire(tmp_path, "run-2", now=after, boot="boot-B", host="host-2")
    assert exc.value.holder.run_id == "run-1"


def test_foreign_expired_same_host_different_boot_overwrites(tmp_path: Path) -> None:
    # a genuine same-host reboot (same hostname, changed boot) still clears.
    _acquire(tmp_path, "run-1", boot="boot-A", host="host-1")  # expires 12:05
    after = "2026-05-28T13:00:00Z"
    ls = _acquire(tmp_path, "run-2", now=after, boot="boot-B", host="host-1")
    assert ls.run_id == "run-2"


# ─── acquire: force overrides expired-foreign ──────────────────────────────────


def test_force_overrides_expired_foreign_same_boot(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A")
    after = "2026-05-28T13:00:00Z"
    ls = _acquire(tmp_path, "run-2", now=after, boot="boot-A", force=True)
    assert ls.run_id == "run-2"
    assert ls.acquired_at == after


def test_force_does_not_bypass_live_foreign(tmp_path: Path) -> None:
    # force only clears an expired foreign lease; a live holder still wins.
    _acquire(tmp_path, "run-1")
    with pytest.raises(lease.LeaseHeld):
        _acquire(tmp_path, "run-2", now=NOW, force=True)


# ─── session_nonce: the per-session mutex component (flow-8i6l) ─────────────────


def test_acquire_mints_nonce_on_free_dir(tmp_path: Path) -> None:
    ls = _acquire(tmp_path, "run-1")
    assert ls.session_nonce  # non-empty
    on_disk = lease.read_lease(tmp_path)
    assert on_disk is not None
    assert on_disk.session_nonce == ls.session_nonce


def test_live_owner_reacquire_without_nonce_raises_held(tmp_path: Path) -> None:
    # THE BUG: a second /flow do reuses run_id from state.json but cannot present
    # the live owner's nonce, so it must be blocked, not silently re-acquire.
    first = _acquire(tmp_path, "run-1")
    assert first.session_nonce
    with pytest.raises(lease.LeaseHeld) as exc:
        _acquire(tmp_path, "run-1", now=NOW)  # still live, no nonce presented
    assert exc.value.holder.run_id == "run-1"


def test_live_owner_reacquire_with_wrong_nonce_raises_held(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1")
    with pytest.raises(lease.LeaseHeld):
        _acquire(tmp_path, "run-1", now=NOW, session_nonce="not-the-nonce")


def test_live_owner_reacquire_with_matching_nonce_succeeds(tmp_path: Path) -> None:
    # still-live re-acquire (12:03 < the 12:05 expiry): the matching nonce lets
    # the same session re-enter, preserving the nonce + acquired_at.
    still_live = "2026-05-28T12:03:00Z"
    first = _acquire(tmp_path, "run-1", stage="plan")
    second = _acquire(
        tmp_path, "run-1", now=still_live, stage="implement", session_nonce=first.session_nonce
    )
    assert second.run_id == "run-1"
    assert second.session_nonce == first.session_nonce  # preserved on re-acquire
    assert second.acquired_at == NOW  # acquired_at preserved
    assert second.stage == "implement"


def test_expired_owner_reacquire_rotates_nonce(tmp_path: Path) -> None:
    # an expired owner is a legitimate resume of our own dead run: the prior
    # session is gone, so the nonce rotates (and acquired_at is preserved).
    first = _acquire(tmp_path, "run-1", now=NOW)  # expires 12:05
    after_expiry = "2026-05-28T13:00:00Z"
    second = _acquire(tmp_path, "run-1", now=after_expiry)  # no nonce presented
    assert second.run_id == "run-1"
    assert second.session_nonce
    assert second.session_nonce != first.session_nonce  # rotated
    assert second.acquired_at == NOW  # original acquired_at kept


def test_force_rotates_nonce_on_live_owner(tmp_path: Path) -> None:
    # --force is an explicit reset/takeover: it overwrites the live owned lease
    # and rotates the nonce even though none is presented.
    first = _acquire(tmp_path, "run-1")
    forced = _acquire(tmp_path, "run-1", now=NOW, force=True)
    assert forced.session_nonce
    assert forced.session_nonce != first.session_nonce


# ─── refresh ───────────────────────────────────────────────────────────────────


def test_refresh_by_owner_moves_expiry(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", stage="plan")
    ls = lease.refresh(
        tmp_path,
        "run-1",
        TTL,
        LATER,
        stage="implement",
        current_boot="boot-A",
        hostname="host-1",
        cwd="/work",
    )
    assert ls.acquired_at == NOW  # preserved
    assert ls.lease_expires_at == "2026-05-28T12:15:00Z"
    assert ls.stage == "implement"


def test_refresh_by_non_owner_raises_lease_lost(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1")
    with pytest.raises(lease.LeaseLost):
        lease.refresh(
            tmp_path,
            "run-2",
            TTL,
            LATER,
            current_boot="boot-A",
            hostname="host-1",
            cwd="/work",
        )


def test_refresh_on_free_dir_raises_lease_lost(tmp_path: Path) -> None:
    with pytest.raises(lease.LeaseLost):
        lease.refresh(
            tmp_path,
            "run-1",
            TTL,
            NOW,
            current_boot="boot-A",
            hostname="host-1",
            cwd="/work",
        )


def test_refresh_after_force_takeover_raises_lost(tmp_path: Path) -> None:
    # this is what proves the carried-nonce threading earns its keep: a force
    # takeover rotates the on-disk nonce, so the evicted session's refresh (which
    # carries the OLD nonce) detects it lost the lease.
    first = _acquire(tmp_path, "run-1")
    _acquire(tmp_path, "run-1", now=NOW, force=True)  # takeover rotates the nonce
    with pytest.raises(lease.LeaseLost):
        lease.refresh(
            tmp_path,
            "run-1",
            TTL,
            LATER,
            current_boot="boot-A",
            hostname="host-1",
            cwd="/work",
            session_nonce=first.session_nonce,  # the now-evicted session's nonce
        )


def test_refresh_with_matching_nonce_ok(tmp_path: Path) -> None:
    first = _acquire(tmp_path, "run-1")
    ls = lease.refresh(
        tmp_path,
        "run-1",
        TTL,
        LATER,
        current_boot="boot-A",
        hostname="host-1",
        cwd="/work",
        session_nonce=first.session_nonce,
    )
    assert ls.session_nonce == first.session_nonce  # preserved across refresh


def test_refresh_empty_caller_nonce_falls_back_to_run_id(tmp_path: Path) -> None:
    # a caller that lost its nonce (e.g. across a compaction) passes None; the
    # both-non-empty rule skips the nonce check and refresh succeeds on run_id.
    _acquire(tmp_path, "run-1")
    ls = lease.refresh(
        tmp_path,
        "run-1",
        TTL,
        LATER,
        current_boot="boot-A",
        hostname="host-1",
        cwd="/work",
        session_nonce=None,
    )
    assert ls.run_id == "run-1"


# ─── assert_lease_still_mine ───────────────────────────────────────────────────


def test_assert_lease_still_mine_ok(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A", host="host-1")
    lease.assert_lease_still_mine(
        tmp_path, "run-1", current_boot="boot-A", hostname="host-1"
    )  # no raise


def test_assert_lease_still_mine_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(lease.LeaseLost):
        lease.assert_lease_still_mine(tmp_path, "run-1")


def test_assert_lease_still_mine_run_id_mismatch(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1")
    with pytest.raises(lease.LeaseLost):
        lease.assert_lease_still_mine(tmp_path, "run-2")


def test_assert_lease_still_mine_boot_mismatch(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A")
    with pytest.raises(lease.LeaseLost):
        lease.assert_lease_still_mine(tmp_path, "run-1", current_boot="boot-B")


def test_assert_lease_still_mine_unknown_current_boot_skips_check(tmp_path: Path) -> None:
    # sandboxed runs can't read sysctl, so boot_id() returns "", that must not read as a reboot
    # (false lost-lease exit 7).
    _acquire(tmp_path, "run-1", boot="boot-A")
    lease.assert_lease_still_mine(tmp_path, "run-1", current_boot="")  # no raise


def test_assert_lease_still_mine_unknown_lease_boot_skips_check(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="")
    lease.assert_lease_still_mine(tmp_path, "run-1", current_boot="boot-B")  # no raise


def test_assert_lease_still_mine_hostname_mismatch(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", host="host-1")
    with pytest.raises(lease.LeaseLost):
        lease.assert_lease_still_mine(tmp_path, "run-1", hostname="host-2")


def test_assert_lease_still_mine_empty_ondisk_hostname_skips_check(tmp_path: Path) -> None:
    # a pre-upgrade lease deserializes hostname as "": inconclusive against a
    # real caller hostname, not a mismatch (both-non-empty rule, mirrors boot_id).
    _acquire(tmp_path, "run-1", boot="boot-A", host="")
    lease.assert_lease_still_mine(tmp_path, "run-1", current_boot="boot-A", hostname="host-1")


def test_assert_lease_still_mine_empty_caller_hostname_skips_check(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A", host="host-1")
    lease.assert_lease_still_mine(tmp_path, "run-1", current_boot="boot-A", hostname="")


def test_assert_lease_still_mine_empty_current_boot_does_not_raise(tmp_path: Path) -> None:
    # sandbox blocked the boot probe -> current_boot "" is inconclusive, not a mismatch.
    _acquire(tmp_path, "run-1", boot="boot-A", host="host-1")
    lease.assert_lease_still_mine(tmp_path, "run-1", current_boot="", hostname="host-1")


def test_assert_lease_still_mine_empty_ondisk_boot_does_not_raise(tmp_path: Path) -> None:
    # symmetric case: an empty on-disk boot id is inconclusive against a known current boot.
    _acquire(tmp_path, "run-1", boot="", host="host-1")
    lease.assert_lease_still_mine(tmp_path, "run-1", current_boot="boot-A", hostname="host-1")


def test_assert_lease_still_mine_both_empty_boot_does_not_raise(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="", host="host-1")
    lease.assert_lease_still_mine(tmp_path, "run-1", current_boot="", hostname="host-1")


def test_assert_lease_still_mine_ignores_expiry(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1")  # expires 12:05
    # owner resuming past expiry must still pass the identity check.
    lease.assert_lease_still_mine(tmp_path, "run-1")  # no raise


def test_assert_lease_still_mine_nonce_mismatch_raises(tmp_path: Path) -> None:
    first = _acquire(tmp_path, "run-1")
    _acquire(tmp_path, "run-1", now=NOW, force=True)  # rotates the nonce
    with pytest.raises(lease.LeaseLost):
        lease.assert_lease_still_mine(tmp_path, "run-1", session_nonce=first.session_nonce)


def test_assert_lease_still_mine_empty_caller_nonce_skips_check(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1")
    lease.assert_lease_still_mine(tmp_path, "run-1", session_nonce=None)  # no raise


# ─── release ───────────────────────────────────────────────────────────────────


def test_release_by_owner_removes_file(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1")
    assert lease.release(tmp_path, "run-1") is True
    assert not lease.run_lock_path(tmp_path).exists()


def test_release_by_non_owner_returns_false(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1")
    assert lease.release(tmp_path, "run-2") is False
    assert lease.run_lock_path(tmp_path).exists()


def test_release_on_free_dir_returns_false(tmp_path: Path) -> None:
    assert lease.release(tmp_path, "run-1") is False


def test_release_after_force_takeover_returns_false(tmp_path: Path) -> None:
    # an evicted session (old nonce) must NOT drop the new owner's lease.
    first = _acquire(tmp_path, "run-1")
    _acquire(tmp_path, "run-1", now=NOW, force=True)  # takeover rotates the nonce
    assert lease.release(tmp_path, "run-1", first.session_nonce) is False
    assert lease.run_lock_path(tmp_path).exists()


def test_release_with_matching_nonce_removes(tmp_path: Path) -> None:
    first = _acquire(tmp_path, "run-1")
    assert lease.release(tmp_path, "run-1", first.session_nonce) is True
    assert not lease.run_lock_path(tmp_path).exists()


# ─── classify ──────────────────────────────────────────────────────────────────


def test_classify_free(tmp_path: Path) -> None:
    result = lease.classify(tmp_path, NOW, current_boot="boot-A")
    assert result == {"state": "free", "holder": None}


def test_classify_live(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A")
    result = lease.classify(tmp_path, NOW, current_boot="boot-A")
    assert result["state"] == "live"
    holder = cast(dict[str, Any], result["holder"])
    assert holder["run_id"] == "run-1"


def test_classify_expired_reboot_clearable(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A", host="host-1")  # expires 12:05
    after = "2026-05-28T13:00:00Z"
    result = lease.classify(tmp_path, after, current_boot="boot-B", hostname="host-1")
    assert result["state"] == "expired_reboot_clearable"


def test_classify_expired_foreign(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A")
    after = "2026-05-28T13:00:00Z"
    result = lease.classify(tmp_path, after, current_boot="boot-A")
    assert result["state"] == "expired_foreign"


def test_classify_expired_different_host_is_foreign_not_reboot_clearable(
    tmp_path: Path,
) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A", host="host-1")
    after = "2026-05-28T13:00:00Z"
    result = lease.classify(tmp_path, after, current_boot="boot-B", hostname="host-2")
    assert result["state"] == "expired_foreign"


def test_classify_expired_same_host_different_boot_is_reboot_clearable(
    tmp_path: Path,
) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A", host="host-1")
    after = "2026-05-28T13:00:00Z"
    result = lease.classify(tmp_path, after, current_boot="boot-B", hostname="host-1")
    assert result["state"] == "expired_reboot_clearable"


def test_classify_reboot_clearable_requires_hostname_arg(tmp_path: Path) -> None:
    # conservative default: no hostname passed -> expired_foreign, never auto-clear.
    _acquire(tmp_path, "run-1", boot="boot-A", host="host-1")
    after = "2026-05-28T13:00:00Z"
    result = lease.classify(tmp_path, after, current_boot="boot-B")
    assert result["state"] == "expired_foreign"


# ─── is_expired boundary ───────────────────────────────────────────────────────


def test_is_expired_boundary() -> None:
    ls = lease.Lease(
        run_id="r",
        boot_id="b",
        hostname="h",
        cwd="/w",
        acquired_at=NOW,
        lease_expires_at="2026-05-28T12:05:00Z",
    )
    assert lease.is_expired(ls, "2026-05-28T12:04:59Z") is False
    assert lease.is_expired(ls, "2026-05-28T12:05:00Z") is True  # equality = expired
    assert lease.is_expired(ls, "2026-05-28T12:05:01Z") is True


# ─── boot_id with injected runner ──────────────────────────────────────────────


def test_boot_id_darwin_uses_sysctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[list[str]] = []

    def runner(args: list[str]) -> str:
        calls.append(args)
        return "ABC-123-UUID\n"

    assert lease.boot_id(runner) == "ABC-123-UUID"
    assert calls == [["sysctl", "-n", "kern.bootsessionuuid"]]


def test_boot_id_returns_empty_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def runner(args: list[str]) -> str:
        raise OSError("nope")

    assert lease.boot_id(runner) == ""


def test_boot_id_linux_reads_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    constructed: list[str] = []

    class FakePath:
        def __init__(self, path):
            constructed.append(path)

        def read_text(self):
            return "uuid-value\n"

    monkeypatch.setattr(lease, "Path", FakePath)
    calls: list[list[str]] = []

    def runner(args: list[str]) -> str:
        calls.append(args)
        return ""

    assert lease.boot_id(runner) == "uuid-value"
    assert constructed == ["/proc/sys/kernel/random/boot_id"]
    assert calls == []


def test_boot_id_linux_returns_empty_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    class FakePath:
        def __init__(self, path):
            pass

        def read_text(self):
            raise OSError("no /proc")

    monkeypatch.setattr(lease, "Path", FakePath)

    def runner(args: list[str]) -> str:
        return ""

    assert lease.boot_id(runner) == ""


def test_boot_id_darwin_returns_empty_on_subprocess_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def runner(args: list[str]) -> str:
        raise subprocess.CalledProcessError(1, ["sysctl"])

    assert lease.boot_id(runner) == ""


def test_boot_id_unknown_platform_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    calls: list[list[str]] = []

    def runner(args: list[str]) -> str:
        calls.append(args)
        return "should-not-be-used"

    assert lease.boot_id(runner) == ""
    assert calls == []


# ─── corrupt run.lock ──────────────────────────────────────────────────────────


def test_read_lease_corrupt_raises(tmp_path: Path) -> None:
    lease.run_lock_path(tmp_path).write_text("{not json", encoding="utf-8")
    with pytest.raises(lease.LeaseError):
        lease.read_lease(tmp_path)


def test_classify_corrupt_unparseable(tmp_path: Path) -> None:
    lease.run_lock_path(tmp_path).write_text("{not json", encoding="utf-8")
    result = lease.classify(tmp_path, NOW, current_boot="boot-A")
    assert result == {"state": "corrupt", "holder": None}


def test_classify_corrupt_missing_required_key(tmp_path: Path) -> None:
    # valid JSON dict but missing run_id -> _deserialize KeyError trigger.
    lease.run_lock_path(tmp_path).write_text(
        json.dumps({"boot_id": "b", "acquired_at": NOW, "lease_expires_at": NOW}),
        encoding="utf-8",
    )
    result = lease.classify(tmp_path, NOW, current_boot="boot-A")
    assert result == {"state": "corrupt", "holder": None}


def test_classify_corrupt_does_not_mutate(tmp_path: Path) -> None:
    lock = lease.run_lock_path(tmp_path)
    lock.write_text("{not json", encoding="utf-8")
    lease.classify(tmp_path, NOW, current_boot="boot-A")
    assert lock.exists()
    assert lock.read_text(encoding="utf-8") == "{not json"
    assert list(tmp_path.glob("run.lock.quarantine.*")) == []


def test_quarantine_corrupt_lock_renames(tmp_path: Path) -> None:
    lock = lease.run_lock_path(tmp_path)
    lock.write_text("{not json", encoding="utf-8")
    dst = lease.quarantine_corrupt_lock(tmp_path)
    assert dst is not None
    assert dst.exists()
    assert dst.name.startswith("run.lock.quarantine.")
    assert dst.read_text(encoding="utf-8") == "{not json"
    assert not lock.exists()


def test_quarantine_corrupt_lock_absent_returns_none(tmp_path: Path) -> None:
    assert lease.quarantine_corrupt_lock(tmp_path) is None


def test_quarantine_corrupt_lock_skips_now_valid_lock(tmp_path: Path) -> None:
    # a lock that parses as a valid lease (a concurrent acquire won the race
    # since the caller classified it corrupt) must never be quarantined.
    _acquire(tmp_path, "run-1")
    assert lease.quarantine_corrupt_lock(tmp_path) is None
    on_disk = lease.read_lease(tmp_path)
    assert on_disk is not None
    assert on_disk.run_id == "run-1"
    assert list(tmp_path.glob("run.lock.quarantine.*")) == []


# ─── takeover_clear ────────────────────────────────────────────────────────────


def _racer_lease_json(expires_at: str) -> str:
    return json.dumps(
        {
            "run_id": "racer",
            "boot_id": "boot-A",
            "hostname": "host-1",
            "cwd": "/work",
            "acquired_at": NOW,
            "lease_expires_at": expires_at,
        }
    )


def test_takeover_clear_quarantines_corrupt(tmp_path: Path) -> None:
    lock = lease.run_lock_path(tmp_path)
    lock.write_text("{not json", encoding="utf-8")
    result = lease.takeover_clear(tmp_path, NOW)
    assert result["cleared"] is True
    assert result["state"] == "corrupt"
    dst = result["quarantined"]
    assert isinstance(dst, Path)
    assert dst.name.startswith("run.lock.quarantine.")
    assert dst.read_text(encoding="utf-8") == "{not json"
    assert not lock.exists()


def test_takeover_clear_refuses_live(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1")
    result = lease.takeover_clear(tmp_path, NOW, current_boot="boot-A", hostname="host-1")
    assert result["cleared"] is False
    assert result["state"] == "live"
    holder = cast(dict[str, Any], result["holder"])
    assert holder["run_id"] == "run-1"
    assert lease.run_lock_path(tmp_path).exists()


def test_takeover_clear_unlinks_expired(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A")  # expires 12:05
    after = "2026-05-28T13:00:00Z"
    result = lease.takeover_clear(tmp_path, after, current_boot="boot-A", hostname="host-1")
    assert result["cleared"] is True
    assert result["state"] == "expired_foreign"
    assert result["quarantined"] is None
    assert not lease.run_lock_path(tmp_path).exists()


def test_takeover_clear_free_is_noop(tmp_path: Path) -> None:
    result = lease.takeover_clear(tmp_path, NOW)
    assert result["cleared"] is True
    assert result["state"] == "free"
    assert result["quarantined"] is None


def test_takeover_clear_race_corrupt_replaced_by_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the TOCTOU itself: a concurrent acquirer wins the flock first and replaces
    # the corrupt lock with a valid live lease. takeover_clear classifies inside
    # its flock span, so it must see the live lease and refuse.
    lock = lease.run_lock_path(tmp_path)
    lock.write_text("{not json", encoding="utf-8")
    real_flock = lease.flock_blocking

    @contextlib.contextmanager
    def racing_flock(path: Path) -> Iterator[None]:
        with real_flock(path):
            lock.write_text(_racer_lease_json("2026-05-28T12:30:00Z"), encoding="utf-8")
            yield

    monkeypatch.setattr(lease, "flock_blocking", racing_flock)
    result = lease.takeover_clear(tmp_path, NOW, current_boot="boot-A", hostname="host-1")
    assert result["cleared"] is False
    assert result["state"] == "live"
    on_disk = lease.read_lease(tmp_path)
    assert on_disk is not None
    assert on_disk.run_id == "racer"
    assert list(tmp_path.glob("run.lock.quarantine.*")) == []


def test_takeover_clear_force_clears_live(tmp_path: Path) -> None:
    # the abort --force escape hatch: force unlinks a lease that still looks live.
    _acquire(tmp_path, "run-1")
    result = lease.takeover_clear(
        tmp_path, NOW, current_boot="boot-A", hostname="host-1", force=True
    )
    assert result["cleared"] is True
    assert result["state"] == "live"
    assert not lease.run_lock_path(tmp_path).exists()


def test_takeover_clear_runs_on_cleared_when_expired(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", now="2020-01-01T00:00:00Z")  # expired by NOW
    calls: list[str] = []
    result = lease.takeover_clear(
        tmp_path,
        NOW,
        current_boot="boot-A",
        hostname="host-1",
        on_cleared=lambda: calls.append("ran"),
    )
    assert result["cleared"] is True
    assert calls == ["ran"]


def test_takeover_clear_skips_on_cleared_when_live(tmp_path: Path) -> None:
    # a refused-live takeover must NOT run on_cleared (no stage resets under a
    # live lease we did not clear).
    _acquire(tmp_path, "run-1")
    calls: list[str] = []
    result = lease.takeover_clear(
        tmp_path,
        NOW,
        current_boot="boot-A",
        hostname="host-1",
        on_cleared=lambda: calls.append("ran"),
    )
    assert result["cleared"] is False
    assert calls == []


def test_takeover_clear_on_cleared_runs_under_flock(tmp_path: Path) -> None:
    # the secondary de-mutex fix: on_cleared (recover takeover's stage resets)
    # must run WHILE the lease flock is still held, so a concurrent acquire
    # cannot land between the clear and the resets. Prove the flock is held by
    # failing a non-blocking re-lock from a fresh fd inside the callback.
    import fcntl
    import os

    _acquire(tmp_path, "run-1", now="2020-01-01T00:00:00Z")  # expired by NOW
    flock_held: list[bool] = []

    def on_cleared() -> None:
        fd = os.open(str(lease._flock_path(tmp_path)), os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            flock_held.append(False)  # acquired -> flock was NOT held
        except BlockingIOError:
            flock_held.append(True)  # blocked -> flock IS held by takeover_clear
        finally:
            os.close(fd)

    result = lease.takeover_clear(
        tmp_path, NOW, current_boot="boot-A", hostname="host-1", on_cleared=on_cleared
    )
    assert result["cleared"] is True
    assert flock_held == [True]


# ─── classify_then ───────────────────────────────────────────────────────────


def test_classify_then_runs_teardown_when_free(tmp_path: Path) -> None:
    calls = []

    def teardown() -> str:
        calls.append("run")
        return "torn"

    result = lease.classify_then(tmp_path, NOW, teardown, current_boot="boot-A", hostname="host-1")
    assert calls == ["run"]
    assert result["torn_down"] is True
    assert result["state"] == "free"
    assert result["result"] == "torn"


def test_classify_then_runs_teardown_when_expired_reboot_clearable(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A", host="host-1")  # expires 12:05
    after = "2026-05-28T13:00:00Z"
    calls = []

    def teardown() -> str:
        calls.append("run")
        return "torn"

    result = lease.classify_then(
        tmp_path, after, teardown, current_boot="boot-B", hostname="host-1"
    )
    assert calls == ["run"]
    assert result["torn_down"] is True
    assert result["state"] == "expired_reboot_clearable"


def test_classify_then_skips_teardown_when_live(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A", host="host-1")
    calls = []

    def teardown() -> str:
        calls.append("run")
        return "torn"

    result = lease.classify_then(tmp_path, NOW, teardown, current_boot="boot-A", hostname="host-1")
    assert calls == []
    assert result["torn_down"] is False
    assert result["state"] == "live"
    holder = cast(dict[str, Any], result["holder"])
    assert holder["run_id"] == "run-1"
    assert "result" not in result


def test_classify_then_skips_teardown_when_corrupt(tmp_path: Path) -> None:
    lease.run_lock_path(tmp_path).write_text("{not json", encoding="utf-8")
    calls = []

    def teardown() -> str:
        calls.append("run")
        return "torn"

    result = lease.classify_then(tmp_path, NOW, teardown, current_boot="boot-A", hostname="host-1")
    assert calls == []
    assert result["torn_down"] is False
    assert result["state"] == "corrupt"


def test_classify_then_race_free_replaced_by_live_skips_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the TOCTOU itself: a concurrent acquirer wins the flock first and writes a
    # live lease into a previously-free dir. classify_then classifies inside its
    # flock span, so it must see the live lease and NOT run the teardown. The
    # teardown-not-called assertion is load-bearing: it is what distinguishes
    # this single-flock seam from a classify-then-mutate that releases the lock
    # before the external teardown.
    real_flock = lease.flock_blocking
    calls = []

    def teardown() -> str:
        calls.append("run")
        return "torn"

    @contextlib.contextmanager
    def racing_flock(path: Path) -> Iterator[None]:
        with real_flock(path):
            lease.run_lock_path(tmp_path).write_text(
                _racer_lease_json("2026-05-28T12:30:00Z"), encoding="utf-8"
            )
            yield

    monkeypatch.setattr(lease, "flock_blocking", racing_flock)
    result = lease.classify_then(tmp_path, NOW, teardown, current_boot="boot-A", hostname="host-1")
    assert calls == []
    assert result["torn_down"] is False
    assert result["state"] == "live"
    holder = cast(dict[str, Any], result["holder"])
    assert holder["run_id"] == "racer"


# ─── CLI ───────────────────────────────────────────────────────────────────────


def test_cli_acquire_then_held(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = lease.cli_main(
        ["acquire", "--ticket-dir", str(tmp_path), "--run-id", "run-1", "--ttl-seconds", "300"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "run-1"

    rc2 = lease.cli_main(
        ["acquire", "--ticket-dir", str(tmp_path), "--run-id", "run-2", "--ttl-seconds", "300"]
    )
    assert rc2 == 1  # LeaseHeld
    held = json.loads(capsys.readouterr().out)
    assert held["error"] == "lease_held"
    assert held["holder"]["run_id"] == "run-1"


def test_cli_release_and_classify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lease.cli_main(
        ["acquire", "--ticket-dir", str(tmp_path), "--run-id", "run-1", "--ttl-seconds", "300"]
    )
    capsys.readouterr()
    rc = lease.cli_main(["release", "--ticket-dir", str(tmp_path), "--run-id", "run-1"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"released": True}

    rc2 = lease.cli_main(["classify", "--ticket-dir", str(tmp_path)])
    assert rc2 == 0
    assert json.loads(capsys.readouterr().out)["state"] == "free"


# ─── Concurrency: multiprocessing flock contention ─────────────────────────────


def _acquire_proc(ticket_dir_str: str, run_id: str) -> None:
    """Top-level so multiprocessing can pickle it on macOS spawn-start.

    Fixed now + large TTL: any winner's lease is live for the loser, so the only
    way the loser proceeds is winning the flock-protected free state. Exactly one
    succeeds (exit 0); the other sees a foreign live lease -> LeaseHeld (exit 1).
    """
    try:
        lease.acquire(
            Path(ticket_dir_str),
            run_id,
            300,
            "2026-05-28T12:00:00Z",
            current_boot="boot-A",
            hostname="host-1",
            cwd="/work",
        )
    except lease.LeaseHeld:
        sys.exit(1)
    sys.exit(0)


def test_concurrent_acquire_exactly_one_wins(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    p1 = ctx.Process(target=_acquire_proc, args=(str(tmp_path), "run-1"))
    p2 = ctx.Process(target=_acquire_proc, args=(str(tmp_path), "run-2"))
    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)
    assert sorted([p1.exitcode, p2.exitcode]) == [0, 1]

    winner = lease.read_lease(tmp_path)
    assert winner is not None
    assert winner.run_id in ("run-1", "run-2")
