from __future__ import annotations

import evolve_drain as ed

# decide() reads only `launch` from the select result; the in-flight picture comes
# entirely from the `liveness` map the CLI builds over open PRs + in-flight beads.
# So the scenarios below are expressed through the liveness map, not select fields.


def _sel(launch=None):
    return {"launch": launch or []}


# ─── decide() — the termination core ─────────────────────────────────────────


def test_launch_nonempty_launches():
    d = ed.decide(_sel(launch=["flow-a", "flow-b"]), {})
    assert d["action"] == "launch"
    assert d["launch"] == ["flow-a", "flow-b"]
    assert d["parked"] == []


def test_drained_is_done():
    # nothing to launch, nothing in flight → terminal
    d = ed.decide(_sel(), {})
    assert d["action"] == "done"
    assert d["parked"] == []


def test_live_inflight_waits():
    d = ed.decide(_sel(), {"flow-run": "live"})
    assert d["action"] == "wait"


def test_held_hot_blocked_by_live_run_waits():
    # a hot bead is held because another hot is still running → wait, don't bail
    d = ed.decide(_sel(), {"flow-hot1": "live"})
    assert d["action"] == "wait"


def test_held_hot_blocked_by_parked_hot_is_done():
    # the blocking hot was WITHHELD (held_guard): ready PR + branch, but session
    # ended → lease non-live → loop terminates, leaves it for the human (no spin)
    d = ed.decide(_sel(), {"flow-hot1": "expired_foreign"})
    assert d["action"] == "done"
    assert d["parked"] == ["flow-hot1"]


def test_backpressure_with_a_live_run_waits():
    # the PR cap is full but a run is still working → wait for it to merge + free cap.
    # (real shape: select returns held_backpressure + empty skipped_in_flight; the
    # cap-occupying run shows up only via the open-PR liveness the CLI computes.)
    d = ed.decide(_sel(), {"flow-busy": "live"})
    assert d["action"] == "wait"


def test_backpressure_all_parked_is_done():
    # cap occupied entirely by parked PRs the human must clear → nothing to do
    d = ed.decide(_sel(), {"flow-x": "expired_foreign", "flow-y": "absent"})
    assert d["action"] == "done"
    assert d["parked"] == ["flow-x", "flow-y"]


def test_absent_rundir_counts_as_non_live():
    # a leaked branch / PR with no worktree run dir → "absent" → never waited on
    d = ed.decide(_sel(), {"flow-orphan": "absent"})
    assert d["action"] == "done"
    assert d["parked"] == ["flow-orphan"]


def test_mixed_live_and_parked_waits_and_reports_parked():
    d = ed.decide(_sel(), {"flow-live": "live", "flow-parked": "absent"})
    assert d["action"] == "wait"
    assert d["parked"] == ["flow-parked"]


# ─── _run_dir_for / liveness_map — the worktree resolution ───────────────────


def test_run_dir_for_absent_returns_none(tmp_path):
    repo = tmp_path / "flow"
    repo.mkdir()
    assert ed._run_dir_for(repo, "flow-nope") is None


def test_run_dir_for_finds_sibling_worktree(tmp_path):
    repo = tmp_path / "flow"
    repo.mkdir()
    run_dir = (
        tmp_path / "flow.worktrees" / "feature-flow-abc-some-slug" / ".flow" / "runs" / "flow-abc"
    )
    run_dir.mkdir(parents=True)
    assert ed._run_dir_for(repo, "flow-abc") == run_dir


def test_liveness_map_absent_key(tmp_path):
    repo = tmp_path / "flow"
    repo.mkdir()
    assert ed.liveness_map(repo, ["flow-gone"]) == {"flow-gone": "absent"}
