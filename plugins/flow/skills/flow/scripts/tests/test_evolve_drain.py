from __future__ import annotations

import json
import subprocess

import pytest

import evolve_drain as ed
import lease
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
    return repo / ".flow" / "worktrees" / f"feat-{key}-{slug}" / ".flow" / "runs" / key


# decide() reads `launch` and `launched_pending` from the select result; the rest of
# the in-flight picture comes from the `liveness` map the CLI builds over open PRs +
# in-flight beads. So the scenarios below are expressed through the liveness map plus
# launched_pending, not the other select fields.


def _sel(launch=None, launched_pending=None):
    return {"launch": launch or [], "launched_pending": launched_pending or []}


# ─── decide(): the termination core ──────────────────────────────────────────


def test_candidates_require_attended_planning():
    d = ed.decide(
        _sel(launch=["flow-a", "flow-b"]),
        {"flow-parked": "absent"},
    )
    assert d == {
        "action": "plan_required",
        "launch": [],
        "plan_required": ["flow-a", "flow-b"],
        "parked": ["flow-parked"],
    }


def test_drained_is_done():
    # nothing to launch, nothing in flight → terminal
    d = ed.decide(_sel(), {})
    assert d["action"] == "done"
    assert d["parked"] == []


def test_live_inflight_waits():
    d = ed.decide(_sel(), {"flow-run": "live"})
    assert d["action"] == "wait"
    assert d["plan_required"] == []


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


def test_launched_pending_blocks_even_with_no_live_lease():
    # a newly launched run is pre-lease (no run.lock yet) so liveness is empty, but it
    # has NOT finished → block termination, don't abandon a held_hot bead behind it.
    d = ed.decide(_sel(launched_pending=["flow-new"]), {})
    assert d["action"] == "wait"
    assert d["parked"] == []


def test_launched_pending_not_parked_alongside_an_absent_key():
    # a still-bootstrapping run is not human-handoff work: it waits, and its key never
    # lands in parked even when a genuinely parked absent key is also in flight.
    d = ed.decide(_sel(launched_pending=["flow-new"]), {"flow-new": "absent", "flow-old": "absent"})
    assert d["action"] == "wait"
    assert d["parked"] == ["flow-old"]


def test_launched_pending_empty_still_done():
    # guard against over-broad waiting: empty launched_pending + nothing live → done.
    d = ed.decide(_sel(launched_pending=[]), {})
    assert d["action"] == "done"
    assert d["parked"] == []


# ─── _run_dir_for / liveness_map: the worktree resolution ────────────────────


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


def test_liveness_map_reboot_clearable_lease(tmp_path, monkeypatch):
    # an expired lease from a previous boot on THIS host reads
    # expired_reboot_clearable, not expired_foreign: liveness_map passes
    # boot/hostname like recover.py. Both states are non-blocking for decide().
    repo = tmp_path / "flow"
    repo.mkdir()
    run_dir = _pool_run_dir(repo, "flow-rb")
    lease.acquire(
        run_dir,
        "run-test",
        1,
        "2020-01-01T00:00:00Z",
        stage="implement",
        current_boot="boot-OLD",
        hostname=lease.hostname(),
        cwd=str(run_dir),
    )
    monkeypatch.setattr(lease, "boot_id", lambda runner=None: "boot-NEW")
    assert ed.liveness_map(repo, ["flow-rb"]) == {"flow-rb": "expired_reboot_clearable"}
    assert ed.decide(_sel(), ed.liveness_map(repo, ["flow-rb"]))["action"] == "done"


# ─── cli_main: --include-proposals threading ─────────────────────────────────


def _stub_cli(monkeypatch, tmp_path, captured):
    repo = tmp_path / "flow"
    repo.mkdir()
    monkeypatch.setattr(ed, "resolve_maintainer_repo", lambda ws: repo)
    monkeypatch.setattr(ed, "_config_defaults", lambda ws: (5, 3))
    monkeypatch.setattr(ed, "liveness_map", lambda repo, keys: {})
    monkeypatch.setattr(ed, "_default_runner", lambda repo_: _StrandRunner())

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
    assert "NOTICE" in cap.err
    assert "planning candidates" in cap.err


# ─── cli_main: exit codes ────────────────────────────────────────────────────


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


# ─── cli_main: pre-PR live-run liveness (real lease + real liveness_map) ──────


def _stub_cli_live(monkeypatch, tmp_path, sel):
    """Like _stub_cli but leaves liveness_map REAL so it reads the on-disk lease."""
    repo = tmp_path / "flow"
    repo.mkdir()
    monkeypatch.setattr(ed, "resolve_maintainer_repo", lambda ws: repo)
    monkeypatch.setattr(ed, "_config_defaults", lambda ws: (5, 3))
    monkeypatch.setattr(ed, "select", lambda ws, **kw: sel)
    monkeypatch.setattr(ed, "_default_runner", lambda repo_: _StrandRunner())
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


def test_cli_open_pr_keys_come_from_select(monkeypatch, tmp_path, capsys):
    # cli_main reuses the open-PR keys select() already gathered (no second
    # `gh pr list`): an open-PR key with no worktree run dir reads absent → parked.
    sel = {"launch": [], "skipped_in_flight": [], "live_runs": [], "open_pr_keys": ["flow-pr"]}
    _stub_cli_live(monkeypatch, tmp_path, sel)
    rc = ed.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["liveness"] == {"flow-pr": "absent"}
    assert out["action"] == "done"
    assert out["parked"] == ["flow-pr"]


def test_cli_removes_launch_marker_once_registered(monkeypatch, tmp_path, capsys):
    # a launched key that has REGISTERED (live lease here) drops out of
    # launched_pending, so it stays out past any later merge/teardown (the
    # merged-teardown window is closed). The fleet entry itself is left alone --
    # it ages out on its own staleness clock, no physical removal needed.
    sel = {
        "launch": [],
        "skipped_in_flight": ["flow-k"],
        "live_runs": ["flow-k"],
        "launched_pending": ["flow-k"],
    }
    repo = tmp_path / "flow"
    repo.mkdir()
    monkeypatch.setattr(ed, "resolve_maintainer_repo", lambda ws: repo)
    monkeypatch.setattr(ed, "_config_defaults", lambda ws: (5, 3))
    monkeypatch.setattr(ed, "liveness_map", lambda repo, keys: {})
    monkeypatch.setattr(ed, "select", lambda ws, **kw: sel)
    monkeypatch.setattr(ed, "_default_runner", lambda repo_: _StrandRunner())

    rc = ed.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["select"]["launched_pending"] == []
    assert out["action"] == "done"


def test_cli_removes_launch_marker_via_open_pr_alone(monkeypatch, tmp_path, capsys):
    # registration proven by an OPEN PR, not a live lease: the run opened its PR then
    # its session ended (lease expired/absent), so live_runs lacks the key but
    # open_pr_keys has it. launched_pending MUST still drop; registered is the union,
    # and the open-PR half carries this case (kills the `| open_pr_keys` mutation).
    sel = {
        "launch": [],
        "skipped_in_flight": [],
        "live_runs": [],
        "open_pr_keys": ["flow-k"],
        "launched_pending": ["flow-k"],
    }
    repo = tmp_path / "flow"
    repo.mkdir()
    monkeypatch.setattr(ed, "resolve_maintainer_repo", lambda ws: repo)
    monkeypatch.setattr(ed, "_config_defaults", lambda ws: (5, 3))
    monkeypatch.setattr(ed, "liveness_map", lambda repo, keys: {})
    monkeypatch.setattr(ed, "select", lambda ws, **kw: sel)
    monkeypatch.setattr(ed, "_default_runner", lambda repo_: _StrandRunner())

    rc = ed.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "done"
    assert out["select"]["launched_pending"] == []


# ─── decide(): the STRANDED recover gate (flow-9ljv) ─────────────────────────


def test_stranded_nonempty_recovers():
    # a stranded pre-PR bead → action `recover` (NOT done), key carried sorted
    d = ed.decide(_sel(), {}, stranded=["flow-z", "flow-a"])
    assert d["action"] == "recover"
    assert d["stranded"] == ["flow-a", "flow-z"]
    assert d["launch"] == []


def test_stranded_excluded_from_parked():
    # a stranded key that ALSO shows in liveness as non-live must not double-count
    # into parked, it is reaped, not handed to the human.
    d = ed.decide(_sel(), {"flow-a": "absent", "flow-b": "absent"}, stranded=["flow-a"])
    assert d["action"] == "recover"
    assert d["stranded"] == ["flow-a"]
    assert d["parked"] == ["flow-b"]


def test_stranded_with_live_run_not_done():
    # stranded beats wait: even with a live run blocking, a true stranded bead must
    # be recovered (it touches only its own dead worktree, fleet-rechecked in prose).
    d = ed.decide(_sel(), {"flow-live": "live"}, stranded=["flow-s"])
    assert d["action"] == "recover"
    assert d["stranded"] == ["flow-s"]


def test_empty_stranded_is_done_with_stable_shape():
    d = ed.decide(_sel(), {}, stranded=[])
    expected = {"action": "done", "launch": [], "plan_required": [], "parked": []}
    assert d == expected
    assert ed.decide(_sel(), {}) == expected


def test_stranded_beats_fresh_candidates():
    d = ed.decide(_sel(launch=["flow-a"]), {}, stranded=["flow-s"])
    assert d == {
        "action": "recover",
        "launch": [],
        "plan_required": [],
        "stranded": ["flow-s"],
        "parked": [],
    }


@pytest.mark.parametrize(
    ("liveness", "launched_pending"),
    [
        ({"flow-live": "live"}, []),
        ({"flow-corrupt": "corrupt"}, []),
        ({}, ["flow-pending"]),
    ],
)
def test_existing_work_waits_before_fresh_candidates(liveness, launched_pending):
    d = ed.decide(
        _sel(launch=["flow-fresh"], launched_pending=launched_pending),
        liveness,
    )
    assert d["action"] == "wait"
    assert d["launch"] == []
    assert d["plan_required"] == []


# ─── cli_main: STRANDED detection (stub bd/gh reads + on-disk lease) ──────────


def _cp(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class _StrandRunner:
    """Stubs the bd-list (in_progress evolve) + gh-pr-list (merged) reads detection makes."""

    def __init__(self, *, in_progress=(), merged_prs=()):
        self.in_progress = list(in_progress)
        self.merged_prs = list(merged_prs)
        self.bd_list_calls: list[list[str]] = []

    def __call__(self, args):
        if args[:2] == ["bd", "list"]:
            self.bd_list_calls.append(list(args))
            return _cp(json.dumps([{"id": k} for k in self.in_progress]))
        if args[:3] == ["gh", "pr", "list"]:
            return _cp(json.dumps(self.merged_prs))
        raise AssertionError(f"unexpected tool call: {args}")


def _stub_cli_strand(monkeypatch, tmp_path, sel, runner):
    """Stubbed select/maintainer/config + injected runner; liveness_map stays REAL."""
    repo = tmp_path / "flow"
    repo.mkdir()
    monkeypatch.setattr(ed, "resolve_maintainer_repo", lambda ws: repo)
    monkeypatch.setattr(ed, "_config_defaults", lambda ws: (5, 3))
    monkeypatch.setattr(ed, "select", lambda ws, **kw: sel)
    monkeypatch.setattr(ed, "_default_runner", lambda repo_: runner)
    return repo


def test_cli_stranded_true_positive_recovers(monkeypatch, tmp_path, capsys):
    # in_progress + non-live (no worktree → absent) + no-PR + not launched_pending → STRANDED
    sel = {"launch": [], "skipped_in_flight": [], "live_runs": [], "open_pr_keys": []}
    runner = _StrandRunner(in_progress=["flow-strand"])
    _stub_cli_strand(monkeypatch, tmp_path, sel, runner)
    rc = ed.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "recover"
    assert [e["key"] for e in out["stranded_pre_pr"]] == ["flow-strand"]
    assert out["stranded"] == ["flow-strand"]


def test_cli_stranded_skips_launched_pending(monkeypatch, tmp_path, capsys):
    # launched_pending + in_progress + non-live + no-PR → NOT stranded (still booting)
    sel = {
        "launch": [],
        "skipped_in_flight": [],
        "live_runs": [],
        "open_pr_keys": [],
        "launched_pending": ["flow-boot"],
    }
    runner = _StrandRunner(in_progress=["flow-boot"])
    _stub_cli_strand(monkeypatch, tmp_path, sel, runner)
    rc = ed.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stranded_pre_pr"] == []
    assert out["action"] == "wait"  # launched_pending still blocks


def test_cli_stranded_skips_live_lease(monkeypatch, tmp_path, capsys):
    # in_progress + LIVE lease → NOT stranded (the run is still working)
    sel = {"launch": [], "skipped_in_flight": [], "live_runs": [], "open_pr_keys": []}
    runner = _StrandRunner(in_progress=["flow-live"])
    repo = _stub_cli_strand(monkeypatch, tmp_path, sel, runner)
    _write_lease(_pool_run_dir(repo, "flow-live"))
    rc = ed.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stranded_pre_pr"] == []


def test_cli_stranded_skips_open_pr(monkeypatch, tmp_path, capsys):
    # in_progress + open PR (in select's open_pr_keys) → NOT stranded
    sel = {
        "launch": [],
        "skipped_in_flight": [],
        "live_runs": [],
        "open_pr_keys": ["flow-pr"],
    }
    runner = _StrandRunner(in_progress=["flow-pr"])
    _stub_cli_strand(monkeypatch, tmp_path, sel, runner)
    rc = ed.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stranded_pre_pr"] == []


def test_cli_stranded_skips_merged_pr(monkeypatch, tmp_path, capsys):
    # in_progress + MERGED PR → NOT stranded (a different inconsistency: close, not relaunch)
    sel = {"launch": [], "skipped_in_flight": [], "live_runs": [], "open_pr_keys": []}
    runner = _StrandRunner(
        in_progress=["flow-merged"],
        merged_prs=[{"number": 7, "headRefName": "feat/flow-merged-slug"}],
    )
    _stub_cli_strand(monkeypatch, tmp_path, sel, runner)
    rc = ed.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stranded_pre_pr"] == []


def test_cli_stranded_query_is_evolve_label_scoped(monkeypatch, tmp_path, capsys):
    # the cross-domain-stomp guard: detection must query `bd list -l evolve --status
    # in_progress`, NEVER a bare unscoped `--status in_progress` (which would let the
    # evolve drain reap a day-job run's worktree in the shared pool). Assert the argv,
    # not merely empty-in→empty-out, a regression dropping `-l evolve` stays caught.
    sel = {"launch": [], "skipped_in_flight": [], "live_runs": [], "open_pr_keys": []}
    runner = _StrandRunner(in_progress=[])
    _stub_cli_strand(monkeypatch, tmp_path, sel, runner)
    rc = ed.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stranded_pre_pr"] == []
    assert out["action"] == "done"
    assert runner.bd_list_calls, "detection must run a bd list query"
    assert all("-l" in c for c in runner.bd_list_calls), "every query must be label-scoped"
    assert any("evolve" in c for c in runner.bd_list_calls), "must query the evolve label"
    assert not any("--status" in c and "-l" not in c for c in runner.bd_list_calls), (
        "no bare unscoped in_progress query"
    )
    assert runner.bd_list_calls, "detection never queried bd list"
    for call in runner.bd_list_calls:
        assert "in_progress" in call
        i = call.index("-l")
        assert call[i + 1] == "evolve"


# ─── stranded_pre_pr: injectable in_progress scope (flow-y8zs, queue parity) ───


def test_stranded_pre_pr_injected_keys_bypass_evolve_query(tmp_path):
    # queue_drain's day-job path injects its own in_progress set; stranded_pre_pr
    # then uses THAT set verbatim and never runs the evolve-label bd list query.
    repo = tmp_path / "flow"
    repo.mkdir()
    runner = _StrandRunner(in_progress=["flow-evolve-unused"])
    out = ed.stranded_pre_pr(
        repo,
        runner,
        launched_pending=set(),
        open_pr_keys=set(),
        in_progress_keys={"flow-dayjob"},
    )
    assert [e["key"] for e in out] == ["flow-dayjob"]
    # the injected set bypasses _inprogress_evolve_keys → no bd list query ran
    assert runner.bd_list_calls == []


def test_stranded_pre_pr_default_uses_evolve_query(tmp_path):
    # the evolve path (in_progress_keys=None) still computes the set via the
    # evolve-label bd list query, for back-compat with evolve_drain.cli_main.
    repo = tmp_path / "flow"
    repo.mkdir()
    runner = _StrandRunner(in_progress=["flow-evo"])
    out = ed.stranded_pre_pr(repo, runner, launched_pending=set(), open_pr_keys=set())
    assert [e["key"] for e in out] == ["flow-evo"]
    assert runner.bd_list_calls, "the default path must run the evolve-scoped query"


def test_stranded_pre_pr_injected_empty_returns_empty(tmp_path):
    # an injected empty set short-circuits (no merged/gh probe needed).
    repo = tmp_path / "flow"
    repo.mkdir()
    runner = _StrandRunner(in_progress=["flow-evolve-unused"])
    out = ed.stranded_pre_pr(
        repo, runner, launched_pending=set(), open_pr_keys=set(), in_progress_keys=set()
    )
    assert out == []
    assert runner.bd_list_calls == []
