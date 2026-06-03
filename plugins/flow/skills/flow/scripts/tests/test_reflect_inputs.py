"""Tests for reflect_inputs.py — reflect-stage input bundler."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import flow_friction
import reflect_inputs
import state
import ticket_frontmatter


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    _git(["init", "--initial-branch=main"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "test"], tmp_path)
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "README.md"], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    return tmp_path


def _seed_state(ticket_dir: Path, head_sha: str, stages: list[str] | None = None) -> None:
    state.init(ticket_dir, "FT-1", "jira", stages or ["ticket", "plan", "implement"])
    state.begin_stage(ticket_dir, "ticket", head_sha)


# ─── bundle() ────────────────────────────────────────────────────────────────


def test_bundle_includes_state_and_ticket_and_run_id(tmp_repo: Path, tmp_path: Path) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["ticket"] == "FT-1"
    assert "run_id" in payload
    assert "state" in payload
    assert payload["state"]["ticket"] == "FT-1"


def test_bundle_reads_frontmatter_when_provided(tmp_repo: Path, tmp_path: Path) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    fm_path = tmp_path / "FT-1.md"
    ticket_frontmatter.update(fm_path, {"ticket": "FT-1", "status": "in_progress"})
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo, ticket_frontmatter_path=fm_path)
    assert payload["ticket_frontmatter"]["status"] == "in_progress"


def test_bundle_omits_frontmatter_when_not_provided(tmp_repo: Path, tmp_path: Path) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["ticket_frontmatter"] == {}


def test_bundle_final_diff_via_diff_extract(tmp_repo: Path, tmp_path: Path) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    (tmp_repo / "a.py").write_text("hi\n", encoding="utf-8")
    _git(["add", "a.py"], tmp_repo)
    _git(["commit", "-m", "add"], tmp_repo)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["final_diff"] is not None
    assert payload["final_diff"]["files_touched"] == ["a.py"]


def test_bundle_diff_null_when_ticket_stage_never_started(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_path / "runs" / "FT-1"
    state.init(ticket_dir, "FT-1", "jira", ["ticket", "plan"])
    # No begin_stage call -> no started_at_sha.
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["final_diff"] is None


def test_bundle_includes_subagent_reports_when_output_path_set(
    tmp_repo: Path, tmp_path: Path
) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    # Simulate the implement stage having an output_path recorded.
    report_path = ticket_dir / "stages" / "implement.out"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("subagent report body\n", encoding="utf-8")
    state.finish_stage(
        ticket_dir,
        "ticket",
        "completed",
        head,
        output_path=str(report_path),
    )
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    reports = payload["subagent_reports"]
    assert any(r["body"] == "subagent report body\n" for r in reports)


def test_bundle_missing_report_file_gives_null_body_no_warning(
    tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An inline stage may record an output_path without ever writing the file;
    # an absent report is normal -> null body, and NOT a warning.
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    missing = ticket_dir / "stages" / "implement.out"
    state.finish_stage(ticket_dir, "ticket", "completed", head, output_path=str(missing))
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    reports = payload["subagent_reports"]
    assert any(r["body"] is None for r in reports)
    captured = capsys.readouterr()
    assert "unreadable" not in captured.err


def test_bundle_real_read_error_still_warns(
    tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A genuine read failure (here: output_path points at a directory) is not a
    # normal absent report and must still warn.
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    as_dir = ticket_dir / "stages" / "implement.out"
    as_dir.mkdir(parents=True, exist_ok=True)
    state.finish_stage(ticket_dir, "ticket", "completed", head, output_path=str(as_dir))
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert any(r["body"] is None for r in payload["subagent_reports"])
    captured = capsys.readouterr()
    assert "unreadable" in captured.err


def test_bundle_skips_stages_with_no_output_path(tmp_repo: Path, tmp_path: Path) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    # state had 3 stages, none with output_path -> reports empty.
    assert payload["subagent_reports"] == []


def test_bundle_missing_state_raises(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_path / "runs" / "missing"
    with pytest.raises(FileNotFoundError):
        reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_emits_json(tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    rc = reflect_inputs.cli_main(
        [
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
    assert payload["ticket"] == "FT-1"


def test_cli_missing_state_returns_1(
    tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = reflect_inputs.cli_main(
        [
            "--ticket",
            "FT-1",
            "--ticket-dir",
            str(tmp_path / "no-such"),
            "--cwd",
            str(tmp_repo),
        ]
    )
    assert rc == 1
    assert "state.json" in capsys.readouterr().err


def test_cli_includes_frontmatter_when_flagged(
    tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    fm_path = tmp_path / "FT-1.md"
    ticket_frontmatter.update(fm_path, {"ticket": "FT-1", "status": "x"})
    rc = reflect_inputs.cli_main(
        [
            "--ticket",
            "FT-1",
            "--ticket-dir",
            str(ticket_dir),
            "--cwd",
            str(tmp_repo),
            "--ticket-frontmatter",
            str(fm_path),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ticket_frontmatter"]["status"] == "x"


def test_bundle_includes_friction_for_this_run_only(tmp_repo: Path, tmp_path: Path) -> None:
    (tmp_repo / ".flow").mkdir(exist_ok=True)
    (tmp_repo / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "jira"\n[memory]\nnamespace = "demo"\n', encoding="utf-8"
    )
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    run_id = state.read(ticket_dir)[0].run_id
    flow_friction.append(
        tmp_repo, "FT-1", run_id, "implement", "RECONCILE", "expanded planned_files"
    )
    flow_friction.append(tmp_repo, "FT-1", "other-run", "ticket", "RETRY", "noise from another run")
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    types = [f["type"] for f in payload["friction"]]
    assert "RECONCILE" in types
    assert "RETRY" not in types  # different run_id is excluded


# ─── reflect_config ──────────────────────────────────────────────────────────


def _write_workspace(tmp_repo: Path, reflect_block: str = "") -> None:
    (tmp_repo / ".flow").mkdir(exist_ok=True)
    (tmp_repo / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "jira"\n[memory]\nnamespace = "demo"\n' + reflect_block,
        encoding="utf-8",
    )


def test_reflect_config_defaults_when_no_block(tmp_repo: Path, tmp_path: Path) -> None:
    _write_workspace(tmp_repo)
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["reflect_config"] == {"machinery": False, "claude_memory": True}


def test_reflect_config_defaults_when_no_workspace_toml(tmp_repo: Path, tmp_path: Path) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["reflect_config"] == {"machinery": False, "claude_memory": True}


def test_reflect_config_block_overrides(tmp_repo: Path, tmp_path: Path) -> None:
    _write_workspace(tmp_repo, "[reflect]\nmachinery = true\nclaude_memory = false\n")
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["reflect_config"] == {"machinery": True, "claude_memory": False}


def test_reflect_config_partial_block_keeps_other_default(tmp_repo: Path, tmp_path: Path) -> None:
    # only machinery set -> claude_memory stays at its default true.
    _write_workspace(tmp_repo, "[reflect]\nmachinery = true\n")
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["reflect_config"] == {"machinery": True, "claude_memory": True}


def test_reflect_config_non_bool_value_ignored(tmp_repo: Path, tmp_path: Path) -> None:
    # a non-bool override is ignored, the default holds (guards against TOML typos).
    _write_workspace(tmp_repo, '[reflect]\nmachinery = "yes"\n')
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["reflect_config"]["machinery"] is False
