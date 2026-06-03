from __future__ import annotations

import socket
from datetime import UTC, datetime
from pathlib import Path

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
