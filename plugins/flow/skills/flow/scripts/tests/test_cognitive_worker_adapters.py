from __future__ import annotations

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
    assert writer[writer.index("--permission-mode") + 1] == "acceptEdits"


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
    assert cw.preflight_route(route, runner=writable, authority="capsule_writer")["harness"] == (
        "codex"
    )


def test_claude_capsule_writer_preflight_requires_accept_edits(monkeypatch) -> None:
    monkeypatch.setattr(cw.shutil, "which", lambda name: f"/usr/bin/{name}")
    route = {"harness": "claude_code", "model": "opus", "effort": "high"}
    read_only_evidence = _probe_runner("auth", _CLAUDE_FLAGS)
    with pytest.raises(cw.WorkerFailure, match="acceptEdits") as error:
        cw.preflight_route(route, runner=read_only_evidence, authority="capsule_writer")
    assert error.value.code == "capability_missing"
    assert (
        cw.preflight_route(route, runner=read_only_evidence, authority="read_only")["harness"]
        == "claude_code"
    )
    writable = _probe_runner("auth", _CLAUDE_FLAGS + " acceptEdits")
    assert (
        cw.preflight_route(route, runner=writable, authority="capsule_writer")["harness"]
        == "claude_code"
    )


def test_claude_disposable_writer_route_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(cw.shutil, "which", lambda name: f"/usr/bin/{name}")
    route = {"harness": "claude_code", "model": "opus", "effort": "high"}
    # Fully write-capable evidence still refuses: claude headless has no confined write-exec
    # sandbox, so the disposable (e2e) writer is blocked before any launch, not on a missing flag.
    evidence = _probe_runner("auth", _CLAUDE_FLAGS + " acceptEdits")
    with pytest.raises(cw.WorkerFailure, match="codex") as error:
        cw.preflight_route(route, runner=evidence, authority="disposable_writer")
    assert error.value.code == "unsupported_writer_harness"
    # The guard is claude-specific: a codex disposable writer clears preflight.
    codex_route = {"harness": "codex", "model": "gpt-5.6-sol", "effort": "high"}
    codex_evidence = _probe_runner("login", _CODEX_FLAGS + " workspace-write")
    assert (
        cw.preflight_route(codex_route, runner=codex_evidence, authority="disposable_writer")[
            "harness"
        ]
        == "codex"
    )
