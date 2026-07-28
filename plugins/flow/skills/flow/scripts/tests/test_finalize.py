from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import finalize as fz

_FULL_TIP = "601c88815983ef5b8f0f8e91a4c61b880d81b8eb"
_OTHER_TIP = "9f2ab41d77c0e5b3a8146d92ef0b7c15d3e6a840"


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
    def __init__(self, porcelain: str, local_branches: list[str] | None = None):
        self.porcelain = porcelain
        self.local_branches = local_branches or []
        self.calls: list[list[str]] = []

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        if args == ["git", "worktree", "list", "--porcelain"]:
            return _cp(self.porcelain)
        if args[:3] == ["git", "for-each-ref", "refs/heads"]:
            return _cp("\n".join(self.local_branches) + "\n")
        raise AssertionError(f"unexpected call: {args}")


class _Tracker:
    def __init__(self, state: str = "in_review", *, transition_failure: str = "none"):
        self.normalized = state
        self.transition_failure = transition_failure
        self.transitioned: list[str] = []
        self.order: list[str] = []

    def state(self, key):
        return {"normalized": self.normalized}

    def list_transitions(self, key):
        return [
            {"id": "31", "name": "Done", "to_normalized_state": "done", "available": True},
        ]

    def transition(self, key, transition_id, fields=None):
        self.order.append("transition")
        self.transitioned.append(transition_id)
        return {"failure_kind": self.transition_failure}


class _Forge:
    def __init__(self, prs: dict[tuple[str, str], dict | None], *, delete_error: str | None = None):
        self.prs = prs
        self.delete_error = delete_error
        self.deleted: list[str] = []
        self.order: list[str] = []

    def detect_pr(self, branch, state="open"):
        return self.prs.get((branch, state))

    def delete_branch(self, branch):
        self.order.append("delete_branch")
        if self.delete_error:
            raise RuntimeError(self.delete_error)
        self.deleted.append(branch)


def _wire(
    monkeypatch,
    tmp_path: Path,
    key: str = "FT-1",
    *,
    entries: list[tuple[Path, str, str]] | None = None,
    local_branches: list[str] | None = None,
    prs: dict[tuple[str, str], dict | None] | None = None,
    tracker: _Tracker | None = None,
    forge: _Forge | None = None,
    lease_blocker: tuple[str, Path] | None = None,
):
    main = tmp_path / "repo"
    entries = entries if entries is not None else []
    porcelain = _porcelain([(main, "main", "main-sha"), *entries])
    runner = _Runner(porcelain, local_branches)
    tracker = tracker or _Tracker()
    forge = forge or _Forge(prs or {})
    order: list[str] = []

    monkeypatch.setattr(fz, "_default_runner", lambda _root: runner)
    monkeypatch.setattr(
        fz.branch_ticket,
        "resolve",
        lambda _root, _cwd, branch=None: key if branch and key in branch else None,
    )
    monkeypatch.setattr(fz, "_read_tracker_config", lambda _root: {})
    tracker.order = order
    monkeypatch.setattr(fz, "make_tracker", lambda _config: tracker)
    monkeypatch.setattr(fz, "read_forge_config", lambda _root: {"backend": "test"})
    monkeypatch.setattr(fz, "make_forge", lambda _config: forge)
    forge.order = order
    monkeypatch.setattr(fz, "_candidate_lease_blocker", lambda _path, _key: lease_blocker)

    observed: list[tuple[str, Path | None]] = []

    def observe(_root, k, worktree):
        order.append("observe")
        observed.append((k, worktree))
        return {"action": "observed"}

    monkeypatch.setattr(fz.observe_at_close, "observe_at_close", observe)
    reaped: list[dict] = []

    def reap(**kwargs):
        order.append("reap")
        reaped.append(kwargs)
        return {"worktree_removed": True, "branch_deleted": True, "skipped": None}

    monkeypatch.setattr(fz, "reap_worktree", reap)
    return main, tracker, forge, order, observed, reaped


def _worktree_entry(main: Path, key: str = "FT-1", tip: str = "head-sha"):
    return (main / ".claude" / "worktrees" / f"feat-{key}-x", f"feat/{key}-x", tip)


def test_open_pr_exits_not_merged_with_zero_writes(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    entry = _worktree_entry(main)
    _, _, _, order, _, _ = _wire(
        monkeypatch,
        tmp_path,
        entries=[entry],
        prs={("feat/FT-1-x", "open"): {"id": "7"}},
    )
    code, report = fz.finalize(main, "FT-1")
    assert code == fz.EXIT_NOT_MERGED
    assert report["reason"] == "pr_open"
    assert order == []


def test_no_pr_exits_not_merged(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    entry = _worktree_entry(main)
    _wire(monkeypatch, tmp_path, entries=[entry], prs={})
    code, report = fz.finalize(main, "FT-1")
    assert code == fz.EXIT_NOT_MERGED
    assert report["reason"] == "no_pr"


def test_merged_runs_full_sequence_in_drain_order(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    entry = _worktree_entry(main)
    _, _, _, order, observed, reaped = _wire(
        monkeypatch,
        tmp_path,
        entries=[entry],
        prs={("feat/FT-1-x", "merged"): {"id": "7", "head_sha": "head-sha"}},
    )
    code, report = fz.finalize(main, "FT-1")
    assert code == fz.EXIT_OK
    assert order == ["transition", "observe", "delete_branch", "reap"]
    assert observed == [("FT-1", entry[0].resolve())]
    assert reaped[0]["branch"] == "feat/FT-1-x"
    assert reaped[0]["expected_tip"] == "head-sha"
    assert report["steps"]["transition"]["action"] == "transitioned"


def test_terminal_ticket_skips_transition(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    entry = _worktree_entry(main)
    _, tracker, _, _, _, _ = _wire(
        monkeypatch,
        tmp_path,
        entries=[entry],
        prs={("feat/FT-1-x", "merged"): {"id": "7", "head_sha": "head-sha"}},
        tracker=_Tracker(state="done"),
    )
    code, report = fz.finalize(main, "FT-1")
    assert code == fz.EXIT_OK
    assert report["steps"]["transition"] == {"action": "skipped", "reason": "already_done"}
    assert tracker.transitioned == []


def test_live_lease_refuses(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    entry = _worktree_entry(main)
    _wire(
        monkeypatch,
        tmp_path,
        entries=[entry],
        prs={("feat/FT-1-x", "merged"): {"id": "7", "head_sha": "head-sha"}},
        lease_blocker=("live", entry[0] / ".flow" / "runs" / "FT-1"),
    )
    with pytest.raises(fz.FinalizeRefused, match="live lease"):
        fz.finalize(main, "FT-1")


def test_invoking_from_doomed_worktree_refuses(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    entry = _worktree_entry(main)
    _wire(
        monkeypatch,
        tmp_path,
        entries=[entry],
        prs={("feat/FT-1-x", "merged"): {"id": "7", "head_sha": "head-sha"}},
    )
    with pytest.raises(fz.FinalizeRefused, match="own worktree"):
        fz.finalize(entry[0], "FT-1")


def test_merged_head_mismatch_refuses(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    entry = _worktree_entry(main, tip="newer-sha")
    _wire(
        monkeypatch,
        tmp_path,
        entries=[entry],
        prs={("feat/FT-1-x", "merged"): {"id": "7", "head_sha": "head-sha"}},
    )
    with pytest.raises(fz.FinalizeRefused, match="does not match merged PR head"):
        fz.finalize(main, "FT-1")


def test_abbreviated_merged_head_matches_full_tip(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    entry = _worktree_entry(main, tip=_FULL_TIP)
    _, _, _, order, _, reaped = _wire(
        monkeypatch,
        tmp_path,
        entries=[entry],
        prs={("feat/FT-1-x", "merged"): {"id": "7", "head_sha": _FULL_TIP[:12]}},
    )
    code, _report = fz.finalize(main, "FT-1")
    assert code == fz.EXIT_OK
    assert order == ["transition", "observe", "delete_branch", "reap"]
    assert reaped[0]["expected_tip"] == _FULL_TIP


def test_abbreviated_merged_head_of_a_different_commit_still_refuses(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    entry = _worktree_entry(main, tip=_FULL_TIP)
    _wire(
        monkeypatch,
        tmp_path,
        entries=[entry],
        prs={("feat/FT-1-x", "merged"): {"id": "7", "head_sha": _OTHER_TIP[:12]}},
    )
    with pytest.raises(fz.FinalizeRefused, match="does not match merged PR head"):
        fz.finalize(main, "FT-1")


def test_head_sha_too_short_to_trust_still_refuses(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    entry = _worktree_entry(main, tip=_FULL_TIP)
    _wire(
        monkeypatch,
        tmp_path,
        entries=[entry],
        prs={("feat/FT-1-x", "merged"): {"id": "7", "head_sha": _FULL_TIP[:4]}},
    )
    with pytest.raises(fz.FinalizeRefused, match="does not match merged PR head"):
        fz.finalize(main, "FT-1")


def test_non_string_head_sha_refuses_instead_of_raising(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    entry = _worktree_entry(main, tip=_FULL_TIP)
    _wire(
        monkeypatch,
        tmp_path,
        entries=[entry],
        prs={("feat/FT-1-x", "merged"): {"id": "7", "head_sha": 12345}},
    )
    with pytest.raises(fz.FinalizeRefused, match="does not match merged PR head"):
        fz.finalize(main, "FT-1")


def test_transition_failure_does_not_stop_the_sequence(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    entry = _worktree_entry(main)
    _, _, _, order, _, _ = _wire(
        monkeypatch,
        tmp_path,
        entries=[entry],
        prs={("feat/FT-1-x", "merged"): {"id": "7", "head_sha": "head-sha"}},
        tracker=_Tracker(transition_failure="permission_denied"),
    )
    code, report = fz.finalize(main, "FT-1")
    assert code == fz.EXIT_OK
    assert report["steps"]["transition"] == {
        "action": "failed",
        "reason": "permission_denied",
    }
    assert order == ["transition", "observe", "delete_branch", "reap"]


def test_delete_branch_failure_still_reaps(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    entry = _worktree_entry(main)
    _, _, _, _, _, reaped = _wire(
        monkeypatch,
        tmp_path,
        entries=[entry],
        prs={("feat/FT-1-x", "merged"): {"id": "7", "head_sha": "head-sha"}},
        forge=_Forge(
            {("feat/FT-1-x", "merged"): {"id": "7", "head_sha": "head-sha"}},
            delete_error="already gone",
        ),
    )
    code, report = fz.finalize(main, "FT-1")
    assert code == fz.EXIT_OK
    assert report["steps"]["delete_remote_branch"]["action"] == "failed_or_absent"
    assert len(reaped) == 1


def test_dry_run_probes_but_writes_nothing(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    entry = _worktree_entry(main)
    _, _, _, order, _, reaped = _wire(
        monkeypatch,
        tmp_path,
        entries=[entry],
        prs={("feat/FT-1-x", "merged"): {"id": "7", "head_sha": "head-sha"}},
    )
    code, report = fz.finalize(main, "FT-1", dry_run=True)
    assert code == fz.EXIT_OK
    assert report["steps"]["reap"] == {"action": "would_run"}
    assert order == []
    assert reaped == []


def test_missing_worktree_finalizes_via_unique_local_branch(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    _, _, _, order, observed, reaped = _wire(
        monkeypatch,
        tmp_path,
        entries=[],
        local_branches=["main", "feat/FT-1-x"],
        prs={("feat/FT-1-x", "merged"): {"id": "7", "head_sha": "head-sha"}},
    )
    code, report = fz.finalize(main, "FT-1")
    assert code == fz.EXIT_OK
    assert report["worktree"] is None
    assert order == ["transition", "observe", "delete_branch", "reap"]
    assert observed == [("FT-1", None)]
    assert reaped[0]["expected_tip"] is None


def test_ambiguous_local_branches_refuse(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    _wire(
        monkeypatch,
        tmp_path,
        entries=[],
        local_branches=["feat/FT-1-x", "feat/FT-1-y"],
    )
    with pytest.raises(fz.FinalizeRefused, match="multiple local branches"):
        fz.finalize(main, "FT-1")


def test_no_branch_evidence_errors(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    _wire(monkeypatch, tmp_path, entries=[], local_branches=["main"])
    with pytest.raises(fz.FinalizeError, match="no worktree or local branch"):
        fz.finalize(main, "FT-1")


def test_cli_exit_codes(monkeypatch, tmp_path):
    main = tmp_path / "repo"
    entry = _worktree_entry(main)
    _wire(
        monkeypatch,
        tmp_path,
        entries=[entry],
        prs={("feat/FT-1-x", "open"): {"id": "7"}},
    )
    assert fz.cli_main(["--workspace-root", str(main), "--key", "FT-1"]) == fz.EXIT_NOT_MERGED
