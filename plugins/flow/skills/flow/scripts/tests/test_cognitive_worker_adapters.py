from __future__ import annotations

from pathlib import Path

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

    for command in (
        cw.ClaudeCodeCliAdapter().command(route, "prompt", schema, tmp_path),
        cw.build_planner_command(route, "prompt", schema_path=schema),
    ):
        assert command[command.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in command
