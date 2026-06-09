from __future__ import annotations

import json

import evolve_drain as ed
import lease


def _write_lease(run_dir, *, expired: bool = False) -> None:
    now = "2020-01-01T00:00:00Z" if expired else lease._utcnow_iso()
    ttl = 1 if expired else 3600
    lease.acquire(
        run_dir,
        "run-test",
        ttl,
        now,
        stage="implement",
        current_boot="boot-A",
        hostname="host-1",
        cwd=str(run_dir),
    )


def _pool_run_dir(repo, key, slug="wip"):
    return repo / ".flow" / "worktrees" / f"feature-{key}-{slug}" / ".flow" / "runs" / key


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


def test_corrupt_inflight_blocks_like_live():
    # corrupt is BLOCKING: a corrupt in-flight lease cannot be confirmed dead,
    # so it must never drain to "done" (it gates a self-merge).
    d = ed.decide(_sel(), {"flow-corrupt": "corrupt"})
    assert d["action"] == "wait"
    # blocking, not parked: corrupt is in neither the parked nor the done bucket.
    assert d["parked"] == []


def test_corrupt_with_parked_still_waits():
    d = ed.decide(_sel(), {"flow-corrupt": "corrupt", "flow-parked": "absent"})
    assert d["action"] == "wait"
    assert d["parked"] == ["flow-parked"]


# ─── _run_dir_for / liveness_map — the worktree resolution ───────────────────


def test_run_dir_for_absent_returns_none(tmp_path):
    repo = tmp_path / "flow"
    repo.mkdir()
    assert ed._run_dir_for(repo, "flow-nope") is None


def test_run_dir_for_finds_pool_worktree(tmp_path):
    repo = tmp_path / "flow"
    repo.mkdir()
    run_dir = _pool_run_dir(repo, "flow-abc", slug="some-slug")
    run_dir.mkdir(parents=True)
    assert ed._run_dir_for(repo, "flow-abc") == run_dir


def test_liveness_map_absent_key(tmp_path):
    repo = tmp_path / "flow"
    repo.mkdir()
    assert ed.liveness_map(repo, ["flow-gone"]) == {"flow-gone": "absent"}


def test_liveness_map_surfaces_corrupt(tmp_path):
    # a corrupt run.lock surfaces as "corrupt" (not absent, not a crash), so the
    # raw liveness picture still shows it for diagnosis.
    repo = tmp_path / "flow"
    repo.mkdir()
    run_dir = _pool_run_dir(repo, "flow-bad")
    run_dir.mkdir(parents=True)
    lease.run_lock_path(run_dir).write_text("{not json", encoding="utf-8")
    assert ed.liveness_map(repo, ["flow-bad"]) == {"flow-bad": "corrupt"}


# ─── cli_main — --include-proposals threading ────────────────────────────────


def _stub_cli(monkeypatch, tmp_path, captured):
    repo = tmp_path / "flow"
    repo.mkdir()
    monkeypatch.setattr(ed, "resolve_maintainer_repo", lambda ws: repo)
    monkeypatch.setattr(ed, "_config_defaults", lambda ws: (5, 3))
    monkeypatch.setattr(ed, "_open_pr_keys", lambda repo: [])
    monkeypatch.setattr(ed, "liveness_map", lambda repo, keys: {})

    def fake_select(ws, *, cap, concurrency, include_proposals=False):
        captured["include_proposals"] = include_proposals
        return _sel()

    monkeypatch.setattr(ed, "select", fake_select)
    return repo


def test_cli_default_does_not_include_proposals(monkeypatch, tmp_path, capsys):
    captured = {}
    _stub_cli(monkeypatch, tmp_path, captured)
    rc = ed.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    assert captured["include_proposals"] is False
    out = json.loads(capsys.readouterr().out)
    assert out["include_proposals"] is False


def test_cli_include_proposals_threads_to_select(monkeypatch, tmp_path, capsys):
    captured = {}
    _stub_cli(monkeypatch, tmp_path, captured)
    rc = ed.cli_main(["--workspace-root", str(tmp_path), "--include-proposals"])
    assert rc == 0
    assert captured["include_proposals"] is True
    cap = capsys.readouterr()
    assert json.loads(cap.out)["include_proposals"] is True
    assert "WARNING" in cap.err  # the dangerous-mode banner fires


# ─── cli_main — exit codes ───────────────────────────────────────────────────


def _plain_ws(tmp_path):
    d = tmp_path / "proj"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text('[tracker]\nbackend = "beads"\n', encoding="utf-8")
    return d


def test_cli_not_maintainer_dormant_exit_4(tmp_path, monkeypatch, capsys):
    # patch maintainer._global_config_path, not ed.resolve_maintainer_repo:
    # resolve_maintainer_repo reads _global_config_path from maintainer's globals
    # at call time, so the directly-imported func still sees the patch (real boundary)
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    plain = _plain_ws(tmp_path)
    rc = ed.cli_main(["--workspace-root", str(plain)])
    assert rc == 4
    assert "drain is dormant" in capsys.readouterr().err


def test_cli_select_not_maintainer_exit_4(monkeypatch, tmp_path, capsys):
    _stub_cli(monkeypatch, tmp_path, {})

    def fake_select(ws, *, cap, concurrency, include_proposals=False):
        raise ed.NotMaintainer("select says not maintainer")

    monkeypatch.setattr(ed, "select", fake_select)
    rc = ed.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 4
    assert "select says not maintainer" in capsys.readouterr().err


def test_cli_tool_error_exit_2(monkeypatch, tmp_path, capsys):
    _stub_cli(monkeypatch, tmp_path, {})

    def fake_select(ws, *, cap, concurrency, include_proposals=False):
        raise ed.ToolError("bd blew up")

    monkeypatch.setattr(ed, "select", fake_select)
    rc = ed.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 2
    assert "bd blew up" in capsys.readouterr().err


# ─── cli_main — pre-PR live-run liveness (real lease + real liveness_map) ─────


def _stub_cli_live(monkeypatch, tmp_path, sel):
    """Like _stub_cli but leaves liveness_map REAL so it reads the on-disk lease."""
    repo = tmp_path / "flow"
    repo.mkdir()
    monkeypatch.setattr(ed, "resolve_maintainer_repo", lambda ws: repo)
    monkeypatch.setattr(ed, "_config_defaults", lambda ws: (5, 3))
    monkeypatch.setattr(ed, "_open_pr_keys", lambda repo: [])
    monkeypatch.setattr(ed, "select", lambda ws, **kw: sel)
    return repo


def test_cli_pre_pr_live_run_waits(monkeypatch, tmp_path, capsys):
    sel = {"launch": [], "skipped_in_flight": [], "live_runs": ["flow-x"]}
    repo = _stub_cli_live(monkeypatch, tmp_path, sel)
    _write_lease(_pool_run_dir(repo, "flow-x"))
    rc = ed.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "wait"
    assert out["liveness"]["flow-x"] == "live"


def test_cli_pre_pr_expired_run_done(monkeypatch, tmp_path, capsys):
    sel = {"launch": [], "skipped_in_flight": [], "live_runs": ["flow-x"]}
    repo = _stub_cli_live(monkeypatch, tmp_path, sel)
    _write_lease(_pool_run_dir(repo, "flow-x"), expired=True)
    rc = ed.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "done"
    assert out["liveness"]["flow-x"] == "expired_foreign"
