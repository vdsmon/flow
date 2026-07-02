"""Tests for branch_ticket.py, git branch → ticket key resolver."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import branch_ticket


def _write_workspace(root: Path, body: str) -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "workspace.toml").write_text(body, encoding="utf-8")


def _jira_workspace(root: Path, project_key: str = "FT") -> None:
    _write_workspace(
        root,
        f'[tracker]\nbackend = "jira"\n\n[tracker.jira]\ncloud_id = "x"\nproject_key = "{project_key}"\n',
    )


def _beads_workspace(root: Path, prefix: str = "bd") -> None:
    _write_workspace(
        root,
        f'[tracker]\nbackend = "beads"\n\n[tracker.beads]\nprefix = "{prefix}"\n',
    )


def _fake_runner(branch_name: str, returncode: int = 0):
    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args,
            returncode=returncode,
            stdout=branch_name + "\n",
            stderr="" if returncode == 0 else "fatal: not a git repository",
        )

    return run


# ─── Happy path: jira ────────────────────────────────────────────────────────


def test_jira_simple_branch(tmp_path: Path) -> None:
    _jira_workspace(tmp_path)
    runner = _fake_runner("feature/FT-1234-add-cooldown")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) == "FT-1234"


def test_jira_bare_key_branch(tmp_path: Path) -> None:
    _jira_workspace(tmp_path)
    runner = _fake_runner("FT-42")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) == "FT-42"


def test_jira_first_match_wins(tmp_path: Path) -> None:
    _jira_workspace(tmp_path)
    runner = _fake_runner("FT-1-and-FT-2")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) == "FT-1"


def test_jira_no_match(tmp_path: Path) -> None:
    _jira_workspace(tmp_path)
    runner = _fake_runner("feature/something-without-key")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) is None


def test_jira_project_key_with_dash(tmp_path: Path) -> None:
    _jira_workspace(tmp_path, project_key="MY-PROJ")
    runner = _fake_runner("MY-PROJ-77-feature")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) == "MY-PROJ-77"


def test_jira_does_not_match_other_project_prefix(tmp_path: Path) -> None:
    _jira_workspace(tmp_path, project_key="FT")
    runner = _fake_runner("XYZ-1234-some-feature")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) is None


# ─── Happy path: beads ───────────────────────────────────────────────────────


def test_beads_simple_branch(tmp_path: Path) -> None:
    _beads_workspace(tmp_path, prefix="bd")
    runner = _fake_runner("feature/bd-a4f7-add-rate-limit")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) == "bd-a4f7"


def test_beads_long_hash_id(tmp_path: Path) -> None:
    _beads_workspace(tmp_path, prefix="bd")
    runner = _fake_runner("bd-abc12345/feature")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) == "bd-abc12345"


def test_beads_custom_prefix(tmp_path: Path) -> None:
    _beads_workspace(tmp_path, prefix="myrepo")
    runner = _fake_runner("feature/myrepo-9zzz/x")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) == "myrepo-9zzz"


def test_beads_no_match_below_min_length(tmp_path: Path) -> None:
    _beads_workspace(tmp_path, prefix="bd")
    runner = _fake_runner("feature/bd-12")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) is None


def test_beads_no_match_other_prefix(tmp_path: Path) -> None:
    _beads_workspace(tmp_path, prefix="bd")
    runner = _fake_runner("feature/other-abcd")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) is None


def test_beads_dotted_child_key(tmp_path: Path) -> None:
    _beads_workspace(tmp_path, prefix="flow")
    runner = _fake_runner("feature/flow-kx17.2-revision-lifecycle-seam")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) == "flow-kx17.2"


def test_beads_dotted_child_never_resolves_as_parent(tmp_path: Path) -> None:
    # longest match wins: flow-kx17.2 is a distinct bead from its parent epic
    # flow-kx17; stopping at the pre-dot word boundary silently operates on the
    # wrong bead (recover / recall / revise all consume this raw)
    _beads_workspace(tmp_path, prefix="flow")
    runner = _fake_runner("feat/flow-kx17.2")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) == "flow-kx17.2"


def test_beads_parent_key_unchanged(tmp_path: Path) -> None:
    _beads_workspace(tmp_path, prefix="flow")
    runner = _fake_runner("feat/flow-kx17-revision-lifecycle")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) == "flow-kx17"


def test_beads_three_char_stem(tmp_path: Path) -> None:
    _beads_workspace(tmp_path, prefix="flow")
    runner = _fake_runner("feat/flow-820-fix-the-thing")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) == "flow-820"


def test_beads_three_char_stem_dotted_child(tmp_path: Path) -> None:
    _beads_workspace(tmp_path, prefix="flow")
    runner = _fake_runner("feature/flow-ml7.1-metric-attribution-stamp")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) == "flow-ml7.1"


def test_beads_multi_level_dotted_child(tmp_path: Path) -> None:
    _beads_workspace(tmp_path, prefix="bd")
    runner = _fake_runner("feat/bd-abc1.2.3-x")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) == "bd-abc1.2.3"


def test_beads_non_numeric_dot_suffix_falls_back_to_stem(tmp_path: Path) -> None:
    # only `.N` child suffixes extend the key; a stray non-numeric dot segment
    # is not part of any bead key
    _beads_workspace(tmp_path, prefix="flow")
    runner = _fake_runner("feat/flow-kx17.next")
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) == "flow-kx17"


# ─── Explicit --branch (PR→ticket enabler) ───────────────────────────────────


def test_explicit_branch_resolves_without_git(tmp_path: Path) -> None:
    _jira_workspace(tmp_path)

    def _boom(args: list[str], cwd: Path):  # must not be called when branch is given
        raise AssertionError("git runner must not run when --branch is explicit")

    assert branch_ticket.resolve(tmp_path, tmp_path, _boom, branch="feature/FT-1-x") == "FT-1"


def test_explicit_branch_no_match_returns_none(tmp_path: Path) -> None:
    _jira_workspace(tmp_path)
    assert branch_ticket.resolve(tmp_path, tmp_path, branch="feature/no-key") is None


def test_explicit_branch_beads(tmp_path: Path) -> None:
    _beads_workspace(tmp_path, prefix="bd")
    assert branch_ticket.resolve(tmp_path, tmp_path, branch="feature/bd-a4f7-x") == "bd-a4f7"


def test_cli_explicit_branch_returns_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _jira_workspace(tmp_path)
    # the current-branch runner would resolve a DIFFERENT key; --branch must win.
    monkeypatch.setattr(branch_ticket, "_default_runner", lambda: _fake_runner("FT-99"))
    rc = branch_ticket.cli_main(
        ["--workspace-root", str(tmp_path), "--cwd", str(tmp_path), "--branch", "feature/FT-1-x"]
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == "FT-1"


# ─── Environment errors ──────────────────────────────────────────────────────


def test_missing_workspace_toml_raises(tmp_path: Path) -> None:
    runner = _fake_runner("any")
    with pytest.raises(branch_ticket._BranchTicketError, match=r"no workspace\.toml"):
        branch_ticket.resolve(tmp_path, tmp_path, runner)


def test_malformed_workspace_toml_raises(tmp_path: Path) -> None:
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text("not = valid = toml", encoding="utf-8")
    runner = _fake_runner("any")
    with pytest.raises(branch_ticket._BranchTicketError, match="does not parse"):
        branch_ticket.resolve(tmp_path, tmp_path, runner)


def test_missing_tracker_block_raises(tmp_path: Path) -> None:
    _write_workspace(tmp_path, '[pipeline]\nstages = ["ticket"]\n')
    runner = _fake_runner("any")
    with pytest.raises(branch_ticket._BranchTicketError, match=r"missing \[tracker\]"):
        branch_ticket.resolve(tmp_path, tmp_path, runner)


def test_missing_jira_project_key_raises(tmp_path: Path) -> None:
    _write_workspace(tmp_path, '[tracker]\nbackend = "jira"\n\n[tracker.jira]\ncloud_id = "x"\n')
    runner = _fake_runner("any")
    with pytest.raises(branch_ticket._BranchTicketError, match="project_key"):
        branch_ticket.resolve(tmp_path, tmp_path, runner)


def test_missing_beads_prefix_raises(tmp_path: Path) -> None:
    _write_workspace(tmp_path, '[tracker]\nbackend = "beads"\n\n[tracker.beads]\n')
    runner = _fake_runner("any")
    with pytest.raises(branch_ticket._BranchTicketError, match="prefix"):
        branch_ticket.resolve(tmp_path, tmp_path, runner)


def test_not_in_git_repo_raises(tmp_path: Path) -> None:
    _jira_workspace(tmp_path)
    runner = _fake_runner("", returncode=128)
    with pytest.raises(branch_ticket._BranchTicketError, match="git rev-parse failed"):
        branch_ticket.resolve(tmp_path, tmp_path, runner)


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_match_returns_0_and_prints_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _jira_workspace(tmp_path)
    monkeypatch.setattr(branch_ticket, "_default_runner", lambda: _fake_runner("FT-99"))
    rc = branch_ticket.cli_main(["--workspace-root", str(tmp_path), "--cwd", str(tmp_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "FT-99"


def test_cli_no_match_returns_3_empty_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _jira_workspace(tmp_path)
    monkeypatch.setattr(branch_ticket, "_default_runner", lambda: _fake_runner("feature/no-key"))
    rc = branch_ticket.cli_main(["--workspace-root", str(tmp_path), "--cwd", str(tmp_path)])
    assert rc == 3
    captured = capsys.readouterr()
    assert captured.out == ""


def test_cli_env_error_returns_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(branch_ticket, "_default_runner", lambda: _fake_runner(""))
    rc = branch_ticket.cli_main(["--workspace-root", str(tmp_path), "--cwd", str(tmp_path)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "branch-ticket:" in captured.err
