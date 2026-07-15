from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import cognitive_workers as cw
import planner_worker as pw


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
    with pytest.raises(pw.WorkerError, match="author identity"):
        pw.validate_envelope(_route(), _envelope(author_id="claude_code:opus"))
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
