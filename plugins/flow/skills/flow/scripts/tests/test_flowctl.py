"""Tests for Flow's allowlisted script facade."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import flowctl


def test_hot_path_command_resolution() -> None:
    expected = {
        "dispatch": "dispatch_stage.py",
        "validate": "validate_workspace.py",
        "diff": "diff_extract.py",
        "worktree": "flow_worktree.py",
        "tracker": "tracker_cli.py",
        "forge": "forge_cli.py",
        "frontmatter": "ticket_frontmatter.py",
        "friction": "flow_friction.py",
        "model": "model_resolve.py",
        "agent-route": "agent_routes.py",
        "plan-review": "plan_review.py",
        "planner-worker": "planner_worker.py",
        "planning-attempt": "planning_attempt.py",
        "pending-mutations": "pending_mutations.py",
        "handler": "resolve_handler.py",
        "merge": "stage_merge.py",
        "commands": "public_commands_cli.py",
        "lifecycle": "lifecycle_cli.py",
        "cockpit": "cockpit_cli.py",
        "cognitive-worker": "cognitive_workers.py",
        "maintainer-preflight": "maintainer_preflight.py",
        "worker-pool": "worker_pool.py",
        "maintainer-senses": "senses_deadman.py",
    }
    assert {name: flowctl.COMMANDS[name] for name in expected} == expected


def test_default_commands_use_kebab_case() -> None:
    assert flowctl.COMMANDS["evolve-drain"] == "evolve_drain.py"
    assert flowctl.COMMANDS["memory-append"] == "memory_append.py"
    assert flowctl.COMMANDS["recall-usage"] == "recall_usage.py"
    assert "init" not in flowctl.COMMANDS
    assert "evolve-session-cleanup" not in flowctl.COMMANDS


def test_unknown_command_refused_with_exit_2(tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        flowctl.cli_main(["--workspace-root", str(tmp_path), "../../arbitrary.py"])
    assert exc.value.code == 2
    assert "unknown command" in capsys.readouterr().err


def test_relative_workspace_refused_with_exit_2(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        flowctl.cli_main(["--workspace-root", ".", "status"])
    assert exc.value.code == 2
    assert "absolute path" in capsys.readouterr().err


def test_unknown_harness_refused_before_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "codeks")
    with pytest.raises(SystemExit) as exc:
        flowctl.cli_main(["--workspace-root", str(tmp_path.resolve()), "status"])
    assert exc.value.code == 2
    assert "FLOW_HARNESS" in capsys.readouterr().err


def test_sets_cwd_environment_and_forwards_arguments(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "target.py"
    script.write_text("", encoding="utf-8")
    observed: dict[str, object] = {}
    original_cwd = Path.cwd()

    def fake_execv(executable: str, argv: list[str]) -> None:
        observed.update(
            executable=executable,
            argv=argv,
            cwd=Path.cwd(),
            flow_skill=os.environ.get("FLOW_SKILL_DIR"),
            claude_skill=os.environ.get("CLAUDE_SKILL_DIR"),
        )
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(flowctl, "resolve_command", lambda command: script)
    monkeypatch.setattr(flowctl.os, "execv", fake_execv)
    monkeypatch.setenv("FLOW_SKILL_DIR", "/stale/flow")
    monkeypatch.setenv("CLAUDE_SKILL_DIR", "/stale/claude")
    try:
        with pytest.raises(RuntimeError, match="exec intercepted"):
            flowctl.cli_main(
                ["--workspace-root", str(tmp_path.resolve()), "status", "--ticket", "FT-1"]
            )
    finally:
        os.chdir(original_cwd)
    assert observed == {
        "executable": sys.executable,
        "argv": [sys.executable, str(script), "--ticket", "FT-1"],
        "cwd": tmp_path.resolve(),
        "flow_skill": str(flowctl.SKILL_ROOT),
        "claude_skill": str(flowctl.SKILL_ROOT),
    }
