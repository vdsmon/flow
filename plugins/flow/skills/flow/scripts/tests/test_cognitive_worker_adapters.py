from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import cognitive_workers as cw


def test_codex_adapter_command_proves_exact_read_only_route(tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    command = cw.CodexCliAdapter().command(
        {"harness": "codex", "model": "gpt-5.6-sol", "effort": "xhigh"},
        "prompt",
        schema,
        tmp_path,
    )
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("--model") + 1] == "gpt-5.6-sol"
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert 'model_reasoning_effort="xhigh"' in command
    assert "--output-schema" in command
    assert command[-1] == "-"


_HUGE_PROMPT = "P" * (3 * 1024 * 1024)
_ARG_MAX_BUDGET_BYTES = 100_000


def test_codex_adapter_command_pipes_a_huge_prompt_off_argv_arg_max(tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    command = cw.CodexCliAdapter().command(
        {"harness": "codex", "model": "gpt-5.6-sol", "effort": "xhigh"},
        _HUGE_PROMPT,
        schema,
        tmp_path,
    )
    assert command[-1] == "-"
    assert _HUGE_PROMPT not in command
    assert sum(len(token) for token in command) < _ARG_MAX_BUDGET_BYTES


def test_claude_adapter_command_pipes_a_huge_prompt_off_argv_arg_max(tmp_path: Path) -> None:
    """--json-schema inlines the schema text, the one argv token stdin delivery left unbounded.

    A stub schema would make the budget assertion pass trivially, so this uses a real
    production schema with nested objects.
    """
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(cw.provider_schema("reflector")), encoding="utf-8")
    command = cw.ClaudeCodeCliAdapter().command(
        {"harness": "claude_code", "model": "opus", "effort": "high"},
        _HUGE_PROMPT,
        schema,
        tmp_path,
    )
    assert _HUGE_PROMPT not in command
    assert command[-1] != _HUGE_PROMPT
    assert sum(len(token) for token in command) < _ARG_MAX_BUDGET_BYTES


def test_claude_adapter_command_proves_exact_plan_permissions(tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    command = cw.ClaudeCodeCliAdapter().command(
        {"harness": "claude_code", "model": "opus", "effort": "high"},
        "prompt",
        schema,
        tmp_path,
    )
    assert command[0] == "claude"
    assert command[command.index("--model") + 1] == "opus"
    assert command[command.index("--effort") + 1] == "high"
    assert command[command.index("--permission-mode") + 1] == "plan"
    assert command[command.index("--json-schema") + 1] == '{"type":"object"}'


def test_worker_environment_is_minimized(monkeypatch) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "do-not-pass")
    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("HOME", "/home/test")
    environment = cw.worker_environment({"FLOW_WORKER_CHALLENGE": "abc"})
    assert environment["PATH"] == "/bin"
    assert environment["HOME"] == "/home/test"
    assert environment["FLOW_WORKER_CHALLENGE"] == "abc"
    assert "SECRET_TOKEN" not in environment


def test_claude_worker_command_is_accepted_by_the_real_cli_contract(tmp_path: Path) -> None:
    """`claude --print --output-format stream-json` exits 1 without --verbose."""
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    route = {"harness": "claude_code", "model": "opus", "effort": "high"}

    command = cw.ClaudeCodeCliAdapter().command(route, "prompt", schema, tmp_path)
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in command


def test_codex_adapter_command_branches_the_sandbox_on_authority(tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    route = {"harness": "codex", "model": "gpt-5.6-sol", "effort": "high"}
    reader = cw.CodexCliAdapter().command(route, "prompt", schema, tmp_path, "read_only")
    writer = cw.CodexCliAdapter().command(route, "prompt", schema, tmp_path, "capsule_writer")
    assert reader[reader.index("--sandbox") + 1] == "read-only"
    assert writer[writer.index("--sandbox") + 1] == "workspace-write"


def test_claude_adapter_command_branches_the_permission_mode_on_authority(tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    route = {"harness": "claude_code", "model": "opus", "effort": "high"}
    reader = cw.ClaudeCodeCliAdapter().command(route, "prompt", schema, tmp_path, "read_only")
    writer = cw.ClaudeCodeCliAdapter().command(route, "prompt", schema, tmp_path, "capsule_writer")
    assert reader[reader.index("--permission-mode") + 1] == "plan"
    assert writer[writer.index("--permission-mode") + 1] == "auto"


def _probe_runner(auth_verb: str, help_text: str):
    def run(command, **kwargs):
        tail = command[1:]
        if tail == ["--version"]:
            return subprocess.CompletedProcess(command, 0, f"{command[0]} 1.0", "")
        if tail == [auth_verb, "status"]:
            return subprocess.CompletedProcess(command, 0, "ok", "")
        return subprocess.CompletedProcess(command, 0, help_text, "")

    return run


_CODEX_FLAGS = "--model --sandbox --output-schema --json"
_CLAUDE_FLAGS = "--model --effort --permission-mode --json-schema --verbose"


def test_codex_writer_preflight_requires_the_writable_sandbox(monkeypatch) -> None:
    monkeypatch.setattr(cw.shutil, "which", lambda name: f"/usr/bin/{name}")
    route = {"harness": "codex", "model": "gpt-5.6-sol", "effort": "high"}
    read_only_evidence = _probe_runner("login", _CODEX_FLAGS)
    with pytest.raises(cw.WorkerFailure, match="workspace-write") as error:
        cw.preflight_route(route, runner=read_only_evidence, authority="capsule_writer")
    assert error.value.code == "capability_missing"
    # The same read-only evidence still clears a read_only route unchanged.
    assert cw.preflight_route(route, runner=read_only_evidence, authority="read_only")[
        "harness"
    ] == ("codex")
    writable = _probe_runner("login", _CODEX_FLAGS + " workspace-write")
    for authority in ("capsule_writer", "disposable_writer"):
        assert cw.preflight_route(route, runner=writable, authority=authority)["harness"] == "codex"


def test_claude_writer_preflight_clears_on_base_flags(monkeypatch) -> None:
    monkeypatch.setattr(cw.shutil, "which", lambda name: f"/usr/bin/{name}")
    route = {"harness": "claude_code", "model": "opus", "effort": "high"}
    # Claude has no writable-specific probe token: its auto writer mode is a value of
    # --permission-mode, already in the base flags. So read-only, capsule, and disposable writer
    # routes all clear on the same base-flag evidence; nothing routes a claude writer away.
    evidence = _probe_runner("auth", _CLAUDE_FLAGS)
    for authority in ("read_only", "capsule_writer", "disposable_writer"):
        assert (
            cw.preflight_route(route, runner=evidence, authority=authority)["harness"]
            == "claude_code"
        )
