from __future__ import annotations

import hashlib
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


def _writer_proof(
    tmp_path: Path,
    *,
    import_result: str = "applied",
    touched: list[str] | None = None,
    allowed: list[str] | None = None,
    stage: bool = True,
    diff_digest_override: str | None = None,
) -> dict:
    """Synthesize a full capsule_writer (importing) proof: staged source, outcome, and manifest.

    Mirrors a real proof-4 run: an implementer capsule imported its validated patch into a
    Flow-owned worktree, so the authoritative index carries the staged change and the durable
    outcome carries an ``applied`` change receipt bound to that staged diff. The knobs let a
    negative fixture diverge in exactly one dimension the writer authority check inspects.
    """
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "flow@example.test")
    _git(source, "config", "user.name", "Flow Test")
    (source / "app.py").write_text("print('base')\n", encoding="utf-8")
    _git(source, "add", "app.py")
    _git(source, "commit", "-qm", "base")
    source_sha = _git(source, "rev-parse", "HEAD")

    before = cognitive_workers.git_receipt(source)
    if stage:
        (source / "app.py").write_text("print('imported')\n", encoding="utf-8")
        _git(source, "add", "app.py")
    staged = cognitive_workers._git_bytes(
        source,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        "-M",
        "--cached",
        source_sha,
    )
    diff_digest = diff_digest_override or hashlib.sha256(staged).hexdigest()

    allowed = ["app.py"] if allowed is None else allowed
    touched = (["app.py"] if stage else []) if touched is None else touched
    change_body = {
        "schema": cognitive_workers.CHANGE_RECEIPT_SCHEMA,
        "baseline_digest": source_sha,
        "patch": {"path": str(source / "patch.bin"), "sha256": "0" * 64, "length": len(staged)},
        "allowed_paths": list(allowed),
        "touched_paths": list(touched),
        "metadata": {},
        "import_target": {"head_before": source_sha, "head_after": source_sha},
        "import_result": import_result,
        "authoritative_diff_digest": diff_digest,
    }
    change_receipt = {**change_body, "digest": cognitive_workers._digest(change_body)}

    route = {"harness": "claude_code", "model": "opus", "effort": "high"}
    route_digest = hashlib.sha256(
        json.dumps(route, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps({"challenge": "writer"}), encoding="utf-8")
    challenge_digest = "c" * 64
    order = cognitive_workers.WorkOrder(
        logical_invocation_id="smoke:writer:1",
        generation=1,
        profile="implementer",
        source_root=str(source),
        source_sha=source_sha,
        route=route,
        route_snapshot_digest=route_digest,
        input_bundle=str(input_path),
        input_digest=hashlib.sha256(input_path.read_bytes()).hexdigest(),
        facts={"base_sha": source_sha},
        authority="capsule_writer",
        allowed_mutation_paths=tuple(allowed),
        run_id="run-1",
        stage="implement",
        lease_fence="fence-1",
        challenge_digest=challenge_digest,
    )
    artifact_root = tmp_path / "artifacts"
    capsule_root = tmp_path / "capsules"
    capsule_root.mkdir()
    invocation = (
        artifact_root
        / "invocations"
        / hashlib.sha256(order.logical_invocation_id.encode()).hexdigest()
    )
    invocation.mkdir(parents=True)
    outcome = cognitive_workers.WorkOutcome(
        logical_invocation_id=order.logical_invocation_id,
        generation=order.generation,
        profile=order.profile,
        status="succeeded",
        result={"report": "done"},
        receipts={
            "route": {"activation": "active", "effective": order.route},
            "process": {
                "child_reaped": True,
                "process_group_absent": True,
                "stdout_eof": True,
                "stderr_eof": True,
            },
            "disposal": {"absent": True, "quarantined": False},
            "change": change_receipt,
        },
        run_id=order.run_id,
        stage=order.stage,
        stage_generation=order.stage_generation,
        route_snapshot_digest=order.route_snapshot_digest,
        source_sha=order.source_sha,
        lease_fence=order.lease_fence,
        input_bundle=order.input_bundle,
        input_digest=order.input_digest,
    )
    outcome_mapping = outcome.to_mapping()
    (invocation / "outcome.json").write_text(json.dumps(outcome_mapping), encoding="utf-8")
    order_path = tmp_path / "work-order.json"
    order_path.write_text(json.dumps(order.to_mapping()), encoding="utf-8")

    facade_command = "FLOW_HARNESS=codex /abs/flow cognitive-worker run --work-order /abs/o.json"
    transcript = _codex_transcript(
        tmp_path / "codex.jsonl",
        facade_command,
        json.dumps({"digest": outcome_mapping["digest"], "status": "succeeded"}),
    )
    stderr_path = tmp_path / "codex.stderr"
    stderr_path.write_text("", encoding="utf-8")
    outer_path = tmp_path / "outer.json"
    outer_path.write_text(
        json.dumps(
            {
                "executable": "/usr/bin/codex",
                "version": "1.2.3",
                "exit_code": 0,
                "facade_command": facade_command,
                "stdout_path": str(transcript),
                "stderr_path": str(stderr_path),
            }
        ),
        encoding="utf-8",
    )
    body = {
        "schema": smoke.SCHEMA,
        "direction": "codex-parent",
        "parent_harness": "codex",
        "worker_harness": "claude_code",
        "challenge_digest": challenge_digest,
        "work_order": str(order_path),
        "artifact_root": str(artifact_root),
        "capsule_root": str(capsule_root),
        "facade_command": facade_command,
        "source_receipt_before": before,
        "outer_evidence": str(outer_path),
        "source_root": str(source),
    }
    return {**body, "digest": cognitive_workers._digest(body)}


def test_writer_proof_with_matching_import_verifies(tmp_path: Path) -> None:
    """A capsule_writer whose staged worktree equals its applied change receipt verifies true."""
    manifest = _writer_proof(tmp_path)
    result = smoke.verify_manifest(manifest)
    assert result["verified"] is True, result["errors"]
    # The leased-writer replay attests the durable outcome without a false owner-mismatch.
    assert not any("owner proof does not match" in item for item in result["errors"])
    assert not any("logical replay" in item for item in result["errors"])


def test_writer_proof_import_result_not_applied_fails(tmp_path: Path) -> None:
    manifest = _writer_proof(tmp_path, import_result="resumed")
    result = smoke.verify_manifest(manifest)
    assert result["verified"] is False
    assert "capsule_writer change receipt is not an applied import" in result["errors"]


def test_writer_proof_touched_outside_allowed_fails(tmp_path: Path) -> None:
    manifest = _writer_proof(tmp_path, touched=["app.py", "escape.py"], allowed=["app.py"])
    result = smoke.verify_manifest(manifest)
    assert result["verified"] is False
    assert "capsule_writer imported outside its allowed mutation paths" in result["errors"]


def test_writer_proof_authoritative_state_diverging_from_receipt_fails(tmp_path: Path) -> None:
    """The staged worktree must be byte-identical to the receipt's authoritative_diff_digest."""
    manifest = _writer_proof(tmp_path, diff_digest_override="0" * 64)
    result = smoke.verify_manifest(manifest)
    assert result["verified"] is False
    assert "authoritative worktree does not match the imported change receipt" in result["errors"]


def test_writer_proof_that_imported_nothing_fails(tmp_path: Path) -> None:
    """An importer that left the authoritative worktree unchanged is not a valid proof."""
    manifest = _writer_proof(tmp_path, stage=False)
    result = smoke.verify_manifest(manifest)
    assert result["verified"] is False
    assert "capsule_writer left the authoritative worktree unchanged" in result["errors"]


def test_read_only_proof_that_changed_authoritative_still_fails(tmp_path: Path) -> None:
    """Unchanged behavior: a read_only run that mutated authoritative source still fails."""
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
    prepared = smoke.prepare(
        direction="codex-parent",
        root=tmp_path / "smoke",
        source_root=source,
        facade=facade,
        route={"harness": "claude_code", "model": "opus", "effort": "high"},
    )
    manifest = json.loads(Path(prepared["manifest_path"]).read_text(encoding="utf-8"))
    (source / "b").write_text("b", encoding="utf-8")
    _git(source, "add", "b")
    result = smoke.verify_manifest(manifest)
    assert result["verified"] is False
    assert "authoritative repository changed during the smoke" in result["errors"]
