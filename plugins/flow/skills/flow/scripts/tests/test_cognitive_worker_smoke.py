from __future__ import annotations

import json
import subprocess
from pathlib import Path

import cognitive_worker_smoke as smoke
import cognitive_workers


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_prepare_binds_fresh_nonce_absolute_facade_and_opposite_worker(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "flow@example.test")
    _git(source, "config", "user.name", "Flow Test")
    (source / "a").write_text("a", encoding="utf-8")
    _git(source, "add", "a")
    _git(source, "commit", "-qm", "a")
    facade = tmp_path / "flow"
    facade.write_text("#!/bin/sh\n", encoding="utf-8")

    manifest = smoke.prepare(
        direction="codex-parent",
        root=tmp_path / "smoke",
        source_root=source,
        facade=facade,
        route={"harness": "claude_code", "model": "opus", "effort": "high"},
    )
    assert manifest["parent_harness"] == "codex"
    assert manifest["worker_harness"] == "claude_code"
    assert manifest["facade_command"].startswith("FLOW_HARNESS=codex ")
    assert str(facade.resolve()) in manifest["facade_command"]
    order = json.loads(Path(manifest["work_order"]).read_text(encoding="utf-8"))
    assert order["challenge_digest"] == manifest["challenge_digest"]
    assert Path(manifest["nonce_path"]).stat().st_mode & 0o077 == 0


def test_verify_refuses_environment_only_parent_claim(tmp_path: Path) -> None:
    manifest = {
        "schema": "flow.cognitive-worker-smoke/v1",
        "direction": "claude-parent",
        "parent_harness": "claude_code",
        "worker_harness": "codex",
        "outer_evidence": str(tmp_path / "outer.json"),
    }
    (tmp_path / "outer.json").write_text(
        json.dumps({"executable": "python", "exit_code": 0}), encoding="utf-8"
    )
    result = smoke.verify_manifest(manifest)
    assert result["verified"] is False
    assert any("real parent executable" in item for item in result["errors"])


def test_verify_rechecks_the_manifests_own_digest(tmp_path: Path) -> None:
    body = {
        "schema": smoke.SCHEMA,
        "direction": "claude-parent",
        "parent_harness": "claude_code",
        "worker_harness": "codex",
        "outer_evidence": str(tmp_path / "outer.json"),
    }
    honest = {**body, "digest": cognitive_workers._digest(body)}
    assert "smoke manifest digest is invalid" not in smoke.verify_manifest(honest)["errors"]

    tampered = {**honest, "parent_harness": "codex"}
    result = smoke.verify_manifest(tampered)
    assert result["verified"] is False
    assert "smoke manifest digest is invalid" in result["errors"]


def _codex_transcript(path: Path, command: str, output: str) -> Path:
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {"item": {"type": "command_execution", "command": f"/bin/zsh -lc '{command}'"}}
                ),
                json.dumps(
                    {
                        "item": {
                            "type": "command_execution",
                            "command": f"/bin/zsh -lc '{command}'",
                            "exit_code": 0,
                            "aggregated_output": output,
                        }
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_codex_transcript_binds_one_terminal_facade_invocation(tmp_path: Path) -> None:
    command = "FLOW_HARNESS=codex /abs/flow cognitive-worker run --work-order /abs/o.json"
    digest = "a" * 64
    transcript = _codex_transcript(
        tmp_path / "codex.jsonl", command, json.dumps({"digest": digest, "status": "succeeded"})
    )

    invocations = smoke.transcript_invocations(transcript, "codex")
    assert len(invocations) == 1
    assert invocations[0]["exit_code"] == 0
    manifest = {"parent_harness": "codex", "facade_command": command}
    assert smoke._verify_real_parent(manifest, {"stdout_path": str(transcript)}, digest) == []


def test_a_parent_transcript_without_the_nested_outcome_digest_is_unjoined(tmp_path: Path) -> None:
    """The exact facade command alone leaves outer and inner evidence forgeable apart."""
    command = "FLOW_HARNESS=codex /abs/flow cognitive-worker run --work-order /abs/o.json"
    transcript = _codex_transcript(
        tmp_path / "codex.jsonl", command, json.dumps({"digest": "b" * 64})
    )

    manifest = {"parent_harness": "codex", "facade_command": command}
    errors = smoke._verify_real_parent(manifest, {"stdout_path": str(transcript)}, "a" * 64)
    assert not any("never executed the exact absolute facade command" in item for item in errors)
    assert any("never carried the nested outcome digest" in item for item in errors)


def test_claude_transcript_binds_its_single_bash_invocation(tmp_path: Path) -> None:
    command = "FLOW_HARNESS=claude-code /abs/flow cognitive-worker run --work-order /abs/o.json"
    digest = "c" * 64
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "t1",
                                    "name": "Bash",
                                    "input": {"command": command},
                                }
                            ]
                        }
                    }
                ),
                json.dumps(
                    {
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "t1",
                                    "content": json.dumps({"digest": digest}),
                                }
                            ]
                        }
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    manifest = {"parent_harness": "claude_code", "facade_command": command}
    assert smoke._verify_real_parent(manifest, {"stdout_path": str(transcript)}, digest) == []


def test_a_parent_that_never_ran_the_facade_cannot_be_attested(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "item": {
                    "type": "command_execution",
                    "command": "echo not-the-facade",
                    "exit_code": 0,
                }
            }
        ),
        encoding="utf-8",
    )

    manifest = {"parent_harness": "codex", "facade_command": "/abs/flow cognitive-worker run"}
    errors = smoke._verify_real_parent(manifest, {"stdout_path": str(transcript)}, "d" * 64)
    assert any("never executed the exact absolute facade command" in item for item in errors)
    assert any("outside its single allowed invocation" in item for item in errors)


def test_a_shell_line_that_only_echoes_the_facade_command_is_not_attributed(tmp_path: Path) -> None:
    """Printing the facade text and reading an outcome.json off disk is not running the facade."""
    command = "FLOW_HARNESS=codex /abs/flow cognitive-worker run --work-order /abs/o.json"
    digest = "e" * 64
    transcript = tmp_path / "codex.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "item": {
                    "type": "command_execution",
                    "command": (
                        f"/bin/zsh -lc \"echo '{command}' ; "
                        'cat /abs/artifacts/invocations/deadbeef/outcome.json"'
                    ),
                    "exit_code": 0,
                    "aggregated_output": json.dumps({"digest": digest, "status": "succeeded"}),
                }
            }
        ),
        encoding="utf-8",
    )

    manifest = {"parent_harness": "codex", "facade_command": command}
    errors = smoke._verify_real_parent(manifest, {"stdout_path": str(transcript)}, digest)
    assert any("never executed the exact absolute facade command" in item for item in errors)


def test_a_login_shell_wrapper_around_the_exact_facade_command_is_attributed(
    tmp_path: Path,
) -> None:
    """Codex records the approved command inside its own /bin/zsh -lc, which stays attributable."""
    command = "FLOW_HARNESS=codex /abs/flow cognitive-worker run --work-order /abs/o.json"
    assert smoke._facade_argv(f"/bin/zsh -lc '{command}'") == smoke._facade_argv(command)


def test_worker_environment_carries_the_sandbox_egress_proxy(monkeypatch) -> None:
    """A sandboxed owner reaches the network only through its injected proxy."""
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pass@localhost:60521")
    monkeypatch.setenv("https_proxy", "http://user:pass@localhost:60521")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")

    environment = cognitive_workers.worker_environment()

    assert environment["HTTPS_PROXY"] == "http://user:pass@localhost:60521"
    assert environment["https_proxy"] == "http://user:pass@localhost:60521"
    assert environment["NO_PROXY"] == "localhost,127.0.0.1"
    assert "AWS_SECRET_ACCESS_KEY" not in environment
