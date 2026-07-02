"""Tests for diff_extract.py, git diff capture for flow stages.

Uses real tmp git repos for fidelity (binary capture, rename detection, blob
sha behavior are git-internal and not worth mocking).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import diff_extract
import state
import ticket_frontmatter

# ─── Tmp git repo fixture ────────────────────────────────────────────────────


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Initialize a tmp git repo with one initial commit."""
    _git(["init", "--initial-branch=main"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "test"], tmp_path)
    (tmp_path / "README.md").write_text("# initial\n", encoding="utf-8")
    _git(["add", "README.md"], tmp_path)
    _git(["commit", "-m", "initial"], tmp_path)
    return tmp_path


# ─── since ───────────────────────────────────────────────────────────────────


def test_since_returns_files_touched(tmp_repo: Path) -> None:
    initial = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    (tmp_repo / "a.py").write_text("print('hi')\n", encoding="utf-8")
    _git(["add", "a.py"], tmp_repo)
    _git(["commit", "-m", "add a"], tmp_repo)
    payload = diff_extract.diff_since(initial, tmp_repo)
    assert payload["files_touched"] == ["a.py"]
    assert payload["insertions"] == 1
    assert payload["deletions"] == 0
    assert payload["binary"] is False


def test_since_counts_insertions_deletions(tmp_repo: Path) -> None:
    initial = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    (tmp_repo / "a.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    _git(["add", "a.py"], tmp_repo)
    _git(["commit", "-m", "add"], tmp_repo)
    (tmp_repo / "a.py").write_text("line1\nline2-changed\n", encoding="utf-8")
    _git(["add", "a.py"], tmp_repo)
    _git(["commit", "-m", "modify"], tmp_repo)
    payload = diff_extract.diff_since(initial, tmp_repo)
    assert payload["insertions"] == 2
    assert payload["deletions"] == 0


def test_check_ownership_ok_when_only_planned_changed(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["a.py"])
    (tmp_repo / "a.py").write_text("print('hi')\n", encoding="utf-8")
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is True
    assert payload["unowned_changes"] == []


def test_check_ownership_refuses_unowned_change(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["a.py"])
    (tmp_repo / "a.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_repo / "b.py").write_text("print('unrelated')\n", encoding="utf-8")
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is False
    assert "b.py" in payload["unowned_changes"]


def test_check_ownership_planned_file_in_new_untracked_dir(tmp_repo: Path, tmp_path: Path) -> None:
    # Regression: bare `git status --porcelain` collapses a fully-untracked dir to
    # "pkg/", which never matches the per-file planned entry and false-positives the
    # whole dir as unowned. --untracked-files=all must list the files individually.
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["pkg/mod.py"])
    (tmp_repo / "pkg").mkdir()
    (tmp_repo / "pkg" / "mod.py").write_text("print('planned')\n", encoding="utf-8")
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is True
    assert payload["unowned_changes"] == []
    assert "pkg/mod.py" in payload["changed"]
    assert "pkg/" not in payload["changed"]


def test_check_ownership_unplanned_sibling_in_new_dir_is_unowned(
    tmp_repo: Path, tmp_path: Path
) -> None:
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["pkg/mod.py"])
    (tmp_repo / "pkg").mkdir()
    (tmp_repo / "pkg" / "mod.py").write_text("print('planned')\n", encoding="utf-8")
    (tmp_repo / "pkg" / "other.py").write_text("print('unplanned')\n", encoding="utf-8")
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is False
    assert "pkg/other.py" in payload["unowned_changes"]
    assert "pkg/mod.py" not in payload["unowned_changes"]


def test_check_ownership_excludes_bootstrap_claude_dir(tmp_repo: Path, tmp_path: Path) -> None:
    # Regression: flow_worktree._copy_config bootstrap-copies the whole .claude/
    # dir into each run worktree; .claude/settings.json is untracked and not
    # gitignored, so it surfaces under --untracked-files=all and false-flags as
    # unowned, exiting 3 and blocking the commit stage. It must be excluded like
    # .flow/ run scratch.
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["a.py"])
    (tmp_repo / "a.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_repo / ".claude").mkdir()
    (tmp_repo / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is True
    assert ".claude/settings.json" not in payload["unowned_changes"]
    assert ".claude/settings.json" not in payload["changed"]


def test_check_ownership_non_ascii_planned_file(tmp_repo: Path, tmp_path: Path) -> None:
    # Regression: git C-quotes non-ASCII paths in --porcelain/ls-files output
    # unless core.quotePath=false (e.g. "pkg/caf\303\251.py"). The quoted string
    # never matches the literal planned entry, so the ownership gate false-flags a
    # legit planned file as unowned and blocks the commit.
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["pkg/café.py"])
    (tmp_repo / "pkg").mkdir()
    (tmp_repo / "pkg" / "café.py").write_text("print('planned')\n", encoding="utf-8")
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is True
    assert payload["unowned_changes"] == []
    assert "pkg/café.py" in payload["changed"]


def test_check_ownership_reconcile_widened_plan_passes(tmp_repo: Path, tmp_path: Path) -> None:
    # the post-implement reconcile widens planned_files to include a file implement
    # legitimately touched outside the original plan; with it recorded in the
    # baseline, the gate owns both files and passes (the fail-safe the commit stage
    # relies on so a legitimate widened commit is not refused).
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["a.py", "b.py"])
    (tmp_repo / "a.py").write_text("print('planned')\n", encoding="utf-8")
    (tmp_repo / "b.py").write_text("print('reconcile-widened')\n", encoding="utf-8")
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is True
    assert payload["unowned_changes"] == []


def test_check_ownership_cli_exit_3(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["a.py"])
    (tmp_repo / "b.py").write_text("x\n", encoding="utf-8")
    rc = diff_extract.cli_main(
        [
            "check-ownership",
            "--ticket",
            "FT-1",
            "--ticket-dir",
            str(ticket_dir),
            "--cwd",
            str(tmp_repo),
        ]
    )
    assert rc == 3


def test_check_ownership_planned_file_with_space(tmp_repo: Path, tmp_path: Path) -> None:
    # Regression: git status --porcelain C-quotes any path containing a space
    # (e.g. ?? "a b.py") for column-disambiguation, independent of core.quotePath.
    # The quoted token never matches the unquoted planned entry, so the gate
    # false-flags a legit planned file as unowned and blocks the commit.
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["a b.py"])
    (tmp_repo / "a b.py").write_text("print('planned')\n", encoding="utf-8")
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is True
    assert payload["unowned_changes"] == []
    assert "a b.py" in payload["changed"]


def test_check_ownership_unplanned_file_with_space_is_unowned(
    tmp_repo: Path, tmp_path: Path
) -> None:
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["a.py"])
    (tmp_repo / "a.py").write_text("print('planned')\n", encoding="utf-8")
    (tmp_repo / "b c.py").write_text("print('unplanned')\n", encoding="utf-8")
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is False
    assert "b c.py" in payload["unowned_changes"]


def test_check_ownership_planned_file_with_special_chars(tmp_repo: Path, tmp_path: Path) -> None:
    # backslash is C-quoted by porcelain and is valid on POSIX filesystems.
    name = "a\\b.py"
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=[name])
    (tmp_repo / name).write_text("print('planned')\n", encoding="utf-8")
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is True
    assert payload["unowned_changes"] == []
    assert name in payload["changed"]


def test_check_ownership_renamed_planned_file_with_space(tmp_repo: Path, tmp_path: Path) -> None:
    (tmp_repo / "old.py").write_text("print('x')\n", encoding="utf-8")
    _git(["add", "old.py"], tmp_repo)
    _git(["commit", "-m", "add old"], tmp_repo)
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["old.py", "new name.py"])
    _git(["mv", "old.py", "new name.py"], tmp_repo)
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is True
    assert payload["unowned_changes"] == []
    assert "new name.py" in payload["changed"]
    assert "old.py" in payload["changed"]


def test_check_ownership_unowned_rename_source_refuses(tmp_repo: Path, tmp_path: Path) -> None:
    # git mv of an unplanned source into a planned destination must not drop the
    # source deletion; the gate refuses the out-of-scope source.
    (tmp_repo / "old.py").write_text("print('x')\n", encoding="utf-8")
    _git(["add", "old.py"], tmp_repo)
    _git(["commit", "-m", "add old"], tmp_repo)
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["new name.py"])
    _git(["mv", "old.py", "new name.py"], tmp_repo)
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is False
    assert "old.py" in payload["unowned_changes"]


def test_check_ownership_rename_within_flow_dir_excluded(tmp_repo: Path, tmp_path: Path) -> None:
    # both rename endpoints route through the .flow/ exclusion, so neither side
    # of a `git mv .flow/x -> .flow/y` counts against ownership.
    (tmp_repo / ".flow").mkdir()
    (tmp_repo / ".flow" / "x.py").write_text("print('x')\n", encoding="utf-8")
    _git(["add", "-f", ".flow/x.py"], tmp_repo)
    _git(["commit", "-m", "add flow x"], tmp_repo)
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["a.py"])
    (tmp_repo / "a.py").write_text("print('planned')\n", encoding="utf-8")
    _git(["mv", ".flow/x.py", ".flow/y.py"], tmp_repo)
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is True
    assert ".flow/x.py" not in payload["changed"]
    assert ".flow/y.py" not in payload["changed"]


def test_check_ownership_committed_unplanned_change_refused(tmp_repo: Path, tmp_path: Path) -> None:
    # Regression: a rogue `git commit` of an unplanned file mid-implement leaves
    # `git status` clean for that path, so a working-tree-only scan passed it and
    # the change rode the branch into the PR. The gate must also cover the
    # baseline.head_sha..HEAD delta.
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["a.py"])
    (tmp_repo / "a.py").write_text("print('planned')\n", encoding="utf-8")
    (tmp_repo / "rogue.py").write_text("print('rogue')\n", encoding="utf-8")
    _git(["add", "rogue.py"], tmp_repo)
    _git(["commit", "-m", "rogue"], tmp_repo)
    assert "rogue.py" not in _git(["status", "--porcelain"], tmp_repo)
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is False
    assert "rogue.py" in payload["unowned_changes"]
    assert "rogue.py" in payload["changed"]


def test_check_ownership_committed_planned_change_ok(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["a.py"])
    (tmp_repo / "a.py").write_text("print('planned')\n", encoding="utf-8")
    _git(["add", "a.py"], tmp_repo)
    _git(["commit", "-m", "planned work"], tmp_repo)
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is True
    assert payload["unowned_changes"] == []
    assert "a.py" in payload["changed"]


def test_check_ownership_committed_flow_dir_change_excluded(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["a.py"])
    (tmp_repo / ".flow" / "notes.md").write_text("run scratch\n", encoding="utf-8")
    _git(["add", "-f", ".flow/notes.md"], tmp_repo)
    _git(["commit", "-m", "flow scratch"], tmp_repo)
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is True
    assert ".flow/notes.md" not in payload["changed"]


def test_check_ownership_committed_rename_source_refused(tmp_repo: Path, tmp_path: Path) -> None:
    # a committed `git mv` of an unplanned source into a planned destination must
    # surface the source deletion; --no-renames lists both endpoints.
    (tmp_repo / "old.py").write_text("print('x')\n", encoding="utf-8")
    _git(["add", "old.py"], tmp_repo)
    _git(["commit", "-m", "add old"], tmp_repo)
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["new.py"])
    _git(["mv", "old.py", "new.py"], tmp_repo)
    _git(["commit", "-m", "rename"], tmp_repo)
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is False
    assert "old.py" in payload["unowned_changes"]
    assert "new.py" not in payload["unowned_changes"]


def test_check_ownership_committed_and_dirty_both_flagged(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["a.py"])
    (tmp_repo / "rogue.py").write_text("committed\n", encoding="utf-8")
    _git(["add", "rogue.py"], tmp_repo)
    _git(["commit", "-m", "rogue"], tmp_repo)
    (tmp_repo / "dirty.py").write_text("uncommitted\n", encoding="utf-8")
    payload = diff_extract.check_ownership(ticket_dir, tmp_repo)
    assert payload["ok"] is False
    assert "rogue.py" in payload["unowned_changes"]
    assert "dirty.py" in payload["unowned_changes"]


def test_check_ownership_missing_head_sha_raises(tmp_repo: Path, tmp_path: Path) -> None:
    # a baseline without head_sha cannot anchor the committed-delta scan; fail
    # closed (exit 1) instead of silently narrowing to the working tree.
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "baseline.json").write_text(
        json.dumps({"stage": "implement", "planned_files": ["a.py"], "blobs": {}}),
        encoding="utf-8",
    )
    with pytest.raises(diff_extract._BaselineMissing, match="head_sha"):
        diff_extract.check_ownership(ticket_dir, tmp_repo)


def test_check_ownership_cli_exit_3_for_committed_change(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_repo / ".flow" / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["a.py"])
    (tmp_repo / "rogue.py").write_text("x\n", encoding="utf-8")
    _git(["add", "rogue.py"], tmp_repo)
    _git(["commit", "-m", "rogue"], tmp_repo)
    rc = diff_extract.cli_main(
        [
            "check-ownership",
            "--ticket",
            "FT-1",
            "--ticket-dir",
            str(ticket_dir),
            "--cwd",
            str(tmp_repo),
        ]
    )
    assert rc == 3


def test_unquote_porcelain_path_octal_utf8() -> None:
    # quotePath=false keeps non-ASCII literal so this is unreachable through
    # check_ownership; exercise the octal multibyte round-trip at the helper level.
    assert diff_extract._unquote_porcelain_path('"caf\\303\\251.py"') == "café.py"


def test_unquote_porcelain_path_passthrough() -> None:
    assert diff_extract._unquote_porcelain_path("a.py") == "a.py"
    assert diff_extract._unquote_porcelain_path('"a b.py"') == "a b.py"
    assert diff_extract._unquote_porcelain_path('"a\\"b.py"') == 'a"b.py'
    assert diff_extract._unquote_porcelain_path('"a\\\\b.py"') == "a\\b.py"
    assert diff_extract._unquote_porcelain_path('"a\\tb.py"') == "a\tb.py"


def test_since_detects_binary(tmp_repo: Path) -> None:
    initial = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    (tmp_repo / "blob.bin").write_bytes(bytes(range(256)))
    _git(["add", "blob.bin"], tmp_repo)
    _git(["commit", "-m", "add binary"], tmp_repo)
    payload = diff_extract.diff_since(initial, tmp_repo)
    assert payload["binary"] is True


def test_since_multiple_files(tmp_repo: Path) -> None:
    initial = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    (tmp_repo / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_repo / "b.py").write_text("b\n", encoding="utf-8")
    _git(["add", "."], tmp_repo)
    _git(["commit", "-m", "add multi"], tmp_repo)
    payload = diff_extract.diff_since(initial, tmp_repo)
    assert sorted(payload["files_touched"]) == ["a.py", "b.py"]


def test_since_no_changes(tmp_repo: Path) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    payload = diff_extract.diff_since(head, tmp_repo)
    assert payload["files_touched"] == []
    assert payload["insertions"] == 0
    assert payload["deletions"] == 0


def test_since_invalid_ref_raises(tmp_repo: Path) -> None:
    with pytest.raises(diff_extract._GitError, match="git diff"):
        diff_extract.diff_since("not-a-ref", tmp_repo)


# ─── since-stage ─────────────────────────────────────────────────────────────


def _seed_state(ticket_dir: Path, stage: str, head_sha: str) -> None:
    state.init(ticket_dir, "FT-1", "jira", [stage])
    state.begin_stage(ticket_dir, stage, head_sha)


def test_since_stage_reads_started_at_sha(tmp_repo: Path, tmp_path: Path) -> None:
    initial = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, "implement", initial)
    (tmp_repo / "a.py").write_text("x\n", encoding="utf-8")
    _git(["add", "a.py"], tmp_repo)
    _git(["commit", "-m", "add a"], tmp_repo)
    payload = diff_extract.diff_since_stage("implement", ticket_dir, tmp_repo)
    assert payload["files_touched"] == ["a.py"]


def test_since_stage_missing_state_exits_1(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_path / "runs" / "missing"
    with pytest.raises(diff_extract._BaselineMissing):
        diff_extract.diff_since_stage("implement", ticket_dir, tmp_repo)


def test_since_stage_missing_stage_record_exits_1(tmp_repo: Path, tmp_path: Path) -> None:
    initial = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, "implement", initial)
    with pytest.raises(diff_extract._BaselineMissing, match=r"not in state\.json"):
        diff_extract.diff_since_stage("commit", ticket_dir, tmp_repo)


def test_since_stage_pending_no_started_sha_raises(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_path / "runs" / "FT-1"
    state.init(ticket_dir, "FT-1", "jira", ["implement"])
    with pytest.raises(diff_extract._BaselineMissing, match="no started_at_sha"):
        diff_extract.diff_since_stage("implement", ticket_dir, tmp_repo)


# ─── record-baseline ─────────────────────────────────────────────────────────


def test_record_baseline_writes_file(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_path / "runs" / "FT-1"
    payload = diff_extract.record_baseline("implement", ticket_dir, tmp_repo)
    bpath = ticket_dir / "baseline.json"
    assert bpath.exists()
    loaded = json.loads(bpath.read_text(encoding="utf-8"))
    assert loaded["stage"] == "implement"
    assert loaded["head_sha"] == payload["head_sha"]
    assert loaded["planned_files"] == []
    assert loaded["blobs"] == {}


def test_record_baseline_with_files(tmp_repo: Path, tmp_path: Path) -> None:
    (tmp_repo / "src").mkdir()
    (tmp_repo / "src" / "a.py").write_text("a\n", encoding="utf-8")
    _git(["add", "src/a.py"], tmp_repo)
    _git(["commit", "-m", "seed"], tmp_repo)
    ticket_dir = tmp_path / "runs" / "FT-1"
    payload = diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["src/a.py"])
    assert payload["planned_files"] == ["src/a.py"]


def test_record_baseline_capture_blobs(tmp_repo: Path, tmp_path: Path) -> None:
    (tmp_repo / "src").mkdir()
    (tmp_repo / "src" / "a.py").write_text("a\n", encoding="utf-8")
    _git(["add", "src/a.py"], tmp_repo)
    _git(["commit", "-m", "seed"], tmp_repo)
    ticket_dir = tmp_path / "runs" / "FT-1"
    payload = diff_extract.record_baseline(
        "implement",
        ticket_dir,
        tmp_repo,
        files=["src/a.py"],
        capture_blobs=True,
    )
    assert "src/a.py" in payload["blobs"]
    entry = payload["blobs"]["src/a.py"]
    assert entry["mode"] == "100644"
    assert entry["type"] == "blob"
    assert len(entry["sha"]) == 40


def test_record_baseline_atomic_write(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_path / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo)
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["x"])
    payload = json.loads((ticket_dir / "baseline.json").read_text(encoding="utf-8"))
    assert payload["planned_files"] == ["x"]


def test_record_baseline_outside_git_raises(tmp_path: Path) -> None:
    ticket_dir = tmp_path / "runs" / "FT-1"
    with pytest.raises(diff_extract._GitError, match="git rev-parse"):
        diff_extract.record_baseline("implement", ticket_dir, tmp_path)


# ─── record-baseline frontmatter union ───────────────────────────────────────


def _seed_frontmatter(repo: Path, ticket: str, planned: list[str]) -> Path:
    """Write a +++-delimited frontmatter file at repo/.flow/tickets/<ticket>.md.

    Production path: record_baseline reads cwd/.flow/tickets/<KEY>.md.
    """
    path = repo / ".flow" / "tickets" / f"{ticket}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    ticket_frontmatter.update(path, {"planned_files": json.dumps(planned)})
    return path


def test_record_baseline_unions_frontmatter_planned_files(tmp_repo: Path, tmp_path: Path) -> None:
    _seed_frontmatter(tmp_repo, "FT-1", ["plugin.json", "marketplace.json"])
    ticket_dir = tmp_path / "runs" / "FT-1"
    payload = diff_extract.record_baseline(
        "implement", ticket_dir, tmp_repo, files=["a.py"], ticket="FT-1"
    )
    assert payload["planned_files"] == ["a.py", "plugin.json", "marketplace.json"]


def test_record_baseline_union_dedup_and_order(tmp_repo: Path, tmp_path: Path) -> None:
    # a.py is in both --files and frontmatter; appears once, --files order first.
    _seed_frontmatter(tmp_repo, "FT-1", ["a.py", "plugin.json"])
    ticket_dir = tmp_path / "runs" / "FT-1"
    payload = diff_extract.record_baseline(
        "implement", ticket_dir, tmp_repo, files=["a.py", "b.py"], ticket="FT-1"
    )
    assert payload["planned_files"] == ["a.py", "b.py", "plugin.json"]


def test_record_baseline_frontmatter_only_file_gets_blob(tmp_repo: Path, tmp_path: Path) -> None:
    # A tracked file present ONLY in frontmatter must get a baseline blob, proving
    # the union runs before the blob-capture block.
    (tmp_repo / "plugin.json").write_text("{}\n", encoding="utf-8")
    _git(["add", "plugin.json"], tmp_repo)
    _git(["commit", "-m", "seed plugin.json"], tmp_repo)
    _seed_frontmatter(tmp_repo, "FT-1", ["plugin.json"])
    ticket_dir = tmp_path / "runs" / "FT-1"
    payload = diff_extract.record_baseline(
        "implement", ticket_dir, tmp_repo, files=["a.py"], ticket="FT-1", capture_blobs=True
    )
    assert "plugin.json" in payload["blobs"]
    assert payload["blobs"]["plugin.json"]["type"] == "blob"


def test_record_baseline_frontmatter_absent_degrades_to_files(
    tmp_repo: Path, tmp_path: Path
) -> None:
    ticket_dir = tmp_path / "runs" / "FT-1"
    payload = diff_extract.record_baseline(
        "implement", ticket_dir, tmp_repo, files=["a.py"], ticket="FT-1"
    )
    assert payload["planned_files"] == ["a.py"]


def test_record_baseline_frontmatter_no_planned_key_degrades(
    tmp_repo: Path, tmp_path: Path
) -> None:
    path = tmp_repo / ".flow" / "tickets" / "FT-1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    ticket_frontmatter.update(path, {"status": "open"})
    ticket_dir = tmp_path / "runs" / "FT-1"
    payload = diff_extract.record_baseline(
        "implement", ticket_dir, tmp_repo, files=["a.py"], ticket="FT-1"
    )
    assert payload["planned_files"] == ["a.py"]


def test_record_baseline_frontmatter_non_list_planned_degrades(
    tmp_repo: Path, tmp_path: Path
) -> None:
    path = tmp_repo / ".flow" / "tickets" / "FT-1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    ticket_frontmatter.update(path, {"planned_files": "a-bare-string"})
    ticket_dir = tmp_path / "runs" / "FT-1"
    payload = diff_extract.record_baseline(
        "implement", ticket_dir, tmp_repo, files=["a.py"], ticket="FT-1"
    )
    assert payload["planned_files"] == ["a.py"]


def test_record_baseline_ticket_none_skips_frontmatter(tmp_repo: Path, tmp_path: Path) -> None:
    # Even with a frontmatter file present, ticket=None (default) reads nothing.
    _seed_frontmatter(tmp_repo, "FT-1", ["plugin.json"])
    ticket_dir = tmp_path / "runs" / "FT-1"
    payload = diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["a.py"])
    assert payload["planned_files"] == ["a.py"]


def test_record_baseline_union_end_to_end_diff(tmp_repo: Path, tmp_path: Path) -> None:
    # Regression (flow-7m8): frontmatter-only version file lands in implement.diff.
    (tmp_repo / "plugin.json").write_text('{"version": "0.0.1"}\n', encoding="utf-8")
    _git(["add", "plugin.json"], tmp_repo)
    _git(["commit", "-m", "seed plugin.json"], tmp_repo)
    _seed_frontmatter(tmp_repo, "FT-1", ["plugin.json"])
    ticket_dir = tmp_path / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["a.py"], ticket="FT-1")
    (tmp_repo / "plugin.json").write_text('{"version": "0.0.2"}\n', encoding="utf-8")
    out = diff_extract.capture_implement_diff(ticket_dir, tmp_repo)
    assert "plugin.json" in out.read_text(encoding="utf-8")


def test_cli_record_baseline_unions_frontmatter(
    tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Locks the cwd/.flow/tickets/<KEY>.md production path through the CLI.
    _seed_frontmatter(tmp_repo, "FT-1", ["plugin.json", "marketplace.json"])
    ticket_dir = tmp_path / "runs" / "FT-1"
    rc = diff_extract.cli_main(
        [
            "record-baseline",
            "--stage",
            "implement",
            "--ticket",
            "FT-1",
            "--ticket-dir",
            str(ticket_dir),
            "--files",
            "a.py",
            "--cwd",
            str(tmp_repo),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["planned_files"] == ["a.py", "plugin.json", "marketplace.json"]


# ─── capture-implement-diff ──────────────────────────────────────────────────


def test_capture_implement_diff_writes_file(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_path / "runs" / "FT-1"
    diff_extract.record_baseline(
        "implement", ticket_dir, tmp_repo, files=["a.py"], capture_blobs=False
    )
    (tmp_repo / "a.py").write_text("hello\n", encoding="utf-8")
    _git(["add", "a.py"], tmp_repo)
    _git(["commit", "-m", "add a"], tmp_repo)
    out = diff_extract.capture_implement_diff(ticket_dir, tmp_repo)
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "a.py" in content


def test_capture_implement_diff_missing_baseline_raises(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_path / "runs" / "FT-1"
    with pytest.raises(diff_extract._BaselineMissing, match=r"no baseline\.json"):
        diff_extract.capture_implement_diff(ticket_dir, tmp_repo)


def test_capture_implement_diff_malformed_baseline_raises(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_path / "runs" / "FT-1"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "baseline.json").write_text("not json", encoding="utf-8")
    with pytest.raises(diff_extract._BaselineMissing, match="malformed"):
        diff_extract.capture_implement_diff(ticket_dir, tmp_repo)


def test_capture_implement_diff_binary_content(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_path / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["blob.bin"])
    (tmp_repo / "blob.bin").write_bytes(bytes(range(256)))
    _git(["add", "blob.bin"], tmp_repo)
    _git(["commit", "-m", "add binary"], tmp_repo)
    out = diff_extract.capture_implement_diff(ticket_dir, tmp_repo)
    content = out.read_text(encoding="utf-8")
    assert "GIT binary patch" in content or "blob.bin" in content


def test_capture_implement_diff_with_rename(tmp_repo: Path, tmp_path: Path) -> None:
    """--raw flag surfaces rename metadata."""
    (tmp_repo / "old.py").write_text("content\n", encoding="utf-8")
    _git(["add", "old.py"], tmp_repo)
    _git(["commit", "-m", "add old"], tmp_repo)
    ticket_dir = tmp_path / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["new.py"])
    _git(["mv", "old.py", "new.py"], tmp_repo)
    _git(["commit", "-m", "rename"], tmp_repo)
    out = diff_extract.capture_implement_diff(ticket_dir, tmp_repo)
    assert out.exists()


def test_capture_implement_diff_includes_untracked_new_file(tmp_repo: Path, tmp_path: Path) -> None:
    """A newly created, never-committed planned file must show in implement.diff.

    Working-tree `git diff <sha>` is blind to untracked files; the capture stages
    intent-to-add first so new files appear as additions.
    """
    ticket_dir = tmp_path / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["fresh.py"])
    (tmp_repo / "fresh.py").write_text("brand new\n", encoding="utf-8")
    # deliberately NOT committed and NOT staged
    out = diff_extract.capture_implement_diff(ticket_dir, tmp_repo)
    content = out.read_text(encoding="utf-8")
    assert content.strip() != ""
    assert "fresh.py" in content


def test_capture_implement_diff_rejects_gitignored_planned_file(
    tmp_repo: Path, tmp_path: Path
) -> None:
    # A gitignored planned file would hard-fail `git add --intent-to-add` with an
    # opaque git error; surface it as a diagnosable one instead.
    (tmp_repo / ".gitignore").write_text("*.csv\n", encoding="utf-8")
    _git(["add", ".gitignore"], tmp_repo)
    _git(["commit", "-m", "ignore csv"], tmp_repo)
    ticket_dir = tmp_path / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["data.csv"])
    (tmp_repo / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(diff_extract._IgnoredPlannedFile):
        diff_extract.capture_implement_diff(ticket_dir, tmp_repo)


def test_capture_implement_diff_staged_deletion_and_gitignore_is_committable(
    tmp_repo: Path, tmp_path: Path
) -> None:
    """A `git rm --cached` path that a same-change .gitignore covers is committable.

    The path is untracked-new to `git ls-files` but `git diff HEAD` already emits
    its deletion, so it must NOT trip the gitignore guard.
    """
    (tmp_repo / "settings.json").write_text("{}\n", encoding="utf-8")
    _git(["add", "settings.json"], tmp_repo)
    _git(["commit", "-m", "add settings"], tmp_repo)
    _git(["rm", "--cached", "settings.json"], tmp_repo)
    (tmp_repo / ".gitignore").write_text("settings.json\n", encoding="utf-8")
    _git(["add", ".gitignore"], tmp_repo)
    ticket_dir = tmp_path / "runs" / "FT-1"
    diff_extract.record_baseline(
        "implement", ticket_dir, tmp_repo, files=["settings.json", ".gitignore"]
    )
    out = diff_extract.capture_implement_diff(ticket_dir, tmp_repo)
    content = out.read_text(encoding="utf-8")
    assert content.strip() != ""
    assert "settings.json" in content
    assert ".gitignore" in content


def test_capture_implement_diff_staged_deletion_preserved_in_index(
    tmp_repo: Path, tmp_path: Path
) -> None:
    """A staged deletion must survive capture: not intent-to-added, not reset."""
    (tmp_repo / "settings.json").write_text("{}\n", encoding="utf-8")
    _git(["add", "settings.json"], tmp_repo)
    _git(["commit", "-m", "add settings"], tmp_repo)
    _git(["rm", "--cached", "settings.json"], tmp_repo)
    ticket_dir = tmp_path / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["settings.json"])
    diff_extract.capture_implement_diff(ticket_dir, tmp_repo)
    assert "D\tsettings.json" in _git(["diff", "--cached", "--name-status"], tmp_repo)


def test_capture_implement_diff_untrack_only_patch_applies_after_reset(
    tmp_repo: Path, tmp_path: Path
) -> None:
    """An untrack-only patch applies through the commit stage after a reset to HEAD.

    `capture_implement_diff` leaves the staged deletion in the index (so `git diff
    HEAD` emits it), so the HEAD-relative patch cannot apply against that dirty index
    (`git apply --cached` errors "does not exist in index"). The `git reset --quiet
    HEAD` here models the fixed commit stage (stage-commit.md step 5): a mixed reset
    that cleans the index back to HEAD while leaving the working tree untouched.
    Without it the apply returns rc=1.
    """
    (tmp_repo / "settings.json").write_text("{}\n", encoding="utf-8")
    _git(["add", "settings.json"], tmp_repo)
    _git(["commit", "-m", "add settings"], tmp_repo)
    _git(["rm", "--cached", "settings.json"], tmp_repo)
    (tmp_repo / ".gitignore").write_text("settings.json\n", encoding="utf-8")
    _git(["add", ".gitignore"], tmp_repo)
    ticket_dir = tmp_path / "runs" / "FT-1"
    diff_extract.record_baseline(
        "implement", ticket_dir, tmp_repo, files=["settings.json", ".gitignore"]
    )
    out = diff_extract.capture_implement_diff(ticket_dir, tmp_repo)
    _git(["reset", "--quiet", "HEAD"], tmp_repo)
    apply = subprocess.run(
        ["git", "apply", "--cached", "--binary", str(out)],
        cwd=str(tmp_repo),
        capture_output=True,
        text=True,
        check=False,
    )
    assert apply.returncode == 0, apply.stderr
    assert "D\tsettings.json" in _git(["diff", "--cached", "--name-status"], tmp_repo)
    assert ".gitignore" in _git(["diff", "--cached", "--name-only"], tmp_repo)


def test_capture_implement_diff_untracked_patch_applies_to_index(
    tmp_repo: Path, tmp_path: Path
) -> None:
    """The captured patch for a new file must round-trip through the commit stage.

    Mirrors the real downstream step: a non-dry-run `git apply --cached --binary`
    that must stage the new file WITH its content. Forces diff.external
    (difftastic-style) to confirm --no-ext-diff keeps the body a real patch.
    """
    _git(["config", "diff.external", "false"], tmp_repo)
    ticket_dir = tmp_path / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["fresh.py"])
    (tmp_repo / "fresh.py").write_text("brand new\n", encoding="utf-8")
    out = diff_extract.capture_implement_diff(ticket_dir, tmp_repo)
    apply = subprocess.run(
        ["git", "apply", "--cached", "--binary", str(out)],
        cwd=str(tmp_repo),
        capture_output=True,
        text=True,
        check=False,
    )
    assert apply.returncode == 0, apply.stderr
    assert "fresh.py" in _git(["diff", "--cached", "--name-only"], tmp_repo)
    assert _git(["show", ":fresh.py"], tmp_repo) == "brand new\n"


def test_capture_implement_diff_leaves_index_clean(tmp_repo: Path, tmp_path: Path) -> None:
    """Capturing must not leave the staged intent-to-add entry behind.

    Capture is an observation, so the new file stays untracked afterward.
    """
    ticket_dir = tmp_path / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["fresh.py"])
    (tmp_repo / "fresh.py").write_text("brand new\n", encoding="utf-8")
    diff_extract.capture_implement_diff(ticket_dir, tmp_repo)
    staged = _git(["diff", "--cached", "--name-only"], tmp_repo)
    assert "fresh.py" not in staged
    assert _git(["status", "--short", "fresh.py"], tmp_repo).strip() == "?? fresh.py"


def test_capture_implement_diff_preserves_prestaged_file(tmp_repo: Path, tmp_path: Path) -> None:
    """A planned file the user already staged must remain staged after capture.

    The index restore only targets files that were untracked before capture, so a
    deliberately staged file is left alone.
    """
    ticket_dir = tmp_path / "runs" / "FT-1"
    diff_extract.record_baseline("implement", ticket_dir, tmp_repo, files=["staged.py"])
    (tmp_repo / "staged.py").write_text("on purpose\n", encoding="utf-8")
    _git(["add", "staged.py"], tmp_repo)
    diff_extract.capture_implement_diff(ticket_dir, tmp_repo)
    assert "staged.py" in _git(["diff", "--cached", "--name-only"], tmp_repo)


def test_capture_implement_diff_ignores_missing_planned_file(
    tmp_repo: Path, tmp_path: Path
) -> None:
    """A planned file absent from the working tree must not crash the capture.

    intent-to-add on a nonexistent pathspec is a git error, so missing paths are
    filtered out first.
    """
    ticket_dir = tmp_path / "runs" / "FT-1"
    diff_extract.record_baseline(
        "implement", ticket_dir, tmp_repo, files=["present.py", "absent.py"]
    )
    (tmp_repo / "present.py").write_text("here\n", encoding="utf-8")
    out = diff_extract.capture_implement_diff(ticket_dir, tmp_repo)
    content = out.read_text(encoding="utf-8")
    assert "present.py" in content


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_since_emits_json(tmp_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    initial = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    (tmp_repo / "a.py").write_text("x\n", encoding="utf-8")
    _git(["add", "a.py"], tmp_repo)
    _git(["commit", "-m", "a"], tmp_repo)
    rc = diff_extract.cli_main(["since", "--ref", initial, "--cwd", str(tmp_repo)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["files_touched"] == ["a.py"]


def test_cli_record_baseline_writes_and_exits_0(
    tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ticket_dir = tmp_path / "runs" / "FT-1"
    rc = diff_extract.cli_main(
        [
            "record-baseline",
            "--stage",
            "implement",
            "--ticket",
            "FT-1",
            "--ticket-dir",
            str(ticket_dir),
            "--files",
            "a.py,b.py",
            "--cwd",
            str(tmp_repo),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["planned_files"] == ["a.py", "b.py"]


def test_cli_capture_implement_diff_missing_baseline_exits_1(
    tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ticket_dir = tmp_path / "runs" / "FT-1"
    rc = diff_extract.cli_main(
        [
            "capture-implement-diff",
            "--ticket",
            "FT-1",
            "--ticket-dir",
            str(ticket_dir),
            "--cwd",
            str(tmp_repo),
        ]
    )
    assert rc == 1
    assert "no baseline.json" in capsys.readouterr().err


def test_cli_since_invalid_ref_exits_2(tmp_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = diff_extract.cli_main(["since", "--ref", "garbage-ref", "--cwd", str(tmp_repo)])
    assert rc == 2
    assert "git diff" in capsys.readouterr().err


def test_cli_since_stage_uses_state(
    tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    initial = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, "implement", initial)
    (tmp_repo / "a.py").write_text("x\n", encoding="utf-8")
    _git(["add", "a.py"], tmp_repo)
    _git(["commit", "-m", "a"], tmp_repo)
    rc = diff_extract.cli_main(
        [
            "since-stage",
            "--stage",
            "implement",
            "--ticket",
            "FT-1",
            "--ticket-dir",
            str(ticket_dir),
            "--cwd",
            str(tmp_repo),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["files_touched"] == ["a.py"]


def test_cli_empty_files_list_normalized(
    tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ticket_dir = tmp_path / "runs" / "FT-1"
    rc = diff_extract.cli_main(
        [
            "record-baseline",
            "--stage",
            "implement",
            "--ticket",
            "FT-1",
            "--ticket-dir",
            str(ticket_dir),
            "--files",
            "  ,  ,",
            "--cwd",
            str(tmp_repo),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["planned_files"] == []


# ─── _parse_files_arg ────────────────────────────────────────────────────────


def test_parse_files_arg_json_array() -> None:
    assert diff_extract._parse_files_arg('["a.py","b.py"]') == ["a.py", "b.py"]


def test_parse_files_arg_json_array_with_spaces() -> None:
    assert diff_extract._parse_files_arg('["a.py", "b.py"]') == ["a.py", "b.py"]
    assert diff_extract._parse_files_arg(' ["a.py"]') == ["a.py"]


def test_parse_files_arg_comma_sep() -> None:
    assert diff_extract._parse_files_arg("a.py,b.py") == ["a.py", "b.py"]
    assert diff_extract._parse_files_arg("a.py, b.py") == ["a.py", "b.py"]


def test_parse_files_arg_single_file() -> None:
    assert diff_extract._parse_files_arg("a.py") == ["a.py"]


def test_parse_files_arg_malformed_json_raises() -> None:
    with pytest.raises(ValueError):
        diff_extract._parse_files_arg('["a.py",')


def test_parse_files_arg_non_string_elements_raises() -> None:
    with pytest.raises(ValueError):
        diff_extract._parse_files_arg("[1, 2]")


def test_record_baseline_cli_json_array_literal_no_frontmatter(
    tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Regression: the planned_files array LITERAL must parse cleanly. No
    # frontmatter file exists, so _union_frontmatter_planned cannot re-add the
    # real paths and mask broken parsing.
    ticket_dir = tmp_path / "runs" / "FT-1"
    rc = diff_extract.cli_main(
        [
            "record-baseline",
            "--stage",
            "implement",
            "--ticket",
            "FT-1",
            "--ticket-dir",
            str(ticket_dir),
            "--files",
            '["a.py","b.py"]',
            "--cwd",
            str(tmp_repo),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["planned_files"] == ["a.py", "b.py"]
    for entry in payload["planned_files"]:
        assert "[" not in entry
        assert "]" not in entry
        assert '"' not in entry


def test_record_baseline_cli_malformed_array_exits_nonzero(
    tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ticket_dir = tmp_path / "runs" / "FT-1"
    rc = diff_extract.cli_main(
        [
            "record-baseline",
            "--stage",
            "implement",
            "--ticket",
            "FT-1",
            "--ticket-dir",
            str(ticket_dir),
            "--files",
            '["a.py",',
            "--cwd",
            str(tmp_repo),
        ]
    )
    assert rc != 0
    assert "malformed JSON array literal" in capsys.readouterr().err
