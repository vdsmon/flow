from __future__ import annotations

import json
import subprocess

import launch_ledger
import lease
import queue_drain as qd
from _timeutil import utcnow_iso


def _write_lease(run_dir, *, expired: bool = False) -> None:
    now = "2020-01-01T00:00:00Z" if expired else utcnow_iso()
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


def _cp(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class _StubRunner:
    """Answers the three tool calls cli_main makes outside select()."""

    def __init__(self, *, evolve_keys=(), merged_prs=(), bead_status=None):
        self.evolve_keys = list(evolve_keys)
        self.merged_prs = list(merged_prs)
        self.bead_status = dict(bead_status or {})
        self.bd_show_calls: list[str] = []

    def __call__(self, args):
        if args[:2] == ["bd", "list"]:
            return _cp(json.dumps([{"id": k} for k in self.evolve_keys]))
        if args[:3] == ["gh", "pr", "list"]:
            return _cp(json.dumps(self.merged_prs))
        if args[:2] == ["bd", "show"]:
            key = args[2]
            self.bd_show_calls.append(key)
            return _cp(json.dumps({"id": key, "status": self.bead_status.get(key, "closed")}))
        raise AssertionError(f"unexpected tool call: {args}")


def _sel(**kw):
    base = {
        "launch": [],
        "skipped_in_flight": [],
        "live_runs": [],
        "open_pr_keys": [],
        "launched_pending": [],
    }
    base.update(kw)
    return base


def _stub_cli(monkeypatch, tmp_path, sel, runner=None):
    """Stubbed select + maintainer + config; liveness_map stays REAL."""
    repo = tmp_path / "flow"
    repo.mkdir()
    monkeypatch.setattr(qd, "resolve_maintainer_repo", lambda ws: repo)
    monkeypatch.setattr(qd, "_config_defaults", lambda ws: (5, 3))
    monkeypatch.setattr(qd, "select", lambda ws, **kw: sel)
    stub = runner or _StubRunner()
    monkeypatch.setattr(qd, "_default_runner", lambda repo_: stub)
    return repo


def _out(capsys):
    return json.loads(capsys.readouterr().out)


# ─── classify_reap — the pure reap classification ────────────────────────────


def test_classify_reap_active_bead_with_worktree():
    merged = [{"number": 7, "headRefName": "feature/flow-a-some-slug"}]
    out = qd.classify_reap(merged, {"flow-a"}, {"flow-a": "open"}, worktree_keys={"flow-a"})
    assert out == [
        {
            "key": "flow-a",
            "branch": "feature/flow-a-some-slug",
            "pr": 7,
            "bead_active": True,
            "has_worktree": True,
        }
    ]


def test_classify_reap_excludes_non_candidate_keys():
    # bead closed + worktree gone: the key never entered candidate_keys → excluded
    merged = [{"number": 8, "headRefName": "feature/flow-gone-x"}]
    assert qd.classify_reap(merged, set(), {}) == []


def test_classify_reap_closed_bead_with_worktree_is_teardown_only():
    merged = [{"number": 9, "headRefName": "feature/flow-b-x"}]
    out = qd.classify_reap(merged, {"flow-b"}, {"flow-b": "closed"}, worktree_keys={"flow-b"})
    assert out[0]["bead_active"] is False
    assert out[0]["has_worktree"] is True


def test_classify_reap_deferred_bead_is_not_active():
    # deferred is the human's triage call: never auto-closed by the reap path
    merged = [{"number": 10, "headRefName": "feature/flow-c-x"}]
    out = qd.classify_reap(merged, {"flow-c"}, {"flow-c": "deferred"}, worktree_keys={"flow-c"})
    assert out[0]["bead_active"] is False


def test_classify_reap_ignores_non_flow_head_refs():
    merged = [
        {"number": 11, "headRefName": "main"},
        {"number": 12, "headRefName": "dependabot/pip/foo-1.2"},
    ]
    assert qd.classify_reap(merged, {"flow-a"}, {}) == []


def test_classify_reap_launch_key_without_worktree():
    # a merged-PR key re-offered by select (bead still open, worktree already gone)
    merged = [{"number": 13, "headRefName": "feature/flow-k-x"}]
    out = qd.classify_reap(merged, {"flow-k"}, {"flow-k": "open"})
    assert out == [
        {
            "key": "flow-k",
            "branch": "feature/flow-k-x",
            "pr": 13,
            "bead_active": True,
            "has_worktree": False,
        }
    ]


def test_classify_reap_dedupes_keys_first_pr_wins():
    merged = [
        {"number": 20, "headRefName": "feature/flow-d-second"},
        {"number": 19, "headRefName": "feature/flow-d-first"},
    ]
    out = qd.classify_reap(merged, {"flow-d"}, {"flow-d": "open"})
    assert len(out) == 1
    assert out[0]["pr"] == 20


# ─── cli_main — launch exclusion of merged keys ──────────────────────────────


def test_cli_drops_launch_key_with_merged_pr(monkeypatch, tmp_path, capsys):
    # merged-but-unclosed bead: select re-offers it, the reap set diverts it to
    # the close path — it must never relaunch.
    runner = _StubRunner(
        merged_prs=[{"number": 30, "headRefName": "feature/flow-k-x"}],
        bead_status={"flow-k": "open"},
    )
    _stub_cli(monkeypatch, tmp_path, _sel(launch=["flow-k"]), runner=runner)
    rc = qd.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = _out(capsys)
    assert out["launch"] == []
    assert out["action"] == "done"
    assert out["reap"] == [
        {
            "key": "flow-k",
            "branch": "feature/flow-k-x",
            "pr": 30,
            "bead_active": True,
            "has_worktree": False,
        }
    ]


def test_cli_launch_passthrough_smoke(monkeypatch, tmp_path, capsys):
    # decide() contract is owned by test_evolve_drain; just prove the passthrough
    _stub_cli(monkeypatch, tmp_path, _sel(launch=["flow-a", "flow-b"]))
    rc = qd.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = _out(capsys)
    assert out["action"] == "launch"
    assert out["launch"] == ["flow-a", "flow-b"]
    assert out["reap"] == []
    assert out["select"]["launch"] == ["flow-a", "flow-b"]


def test_cli_reap_classifies_worktree_key(monkeypatch, tmp_path, capsys):
    # a merged PR whose worktree is still registered (run exited, lease expired)
    # → teardown-only reap entry, and the loop reads done, not wait.
    runner = _StubRunner(
        merged_prs=[{"number": 31, "headRefName": "feature/flow-m-z"}],
        bead_status={"flow-m": "closed"},
    )
    repo = _stub_cli(monkeypatch, tmp_path, _sel(), runner=runner)
    run_dir = _pool_run_dir(repo, "flow-m", slug="z")
    run_dir.mkdir(parents=True)
    _write_lease(run_dir, expired=True)
    rc = qd.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = _out(capsys)
    assert out["action"] == "done"
    assert out["reap"] == [
        {
            "key": "flow-m",
            "branch": "feature/flow-m-z",
            "pr": 31,
            "bead_active": False,
            "has_worktree": True,
        }
    ]


# ─── cli_main — queue-scoping of the wait gate ───────────────────────────────


def test_cli_scopes_liveness_to_day_job_keys(monkeypatch, tmp_path, capsys):
    # the worktree pool is shared with the evolve drain: an active evolve key in
    # live_runs must not enter this loop's liveness picture.
    runner = _StubRunner(evolve_keys=["flow-evolve1"])
    sel = _sel(live_runs=["flow-evolve1", "flow-day1"])
    repo = _stub_cli(monkeypatch, tmp_path, sel, runner=runner)
    _write_lease(_pool_run_dir(repo, "flow-day1"))
    _write_lease(_pool_run_dir(repo, "flow-evolve1"))
    rc = qd.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = _out(capsys)
    assert out["liveness"] == {"flow-day1": "live"}
    assert out["action"] == "wait"


def test_cli_live_evolve_run_alone_is_done(monkeypatch, tmp_path, capsys):
    runner = _StubRunner(evolve_keys=["flow-evolve1"])
    sel = _sel(live_runs=["flow-evolve1"])
    repo = _stub_cli(monkeypatch, tmp_path, sel, runner=runner)
    _write_lease(_pool_run_dir(repo, "flow-evolve1"))
    rc = qd.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = _out(capsys)
    assert out["liveness"] == {}
    assert out["action"] == "done"


def test_cli_evolve_launched_pending_does_not_block(monkeypatch, tmp_path, capsys):
    # the launch ledger is shared too: an evolve drain's pre-lease launch marker
    # must not hold THIS loop's termination gate (and its marker is evolve's to
    # remove, never this loop's).
    runner = _StubRunner(evolve_keys=["flow-ev"])
    repo = _stub_cli(monkeypatch, tmp_path, _sel(launched_pending=["flow-ev"]), runner=runner)
    launch_ledger.add(repo, "flow-ev")
    marker = repo / ".flow" / "launch-ledger" / "flow-ev"
    rc = qd.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = _out(capsys)
    assert out["action"] == "done"
    assert out["select"]["launched_pending"] == []
    assert marker.exists()


# ─── cli_main — marker removal at registration ───────────────────────────────


def test_cli_removes_launch_marker_once_registered(monkeypatch, tmp_path, capsys):
    # a launched key that has REGISTERED (live lease here) drops out of the ledger:
    # cli_main physically unlinks its marker, so it stays out of launched_pending past
    # any later merge/teardown (the merged-teardown window is closed).
    sel = _sel(
        skipped_in_flight=["flow-k"],
        live_runs=["flow-k"],
        launched_pending=["flow-k"],
    )
    repo = _stub_cli(monkeypatch, tmp_path, sel)
    monkeypatch.setattr(qd, "liveness_map", lambda repo, keys: {})
    launch_ledger.add(repo, "flow-k")
    marker = repo / ".flow" / "launch-ledger" / "flow-k"
    assert marker.exists()

    rc = qd.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    assert not marker.exists()
    out = _out(capsys)
    assert out["action"] == "done"
    assert out["select"]["launched_pending"] == []


def test_cli_removes_launch_marker_via_open_pr_alone(monkeypatch, tmp_path, capsys):
    # registration proven by an OPEN PR, not a live lease: the run opened its PR then
    # its session ended (lease expired/absent), so live_runs lacks the key but
    # open_pr_keys has it. The marker MUST still drop — registered is the union, and
    # the open-PR half carries this case (kills the `| open_pr_keys` mutation).
    sel = _sel(
        open_pr_keys=["flow-k"],
        launched_pending=["flow-k"],
    )
    repo = _stub_cli(monkeypatch, tmp_path, sel)
    monkeypatch.setattr(qd, "liveness_map", lambda repo, keys: {})
    launch_ledger.add(repo, "flow-k")
    marker = repo / ".flow" / "launch-ledger" / "flow-k"
    assert marker.exists()

    rc = qd.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    assert not marker.exists()
    out = _out(capsys)
    assert out["action"] == "done"
    assert out["select"]["launched_pending"] == []


def test_cli_unregistered_pending_still_blocks(monkeypatch, tmp_path, capsys):
    # a launched-but-pre-lease day-job key keeps blocking until it registers or
    # its marker TTL-expires.
    _stub_cli(monkeypatch, tmp_path, _sel(launched_pending=["flow-new"]))
    rc = qd.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    assert _out(capsys)["action"] == "wait"


# ─── cli_main — lease liveness (real lease + real liveness_map) ──────────────


def test_cli_pre_pr_live_run_waits(monkeypatch, tmp_path, capsys):
    repo = _stub_cli(monkeypatch, tmp_path, _sel(live_runs=["flow-x"]))
    _write_lease(_pool_run_dir(repo, "flow-x"))
    rc = qd.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = _out(capsys)
    assert out["action"] == "wait"
    assert out["liveness"]["flow-x"] == "live"


def test_cli_pre_pr_expired_run_done_and_parked(monkeypatch, tmp_path, capsys):
    repo = _stub_cli(monkeypatch, tmp_path, _sel(live_runs=["flow-x"]))
    _write_lease(_pool_run_dir(repo, "flow-x"), expired=True)
    rc = qd.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = _out(capsys)
    assert out["action"] == "done"
    assert out["liveness"]["flow-x"] == "expired_foreign"
    assert out["parked"] == ["flow-x"]


def test_cli_open_pr_key_without_run_dir_is_parked(monkeypatch, tmp_path, capsys):
    # cli_main reuses the open-PR keys select() already gathered (no second
    # `gh pr list --state open`): no worktree run dir reads absent → parked.
    _stub_cli(monkeypatch, tmp_path, _sel(open_pr_keys=["flow-pr"]))
    rc = qd.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = _out(capsys)
    assert out["liveness"] == {"flow-pr": "absent"}
    assert out["action"] == "done"
    assert out["parked"] == ["flow-pr"]


# ─── cli_main — exit codes ───────────────────────────────────────────────────


def _plain_ws(tmp_path):
    d = tmp_path / "proj"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text('[tracker]\nbackend = "beads"\n', encoding="utf-8")
    return d


def test_cli_not_maintainer_dormant_exit_4(tmp_path, monkeypatch, capsys):
    # patch maintainer._global_config_path, not qd.resolve_maintainer_repo:
    # resolve_maintainer_repo reads _global_config_path from maintainer's globals
    # at call time, so the directly-imported func still sees the patch (real boundary)
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    plain = _plain_ws(tmp_path)
    rc = qd.cli_main(["--workspace-root", str(plain)])
    assert rc == 4
    assert "drain is dormant" in capsys.readouterr().err


def test_cli_select_not_maintainer_exit_4(monkeypatch, tmp_path, capsys):
    _stub_cli(monkeypatch, tmp_path, _sel())

    def fake_select(ws, **kw):
        raise qd.NotMaintainer("select says not maintainer")

    monkeypatch.setattr(qd, "select", fake_select)
    rc = qd.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 4
    assert "select says not maintainer" in capsys.readouterr().err


def test_cli_tool_error_exit_2(monkeypatch, tmp_path, capsys):
    _stub_cli(monkeypatch, tmp_path, _sel())

    def fake_select(ws, **kw):
        raise qd.ToolError("bd blew up")

    monkeypatch.setattr(qd, "select", fake_select)
    rc = qd.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 2
    assert "bd blew up" in capsys.readouterr().err


def test_cli_bead_status_gather_is_bounded(monkeypatch, tmp_path, capsys):
    # bd show fires only for merged-flow-PR keys that are also candidates
    runner = _StubRunner(
        merged_prs=[
            {"number": 40, "headRefName": "feature/flow-k-x"},
            {"number": 41, "headRefName": "feature/flow-other-y"},
        ],
        bead_status={"flow-k": "open"},
    )
    _stub_cli(monkeypatch, tmp_path, _sel(launch=["flow-k"]), runner=runner)
    rc = qd.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    assert runner.bd_show_calls == ["flow-k"]
    assert _out(capsys)["reap"][0]["key"] == "flow-k"
