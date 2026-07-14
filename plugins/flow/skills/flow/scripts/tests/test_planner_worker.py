from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import override

import pytest

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


class _FakeProcess:
    def __init__(self, outcomes: list[object], *, pid: int = 71) -> None:
        self.outcomes = outcomes
        self.pid = pid
        self.returncode: int | None = None
        self.killed = False
        self.timeouts: list[float | None] = []

    def communicate(self, timeout: float | None = None):
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        self.returncode = 0
        return outcome

    def poll(self):
        return self.returncode


def test_soft_deadline_emits_event_but_allows_completion(tmp_path: Path) -> None:
    process = _FakeProcess(
        [
            subprocess.TimeoutExpired(["codex"], 10),
            (json.dumps({"thread_id": "T-1", "result": {"status": "PLAN_READY"}}), ""),
        ]
    )
    events: list[str] = []
    result = pw.run_process(
        ["codex"],
        popen=lambda *a, **k: process,
        soft_timeout=10,
        hard_timeout=40,
        on_event=lambda event: events.append(event["type"]),
    )
    assert result.thread_id == "T-1"
    assert result.command == ("codex",)
    assert events == ["soft_deadline"]
    assert process.timeouts == [10, 30]
    assert result.attempts[0]["attempt"] == 1
    assert result.attempts[0]["outcome"] == "success"
    assert result.attempts[0]["soft_budget_seconds"] == 10
    assert result.attempts[0]["hard_budget_seconds"] == 40
    assert result.attempts[0]["deadline_events"] == ["soft_deadline"]
    assert result.attempts[0]["terminal_acknowledged"] is True


def test_hard_timeout_requires_terminal_ack_before_retry(monkeypatch) -> None:
    first = _FakeProcess(
        [subprocess.TimeoutExpired(["codex"], 10), subprocess.TimeoutExpired(["codex"], 30)]
    )
    second = _FakeProcess(
        [(json.dumps({"thread_id": "fresh", "result": {"status": "PLAN_READY"}}), "")]
    )
    processes = iter([first, second])
    killed: list[tuple[int, int]] = []

    def killpg(pid: int, signal: int) -> None:
        killed.append((pid, signal))
        first.returncode = -signal
        first.outcomes.append(("", ""))

    result = pw.run_with_retry(
        lambda fresh: ["codex", "fresh" if fresh else "resume"],
        popen=lambda *a, **k: next(processes),
        killpg=killpg,
        soft_timeout=10,
        hard_timeout=40,
    )
    assert result.thread_id == "fresh"
    assert killed
    assert result.attempt == 2
    assert result.command[-1] == "fresh"
    assert first.timeouts[:2] == [10, 30]
    assert second.timeouts == [10]
    assert [metric["attempt"] for metric in result.attempts] == [1, 2]
    assert [metric["outcome"] for metric in result.attempts] == [
        "hard_timeout",
        "success",
    ]
    assert all(metric["soft_budget_seconds"] == 10 for metric in result.attempts)
    assert all(metric["hard_budget_seconds"] == 40 for metric in result.attempts)
    assert result.attempts[0]["terminal_acknowledged"] is True
    assert result.aggregate_elapsed_seconds >= sum(
        metric["elapsed_seconds"] for metric in result.attempts
    )


def test_unacknowledged_termination_never_starts_retry() -> None:
    first = _FakeProcess(
        [subprocess.TimeoutExpired(["codex"], 10), subprocess.TimeoutExpired(["codex"], 30)]
    )
    launches = 0

    def popen(*args, **kwargs):
        nonlocal launches
        launches += 1
        return first

    with pytest.raises(pw.WorkerError, match="terminal acknowledgement"):
        pw.run_with_retry(
            lambda fresh: ["codex"],
            popen=popen,
            killpg=lambda pid, signal: None,
            soft_timeout=10,
            hard_timeout=40,
            termination_grace=0,
        )
    assert launches == 1


def test_second_hard_timeout_has_two_distinct_metrics_and_no_third_launch() -> None:
    processes = [
        _FakeProcess(
            [subprocess.TimeoutExpired(["codex"], 600), subprocess.TimeoutExpired(["codex"], 1800)],
            pid=71,
        ),
        _FakeProcess(
            [subprocess.TimeoutExpired(["codex"], 600), subprocess.TimeoutExpired(["codex"], 1800)],
            pid=72,
        ),
    ]
    launches = 0

    def popen(*args, **kwargs):
        nonlocal launches
        process = processes[launches]
        launches += 1
        return process

    def killpg(pid: int, sig: int) -> None:
        process = next(item for item in processes if item.pid == pid)
        process.returncode = -sig
        process.outcomes.append(("", ""))

    with pytest.raises(pw.WorkerError, match="one fresh retry") as error:
        pw.run_with_retry(
            lambda fresh: ["codex", "fresh" if fresh else "initial"],
            popen=popen,
            killpg=killpg,
        )
    assert launches == 2
    assert [process.timeouts[:2] for process in processes] == [[600, 1800], [600, 1800]]
    assert [metric["attempt"] for metric in error.value.attempts] == [1, 2]
    assert all(metric["outcome"] == "hard_timeout" for metric in error.value.attempts)
    assert all(metric["terminal_acknowledged"] is True for metric in error.value.attempts)


def test_open_output_pipe_prevents_retry_even_after_process_exit() -> None:
    process = _FakeProcess(
        [
            subprocess.TimeoutExpired(["codex"], 600),
            subprocess.TimeoutExpired(["codex"], 1800),
            subprocess.TimeoutExpired(["codex"], 5),
            subprocess.TimeoutExpired(["codex"], 5),
        ]
    )
    launches = 0

    def popen(*args, **kwargs):
        nonlocal launches
        launches += 1
        return process

    with pytest.raises(pw.WorkerError, match="terminal acknowledgement") as error:
        pw.run_with_retry(
            lambda fresh: ["codex"],
            popen=popen,
            killpg=lambda pid, sig: setattr(process, "returncode", -sig),
        )
    assert launches == 1
    assert len(error.value.attempts) == 1
    assert error.value.attempts[0]["terminal_acknowledged"] is False


def test_malformed_output_is_not_approvable() -> None:
    process = _FakeProcess([("not-json\n", "")])
    with pytest.raises(pw.WorkerError, match="typed planner result") as error:
        pw.run_process(["codex"], popen=lambda *a, **k: process, soft_timeout=10, hard_timeout=40)
    assert len(error.value.attempts) == 1
    assert error.value.attempts[0]["outcome"] == "invalid_output"
    assert error.value.attempts[0]["terminal_acknowledged"] is True


def test_cli_failure_surfaces_structured_stdout_error_over_stderr_noise() -> None:
    class _FailingProcess(_FakeProcess):
        @override
        def communicate(self, timeout: float | None = None):
            result = super().communicate(timeout)
            self.returncode = 1
            return result

    process = _FailingProcess(
        [
            (
                "\n".join(
                    [
                        json.dumps({"type": "thread.started", "thread_id": "T-1"}),
                        json.dumps(
                            {
                                "type": "error",
                                "message": "You've hit your usage limit. Try again later.",
                            }
                        ),
                    ]
                ),
                "Reading additional input from stdin...\n",
            )
        ]
    )

    with pytest.raises(pw.WorkerError, match="usage limit") as error:
        pw.run_process(["codex"], popen=lambda *a, **k: process, soft_timeout=10, hard_timeout=40)

    assert "Reading additional input from stdin" not in str(error.value)
    assert error.value.attempts[0]["outcome"] == "cli_error"


def test_typed_worker_result_must_match_the_actual_route_identity() -> None:
    with pytest.raises(pw.WorkerError, match="author identity"):
        pw.validate_envelope(_route(), _envelope(author_id="claude_code:opus"))
    validated = pw.validate_envelope(_route(), _envelope())
    assert validated["author"]["id"] == "codex:gpt-5.6-sol"


def test_jsonl_parser_joins_thread_and_typed_result_events() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "T-9"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps({"status": "PLAN_READY"}),
                    },
                }
            ),
        ]
    )
    process = _FakeProcess([(stdout, "")])
    result = pw.run_process(
        ["codex"], popen=lambda *a, **k: process, soft_timeout=10, hard_timeout=40
    )
    assert result.thread_id == "T-9"
    assert result.envelope["status"] == "PLAN_READY"


def test_preflight_has_no_fallback_and_bounds_probes(monkeypatch) -> None:
    calls: list[tuple[list[str], float]] = []

    def run(command, **kwargs):
        calls.append((command, kwargs["timeout"]))
        return subprocess.CompletedProcess(command, 1, "", "not logged in")

    monkeypatch.setattr(pw.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(pw.WorkerError, match="authentication"):
        pw.preflight(_route(), runner=run, timeout=3)
    assert calls
    assert all(timeout == 3 for _, timeout in calls)


def test_resumed_cli_retry_uses_rehydration_prompt_and_reports_actual_command(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    prompt = tmp_path / "feedback.txt"
    prompt.write_text("one feedback delta", encoding="utf-8")
    fresh_prompt = tmp_path / "rehydrate.txt"
    fresh_prompt.write_text("complete plan plus ledger", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")

    monkeypatch.setattr(
        pw,
        "preflight",
        lambda route: {"executable": "/usr/bin/codex", "version": "codex 1", "harness": "codex"},
    )

    def run_retry(factory):
        resumed = factory(False)
        fresh = factory(True)
        assert resumed[-2:] == ["thread-old", "one feedback delta"]
        assert "resume" not in fresh[:3]
        assert fresh[-1] == "complete plan plus ledger"
        return pw.WorkerResult(
            envelope=_envelope(),
            thread_id="thread-new",
            stdout="",
            stderr="",
            command=tuple(fresh),
            attempt=2,
            attempts=(
                {
                    "attempt": 1,
                    "outcome": "hard_timeout",
                    "soft_budget_seconds": 600,
                    "hard_budget_seconds": 2400,
                    "deadline_events": ["soft_deadline", "hard_deadline"],
                    "elapsed_seconds": 2400.0,
                    "terminal_acknowledged": True,
                },
                {
                    "attempt": 2,
                    "outcome": "success",
                    "soft_budget_seconds": 600,
                    "hard_budget_seconds": 2400,
                    "deadline_events": [],
                    "elapsed_seconds": 1.0,
                    "terminal_acknowledged": True,
                },
            ),
            aggregate_elapsed_seconds=2401.0,
        )

    monkeypatch.setattr(pw, "run_with_retry", run_retry)
    assert (
        pw.cli_main(
            [
                "--harness",
                "codex",
                "--model",
                "gpt-5.6-sol",
                "--effort",
                "xhigh",
                "--prompt-from",
                str(prompt),
                "--fresh-prompt-from",
                str(fresh_prompt),
                "--schema",
                str(schema),
                "--thread-id",
                "thread-old",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["command"][-1] == "<prompt>"
    assert result["command"][-2] != "thread-old"
    assert (
        result["acceptance"]["prompt_hash"]
        == pw.hashlib.sha256(b"complete plan plus ledger").hexdigest()
    )
    assert [item["attempt"] for item in result["physical_attempts"]] == [1, 2]
    assert result["aggregate_wall_seconds"] == 2401.0


def test_resumed_cli_requires_a_fresh_rehydration_prompt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    prompt = tmp_path / "feedback.txt"
    prompt.write_text("delta only", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    monkeypatch.setattr(
        pw,
        "preflight",
        lambda route: {"executable": "/usr/bin/codex", "version": "codex 1", "harness": "codex"},
    )
    assert (
        pw.cli_main(
            [
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
                "--thread-id",
                "thread-old",
            ]
        )
        == 2
    )
    assert "fresh rehydration prompt" in capsys.readouterr().err


def test_cli_failure_reports_each_terminal_physical_attempt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("plan", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    metric = {
        "attempt": 1,
        "outcome": "hard_timeout",
        "soft_budget_seconds": 600,
        "hard_budget_seconds": 2400,
        "deadline_events": ["soft_deadline", "hard_deadline"],
        "elapsed_seconds": 2400.0,
        "terminal_acknowledged": False,
    }
    monkeypatch.setattr(
        pw,
        "preflight",
        lambda route: {
            "executable": "/usr/bin/codex",
            "version": "codex 1",
            "harness": "codex",
        },
    )

    def fail(factory):
        del factory
        raise pw.WorkerError("terminal acknowledgement missing", attempts=(metric,))

    monkeypatch.setattr(pw, "run_with_retry", fail)
    assert (
        pw.cli_main(
            [
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
            ]
        )
        == 2
    )
    error = capsys.readouterr().err.removeprefix("planner-worker: ")
    detail = json.loads(error)
    assert detail["error"] == "terminal acknowledgement missing"
    assert detail["physical_attempts"] == [metric]


def test_cli_semantic_output_failure_reclassifies_the_terminal_attempt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("plan", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    metric = {
        "attempt": 1,
        "outcome": "success",
        "soft_budget_seconds": 600,
        "hard_budget_seconds": 2400,
        "deadline_events": [],
        "elapsed_seconds": 1.0,
        "terminal_acknowledged": True,
    }
    monkeypatch.setattr(
        pw,
        "preflight",
        lambda route: {
            "executable": "/usr/bin/codex",
            "version": "codex 1",
            "harness": "codex",
        },
    )
    monkeypatch.setattr(
        pw,
        "run_with_retry",
        lambda factory: pw.WorkerResult(
            envelope=_envelope(author_id="claude_code:opus"),
            thread_id="thread-1",
            stdout="",
            stderr="",
            command=("codex", "plan"),
            attempts=(metric,),
            aggregate_elapsed_seconds=1.0,
        ),
    )
    assert (
        pw.cli_main(
            [
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
            ]
        )
        == 2
    )
    detail = json.loads(capsys.readouterr().err.removeprefix("planner-worker: "))
    assert detail["physical_attempts"][0]["outcome"] == "invalid_output"
    assert detail["physical_attempts"][0]["terminal_acknowledged"] is True
