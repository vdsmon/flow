from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, override

import pytest

import _evolve_common
import fleet
import lease
import queue_status as qst
from _timeutil import utcnow_iso
from forge import ForgeError, NotSupported

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


def test_happy_path_ready_requires_planning(tmp_path):
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
    assert out["launch"] == []
    assert out["plan_required"] == ["flow-a", "flow-b"]
    assert out["action"] == "plan_required"
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
    assert out["launch"] == []
    assert len(out["plan_required"]) == 2
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
    # launched_pending, but the fleet entry stays on disk (this script never
    # mutates anything, read-only by construction)
    ws = _marked_ws(tmp_path)
    fleet.register(fleet.resolve_fleet_dir(ws), "flow-k", "", now=utcnow_iso())
    assert fleet.read(fleet.resolve_fleet_dir(ws), "flow-k") is not None
    _write_lease(_pool_run_dir(ws, "flow-k"))
    run, calls = _dispatch(ready=[_cand("flow-k")])
    out = qst.status(ws, cap=5, concurrency=3, runner=run)
    assert out["select"]["launched_pending"] == []
    assert fleet.read(fleet.resolve_fleet_dir(ws), "flow-k") is not None
    for args in calls:
        assert any(args[: len(p)] == p for p in _READ_ONLY_PREFIXES), f"mutating call: {args}"


def test_unregistered_launched_key_stays_pending(tmp_path):
    # no lease, no PR: the launch->init blind window still holds the key
    ws = _marked_ws(tmp_path)
    fleet.register(fleet.resolve_fleet_dir(ws), "flow-led", "", now=utcnow_iso())
    run, _ = _dispatch(ready=[_cand("flow-led")])
    out = qst.status(ws, cap=5, concurrency=3, runner=run)
    assert out["select"]["launched_pending"] == ["flow-led"]
    assert out["action"] == "wait"
    assert fleet.read(fleet.resolve_fleet_dir(ws), "flow-led") is not None


# ---- model_per_key passthrough ----


def test_model_per_key_passthrough(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-t", labels=["tier:trivial"])])
    out = qst.status(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == []
    assert out["plan_required"] == ["flow-t"]
    assert out["select"]["launch"] == ["flow-t"]
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
        "plan_required",
        "parked",
        "reviews",
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
    # the stranded probe adds bd/gh reads only; the fleet entry of an
    # unregistered key must survive the status call untouched
    ws = _marked_ws(tmp_path)
    fleet.register(fleet.resolve_fleet_dir(ws), "flow-strand", "", now=utcnow_iso())
    run, calls = _stranded_dispatch([{"id": "flow-strand"}])
    out = qst.status(ws, cap=5, concurrency=3, runner=run)
    # launched_pending covers the key, so it is still booting, not stranded
    assert out["stranded_pre_pr"] == []
    assert fleet.read(fleet.resolve_fleet_dir(ws), "flow-strand") is not None
    for args in calls:
        assert any(args[: len(p)] == p for p in _READ_ONLY_PREFIXES), f"mutating call: {args}"


# ---- parked-PR review enrichment (absorbed from queue_reviews.py) ----


class _FakeForge:
    """Records calls; scripts responses. Mirrors the Forge surface the enrichment uses.

    `prs` maps an EXACT head ref -> the PullRequest detect_pr returns (None = no PR).
    `threads` maps a pr_id -> the review_threads list.
    """

    backend = "github"
    capabilities: ClassVar[list] = []

    def __init__(self, *, prs=None, threads=None, fail_threads_on=None, fail_detect_on=None):
        self.calls: list[tuple] = []
        self._prs = prs or {}
        self._threads = threads or {}
        self._fail_threads_on = fail_threads_on or set()
        self._fail_detect_on = fail_detect_on or set()

    def detect_pr(self, branch):
        self.calls.append(("detect_pr", branch))
        if branch in self._fail_detect_on:
            raise ForgeError(f"detect failed for {branch}")
        return self._prs.get(branch)

    def review_threads(self, pr_id):
        self.calls.append(("review_threads", pr_id))
        if pr_id in self._fail_threads_on:
            raise NotSupported("no review threads on this host")
        return self._threads.get(pr_id, [])


def _pr(number, *, state="OPEN"):
    return {
        "id": str(number),
        "url": f"https://example/pr/{number}",
        "number": number,
        "draft": False,
        "base": "main",
        "head": f"feat/flow-{number}",
        "state": state,
    }


def _thread(tid, severity, *, resolved=False, title="t"):
    return {
        "id": tid,
        "file": None,
        "line": None,
        "severity": severity,
        "title": title,
        "body": "b",
        "resolved": resolved,
        "author": "a",
        "parent_id": None,
    }


def test_slugged_ref_resolves_and_flags_major():
    # DISCRIMINATING: feed the SLUGGED head ref, not the bare feat/<key>. A bare-key
    # resolution (detect_pr("feat/flow-kx17.5")) would NOT match this ref and silently
    # flag nothing.
    ref = "feat/flow-kx17.5-queue-surfacing"
    fake = _FakeForge(
        prs={ref: _pr(310)},
        threads={"310": [_thread("rt1", "major")]},
    )
    results = qst.flag_parked_reviews(["flow-kx17.5"], [ref], fake)

    assert ("detect_pr", ref) in fake.calls
    assert len(results) == 1
    r = results[0]
    assert r["key"] == "flow-kx17.5"
    assert r["pr_id"] == "310"
    assert r["pr_url"] == "https://example/pr/310"
    assert r["unresolved_major"] == 1
    assert r["threads"] == [{"id": "rt1", "severity": "major", "title": "t"}]


def test_resolved_and_minor_threads_not_flagged():
    # only resolved threads + a leftover bot minor -> NOT flagged. proves the plain-comment
    # floor is NOT applied at this surfacing layer.
    ref = "feat/flow-abc-slug"
    fake = _FakeForge(
        prs={ref: _pr(100)},
        threads={
            "100": [
                _thread("a", "major", resolved=True),
                _thread("b", "minor"),
                _thread("c", "nit"),
            ]
        },
    )
    assert qst.flag_parked_reviews(["flow-abc"], [ref], fake) == []


def test_no_matching_pr_ref_skipped():
    # parked key with no matching pr_refs entry -> skipped, no crash, no detect_pr.
    fake = _FakeForge(prs={}, threads={})
    results = qst.flag_parked_reviews(["flow-xyz"], ["feat/flow-other-slug"], fake)
    assert results == []
    assert all(c[0] != "detect_pr" for c in fake.calls)


def test_detect_pr_none_skipped():
    # ref present but the PR has been closed/merged (detect_pr -> None) -> skipped.
    ref = "feat/flow-gone-slug"
    fake = _FakeForge(prs={ref: None}, threads={})
    assert qst.flag_parked_reviews(["flow-gone"], [ref], fake) == []


def test_forge_error_on_one_key_does_not_drop_others():
    # the failing key is FIRST to prove the loop continues past it.
    ref_bad = "feat/flow-bad-slug"
    ref_good = "feat/flow-good-slug"
    fake = _FakeForge(
        prs={ref_bad: _pr(1), ref_good: _pr(2)},
        threads={"2": [_thread("g", "critical")]},
        fail_threads_on={"1"},
    )
    results = qst.flag_parked_reviews(["flow-bad", "flow-good"], [ref_bad, ref_good], fake)
    assert {r["key"] for r in results} == {"flow-good"}


def test_not_supported_swallowed():
    ref = "feat/flow-nohost-slug"
    fake = _FakeForge(prs={ref: _pr(5)}, threads={}, fail_threads_on={"5"})
    assert qst.flag_parked_reviews(["flow-nohost"], [ref], fake) == []


def test_detect_pr_error_swallowed():
    ref = "feat/flow-derr-slug"
    fake = _FakeForge(prs={}, threads={}, fail_detect_on={ref})
    assert qst.flag_parked_reviews(["flow-derr"], [ref], fake) == []


def test_non_forge_error_on_one_key_does_not_drop_others():
    # a non-ForgeError leaking from an adapter (unexpected payload -> KeyError,
    # raw JSON parse error, ...) must be swallowed per-key like a ForgeError, or
    # the best-effort contract breaks with a traceback.
    ref_bad = "feat/flow-boom-slug"
    ref_good = "feat/flow-good-slug"

    class _Boom(_FakeForge):
        @override
        def review_threads(self, pr_id):
            if pr_id == "1":
                raise KeyError("unexpected payload shape")
            return super().review_threads(pr_id)

    fake = _Boom(
        prs={ref_bad: _pr(1), ref_good: _pr(2)},
        threads={"2": [_thread("g", "critical")]},
    )
    results = qst.flag_parked_reviews(["flow-boom", "flow-good"], [ref_bad, ref_good], fake)
    assert {r["key"] for r in results} == {"flow-good"}


def test_detect_pr_payload_without_id_skipped():
    # a payload missing `id` cannot be probed for threads; skip the key instead
    # of raising KeyError out of the enrichment.
    ref = "feat/flow-noid-slug"
    pr = _pr(3)
    del pr["id"]
    fake = _FakeForge(prs={ref: pr}, threads={})
    results = qst.flag_parked_reviews(["flow-noid"], [ref], fake)
    assert results == []
    assert all(c[0] != "review_threads" for c in fake.calls)


@pytest.mark.parametrize(
    ("severity", "flagged"),
    [
        ("critical", True),
        ("major", True),
        ("minor", False),
        ("nit", False),
        ("unknown", False),
    ],
)
def test_severity_floor_is_major_plus(severity, flagged):
    ref = "feat/flow-sev-slug"
    fake = _FakeForge(prs={ref: _pr(9)}, threads={"9": [_thread("x", severity)]})
    results = qst.flag_parked_reviews(["flow-sev"], [ref], fake)
    assert (len(results) == 1) is flagged


def test_parked_reviews_no_forge_block_empty(tmp_path):
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n', encoding="utf-8"
    )
    out = qst._parked_reviews(tmp_path, ["flow-x"], ["feat/flow-x-s"])
    assert out == []


def test_status_surfaces_parked_reviews(tmp_path):
    # end-to-end through status(): a parked key with an open slugged PR carrying an
    # unresolved major thread lands in the `reviews` field.
    ws = _marked_ws(tmp_path)
    (ws / ".flow" / "workspace.toml").write_text(
        '[maintainer]\nself_target = true\n[forge]\nbackend = "github"\n[forge.github]\n',
        encoding="utf-8",
    )
    _write_lease(_pool_run_dir(ws, "flow-x"), expired=True)
    ref = "feat/flow-x-wip"
    fake = _FakeForge(prs={ref: _pr(42)}, threads={"42": [_thread("m", "major")]})
    run, calls = _dispatch(ready=[], prs=[{"headRefName": ref}], evolve_list=[])
    out = qst.status(ws, cap=5, concurrency=3, runner=run, forge_factory=lambda _cfg: fake)
    assert out["parked"] == ["flow-x"]
    assert len(out["reviews"]) == 1
    assert out["reviews"][0]["key"] == "flow-x"
    assert out["reviews"][0]["pr_id"] == "42"
    for args in calls:
        assert any(args[: len(p)] == p for p in _READ_ONLY_PREFIXES), f"mutating call: {args}"


def test_status_no_parked_skips_review_probe(tmp_path):
    # nothing parked -> no forge factory call at all (the probe is gated)
    ws = _marked_ws(tmp_path)

    def boom_factory(_cfg):
        raise AssertionError("factory must not be called when nothing is parked")

    run, _ = _dispatch(ready=[_cand("flow-a")])
    out = qst.status(ws, cap=5, concurrency=3, runner=run, forge_factory=boom_factory)
    assert out["reviews"] == []


def test_status_review_probe_toolerror_is_swallowed(tmp_path, monkeypatch):
    # a transient gh failure inside the review probe (gather_refs) must not
    # abort the status report: reviews degrades to [], everything else lands
    ws = _marked_ws(tmp_path)
    _write_lease(_pool_run_dir(ws, "flow-x"), expired=True)
    run, _ = _dispatch(ready=[], prs=[{"headRefName": "feat/flow-x-wip"}], evolve_list=[])

    def boom(runner):
        raise _evolve_common.ToolError("gh pr list failed: transient")

    monkeypatch.setattr(_evolve_common, "gather_refs", boom)
    out = qst.status(ws, cap=5, concurrency=3, runner=run)
    assert out["parked"] == ["flow-x"]
    assert out["reviews"] == []
