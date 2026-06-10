from __future__ import annotations

import contextlib
import json
import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

import lease
import recover
import state


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _identity() -> tuple[str, str]:
    return lease.boot_id(), socket.gethostname()


def _ws(root: Path, stages: tuple[str, ...] = ("ticket", "plan")) -> Path:
    flow = root / ".flow"
    flow.mkdir()
    (flow / "workspace.toml").write_text(
        '[tracker]\nbackend = "jira"\n'
        '[tracker.jira]\ncloud_id = "x"\nproject_key = "FT"\n'
        '[pipeline]\nstages = ["ticket", "plan"]\n'
        '[pipeline.handlers]\nticket = "inline"\nplan = "inline"\n'
        '[memory]\nnamespace = "FT"\n',
        encoding="utf-8",
    )
    td = flow / "runs" / "T-1"
    state.init(td, "T-1", "jira", list(stages))
    return td


def test_detect_fresh(tmp_path: Path) -> None:
    _ws(tmp_path)
    rep = recover.detect(tmp_path, "T-1", now_iso=_now())
    assert rep["state_exit"] == 0
    assert set(rep["stages"]) == {"ticket", "plan"}
    assert rep["lease"]["state"] == "free"
    assert rep["snapshot"]["ok"] is True
    assert rep["ship_event_attention"] == 0
    # progress-map consumer was removed (flow-dwd): detect no longer emits a progress map.
    assert "progress" not in rep


def test_detect_no_state(tmp_path: Path) -> None:
    (tmp_path / ".flow").mkdir()
    rep = recover.detect(tmp_path, "ZZ-9", now_iso=_now())
    # state.read returns exit 0 for an absent (not-yet-initialized) state.json.
    assert rep["state_exit"] == 0
    assert rep["stages"] is None


def test_takeover_clears_expired_lease_and_resets(tmp_path: Path) -> None:
    td = _ws(tmp_path)
    boot, host = _identity()
    lease.acquire(
        td, "old-run", 1, "2020-01-01T00:00:00Z", current_boot=boot, hostname=host, cwd=str(td)
    )
    state.begin_stage(td, "ticket", "sha")
    rc, payload = recover.takeover(tmp_path, "T-1", now_iso=_now())
    assert rc == 0
    assert payload["took_over"] is True
    assert "ticket" in payload["reset_stages"]
    assert not lease.run_lock_path(td).exists()
    ts, _ = state.read(td)
    assert ts is not None
    assert ts.stages["ticket"].status == "pending"


def test_takeover_refused_on_live_lease(tmp_path: Path) -> None:
    td = _ws(tmp_path)
    boot, host = _identity()
    lease.acquire(td, "live-run", 600, _now(), current_boot=boot, hostname=host, cwd=str(td))
    rc, payload = recover.takeover(tmp_path, "T-1", now_iso=_now())
    assert rc == 1
    assert "live" in payload["error"]


def test_takeover_quarantines_corrupt_lock(tmp_path: Path) -> None:
    td = _ws(tmp_path)
    lock = lease.run_lock_path(td)
    lock.write_text("{not json", encoding="utf-8")
    rc, payload = recover.takeover(tmp_path, "T-1", now_iso=_now())
    assert rc == 0
    assert payload["took_over"] is True
    # RENAME for forensics, not blind-unlink: original gone, quarantine sibling present.
    assert not lock.exists()
    quarantined = list(td.glob("run.lock.quarantine.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{not json"
    assert payload["quarantined"] == str(quarantined[0])


def test_takeover_refuses_when_corrupt_lock_becomes_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TOCTOU regression: a corrupt lock replaced by a valid live lease (a
    # concurrent acquirer winning the flock first) must refuse takeover and
    # leave the lease + stages untouched.
    td = _ws(tmp_path)
    state.begin_stage(td, "ticket", "sha")
    lock = lease.run_lock_path(td)
    lock.write_text("{not json", encoding="utf-8")
    boot, host = _identity()
    live = json.dumps(
        {
            "run_id": "racer",
            "boot_id": boot,
            "hostname": host,
            "cwd": str(td),
            "acquired_at": _now(),
            "lease_expires_at": "2099-01-01T00:00:00Z",
        }
    )
    real_flock = lease.flock_blocking

    @contextlib.contextmanager
    def racing_flock(path: Path) -> Iterator[None]:
        with real_flock(path):
            lock.write_text(live, encoding="utf-8")
            yield

    monkeypatch.setattr(lease, "flock_blocking", racing_flock)
    rc, payload = recover.takeover(tmp_path, "T-1", now_iso=_now())
    assert rc == 1
    assert "live" in payload["error"]
    on_disk = lease.read_lease(td)
    assert on_disk is not None
    assert on_disk.run_id == "racer"
    ts, _ = state.read(td)
    assert ts is not None
    assert ts.stages["ticket"].status == "in_progress"


def test_retry_resets_failed_to_pending(tmp_path: Path) -> None:
    td = _ws(tmp_path)
    state.force_stage_status(td, "plan", "failed")
    rc = recover.cli_main(
        ["retry", "--ticket", "T-1", "--workspace-root", str(tmp_path), "--stage", "plan"]
    )
    assert rc == 0
    ts, _ = state.read(td)
    assert ts is not None
    assert ts.stages["plan"].status == "pending"


def test_skip_marks_completed(tmp_path: Path) -> None:
    td = _ws(tmp_path)
    state.force_stage_status(td, "plan", "failed")
    rc = recover.cli_main(
        ["skip", "--ticket", "T-1", "--workspace-root", str(tmp_path), "--stage", "plan"]
    )
    assert rc == 0
    ts, _ = state.read(td)
    assert ts is not None
    assert ts.stages["plan"].status == "completed"


def test_abort_removes_lock(tmp_path: Path) -> None:
    td = _ws(tmp_path)
    boot, host = _identity()
    lease.acquire(td, "r", 600, _now(), current_boot=boot, hostname=host, cwd=str(td))
    rc, payload = recover.abort(tmp_path, "T-1")
    assert rc == 0
    assert payload["lease_removed"] is True
    assert not lease.run_lock_path(td).exists()


def test_reload_snapshot_writes_sha(tmp_path: Path) -> None:
    td = _ws(tmp_path)
    rc, payload = recover.reload_snapshot(tmp_path, "T-1")
    assert rc == 0
    assert payload["snapshot_reloaded"] is True
    assert (td / "snapshot.sha").exists()


def test_detect_ship_event_attention(tmp_path: Path) -> None:
    _ws(tmp_path)
    ship = tmp_path / ".flow" / "FT" / "ship-events"
    ship.mkdir(parents=True)
    (ship / "evt.dupe.1.json").write_text("{}", encoding="utf-8")
    (ship / "evt.corrupt.json").write_text("{}", encoding="utf-8")
    (ship / ".quarantine-intent-evt").write_text("{}", encoding="utf-8")
    (ship / "clean.json").write_text("{}", encoding="utf-8")
    rep = recover.detect(tmp_path, "T-1", now_iso=_now())
    assert rep["ship_event_attention"] == 3


def test_retry_exit2_no_state(tmp_path: Path) -> None:
    (tmp_path / ".flow").mkdir()
    rc = recover.cli_main(
        ["retry", "--ticket", "ZZ-9", "--workspace-root", str(tmp_path), "--stage", "plan"]
    )
    assert rc == 2


def test_retry_exit1_unknown_stage(tmp_path: Path) -> None:
    _ws(tmp_path)
    rc = recover.cli_main(
        ["retry", "--ticket", "T-1", "--workspace-root", str(tmp_path), "--stage", "nope"]
    )
    assert rc == 1


def test_skip_exit1_unknown_stage(tmp_path: Path) -> None:
    _ws(tmp_path)
    rc = recover.cli_main(
        ["skip", "--ticket", "T-1", "--workspace-root", str(tmp_path), "--stage", "nope"]
    )
    assert rc == 1
