from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import cognitive_workers as cw
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


# ─── quarantine sweeper ───────────────────────────────────────────────────────


def _quarantined_journal(
    workspace: Path,
    *,
    run_id: str = "run-1",
    stage: str = "code_review",
    token: str = "abc",
    quarantine_path: Path | None = None,
    quarantine_dir_exists: bool = True,
    age_seconds: float = 0.0,
) -> tuple[Path, Path]:
    """Build an already-quarantined journal.json plus (optionally) its real quarantine dir."""
    invocation_dir = (
        workspace / ".flow" / "runs" / run_id / "cognitive" / stage / "invocations" / token
    )
    invocation_dir.mkdir(parents=True)
    journal_path = invocation_dir / "journal.json"
    logical_id = f"{run_id}:{stage}:main:1"
    journal = cw.InvocationJournal(journal_path, logical_id)
    journal.transition("prepared", launch_nonce="nonce")
    if quarantine_path is None:
        quarantine_path = (
            workspace / ".flow" / "runs" / run_id / "cognitive" / "capsules" / "quarantine" / token
        )
    if quarantine_dir_exists:
        quarantine_path.mkdir(parents=True)
        (quarantine_path / "evidence.txt").write_text("x", encoding="utf-8")
    journal.transition(
        "quarantined",
        failure={"code": "termination_unconfirmed"},
        disposal={
            "capsule": str(workspace / "orig-capsule"),
            "absent": True,
            "quarantined": True,
            "quarantine_path": str(quarantine_path),
        },
    )
    if age_seconds:
        value = json.loads(journal_path.read_text(encoding="utf-8"))
        value["updated_at"] = value["updated_at"] - age_seconds
        body = {key: item for key, item in value.items() if key != "digest"}
        value["digest"] = cw._digest(body)
        journal_path.write_text(json.dumps(value), encoding="utf-8")
    return journal_path, quarantine_path


def test_quarantine_clean_preview_lists_an_aged_row_and_a_recorded_missing_row(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    _journal_path, quarantine_path = _quarantined_journal(
        workspace, token="aged", age_seconds=8 * 24 * 60 * 60
    )
    _missing_journal_path, missing_path = _quarantined_journal(
        workspace, token="missing", quarantine_dir_exists=False
    )

    preview = wj.quarantine_clean(workspace, dry_run=True)

    assert preview["target_root"] == str(workspace.resolve())
    assert [row["quarantine_path"] for row in preview["reapable"]] == [str(quarantine_path)]
    assert preview["reapable"][0]["failure_code"] == "termination_unconfirmed"
    assert preview["younger"] == []
    assert [row["quarantine_path"] for row in preview["recorded_missing"]] == [str(missing_path)]
    assert preview["archived"] == []


def test_quarantine_clean_real_pass_archives_a_confirmed_candidate_by_rename(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    journal_path, quarantine_path = _quarantined_journal(workspace, age_seconds=8 * 24 * 60 * 60)

    preview = wj.quarantine_clean(workspace, dry_run=True)
    row = preview["reapable"][0]

    result = wj.quarantine_clean(
        workspace,
        dry_run=False,
        confirmed_target=Path(preview["target_root"]),
        confirmed_candidates=frozenset({row["confirmation_id"]}),
    )

    assert len(result["archived"]) == 1
    archive_path = Path(result["archived"][0]["archive_path"])
    assert not quarantine_path.exists()
    assert archive_path.is_dir()
    assert (archive_path / "evidence.txt").read_text(encoding="utf-8") == "x"
    on_disk = json.loads(journal_path.read_text(encoding="utf-8"))
    assert on_disk["state"] == "quarantined"
    assert on_disk["archive"]["archive_path"] == str(archive_path)


def test_quarantine_clean_never_reaps_a_candidate_whose_journal_changed_since_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mutation landing strictly between the pre-lock row read and the lock-protected
    re-check (a fresh recovery, a concurrent archive annotation) must report
    "drifted_since_preview" -- distinct from a candidate that was simply never selected (the
    next test's "not_confirmed"). The outer confirmation_id check still matches at read time
    here, so the row only fails the *inner* re-check once the lock is held.
    """
    workspace = tmp_path / "ws"
    journal_path, quarantine_path = _quarantined_journal(workspace, age_seconds=8 * 24 * 60 * 60)
    preview = wj.quarantine_clean(workspace, dry_run=True)
    row = preview["reapable"][0]

    real_flock_blocking = wj.flock_blocking

    def flock_and_drift(path):
        # The journal drifts right as the real pass takes its lock -- after the outer
        # not_confirmed check already matched, before the inner fresh read.
        logical_id = "run-1:code_review:main:1"
        cw.InvocationJournal(journal_path, logical_id).transition("quarantined", extra="drift")
        return real_flock_blocking(path)

    monkeypatch.setattr(wj, "flock_blocking", flock_and_drift)

    result = wj.quarantine_clean(
        workspace,
        dry_run=False,
        confirmed_target=Path(preview["target_root"]),
        confirmed_candidates=frozenset({row["confirmation_id"]}),
    )

    assert result["archived"] == []
    assert len(result["skipped_unconfirmed"]) == 1
    assert result["skipped_unconfirmed"][0]["reason"] == "drifted_since_preview"
    assert quarantine_path.exists()


def test_quarantine_clean_real_pass_reports_not_confirmed_for_an_unselected_candidate(
    tmp_path: Path,
) -> None:
    """A candidate never passed in `confirmed_candidates` gets a distinct reason from one
    whose journal drifted between preview and confirmation (the prior test) -- an operator
    needs to tell "you did not select this" apart from "something touched this mid-decision".
    """
    workspace = tmp_path / "ws"
    _journal_path, quarantine_path = _quarantined_journal(workspace, age_seconds=8 * 24 * 60 * 60)
    preview = wj.quarantine_clean(workspace, dry_run=True)
    assert preview["reapable"], preview

    result = wj.quarantine_clean(
        workspace,
        dry_run=False,
        confirmed_target=Path(preview["target_root"]),
        confirmed_candidates=frozenset(),
    )

    assert result["archived"] == []
    assert len(result["skipped_unconfirmed"]) == 1
    assert result["skipped_unconfirmed"][0]["reason"] == "not_confirmed"
    assert quarantine_path.exists()


def test_quarantine_clean_repreview_reports_an_archived_capsule_as_archived(
    tmp_path: Path,
) -> None:
    """Re-previewing after an archive must not read the archived journal as a suppressed
    move failure: the annotation `_quarantine_row` writes on archive names where the capsule
    went, and a later preview must recognize it instead of filing it under `recorded_missing`
    (the bucket reserved for a genuinely absent, unexplained quarantine path).
    """
    workspace = tmp_path / "ws"
    journal_path, quarantine_path = _quarantined_journal(workspace, age_seconds=8 * 24 * 60 * 60)
    preview = wj.quarantine_clean(workspace, dry_run=True)
    row = preview["reapable"][0]
    first_pass = wj.quarantine_clean(
        workspace,
        dry_run=False,
        confirmed_target=Path(preview["target_root"]),
        confirmed_candidates=frozenset({row["confirmation_id"]}),
    )
    assert len(first_pass["archived"]) == 1
    assert not quarantine_path.exists()
    on_disk = json.loads(journal_path.read_text(encoding="utf-8"))
    assert on_disk["archive"]["archive_path"]

    repreview = wj.quarantine_clean(workspace, dry_run=True)

    assert repreview["recorded_missing"] == []
    assert len(repreview["archived"]) == 1
    assert repreview["archived"][0]["already_archived"] is True
    assert repreview["archived"][0]["archive_path"] == on_disk["archive"]["archive_path"]


def test_quarantine_clean_refuses_a_quarantine_path_outside_the_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside" / "cap"
    _journal_path, _quarantine_path = _quarantined_journal(workspace, quarantine_path=outside)

    preview = wj.quarantine_clean(workspace, dry_run=True)

    assert preview["reapable"] == []
    assert preview["younger"] == []
    assert [row["quarantine_path"] for row in preview["skipped_uncontained"]] == [str(outside)]


def test_quarantine_clean_contained_root_resolves_through_a_flow_runs_symlink(
    tmp_path: Path,
) -> None:
    """`.flow/runs` may itself be a symlink. The containment check must resolve both sides or
    every quarantine_path.resolve() lands on the real path while contained_root still names the
    symlinked one, and the sweeper reports the whole workspace as skipped_uncontained --
    archiving nothing while reporting success.
    """
    workspace = tmp_path / "ws"
    real_runs = tmp_path / "real-runs"
    real_runs.mkdir(parents=True)
    (workspace / ".flow").mkdir(parents=True)
    (workspace / ".flow" / "runs").symlink_to(real_runs, target_is_directory=True)

    _journal_path, quarantine_path = _quarantined_journal(workspace, age_seconds=8 * 24 * 60 * 60)

    preview = wj.quarantine_clean(workspace, dry_run=True)

    assert preview["skipped_uncontained"] == []
    assert [row["quarantine_path"] for row in preview["reapable"]] == [str(quarantine_path)]


def test_quarantine_clean_cli_dry_run_lists_the_aged_row(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "ws"
    _journal_path, quarantine_path = _quarantined_journal(workspace, age_seconds=8 * 24 * 60 * 60)

    rc = wj.cli_main(["quarantine-clean", "--workspace-root", str(workspace), "--dry-run"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target_root"] == str(workspace.resolve())
    assert [row["quarantine_path"] for row in payload["reapable"]] == [str(quarantine_path)]
