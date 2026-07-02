from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import launch_ledger
import lease
import queue_status as qst
from _timeutil import utcnow_iso

Recorder = list[list[str]]

# every command queue_status may legitimately run; anything else is a mutation
_READ_ONLY_PREFIXES = (
    ["bd", "ready"],
    ["bd", "list"],
    ["gh", "pr", "list"],
    ["git", "for-each-ref"],
)


def _write_lease(run_dir: Path, *, expired: bool = False) -> None:
    """Acquire a real lease in run_dir (live by default, expired on request)."""
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


def _pool_run_dir(repo: Path, key: str, slug: str = "wip") -> Path:
    return repo / ".flow" / "worktrees" / f"feat-{key}-{slug}" / ".flow" / "runs" / key


def _cand(
    key: str,
    *,
    priority: int = 2,
    labels: list[str] | None = None,
    title: str | None = None,
    issue_type: str = "task",
) -> dict:
    out = {
        "id": key,
        "priority": priority,
        "labels": labels if labels is not None else [],
        "issue_type": issue_type,
        "description": "no blast line",
    }
    if title is not None:
        out["title"] = title
    return out


def _marked_ws(tmp_path: Path) -> Path:
    d = tmp_path / "flow"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text(
        "[maintainer]\nself_target = true\n", encoding="utf-8"
    )
    return d


def _dispatch(
    *,
    ready: list[dict],
    prs: list[dict] | None = None,
    branches: str = "",
    evolve_list: list[dict] | None = None,
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], Recorder]:
    calls: Recorder = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["bd", "ready"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(ready), "")
        if args[:2] == ["bd", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(evolve_list or []), "")
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(prs or []), "")
        if args[:2] == ["git", "for-each-ref"]:
            return subprocess.CompletedProcess(args, 0, branches, "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected: {args}")

    return run, calls


# ---- status(): happy path ----


def test_happy_path_ready_and_launch(tmp_path):
    ws = _marked_ws(tmp_path)
    run, calls = _dispatch(
        ready=[
            _cand("flow-b", priority=2, title="second"),
            _cand("flow-a", priority=1, title="first"),
            _cand("flow-ev", labels=["evolve"]),
            _cand("flow-prop", labels=["proposal"]),
            _cand("flow-hot", labels=["hot"]),
            _cand("flow-epi", issue_type="epic"),
        ]
    )
    out = qst.status(ws, cap=5, concurrency=3, runner=run)
    assert [r["id"] for r in out["ready"]] == ["flow-a", "flow-b"]
    assert out["ready"][0]["title"] == "first"
    assert out["launch"] == ["flow-a", "flow-b"]
    assert out["action"] == "launch"
    assert out["parked"] == []
    assert out["liveness"] == {}
    # the status verb re-reads the full backlog: one bd ready inside select(),
    # one for the ready listing (select hides the budget-overflow tail)
    assert calls.count(["bd", "ready", "--json"]) == 2


def test_ready_sorts_by_priority_then_id(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(
        ready=[
            _cand("flow-z", priority=1),
            _cand("flow-m", priority=2),
            _cand("flow-a", priority=2),
        ]
    )
    out = qst.status(ws, cap=5, concurrency=3, runner=run)
    assert [r["id"] for r in out["ready"]] == ["flow-z", "flow-a", "flow-m"]


def test_ready_tolerates_missing_labels_and_title(tmp_path):
    # live `bd ready --json` omits the labels key for unlabeled beads
    ws = _marked_ws(tmp_path)
    cand = _cand("flow-a")
    del cand["labels"]
    run, _ = _dispatch(ready=[cand])
    out = qst.status(ws, cap=5, concurrency=3, runner=run)
    assert out["ready"] == [{"id": "flow-a", "priority": 2, "labels": [], "title": None}]


def test_ready_lists_past_the_launch_budget(tmp_path):
    # select() stops partitioning at the budget; the ready listing is the
    # whole backlog depth
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand(f"flow-{i}") for i in range(5)])
    out = qst.status(ws, cap=5, concurrency=2, runner=run)
    assert len(out["launch"]) == 2
    assert len(out["ready"]) == 5


# ---- backpressure ----


def test_backpressure_holds_launch(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(
        ready=[_cand("flow-a")],
        prs=[{"headRefName": "feat/flow-d1-wip"}, {"headRefName": "feat/flow-d2-wip"}],
        evolve_list=[],
    )
    out = qst.status(ws, cap=2, concurrency=3, runner=run)
    assert out["launch"] == []
    assert out["select"]["held_backpressure"] is True
    assert out["select"]["open_pr_count"] == 2


def test_backpressure_with_live_run_waits(tmp_path):
    ws = _marked_ws(tmp_path)
    _write_lease(_pool_run_dir(ws, "flow-d1"))
    run, _ = _dispatch(
        ready=[_cand("flow-a")],
        prs=[{"headRefName": "feat/flow-d1-wip"}, {"headRefName": "feat/flow-d2-wip"}],
        evolve_list=[],
    )
    out = qst.status(ws, cap=2, concurrency=3, runner=run)
    assert out["launch"] == []
    assert out["action"] == "wait"
    assert out["liveness"]["flow-d1"] == "live"


# ---- liveness ----


def test_live_lease_reads_live_and_waits(tmp_path):
    ws = _marked_ws(tmp_path)
    _write_lease(_pool_run_dir(ws, "flow-x"))
    run, _ = _dispatch(ready=[_cand("flow-x")])
    out = qst.status(ws, cap=5, concurrency=3, runner=run)
    assert out["liveness"]["flow-x"] == "live"
    assert out["select"]["skipped_in_flight"] == ["flow-x"]
    assert out["action"] == "wait"


def test_expired_lease_parks_and_done(tmp_path):
    ws = _marked_ws(tmp_path)
    _write_lease(_pool_run_dir(ws, "flow-x"), expired=True)
    run, _ = _dispatch(
        ready=[],
        prs=[{"headRefName": "feat/flow-x-wip"}],
        evolve_list=[],
    )
    out = qst.status(ws, cap=5, concurrency=3, runner=run)
    assert out["liveness"]["flow-x"] == "expired_foreign"
    assert out["action"] == "done"
    assert out["parked"] == ["flow-x"]


# ---- purity: the read-only invariant ----


def test_registered_key_drops_from_launched_pending_in_memory_only(tmp_path):
    # a launched key with a live lease has registered: the REPORT drops it from
    # launched_pending, but the marker file stays on disk (evolve_drain's
    # cli_main owns removal; this script never mutates anything)
    ws = _marked_ws(tmp_path)
    launch_ledger.add(ws, "flow-k")
    marker = ws / ".flow" / "launch-ledger" / "flow-k"
    assert marker.exists()
    _write_lease(_pool_run_dir(ws, "flow-k"))
    run, calls = _dispatch(ready=[_cand("flow-k")])
    out = qst.status(ws, cap=5, concurrency=3, runner=run)
    assert out["select"]["launched_pending"] == []
    assert marker.exists()
    for args in calls:
        assert any(args[: len(p)] == p for p in _READ_ONLY_PREFIXES), f"mutating call: {args}"


def test_unregistered_launched_key_stays_pending(tmp_path):
    # no lease, no PR: the launch->init blind window still holds the key
    ws = _marked_ws(tmp_path)
    launch_ledger.add(ws, "flow-led")
    run, _ = _dispatch(ready=[_cand("flow-led")])
    out = qst.status(ws, cap=5, concurrency=3, runner=run)
    assert out["select"]["launched_pending"] == ["flow-led"]
    assert out["action"] == "wait"
    assert (ws / ".flow" / "launch-ledger" / "flow-led").exists()


# ---- model_per_key passthrough ----


def test_model_per_key_passthrough(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-t", labels=["tier:trivial"])])
    out = qst.status(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-t"]
    assert out["select"]["model_per_key"]["flow-t"] == "sonnet"


# ---- cli_main: exit codes + config precedence ----


def test_cli_not_maintainer_exit_4(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    plain = tmp_path / "proj"
    (plain / ".flow").mkdir(parents=True)
    (plain / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n', encoding="utf-8"
    )
    rc = qst.cli_main(["--workspace-root", str(plain)])
    assert rc == 4
    assert "not a flow maintainer setup" in capsys.readouterr().err


def test_cli_tool_error_exit_2(tmp_path, monkeypatch, capsys):
    ws = _marked_ws(tmp_path)

    def boom(args):
        return subprocess.CompletedProcess(args, 1, "", "bd boom")

    monkeypatch.setattr(qst, "cwd_default_runner", lambda repo: boom)
    rc = qst.cli_main(["--workspace-root", str(ws)])
    assert rc == 2
    assert "bd boom" in capsys.readouterr().err


def test_cli_config_defaults_from_queue_section(tmp_path, monkeypatch, capsys):
    ws = tmp_path / "flow"
    (ws / ".flow").mkdir(parents=True)
    (ws / ".flow" / "workspace.toml").write_text(
        "[maintainer]\nself_target = true\n[queue]\ncap = 7\nconcurrency = 2\n",
        encoding="utf-8",
    )
    run, _ = _dispatch(ready=[])
    monkeypatch.setattr(qst, "cwd_default_runner", lambda repo: run)
    rc = qst.cli_main(["--workspace-root", str(ws)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["select"]["cap"] == 7
    assert out["select"]["concurrency"] == 2


def test_cli_flags_override_queue_config(tmp_path, monkeypatch, capsys):
    ws = tmp_path / "flow"
    (ws / ".flow").mkdir(parents=True)
    (ws / ".flow" / "workspace.toml").write_text(
        "[maintainer]\nself_target = true\n[queue]\ncap = 7\nconcurrency = 2\n",
        encoding="utf-8",
    )
    run, _ = _dispatch(ready=[])
    monkeypatch.setattr(qst, "cwd_default_runner", lambda repo: run)
    rc = qst.cli_main(["--workspace-root", str(ws), "--cap", "1", "--concurrency", "1"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["select"]["cap"] == 1
    assert out["select"]["concurrency"] == 1


def test_cli_output_shape(tmp_path, monkeypatch, capsys):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-a")])
    monkeypatch.setattr(qst, "cwd_default_runner", lambda repo: run)
    rc = qst.cli_main(["--workspace-root", str(ws)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {
        "action",
        "launch",
        "parked",
        "stranded_pre_pr",
        "liveness",
        "ready",
        "select",
    }


# ---- advisory parity with queue_drain (evolve scoping + stranded) ----


def test_live_evolve_run_does_not_wait(tmp_path):
    # the advisory must mirror queue_drain's scoping: a live evolve lease in the
    # shared pool is not this queue's to wait on, so the real drain ignores it
    # and the report must too (it used to say `wait`).
    ws = _marked_ws(tmp_path)
    _write_lease(_pool_run_dir(ws, "flow-ev"))
    run, _ = _dispatch(
        ready=[],
        evolve_list=[{"id": "flow-ev", "labels": ["evolve"], "status": "in_progress"}],
    )
    out = qst.status(ws, cap=5, concurrency=3, runner=run)
    assert out["liveness"] == {}
    assert out["action"] == "done"


def _stranded_dispatch(in_progress: list[dict]):
    """bd list dispatched by `-l` like queue_drain's stub: the label-scoped
    active-evolve query returns [], the unscoped in_progress query returns the
    fixture (a single fixture list cannot serve both without conflating scopes)."""
    calls: Recorder = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["bd", "ready"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        if args[:2] == ["bd", "list"]:
            payload = [] if "-l" in args else in_progress
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        if args[:2] == ["git", "for-each-ref"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected: {args}")

    return run, calls


def test_stranded_day_job_bead_reports_recover(tmp_path):
    # an in_progress day-job bead with no lease, no PR, no launch marker is
    # STRANDED; the real drain returns `recover`, so the advisory must too
    # (it used to false-positive `done`).
    ws = _marked_ws(tmp_path)
    run, _ = _stranded_dispatch([{"id": "flow-strand"}])
    out = qst.status(ws, cap=5, concurrency=3, runner=run)
    assert out["action"] == "recover"
    assert [e["key"] for e in out["stranded_pre_pr"]] == ["flow-strand"]


def test_stranded_detection_stays_read_only(tmp_path):
    # the stranded probe adds bd/gh reads only; the launch-ledger marker of an
    # unregistered key must survive the status call untouched
    ws = _marked_ws(tmp_path)
    launch_ledger.add(ws, "flow-strand")
    marker = ws / ".flow" / "launch-ledger" / "flow-strand"
    run, calls = _stranded_dispatch([{"id": "flow-strand"}])
    out = qst.status(ws, cap=5, concurrency=3, runner=run)
    # launched_pending covers the key, so it is still booting, not stranded
    assert out["stranded_pre_pr"] == []
    assert marker.exists()
    for args in calls:
        assert any(args[: len(p)] == p for p in _READ_ONLY_PREFIXES), f"mutating call: {args}"
