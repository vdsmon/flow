"""Tests for recall_pending.py — hook-appends / dispatcher-promotes protocol.

Most tests inject a fake git runner for the ancestor check (rule (e)); one test
uses a real tmp git repo to exercise merge-base --is-ancestor for real.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import recall_pending

# ─── Helpers ───────────────────────────────────────────────────────────────────


def _fake_runner(returncode: int) -> recall_pending.Runner:
    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args, returncode=returncode, stdout="", stderr="")

    return run


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
_NOW_ISO = _iso(_NOW)


def _append(root: Path, **overrides: object) -> dict:
    kwargs: dict = {
        "hook_observed_at": _iso(_NOW - timedelta(hours=1)),
        "branch": "feature/FT-1",
        "head_sha": "abc123",
        "cwd": "/work/repo",
        "hook_time_resolved_ticket": "",
        "query": "how do we lock",
        "returned_ids": ["id1", "id2"],
        "rank_scores": [0.9, 0.5],
    }
    kwargs.update(overrides)
    return recall_pending.append_pending(root, **kwargs)


def _promote(root: Path, runner: recall_pending.Runner, **overrides: object) -> list[dict]:
    kwargs: dict = {
        "ticket": "FT-1",
        "branch": "feature/FT-1",
        "head_sha": "deadbeef",
        "cwd": "/work/repo",
        "now_iso": _NOW_ISO,
        "runner": runner,
    }
    kwargs.update(overrides)
    return recall_pending.promote_matching(root, **kwargs)


# ─── pending_id determinism ────────────────────────────────────────────────────


def test_pending_id_deterministic() -> None:
    a = recall_pending.compute_pending_id("2026-05-28T11:00:00Z", "br", "sha", "/cwd")
    b = recall_pending.compute_pending_id("2026-05-28T11:00:00Z", "br", "sha", "/cwd")
    assert a == b
    assert len(a) == 16


def test_pending_id_varies_with_inputs() -> None:
    base = recall_pending.compute_pending_id("t", "br", "sha", "/cwd")
    assert base != recall_pending.compute_pending_id("t", "br2", "sha", "/cwd")
    assert base != recall_pending.compute_pending_id("t", "br", "sha2", "/cwd")
    assert base != recall_pending.compute_pending_id("t2", "br", "sha", "/cwd")


# ─── append idempotency ────────────────────────────────────────────────────────


def test_append_writes_one_line(tmp_path: Path) -> None:
    entry = _append(tmp_path)
    path = recall_pending.recall_pending_path(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["pending_id"] == entry["pending_id"]


def test_append_idempotent_same_fields(tmp_path: Path) -> None:
    first = _append(tmp_path)
    second = _append(tmp_path)
    assert first["pending_id"] == second["pending_id"]
    path = recall_pending.recall_pending_path(tmp_path)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_append_idempotent_returns_disk_entry(tmp_path: Path) -> None:
    """Same observation, different payload -> no-op returning the on-disk entry."""
    first = _append(tmp_path, query="original", returned_ids=["a"])
    second = _append(tmp_path, query="different", returned_ids=["b", "c"])
    assert second["query"] == "original"
    assert second["returned_ids"] == ["a"]
    assert first["pending_id"] == second["pending_id"]
    path = recall_pending.recall_pending_path(tmp_path)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


# ─── list quarantines malformed lines ───────────────────────────────────────────


def test_list_quarantines_malformed(tmp_path: Path) -> None:
    _append(tmp_path)
    path = recall_pending.recall_pending_path(tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        fh.write('["a list not an object"]\n')
    entries = recall_pending.list_pending(tmp_path)
    assert len(entries) == 1
    quarantine = path.with_name(path.name + ".quarantine")
    assert quarantine.exists()
    q_lines = quarantine.read_text(encoding="utf-8").splitlines()
    assert len(q_lines) == 2


# ─── promote: all five rules pass ────────────────────────────────────────────────


def test_promote_matches_and_writes_log(tmp_path: Path) -> None:
    entry = _append(tmp_path)
    promoted = _promote(tmp_path, _fake_runner(0))
    assert len(promoted) == 1
    assert promoted[0]["pending_id"] == entry["pending_id"]
    assert promoted[0]["recalled_at"] == _NOW_ISO
    # hook_observed_at preserved as metadata
    assert promoted[0]["hook_observed_at"] == entry["hook_observed_at"]

    log_path = tmp_path / ".flow" / "runs" / "FT-1" / "recall-log.jsonl"
    log_lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 1
    assert json.loads(log_lines[0])["recalled_at"] == _NOW_ISO

    # removed from pending
    assert recall_pending.list_pending(tmp_path) == []


# ─── promote: each rule failing -> not promoted ──────────────────────────────────


def test_rule_a_branch_mismatch_keeps(tmp_path: Path) -> None:
    _append(tmp_path, branch="other-branch")
    promoted = _promote(tmp_path, _fake_runner(0))
    assert promoted == []
    assert len(recall_pending.list_pending(tmp_path)) == 1


def test_rule_b_cwd_mismatch_keeps(tmp_path: Path) -> None:
    _append(tmp_path, cwd="/somewhere/else")
    promoted = _promote(tmp_path, _fake_runner(0))
    assert promoted == []
    assert len(recall_pending.list_pending(tmp_path)) == 1


def test_rule_c_within_window_recent_promotes(tmp_path: Path) -> None:
    _append(tmp_path, hook_observed_at=_iso(_NOW - timedelta(hours=23)))
    promoted = _promote(tmp_path, _fake_runner(0))
    assert len(promoted) == 1


def test_rule_d_resolved_ticket_mismatch_keeps(tmp_path: Path) -> None:
    _append(tmp_path, hook_time_resolved_ticket="FT-999")
    promoted = _promote(tmp_path, _fake_runner(0))
    assert promoted == []
    assert len(recall_pending.list_pending(tmp_path)) == 1


def test_rule_d_resolved_ticket_match_promotes(tmp_path: Path) -> None:
    _append(tmp_path, hook_time_resolved_ticket="FT-1")
    promoted = _promote(tmp_path, _fake_runner(0))
    assert len(promoted) == 1


def test_rule_e_non_ancestor_keeps(tmp_path: Path) -> None:
    _append(tmp_path)
    promoted = _promote(tmp_path, _fake_runner(1))
    assert promoted == []
    assert len(recall_pending.list_pending(tmp_path)) == 1


# ─── promote: >24h entry -> .stale ───────────────────────────────────────────────


def test_stale_entry_moved_to_stale_file(tmp_path: Path) -> None:
    _append(tmp_path, hook_observed_at=_iso(_NOW - timedelta(hours=25)))
    promoted = _promote(tmp_path, _fake_runner(0))
    assert promoted == []
    # not in pending anymore
    assert recall_pending.list_pending(tmp_path) == []
    stale_path = recall_pending.recall_pending_path(tmp_path).with_name(
        "recall-pending.jsonl.stale"
    )
    assert stale_path.exists()
    assert len(stale_path.read_text(encoding="utf-8").splitlines()) == 1


def test_stale_takes_precedence_over_match(tmp_path: Path) -> None:
    """A >24h entry that matches all other rules still goes to stale, not log."""
    _append(tmp_path, hook_observed_at=_iso(_NOW - timedelta(hours=30)))
    promoted = _promote(tmp_path, _fake_runner(0))
    assert promoted == []
    log_path = tmp_path / ".flow" / "runs" / "FT-1" / "recall-log.jsonl"
    assert not log_path.exists()


def test_promote_partitions_three_ways(tmp_path: Path) -> None:
    _append(tmp_path, head_sha="match-sha")  # promotes
    _append(tmp_path, branch="other", head_sha="keep-sha")  # keep (rule a)
    _append(tmp_path, head_sha="stale-sha", hook_observed_at=_iso(_NOW - timedelta(hours=48)))
    promoted = _promote(tmp_path, _fake_runner(0))
    assert len(promoted) == 1
    assert promoted[0]["head_sha"] == "match-sha"
    remaining = recall_pending.list_pending(tmp_path)
    assert len(remaining) == 1
    assert remaining[0]["branch"] == "other"
    stale_path = recall_pending.recall_pending_path(tmp_path).with_name(
        "recall-pending.jsonl.stale"
    )
    assert len(stale_path.read_text(encoding="utf-8").splitlines()) == 1


# ─── real git ancestor check ─────────────────────────────────────────────────────


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout


def test_real_git_ancestor_promotes_descendant_keeps(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "--initial-branch=main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "test"], repo)
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-m", "c1"], repo)
    ancestor_sha = _git(["rev-parse", "HEAD"], repo).strip()

    # side branch off c1, committed but not merged -> not an ancestor of main HEAD
    _git(["checkout", "-b", "side"], repo)
    (repo / "g.txt").write_text("side\n", encoding="utf-8")
    _git(["add", "g.txt"], repo)
    _git(["commit", "-m", "side commit"], repo)
    side_sha = _git(["rev-parse", "HEAD"], repo).strip()

    _git(["checkout", "main"], repo)
    (repo / "f.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-m", "c2"], repo)

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).strip()
    cwd = str(repo)

    # ancestor (c1) promotes
    root_a = tmp_path / "ws_a"
    recall_pending.append_pending(
        root_a,
        hook_observed_at=_iso(_NOW - timedelta(hours=1)),
        branch=branch,
        head_sha=ancestor_sha,
        cwd=cwd,
        hook_time_resolved_ticket="",
        query="q",
        returned_ids=[],
        rank_scores=[],
    )
    promoted = recall_pending.promote_matching(
        root_a, ticket="FT-1", branch=branch, head_sha="x", cwd=cwd, now_iso=_NOW_ISO
    )
    assert len(promoted) == 1

    # side branch sha (not an ancestor of main HEAD) is kept
    root_b = tmp_path / "ws_b"
    recall_pending.append_pending(
        root_b,
        hook_observed_at=_iso(_NOW - timedelta(hours=1)),
        branch=branch,
        head_sha=side_sha,
        cwd=cwd,
        hook_time_resolved_ticket="",
        query="q",
        returned_ids=[],
        rank_scores=[],
    )
    promoted_b = recall_pending.promote_matching(
        root_b, ticket="FT-1", branch=branch, head_sha="x", cwd=cwd, now_iso=_NOW_ISO
    )
    assert promoted_b == []
    assert len(recall_pending.list_pending(root_b)) == 1


# ─── malformed hook_observed_at -> kept, no crash ──────────────────────────────


def test_malformed_observed_at_kept(tmp_path: Path) -> None:
    _append(tmp_path, hook_observed_at="not-a-date")
    promoted = _promote(tmp_path, _fake_runner(0))
    assert promoted == []
    assert len(recall_pending.list_pending(tmp_path)) == 1
