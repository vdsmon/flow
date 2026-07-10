"""Tests for observe_at_close.py (the post-merge reap-seam ship-event observer).

Topology matters: `workspace_root` (the MAIN store owner) and `worktree` (the doomed run's dir,
holding state.json) are DISTINCT dirs. The whole ticket is "read attribution from the worktree's
state.json, write the event to the main store", so collapsing the two would let a broken threading
of `state_path` / `workspace_root` pass green.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import observe_at_close
import tracker


class _FakeTracker:
    """Answers only the two methods observe_at_close calls: is_shipped + get."""

    def __init__(self, ship, ticket=None, *, is_shipped_error=False, get_error=False):
        self._ship = ship
        self._ticket = ticket if ticket is not None else {}
        self._is_shipped_error = is_shipped_error
        self._get_error = get_error

    def is_shipped(self, key):
        if self._is_shipped_error:
            raise tracker.TrackerError("is_shipped boom")
        return self._ship

    def get(self, key):
        if self._get_error:
            raise tracker.TrackerError("get boom")
        return self._ticket


def _seed_workspace(root: Path, namespace: str = "demo") -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n\n[memory]\nnamespace = "demo"\n',
        encoding="utf-8",
    )


def _seed_state(
    worktree: Path,
    key: str,
    *,
    run_id: str = "abcdef0123456789",
    plan_started: str | None = "2026-05-28T00:00:00Z",
    create_pr_finished: str | None = "2026-05-28T12:00:00Z",
) -> None:
    stages: dict = {}
    if plan_started is not None:
        stages["plan"] = {"started_at_iso": plan_started}
    if create_pr_finished is not None:
        stages["create_pr"] = {"finished_at_iso": create_pr_finished}
    run_dir = worktree / ".flow" / "runs" / key
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(
        json.dumps({"ticket": key, "run_id": run_id, "stages": stages}) + "\n",
        encoding="utf-8",
    )


def _seed_frontmatter(worktree: Path, key: str, lane: str = "light") -> None:
    tickets = worktree / ".flow" / "tickets"
    tickets.mkdir(parents=True, exist_ok=True)
    (tickets / f"{key}.md").write_text(
        f'+++\nticket = "{key}"\nlane = "{lane}"\n+++\n', encoding="utf-8"
    )


def _ship(state: str = "not_yet_observed", closed_at: object = "2026-05-28T12:00:00Z") -> dict:
    return {
        "state": state,
        "shipped_at": None,
        "evidence": {
            "tracker": "beads",
            "tracker_status": "closed",
            "commit_sha": "deadbeef",
            "closure_reason": "merged via PR #7",
            "closed_at": closed_at,
        },
        "source": "live_backend_query" if state == "not_yet_observed" else "none",
    }


def _install_tracker(monkeypatch, fake) -> None:
    monkeypatch.setattr(observe_at_close, "make_tracker", lambda config: fake)


def _ship_events_dir(main: Path, namespace: str = "demo") -> Path:
    return main / ".flow" / namespace / "ship-events"


# ─── 1. observed: event to main store, attribution from the worktree ─────────


def test_observed_writes_to_main_store_from_worktree_state(tmp_path, monkeypatch):
    main, wt = tmp_path / "main", tmp_path / "wt"
    _seed_workspace(main)
    _seed_state(wt, "flow-a")
    _seed_frontmatter(wt, "flow-a", lane="light")
    fake = _FakeTracker(
        _ship(),
        ticket={
            "labels": ["evolve", "tier:light"],
            "description": "context line\nACCEPTANCE-INVARIANT: sign stays positive\ntail",
        },
    )
    _install_tracker(monkeypatch, fake)

    result = observe_at_close.observe_at_close(main, "flow-a", wt)

    assert result["action"] == "observed"
    frozen = Path(result["path"])
    # the event lands under the MAIN store, never inside the doomed worktree
    assert frozen.exists()
    assert str(frozen).startswith(str(main))
    assert str(wt) not in str(frozen)
    assert not (wt / ".flow" / "demo").exists()
    data = json.loads(frozen.read_text(encoding="utf-8"))
    # flow_attribution present PROVES the worktree's state.json was read via the state_path override
    # (the main root has no state.json to fall back to).
    assert data["flow_attribution"] == {
        "plan_started_at_iso": "2026-05-28T00:00:00Z",
        "create_pr_finished_at_iso": "2026-05-28T12:00:00Z",
    }
    assert data["observed_by_run_id"] == "abcdef0123456789"
    assert data["tier"] == "tier:light"
    assert data["acceptance_invariant"] == "sign stays positive"
    assert data["lane"] == "light"
    assert data["arm"] == "flow"
    assert data["evidence"] == _ship()["evidence"]
    assert data["shipped_at"] == "2026-05-28T12:00:00Z"


def test_observed_without_attribution_when_stage_timestamps_missing(tmp_path, monkeypatch):
    main, wt = tmp_path / "main", tmp_path / "wt"
    _seed_workspace(main)
    _seed_state(wt, "flow-a", plan_started=None, create_pr_finished=None)
    _install_tracker(monkeypatch, _FakeTracker(_ship()))

    result = observe_at_close.observe_at_close(main, "flow-a", wt)

    assert result["action"] == "observed"
    data = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    # a valid run_id with incoherent stage timestamps still freezes the event, just unstamped
    assert "flow_attribution" not in data
    assert data["observed_by_run_id"] == "abcdef0123456789"


def test_is_dupe_from_write_race_propagates(tmp_path, monkeypatch):
    main, wt = tmp_path / "main", tmp_path / "wt"
    _seed_workspace(main)
    _seed_state(wt, "flow-a")
    _install_tracker(monkeypatch, _FakeTracker(_ship()))
    # the frozen file appearing between the pre-check and the O_EXCL write is only
    # reachable by a concurrent observer; simulate the dupe outcome at the seam
    monkeypatch.setattr(
        observe_at_close.observe_ship_event,
        "observe",
        lambda *a, **k: (tmp_path / "main" / "raced.dupe.1.json", True),
    )

    result = observe_at_close.observe_at_close(main, "flow-a", wt)

    assert result["action"] == "observed"
    assert result["is_dupe"] is True


# ─── 2. HARD CONSTRAINT: a non-observable state NEVER writes an event ─────────


@pytest.mark.parametrize("state", ["indeterminate", "not_shipped"])
def test_non_observable_state_never_writes(tmp_path, monkeypatch, state):
    main, wt = tmp_path / "main", tmp_path / "wt"
    _seed_workspace(main)
    _seed_state(wt, "flow-a")
    _install_tracker(monkeypatch, _FakeTracker(_ship(state=state)))

    result = observe_at_close.observe_at_close(main, "flow-a", wt)

    assert result == {"action": "skipped", "reason": state}
    assert not _ship_events_dir(main).exists()


# ─── 3. idempotence: a frozen event skips before the gate, no dupe ───────────


def test_already_observed_skips_before_gate_no_dupe(tmp_path, monkeypatch):
    main, wt = tmp_path / "main", tmp_path / "wt"
    _seed_workspace(main)
    _seed_state(wt, "flow-a")
    ship_dir = _ship_events_dir(main)
    ship_dir.mkdir(parents=True)
    (ship_dir / "flow-a.json").write_text('{"ticket": "flow-a"}\n', encoding="utf-8")
    # is_shipped would raise; a skip (not failed) proves the pre-check runs first.
    _install_tracker(monkeypatch, _FakeTracker(_ship(), is_shipped_error=True))

    result = observe_at_close.observe_at_close(main, "flow-a", wt)

    assert result["action"] == "skipped"
    assert result["reason"] == "already_observed"
    assert list(ship_dir.glob("flow-a.json.dupe.*")) == []


# ─── 4. no run state: missing / non-hex run_id skips without writing ─────────


def test_missing_state_json_skips_no_run_state(tmp_path, monkeypatch):
    main, wt = tmp_path / "main", tmp_path / "wt"
    _seed_workspace(main)  # wt has no state.json
    _install_tracker(monkeypatch, _FakeTracker(_ship()))

    result = observe_at_close.observe_at_close(main, "flow-a", wt)

    assert result == {"action": "skipped", "reason": "no_run_state"}
    assert not _ship_events_dir(main).exists()


def test_non_hex_run_id_skips_no_run_state(tmp_path, monkeypatch):
    main, wt = tmp_path / "main", tmp_path / "wt"
    _seed_workspace(main)
    _seed_state(wt, "flow-a", run_id="NOT-16-HEX-ID")
    _install_tracker(monkeypatch, _FakeTracker(_ship()))

    result = observe_at_close.observe_at_close(main, "flow-a", wt)

    assert result == {"action": "skipped", "reason": "no_run_state"}
    assert not _ship_events_dir(main).exists()


# ─── 5. shipped_at synthesis ─────────────────────────────────────────────────


def _observe_with_closed_at(main, wt, monkeypatch, closed_at) -> dict:
    _seed_workspace(main)
    _seed_state(wt, "flow-a")
    _install_tracker(monkeypatch, _FakeTracker(_ship(closed_at=closed_at)))
    result = observe_at_close.observe_at_close(main, "flow-a", wt)
    return json.loads(Path(result["path"]).read_text(encoding="utf-8"))


def test_shipped_at_passthrough_when_already_z(tmp_path, monkeypatch):
    data = _observe_with_closed_at(
        tmp_path / "main", tmp_path / "wt", monkeypatch, "2026-05-28T12:00:00Z"
    )
    assert data["shipped_at"] == "2026-05-28T12:00:00Z"


def test_shipped_at_normalizes_offset_iso_to_utc_z(tmp_path, monkeypatch):
    data = _observe_with_closed_at(
        tmp_path / "main", tmp_path / "wt", monkeypatch, "2026-05-28T12:00:00+02:00"
    )
    assert data["shipped_at"] == "2026-05-28T10:00:00Z"


def test_shipped_at_absent_uses_now(tmp_path, monkeypatch):
    data = _observe_with_closed_at(tmp_path / "main", tmp_path / "wt", monkeypatch, None)
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", data["shipped_at"])


# ─── 6. gate failures return failed (never raise, never write) ───────────────


def test_workspace_config_error_returns_failed(tmp_path, monkeypatch):
    main, wt = tmp_path / "main", tmp_path / "wt"
    _seed_workspace(main)
    _seed_state(wt, "flow-a")

    def boom(_ws):
        raise observe_at_close._WorkspaceConfigError("no [tracker] block")

    monkeypatch.setattr(observe_at_close, "_read_tracker_config", boom)

    result = observe_at_close.observe_at_close(main, "flow-a", wt)

    assert result["action"] == "failed"
    assert not _ship_events_dir(main).exists()


def test_is_shipped_error_returns_failed(tmp_path, monkeypatch):
    main, wt = tmp_path / "main", tmp_path / "wt"
    _seed_workspace(main)
    _seed_state(wt, "flow-a")
    _install_tracker(monkeypatch, _FakeTracker(_ship(), is_shipped_error=True))

    result = observe_at_close.observe_at_close(main, "flow-a", wt)

    assert result["action"] == "failed"
    assert not _ship_events_dir(main).exists()


# ─── 7. worktree auto-resolution from the pool when the param is omitted ──────


def test_auto_resolves_worktree_from_pool(tmp_path, monkeypatch):
    main = tmp_path / "main"
    _seed_workspace(main)
    wt = main / ".claude" / "worktrees" / "feat-flow-a-slug"
    _seed_state(wt, "flow-a")
    _seed_frontmatter(wt, "flow-a", lane="full")
    fake = _FakeTracker(_ship(), ticket={"labels": ["tier:trivial"], "description": ""})
    _install_tracker(monkeypatch, fake)

    result = observe_at_close.observe_at_close(main, "flow-a", None)

    assert result["action"] == "observed"
    data = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    # attribution + lane came from the pool-resolved worktree (state = run_dir/state.json, lane read
    # from run_dir.parents[2]).
    assert data["flow_attribution"]["plan_started_at_iso"] == "2026-05-28T00:00:00Z"
    assert data["lane"] == "full"
    assert data["tier"] == "tier:trivial"


def test_auto_resolution_absent_pool_skips_no_run_state(tmp_path, monkeypatch):
    main = tmp_path / "main"
    _seed_workspace(main)  # no worktree in the pool
    _install_tracker(monkeypatch, _FakeTracker(_ship()))

    result = observe_at_close.observe_at_close(main, "flow-a", None)

    assert result == {"action": "skipped", "reason": "no_run_state"}


# ─── 8. CLI: JSON shape + exit codes ─────────────────────────────────────────


def test_cli_observed_prints_json_exit_0(tmp_path, monkeypatch, capsys):
    main, wt = tmp_path / "main", tmp_path / "wt"
    _seed_workspace(main)
    _seed_state(wt, "flow-a")
    _install_tracker(monkeypatch, _FakeTracker(_ship()))

    rc = observe_at_close.cli_main(
        ["--workspace-root", str(main), "--key", "flow-a", "--worktree", str(wt)]
    )

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "observed"
    assert "path" in out


def test_cli_failed_exit_1(tmp_path, monkeypatch, capsys):
    main, wt = tmp_path / "main", tmp_path / "wt"
    _seed_workspace(main)
    _seed_state(wt, "flow-a")
    _install_tracker(monkeypatch, _FakeTracker(_ship(), is_shipped_error=True))

    rc = observe_at_close.cli_main(
        ["--workspace-root", str(main), "--key", "flow-a", "--worktree", str(wt)]
    )

    assert rc == 1
    assert json.loads(capsys.readouterr().out)["action"] == "failed"
