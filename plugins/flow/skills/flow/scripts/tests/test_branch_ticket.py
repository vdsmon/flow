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
        '[tracker]\nbackend = "jira"\n\n'
        f'[tracker.jira]\ncloud_id = "x"\nproject_key = "{project_key}"\n',
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


# ─── Key-shape truth table (jira + beads happy/negative paths) ───────────────
# One (backend-config, branch, expected) row per shape; ids carry the old test
# names so a failure still names its case.


@pytest.mark.parametrize(
    ("backend", "key", "branch", "expected"),
    [
        pytest.param("jira", "FT", "feature/FT-1234-add-cooldown", "FT-1234", id="jira-simple"),
        pytest.param("jira", "FT", "FT-42", "FT-42", id="jira-bare-key"),
        pytest.param("jira", "FT", "FT-1-and-FT-2", "FT-1", id="jira-first-match-wins"),
        pytest.param("jira", "FT", "feature/something-without-key", None, id="jira-no-match"),
        pytest.param("jira", "MY-PROJ", "MY-PROJ-77-feature", "MY-PROJ-77", id="jira-dash-key"),
        # Cross-project keys resolve: intake accepts and delivers them (CO-222 in an
        # FT workspace), so the resolver must see their branches too, or the finalize
        # sweep goes blind and single-key finalize falls to the guard-skipping
        # local-branch path.
        pytest.param(
            "jira", "FT", "XYZ-1234-some-feature", "XYZ-1234", id="jira-cross-project-key"
        ),
        pytest.param(
            "jira", "FT", "feat/CO-222-efd-contrib-bloco-m-credits", "CO-222", id="jira-co-witness"
        ),
        # The home project outranks a foreign key even when the foreign one comes
        # first in the branch name; flow-minted branches carry exactly one key, so
        # this only decides hand-named multi-key branches.
        pytest.param("jira", "FT", "CO-2-then-FT-1", "FT-1", id="jira-home-project-outranks"),
        # A single-letter prefix is not a Jira key shape (min 2 chars).
        pytest.param("jira", "FT", "feat/X-1-thing", None, id="jira-single-letter-no-match"),
        pytest.param("beads", "bd", "feature/bd-a4f7-add-rate-limit", "bd-a4f7", id="beads-simple"),
        pytest.param("beads", "bd", "bd-abc12345/feature", "bd-abc12345", id="beads-long-hash"),
        pytest.param(
            "beads", "myrepo", "feature/myrepo-9zzz/x", "myrepo-9zzz", id="beads-custom-prefix"
        ),
        pytest.param("beads", "bd", "feature/bd-12", None, id="beads-below-min-length"),
        pytest.param("beads", "bd", "feature/other-abcd", None, id="beads-other-prefix"),
        pytest.param(
            "beads",
            "flow",
            "feature/flow-kx17.2-revision-lifecycle-seam",
            "flow-kx17.2",
            id="beads-dotted-child",
        ),
        # longest match wins: flow-kx17.2 is a distinct bead from its parent epic
        # flow-kx17; stopping at the pre-dot word boundary silently operates on the
        # wrong bead (recover / recall / revise all consume this raw)
        pytest.param(
            "beads",
            "flow",
            "feat/flow-kx17.2",
            "flow-kx17.2",
            id="beads-dotted-child-never-parent",
        ),
        pytest.param(
            "beads", "flow", "feat/flow-kx17-revision-lifecycle", "flow-kx17", id="beads-parent-key"
        ),
        pytest.param(
            "beads", "flow", "feat/flow-820-fix-the-thing", "flow-820", id="beads-three-char-stem"
        ),
        pytest.param(
            "beads", "bd", "feat/bd-abc1.2.3-x", "bd-abc1.2.3", id="beads-multi-level-dotted"
        ),
        # only `.N` child suffixes extend the key; a stray non-numeric dot segment
        # is not part of any bead key
        pytest.param(
            "beads", "flow", "feat/flow-kx17.next", "flow-kx17", id="beads-non-numeric-dot"
        ),
    ],
)
def test_resolve_key_shapes(tmp_path: Path, backend, key, branch, expected) -> None:
    if backend == "jira":
        _jira_workspace(tmp_path, project_key=key)
    else:
        _beads_workspace(tmp_path, prefix=key)
    runner = _fake_runner(branch)
    assert branch_ticket.resolve(tmp_path, tmp_path, runner) == expected


# ─── Explicit --branch (PR→ticket enabler) ───────────────────────────────────


def test_explicit_branch_resolves_without_git(tmp_path: Path) -> None:
    _jira_workspace(tmp_path)

    def _boom(args: list[str], cwd: Path):  # must not be called when branch is given
        raise AssertionError("git runner must not run when --branch is explicit")

    assert branch_ticket.resolve(tmp_path, tmp_path, _boom, branch="feature/FT-1-x") == "FT-1"


def test_explicit_branch_no_match_returns_none(tmp_path: Path) -> None:
    _jira_workspace(tmp_path)
    assert branch_ticket.resolve(tmp_path, tmp_path, branch="feature/no-key") is None


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
