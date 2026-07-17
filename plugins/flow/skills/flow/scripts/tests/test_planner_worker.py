from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import agent_routes
import cognitive_workers as cw
import planner_worker as pw
import planning_attempt as pa


def _route(harness: str = "codex") -> pw.PlannerRoute:
    return pw.PlannerRoute(
        harness=harness,
        model="gpt-5.6-sol" if harness == "codex" else "opus",
        effort="xhigh" if harness == "codex" else "high",
    )


def _envelope(*, author_id: str = "codex:gpt-5.6-sol") -> dict[str, object]:
    return {
        "attempt_id": "attempt-1",
        "version": 1,
        "parent_digest": None,
        "base_sha": "a" * 40,
        "route_digest": "b" * 64,
        "author": {
            "id": author_id,
            "harness": "codex",
            "model": "gpt-5.6-sol",
        },
        "status": "PLAN_READY",
        "plan": {
            "motivation": "Make planning provenance explicit.",
            "goal": "Return one exact typed plan.",
            "scenarios": [{"before": "Implicit", "after": "Explicit"}],
            "architecture": ["owner", "planner"],
            "decisions": ["Use a read-only worker"],
            "acceptance_outcomes": ["The result validates at the adapter seam"],
            "steps": ["Launch", "Validate"],
            "files": ["planning.py"],
            "context_paths": [],
            "verification": ["Run worker tests"],
            "e2e_recipe": "Execute a fake CLI process.",
            "lane": "full",
            "compatibility": [],
            "rollout": "Activate through an explicit route.",
            "risks": ["Provider output drift"],
        },
        "questions": [],
        "incorporated_feedback_ids": [],
    }


class _FakeAdapter:
    """Stand in for the exact CLI while keeping the real process lifecycle."""

    harness = "codex"

    def __init__(self, payload: object, *, returncode: int = 0) -> None:
        self.payload = payload
        self.returncode = returncode
        self.prompts: list[str] = []
        self.thread_ids: list[str | None] = []

    def preflight(self, route, authority="read_only"):
        return {"executable": "/usr/bin/codex", "version": "codex 1", "harness": "codex"}

    def command(self, route, prompt, schema_path, capsule, authority="read_only"):
        raise AssertionError("the planner compatibility order must use the session command")

    def session_command(self, route, prompt, schema_path, *, thread_id, new_thread_id):
        self.prompts.append(prompt)
        self.thread_ids.append(thread_id)
        event = {"thread_id": thread_id or new_thread_id, "result": self.payload}
        script = (
            f"import json,sys; sys.stdout.write(json.dumps({event!r})); sys.exit({self.returncode})"
        )
        return [sys.executable, "-c", script]


class _FakeCapsuleAdapter:
    """Stand in for a read-only capsule CLI emitting a typed result plus a session id."""

    harness = "codex"

    def __init__(self, payload: object, *, session_id: str | None = "assessor-thread-1") -> None:
        self.payload = payload
        self.session_id = session_id
        self.prompts: list[str] = []

    def preflight(self, route, authority="read_only"):
        return {"executable": "/usr/bin/codex", "version": "codex 1", "harness": "codex"}

    def session_command(self, route, prompt, schema_path, *, thread_id, new_thread_id):
        raise AssertionError("a plan-assessor order must use the plain capsule command")

    def command(self, route, prompt, schema_path, capsule, authority="read_only"):
        self.prompts.append(prompt)
        event: dict[str, object] = {"result": self.payload}
        if self.session_id:
            event["session_id"] = self.session_id
        script = f"import json,sys; sys.stdout.write(json.dumps({event!r})); sys.exit(0)"
        return [sys.executable, "-c", script]


def _planner_receipt(snapshot_digest: str) -> dict[str, object]:
    desired = {"harness": "codex", "model": "gpt-5.6-sol", "effort": "xhigh"}
    body: dict[str, object] = {
        "schema": "flow.agent-route-receipt/v1",
        "snapshot_digest": snapshot_digest,
        "profile": "planner",
        "source": "built_in",
        "desired": desired,
        "effective": desired,
        "activation": "active",
        "reason": "test receipt",
        "launch_request": desired,
        "transport": "cli",
        "adapter_version": "test",
        "canonical_model": None,
        "worker_id": None,
        "prompt_hash": "c" * 64,
        "schema_hash": "d" * 64,
        "physical_attempt": {"pid": 17, "terminal_acknowledged": True},
        "cleanup": {"capsule_absent": True, "quarantined": False},
    }
    return {**body, "digest": agent_routes.canonical_digest(body)}


def _assessor_bundle(tmp_path: Path) -> tuple[dict[str, object], Path, dict[str, object]]:
    """Build a saved planning bundle whose route digest is a real snapshot digest."""
    snapshot = agent_routes.snapshot_config(
        b"", "codex", overrides=["plan_assessor=codex,gpt-5.6-sol,xhigh"]
    )
    digest = str(snapshot["digest"])
    attempt = pa.PlanningAttempt.create(
        attempt_id="attempt-1", base_sha="a" * 40, route_digest=digest, owner_identity="owner"
    )
    envelope_value = _envelope()
    envelope_value["route_digest"] = digest
    envelope = attempt.accept(envelope_value, launch_receipt=_planner_receipt(digest))
    attempt_dir = tmp_path / "attempt"
    attempt.save_bundle(attempt_dir)
    return snapshot, attempt_dir, envelope.to_mapping()


def _assessment(plan_digest: str) -> dict[str, object]:
    return {
        "verdict": "approve",
        "confidence": "high",
        "summary": "The candidate plan is coherent and complete.",
        "findings": [],
        "assessed_plan_digest": plan_digest,
    }


def _assessor_argv(
    tmp_path: Path,
    source: Path,
    attempt_dir: Path,
    route_digest: str,
    *extra: str,
    facts_path: Path | None = None,
) -> list[str]:
    facts = facts_path or tmp_path / "assessor-facts.json"
    if not facts.exists():
        facts.write_text(
            json.dumps({"ticket": {"key": "t-1"}, "assessment_rubric": "judge the plan"}),
            encoding="utf-8",
        )
    return [
        "--harness",
        "codex",
        "--model",
        "gpt-5.6-sol",
        "--effort",
        "xhigh",
        "--profile",
        "plan_assessor",
        "--attempt-dir",
        str(attempt_dir),
        "--facts-from",
        str(facts),
        "--route-digest",
        route_digest,
        "--source-root",
        str(source),
        "--invocation-root",
        str(tmp_path / "invocation"),
        *extra,
    ]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "flow@example.test")
    _git(root, "config", "user.name", "Flow Test")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "base")
    return root


def _argv(tmp_path: Path, source: Path, *extra: str) -> list[str]:
    prompt = tmp_path / "prompt.txt"
    if not prompt.exists():
        prompt.write_text("plan the ticket", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    return [
        "--harness",
        "codex",
        "--model",
        "gpt-5.6-sol",
        "--effort",
        "xhigh",
        "--prompt-from",
        str(prompt),
        "--schema",
        str(schema),
        "--attempt-id",
        "attempt-1",
        "--plan-version",
        "1",
        "--route-digest",
        "b" * 64,
        "--source-root",
        str(source),
        "--invocation-root",
        str(tmp_path / "invocation"),
        *extra,
    ]


def test_codex_command_is_exact_read_only_and_resumable(tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"
    command = pw.build_command(_route(), "prompt", schema_path=schema, thread_id="thread-7")
    assert command[:3] == ["codex", "exec", "resume"]
    assert command[3:5] == ["--model", "gpt-5.6-sol"]
    assert 'sandbox_mode="read-only"' in command
    assert 'model_reasoning_effort="xhigh"' in command
    assert "--json" in command
    assert command[-2:] == ["thread-7", "prompt"]


def test_claude_command_is_exact_read_only_and_resumable(tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    command = pw.build_command(
        _route("claude_code"), "prompt", schema_path=schema, thread_id="session-7"
    )
    assert command[0] == "claude"
    assert command[command.index("--model") + 1] == "opus"
    assert command[command.index("--effort") + 1] == "high"
    assert command[command.index("--permission-mode") + 1] == "plan"
    assert command[command.index("--resume") + 1] == "session-7"
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert command[command.index("--json-schema") + 1] == '{"type":"object"}'
    # The real CLI rejects --print with stream-json unless --verbose is present.
    assert "--verbose" in command


def test_planning_owns_no_second_process_lifecycle() -> None:
    source = Path(pw.__file__).read_text(encoding="utf-8")
    assert "Popen" not in source
    assert "killpg" not in source
    assert "communicate" not in source
    assert not hasattr(pw, "run_process")
    assert not hasattr(pw, "run_with_retry")


def test_rotation_after_three_revisions_or_context_pressure() -> None:
    assert not pw.should_rotate(revision_rounds=2, context_pressure=False)
    assert pw.should_rotate(revision_rounds=3, context_pressure=False)
    assert pw.should_rotate(revision_rounds=0, context_pressure=True)


def test_rehydration_contains_complete_plan_and_verbatim_ledger() -> None:
    prompt = pw.rehydration_prompt(
        current_plan={"motivation": "why", "files": ["a.py"]},
        feedback=[
            {
                "id": "F-1",
                "verbatim": "Do not hide the fallback.",
                "anchors": ["review:fallback"],
                "owner_synthesis": "Preserve behavior.",
            }
        ],
    )
    assert '"motivation":"why"' in prompt
    assert "Do not hide the fallback." in prompt
    assert "OWNER SYNTHESIS" in prompt


def test_contradictory_relay_fails_closed() -> None:
    with pytest.raises(pw.WorkerError, match="clarification"):
        pw.feedback_relay(
            verbatim="Use Codex.", owner_synthesis="Use Claude.", anchors=[], contradiction=True
        )


def test_typed_worker_result_must_match_the_actual_route_identity() -> None:
    with pytest.raises(pw.WorkerError, match="author identity") as excinfo:
        pw.validate_envelope(_route(), _envelope(author_id="claude_code:opus"))
    assert "codex:gpt-5.6-sol" in str(excinfo.value)
    validated = pw.validate_envelope(_route(), _envelope())
    assert validated["author"]["id"] == "codex:gpt-5.6-sol"


def test_preflight_has_no_fallback_and_bounds_probes(monkeypatch) -> None:
    calls: list[tuple[list[str], float]] = []

    def run(command, **kwargs):
        calls.append((command, kwargs["timeout"]))
        return subprocess.CompletedProcess(command, 1, "", "not logged in")

    monkeypatch.setattr(cw.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(pw.WorkerError, match="authentication"):
        pw.preflight(_route(), runner=run, timeout=3)
    assert calls
    assert all(timeout == 3 for _, timeout in calls)


def test_initial_launch_reports_capsule_disposal_and_terminal_acceptance(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    adapter = _FakeAdapter(_envelope())
    monkeypatch.setattr(cw, "ADAPTERS", {"codex": adapter})
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    source = _source(tmp_path)

    assert pw.cli_main(_argv(tmp_path, source)) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["envelope"]["author"]["id"] == "codex:gpt-5.6-sol"
    assert result["thread_id"]
    assert result["command"][-1] == "<prompt>"
    assert result["acceptance"]["response"]["accepted"] is True
    assert result["acceptance"]["physical_attempt"]["terminal_acknowledged"] is True
    assert result["acceptance"]["cleanup"] == {
        "capsule_absent": True,
        "quarantined": False,
        "invocation_root": str((tmp_path / "invocation").resolve()),
    }
    assert result["acceptance"]["capsule"]["source_sha"] == _git(source, "rev-parse", "HEAD")
    assert result["capability"]["version"] == "codex 1"
    assert [item["attempt"] for item in result["physical_attempts"]] == [1]
    assert adapter.thread_ids == [None]
    assert not list((tmp_path / "invocation" / "capsules").glob("*"))


def test_resumed_launch_binds_the_live_thread_and_needs_a_fresh_prompt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    adapter = _FakeAdapter(_envelope())
    monkeypatch.setattr(cw, "ADAPTERS", {"codex": adapter})
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    source = _source(tmp_path)

    assert pw.cli_main(_argv(tmp_path, source, "--thread-id", "thread-old")) == 2
    assert "fresh rehydration prompt" in capsys.readouterr().err

    fresh = tmp_path / "rehydrate.txt"
    fresh.write_text("complete plan plus ledger", encoding="utf-8")
    assert (
        pw.cli_main(
            _argv(
                tmp_path,
                source,
                "--thread-id",
                "thread-old",
                "--fresh-prompt-from",
                str(fresh),
            )
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert adapter.thread_ids == ["thread-old"]
    assert adapter.prompts[-1] == "plan the ticket"
    assert result["thread_id"] == "thread-old"


def test_launch_requires_the_exact_attempt_and_route_binding(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cw, "ADAPTERS", {"codex": _FakeAdapter(_envelope())})
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    source = _source(tmp_path)
    argv = [item for item in _argv(tmp_path, source) if item not in {"--route-digest", "b" * 64}]

    assert pw.cli_main(argv) == 2
    assert "--route-digest" in capsys.readouterr().err


def test_launch_requires_the_owner_harness(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cw, "ADAPTERS", {"codex": _FakeAdapter(_envelope())})
    monkeypatch.delenv("FLOW_HARNESS", raising=False)
    source = _source(tmp_path)

    assert pw.cli_main(_argv(tmp_path, source)) == 2
    assert "FLOW_HARNESS" in capsys.readouterr().err


def test_cli_failure_reports_each_terminal_physical_attempt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    adapter = _FakeAdapter(
        {"type": "error", "message": "You've hit your usage limit."}, returncode=1
    )
    monkeypatch.setattr(cw, "ADAPTERS", {"codex": adapter})
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    source = _source(tmp_path)

    assert pw.cli_main(_argv(tmp_path, source)) == 2

    detail = json.loads(capsys.readouterr().err.removeprefix("planner-worker: "))
    assert "exited 1" in detail["error"]
    assert [item["outcome"] for item in detail["physical_attempts"]] == ["cli_error"]
    assert detail["physical_attempts"][0]["terminal_acknowledged"] is True


def test_envelope_author_outside_the_launched_route_is_not_approvable(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        cw, "ADAPTERS", {"codex": _FakeAdapter(_envelope(author_id="claude_code:opus"))}
    )
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    source = _source(tmp_path)

    assert pw.cli_main(_argv(tmp_path, source)) == 2
    assert "author identity" in capsys.readouterr().err


def test_result_output_persists_the_result_before_ephemeral_disposal(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    adapter = _FakeAdapter(_envelope())
    monkeypatch.setattr(cw, "ADAPTERS", {"codex": adapter})
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    tmp_root = tmp_path / "tmproot"
    tmp_root.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_root))
    source = _source(tmp_path)
    result_path = tmp_path / "result.json"
    argv = _argv(tmp_path, source, "--result-output", str(result_path))
    root_flag = argv.index("--invocation-root")
    del argv[root_flag : root_flag + 2]

    assert pw.cli_main(argv) == 0

    emitted = json.loads(capsys.readouterr().out)
    assert emitted["thread_id"]
    assert emitted["acceptance"]["cleanup"]["invocation_root_absent"] is True
    assert not list(tmp_root.glob("flow-planner-worker-*"))
    persisted_text = result_path.read_text(encoding="utf-8")
    # The live session id doubles as worker_id and rides the command argv, so the
    # planner-profile file copy must carry it nowhere in the serialized bytes.
    assert emitted["thread_id"] not in persisted_text
    persisted = json.loads(persisted_text)
    expected = {key: value for key, value in emitted.items() if key not in {"thread_id", "command"}}
    expected["acceptance"]["response"] = {
        key: value
        for key, value in expected["acceptance"]["response"].items()
        if key != "worker_id"
    }
    del expected["acceptance"]["cleanup"]["invocation_root_absent"]
    assert persisted == expected


def test_failed_launch_keeps_evidence_and_fabricates_no_result_file(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    adapter = _FakeAdapter(
        {"type": "error", "message": "You've hit your usage limit."}, returncode=1
    )
    monkeypatch.setattr(cw, "ADAPTERS", {"codex": adapter})
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    tmp_root = tmp_path / "tmproot"
    tmp_root.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_root))
    source = _source(tmp_path)
    result_path = tmp_path / "result.json"
    argv = _argv(tmp_path, source, "--result-output", str(result_path))
    root_flag = argv.index("--invocation-root")
    del argv[root_flag : root_flag + 2]

    assert pw.cli_main(argv) == 2

    assert "exited 1" in capsys.readouterr().err
    assert not result_path.exists()
    assert len(list(tmp_root.glob("flow-planner-worker-*"))) == 1


def test_defaulted_source_root_is_refused_for_both_profiles(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cw, "ADAPTERS", {"codex": _FakeAdapter(_envelope())})
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    source = _source(tmp_path)
    argv = _argv(tmp_path, source)
    flag = argv.index("--source-root")
    del argv[flag : flag + 2]

    assert pw.cli_main(argv) == 2
    err = capsys.readouterr().err
    assert "shared" in err
    assert "pristine mirror" in err

    snapshot, attempt_dir, _ = _assessor_bundle(tmp_path)
    assessor_argv = _assessor_argv(tmp_path, source, attempt_dir, str(snapshot["digest"]))
    flag = assessor_argv.index("--source-root")
    del assessor_argv[flag : flag + 2]

    assert pw.cli_main(assessor_argv) == 2
    assert "pristine mirror" in capsys.readouterr().err


def test_schema_with_draft_marker_is_normalized_by_the_worker(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    adapter = _FakeAdapter(_envelope())
    monkeypatch.setattr(cw, "ADAPTERS", {"codex": adapter})
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    source = _source(tmp_path)
    argv = _argv(tmp_path, source)
    (tmp_path / "schema.json").write_text(
        json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}),
        encoding="utf-8",
    )

    assert pw.cli_main(argv) == 0

    capsys.readouterr()
    emitted = list(
        (tmp_path / "invocation" / "artifacts" / "invocations").glob("*/provider-schema.json")
    )
    assert len(emitted) == 1
    handed = json.loads(emitted[0].read_text(encoding="utf-8"))
    assert "$schema" not in handed
    assert handed["type"] == "object"


def test_plan_assessor_launch_satisfies_assess_require_fresh_end_to_end(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    snapshot, attempt_dir, envelope = _assessor_bundle(tmp_path)
    adapter = _FakeCapsuleAdapter(_assessment(str(envelope["digest"])))
    monkeypatch.setattr(cw, "ADAPTERS", {"codex": adapter})
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    source = _source(tmp_path)
    result_path = tmp_path / "assessor-result.json"
    argv = _assessor_argv(
        tmp_path, source, attempt_dir, str(snapshot["digest"]), "--result-output", str(result_path)
    )

    assert pw.cli_main(argv) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["assessment"]["verdict"] == "approve"
    assert result["thread_id"] == "assessor-thread-1"
    persisted = json.loads(result_path.read_text(encoding="utf-8"))
    assert "thread_id" not in persisted
    # The assessor's worker_id is the durable attested identity and stays in the file copy.
    assert persisted["acceptance"]["response"]["worker_id"] == "assessor-thread-1"
    assert "FLOW COGNITIVE ROLE: plan_assessor" in adapter.prompts[-1]
    assert "judge the plan" in adapter.prompts[-1]

    receipt = agent_routes.attest(snapshot, "plan_assessor", result["acceptance"])
    assert receipt["activation"] == "active"
    assert receipt["worker_id"] == "assessor-thread-1"
    verdict = pa.AssessorVerdict.create(
        assessor_id=str(receipt["worker_id"]),
        author_id="codex:gpt-5.6-sol",
        plan_digest=str(envelope["digest"]),
        outcome="pass",
        findings=[],
        fresh=True,
        launch_receipt_digest=str(receipt["digest"]),
    )
    attempt = pa.PlanningAttempt.load_bundle(attempt_dir)
    attempt.assess(verdict, require_fresh=True, launch_receipt=receipt)
    assert attempt.assessment is not None
    assert attempt.assessment_launch_receipt == receipt


def test_plan_assessor_without_worker_session_id_fails_closed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    snapshot, attempt_dir, envelope = _assessor_bundle(tmp_path)
    adapter = _FakeCapsuleAdapter(_assessment(str(envelope["digest"])), session_id=None)
    monkeypatch.setattr(cw, "ADAPTERS", {"codex": adapter})
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    source = _source(tmp_path)
    result_path = tmp_path / "result.json"
    argv = _assessor_argv(
        tmp_path,
        source,
        attempt_dir,
        str(snapshot["digest"]),
        "--result-output",
        str(result_path),
    )

    assert pw.cli_main(argv) == 2

    assert "worker session id" in capsys.readouterr().err
    assert not result_path.exists()


def test_plan_assessor_refuses_mismatched_or_incomplete_inputs(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    snapshot, attempt_dir, envelope = _assessor_bundle(tmp_path)
    adapter = _FakeCapsuleAdapter(_assessment(str(envelope["digest"])))
    monkeypatch.setattr(cw, "ADAPTERS", {"codex": adapter})
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    source = _source(tmp_path)

    assert pw.cli_main(_assessor_argv(tmp_path, source, attempt_dir, "f" * 64)) == 2
    assert "does not match the planning attempt" in capsys.readouterr().err

    empty_dir = tmp_path / "empty-attempt"
    pa.PlanningAttempt.create(
        attempt_id="attempt-2",
        base_sha="a" * 40,
        route_digest=str(snapshot["digest"]),
        owner_identity="owner",
    ).save_bundle(empty_dir)
    assert pw.cli_main(_assessor_argv(tmp_path, source, empty_dir, str(snapshot["digest"]))) == 2
    assert "no current complete plan" in capsys.readouterr().err

    bad_facts = tmp_path / "bad-facts.json"
    bad_facts.write_text(json.dumps({"ticket": {"key": "t-1"}, "extra": True}), encoding="utf-8")
    argv = _assessor_argv(
        tmp_path, source, attempt_dir, str(snapshot["digest"]), facts_path=bad_facts
    )
    assert pw.cli_main(argv) == 2
    assert "exactly ticket and assessment_rubric" in capsys.readouterr().err
