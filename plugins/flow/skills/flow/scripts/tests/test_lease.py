"""Contract tests for lease.py.

The lease is a per-ticket mutex, not a liveness checker: identity is
run_id + boot_id + hostname compared under a flock. All logic tests inject
current_boot/hostname/cwd/now explicitly so nothing touches real platform
state. The contention test uses multiprocessing("spawn") (threads can't show
POSIX flock — the GIL hides it) with a fixed large TTL so exactly one of two
foreign-run_id acquirers wins.
"""

from __future__ import annotations

import json
import multiprocessing
import sys
from pathlib import Path

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


def test_assert_lease_still_mine_hostname_mismatch(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", host="host-1")
    with pytest.raises(lease.LeaseLost):
        lease.assert_lease_still_mine(tmp_path, "run-1", hostname="host-2")


def test_assert_lease_still_mine_ignores_expiry(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1")  # expires 12:05
    # owner resuming past expiry must still pass the identity check.
    lease.assert_lease_still_mine(tmp_path, "run-1")  # no raise


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


# ─── classify ──────────────────────────────────────────────────────────────────


def test_classify_free(tmp_path: Path) -> None:
    result = lease.classify(tmp_path, NOW, current_boot="boot-A")
    assert result == {"state": "free", "holder": None}


def test_classify_live(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A")
    result = lease.classify(tmp_path, NOW, current_boot="boot-A")
    assert result["state"] == "live"
    assert result["holder"]["run_id"] == "run-1"  # type: ignore[index]


def test_classify_expired_reboot_clearable(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A")  # expires 12:05
    after = "2026-05-28T13:00:00Z"
    result = lease.classify(tmp_path, after, current_boot="boot-B")
    assert result["state"] == "expired_reboot_clearable"


def test_classify_expired_foreign(tmp_path: Path) -> None:
    _acquire(tmp_path, "run-1", boot="boot-A")
    after = "2026-05-28T13:00:00Z"
    result = lease.classify(tmp_path, after, current_boot="boot-A")
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


# ─── corrupt run.lock ──────────────────────────────────────────────────────────


def test_read_lease_corrupt_raises(tmp_path: Path) -> None:
    lease.run_lock_path(tmp_path).write_text("{not json", encoding="utf-8")
    with pytest.raises(lease.LeaseError):
        lease.read_lease(tmp_path)


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
