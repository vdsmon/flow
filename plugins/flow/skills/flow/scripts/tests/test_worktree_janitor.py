from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import worktree_janitor as wj


def _cp(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _porcelain(entries: list[tuple[Path, str, str]]) -> str:
    return (
        "\n\n".join(
            f"worktree {path}\nHEAD {tip}\nbranch refs/heads/{branch}"
            for path, branch, tip in entries
        )
        + "\n"
    )


class _Runner:
    def __init__(self, porcelain: str, *, unique: dict[str, int] | None = None):
        self.porcelain = porcelain
        self.unique = unique or {}
        self.calls: list[list[str]] = []

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        if args == ["git", "worktree", "list", "--porcelain"]:
            return _cp(self.porcelain)
        if args == ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"]:
            return _cp("refs/remotes/origin/main\n")
        if args == ["git", "rev-parse", "refs/remotes/origin/main"]:
            return _cp("default-sha\n")
        if args == ["git", "ls-remote", "origin", "refs/heads/main"]:
            return _cp("default-sha\trefs/heads/main\n")
        if args[:3] == ["git", "rev-list", "--count"]:
            return _cp(f"{self.unique.get(args[3], 0)}\n")
        raise AssertionError(f"unexpected call: {args}")


class _Tracker:
    def __init__(self, states: dict[str, str], failures: set[str] | None = None):
        self.states = states
        self.failures = failures or set()

    def state(self, key):
        if key in self.failures:
            raise RuntimeError(f"tracker failed for {key}")
        return {"normalized": self.states[key]}


class _Forge:
    def __init__(self, prs: dict[tuple[str, str], dict | None], failures=()):
        self.prs = prs
        self.failures = set(failures)
        self.calls: list[tuple[str, str]] = []

    def detect_pr(self, branch, state="open"):
        self.calls.append((branch, state))
        if (branch, state) in self.failures:
            raise RuntimeError(f"forge {state} failed")
        return self.prs.get((branch, state))


def _wire(
    monkeypatch,
    tmp_path: Path,
    *,
    entries: list[tuple[Path, str, str]],
    states: dict[str, str],
    prs: dict[tuple[str, str], dict | None] | None = None,
    unique: dict[str, int] | None = None,
    lease_states: dict[str, str] | None = None,
    tracker_failures: set[str] | None = None,
    forge_failures=(),
):
    runner = _Runner(_porcelain(entries), unique=unique)
    tracker = _Tracker(states, tracker_failures)
    forge = _Forge(prs or {}, forge_failures)
    monkeypatch.setattr(wj, "_default_runner", lambda _root: runner)
    monkeypatch.setattr(wj, "_load_tracker", lambda _root: tracker)
    monkeypatch.setattr(wj, "_load_forge", lambda _root: forge)
    monkeypatch.setattr(
        wj.branch_ticket,
        "resolve",
        lambda _root, _cwd, branch=None: next(
            (key for key in states if branch and key in branch), None
        ),
    )
    lease_calls: list[Path] = []

    def classify(ticket_dir, *_args, **_kwargs):
        lease_calls.append(ticket_dir)
        return {"state": (lease_states or {}).get(ticket_dir.name, "free")}

    monkeypatch.setattr(wj.lease, "classify", classify)
    monkeypatch.setattr(wj.lease, "boot_id", lambda: "boot")
    monkeypatch.setattr(wj.lease, "hostname", lambda: "host")
    reap_calls: list[dict] = []
    order: list[tuple[str, str]] = []

    def reap(**kwargs):
        reap_calls.append(kwargs)
        before_remove = kwargs.get("before_remove")
        before_result = None
        if before_remove is not None:
            worktree = next(path for path, branch, _tip in entries if kwargs["ticket"] in branch)
            before_result = before_remove(worktree)
        order.append(("reap", kwargs["ticket"]))
        return {
            "worktree_removed": True,
            "branch_deleted": True,
            "before_remove_result": before_result,
            "before_remove_error": None,
        }

    def observe(_root, key, _worktree):
        order.append(("observe", key))
        return {"action": "observed"}

    monkeypatch.setattr(wj, "reap_worktree", reap)
    monkeypatch.setattr(wj.observe_at_close, "observe_at_close", observe)
    return runner, forge, lease_calls, reap_calls, order


def _confirmed_sweep(workspace_root: Path) -> dict:
    preview = wj.sweep(workspace_root, dry_run=True)
    return wj.sweep(
        workspace_root,
        dry_run=False,
        confirmed_target=Path(preview["target_root"]),
        confirmed_candidates=frozenset(row["confirmation_id"] for row in preview["reapable"]),
    )


def test_sweep_scopes_to_primary_repo_managed_dirs_and_skips_invoking_checkout(
    monkeypatch, tmp_path
):
    main = tmp_path / "repo"
    invoking = main / ".claude" / "worktrees" / "feat-flow-run"
    managed = main / ".flow" / "worktrees" / "feat-flow-old"
    foreign = tmp_path / "elsewhere" / "feat-flow-outside"
    entries = [
        (main, "main", "default-sha"),
        (invoking, "feat/flow-run-x", "run-tip"),
        (managed, "feat/flow-old-x", "old-tip"),
        (foreign, "feat/flow-out-x", "out-tip"),
    ]
    _wire(
        monkeypatch,
        tmp_path,
        entries=entries,
        states={"flow-run": "done", "flow-old": "done", "flow-out": "done"},
    )

    result = wj.sweep(invoking, dry_run=True)

    assert result["target_root"] == str(main.resolve())
    assert [row["key"] for row in result["reapable"]] == ["flow-old"]
    assert [row["branch"] for row in result["skipped_invoking_checkout"]] == ["feat/flow-run-x"]
    assert [row["branch"] for row in result["skipped_unmanaged"]] == ["feat/flow-out-x"]


def test_open_pr_is_always_preserved(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    wt = main / ".flow" / "worktrees" / "feat-FT-10"
    branch = "feat/FT-10-x"
    _runner, forge, _lease, reap_calls, _order = _wire(
        monkeypatch,
        tmp_path,
        entries=[(main, "main", "default-sha"), (wt, branch, "tip")],
        states={"FT-10": "done"},
        prs={(branch, "open"): {"id": "9", "state": "OPEN", "head_sha": "tip"}},
    )

    result = _confirmed_sweep(main)

    assert [row["key"] for row in result["skipped_open_pr"]] == ["FT-10"]
    assert reap_calls == []
    assert forge.calls == [(branch, "open"), (branch, "open")]


def test_merged_pr_reaps_only_when_local_tip_matches_head(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    match = main / ".claude" / "worktrees" / "match"
    ahead = main / ".claude" / "worktrees" / "ahead"
    match_branch = "feat/FT-10-match"
    ahead_branch = "feat/FT-11-ahead"
    _runner, _forge, lease_calls, reap_calls, order = _wire(
        monkeypatch,
        tmp_path,
        entries=[
            (main, "main", "default-sha"),
            (match, match_branch, "match-tip"),
            (ahead, ahead_branch, "ahead-tip"),
        ],
        states={"FT-10": "done", "FT-11": "cancelled"},
        prs={
            (match_branch, "merged"): {
                "id": "10",
                "state": "MERGED",
                "head_sha": "match-tip",
            },
            (ahead_branch, "merged"): {
                "id": "11",
                "state": "MERGED",
                "head_sha": "merged-tip",
            },
        },
    )

    result = _confirmed_sweep(main)

    assert [row["key"] for row in result["reaped"]] == ["FT-10"]
    assert [row["key"] for row in result["skipped_merged_head_mismatch"]] == ["FT-11"]
    assert reap_calls[0]["branch"] == match_branch
    assert order == [("observe", "FT-10"), ("reap", "FT-10")]
    assert lease_calls == [
        match / ".flow" / "runs" / "FT-10",
        ahead / ".flow" / "runs" / "FT-11",
        match / ".flow" / "runs" / "FT-10",
        ahead / ".flow" / "runs" / "FT-11",
    ]


def test_terminal_no_pr_requires_verified_default_and_zero_unique_commits(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    zero = main / ".claude" / "worktrees" / "zero"
    unique = main / ".claude" / "worktrees" / "unique"
    _runner, _forge, _lease, reap_calls, _order = _wire(
        monkeypatch,
        tmp_path,
        entries=[
            (main, "main", "default-sha"),
            (zero, "feat/FT-10-zero", "zero-tip"),
            (unique, "feat/FT-11-unique", "unique-tip"),
        ],
        states={"FT-10": "done", "FT-11": "done"},
        unique={"default-sha..zero-tip": 0, "default-sha..unique-tip": 2},
    )

    result = _confirmed_sweep(main)

    assert [row["key"] for row in result["reaped"]] == ["FT-10"]
    assert [row["key"] for row in result["skipped_unique_commits"]] == ["FT-11"]
    assert [call["ticket"] for call in reap_calls] == ["FT-10"]


def test_probe_failures_are_bucketed_per_candidate_and_preserved(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    tracker_fail = main / ".flow" / "worktrees" / "tracker-fail"
    forge_fail = main / ".flow" / "worktrees" / "forge-fail"
    _runner, _forge, _lease, reap_calls, _order = _wire(
        monkeypatch,
        tmp_path,
        entries=[
            (main, "main", "default-sha"),
            (tracker_fail, "feat/FT-10-x", "tip-10"),
            (forge_fail, "feat/FT-11-x", "tip-11"),
        ],
        states={"FT-10": "done", "FT-11": "done"},
        tracker_failures={"FT-10"},
        forge_failures={("feat/FT-11-x", "open")},
    )

    result = _confirmed_sweep(main)

    assert {(row["key"], row["probe"]) for row in result["probe_failed"]} == {
        ("FT-10", "tracker_state"),
        ("FT-11", "forge_open_pr"),
    }
    assert reap_calls == []


def test_live_and_corrupt_exact_leases_are_preserved(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    live = main / ".flow" / "worktrees" / "live"
    corrupt = main / ".flow" / "worktrees" / "corrupt"
    _runner, _forge, _lease, reap_calls, _order = _wire(
        monkeypatch,
        tmp_path,
        entries=[
            (main, "main", "default-sha"),
            (live, "feat/FT-10-x", "tip-10"),
            (corrupt, "feat/FT-11-x", "tip-11"),
        ],
        states={"FT-10": "done", "FT-11": "done"},
        lease_states={"FT-10": "live", "FT-11": "corrupt"},
    )

    result = _confirmed_sweep(main)

    assert [row["key"] for row in result["skipped_live_lease"]] == ["FT-10"]
    assert [row["key"] for row in result["skipped_corrupt_lease"]] == ["FT-11"]
    assert reap_calls == []


def test_live_revision_lease_is_preserved_in_preview(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    wt = main / ".flow" / "worktrees" / "revision"
    revision = wt / ".flow" / "runs" / "FT-10" / "revisions" / "r1"
    revision.mkdir(parents=True)
    _runner, _forge, _lease, reap_calls, _order = _wire(
        monkeypatch,
        tmp_path,
        entries=[(main, "main", "default-sha"), (wt, "feat/FT-10-x", "tip")],
        states={"FT-10": "done"},
        lease_states={"r1": "live"},
    )

    result = wj.sweep(main, dry_run=True)

    assert [row["key"] for row in result["skipped_live_lease"]] == ["FT-10"]
    assert result["reapable"] == []
    assert reap_calls == []


def test_dry_run_never_observes_or_reaps(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    wt = main / ".flow" / "worktrees" / "orphan"
    _runner, _forge, _lease, reap_calls, order = _wire(
        monkeypatch,
        tmp_path,
        entries=[(main, "main", "default-sha"), (wt, "feat/FT-10-x", "tip")],
        states={"FT-10": "done"},
    )

    result = wj.sweep(main, dry_run=True)

    assert [row["key"] for row in result["reapable"]] == ["FT-10"]
    assert result["reaped"] == []
    assert reap_calls == []
    assert order == []


def test_real_sweep_never_reaps_candidate_that_was_not_confirmed(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    wt = main / ".flow" / "worktrees" / "orphan"
    branch = "feat/FT-10-x"
    _runner, forge, _lease, reap_calls, _order = _wire(
        monkeypatch,
        tmp_path,
        entries=[(main, "main", "default-sha"), (wt, branch, "tip")],
        states={"FT-10": "done"},
        prs={(branch, "open"): {"id": "10", "state": "OPEN", "head_sha": "tip"}},
    )
    preview = wj.sweep(main, dry_run=True)
    assert preview["reapable"] == []

    forge.prs[(branch, "open")] = None
    result = wj.sweep(
        main,
        dry_run=False,
        confirmed_target=Path(preview["target_root"]),
        confirmed_candidates=frozenset(),
    )

    assert [row["key"] for row in result["skipped_unconfirmed"]] == ["FT-10"]
    assert result["reaped"] == []
    assert reap_calls == []


def test_real_sweep_refuses_changed_confirmed_target(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    _wire(monkeypatch, tmp_path, entries=[(main, "main", "default-sha")], states={})

    with pytest.raises(wj._JanitorError, match="does not match current target"):
        wj.sweep(
            main,
            dry_run=False,
            confirmed_target=tmp_path / "different-repo",
            confirmed_candidates=frozenset(),
        )


def test_cli_outputs_absolute_target_root(monkeypatch, tmp_path, capsys):
    main = tmp_path / "repo"
    _wire(
        monkeypatch,
        tmp_path,
        entries=[(main, "main", "default-sha")],
        states={},
    )

    rc = wj.cli_main(["sweep", "--workspace-root", str(main), "--dry-run"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["target_root"] == str(main.resolve())
