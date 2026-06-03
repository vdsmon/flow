"""Tests for diff_extract.py — git diff capture for flow stages.

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
