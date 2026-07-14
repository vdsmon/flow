"""Read-only Codex and Claude Code planner process adapters.

The adapter owns exact CLI arguments, bounded process lifetime, and typed-output
extraction. Its returned thread identifier belongs only to the live owner and is
never written to a Flow run or planning bundle.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import planning_attempt

SOFT_TIMEOUT_SECONDS = 10 * 60
HARD_TIMEOUT_SECONDS = 40 * 60
TERMINATION_GRACE_SECONDS = 5.0


class WorkerError(RuntimeError):
    """A planner launch cannot satisfy the exact safe-worker contract."""

    def __init__(self, message: str, *, attempts: tuple[dict[str, Any], ...] = ()) -> None:
        super().__init__(message)
        self.attempts = attempts


class _HardTimeout(WorkerError):
    def __init__(self, terminal_acknowledged: bool, metric: dict[str, Any]) -> None:
        super().__init__("planner reached its hard deadline", attempts=(metric,))
        self.terminal_acknowledged = terminal_acknowledged


class ProcessLike(Protocol):
    pid: int
    returncode: int | None

    def communicate(self, timeout: float | None = None) -> tuple[str, str]: ...

    def poll(self) -> int | None: ...


@dataclass(frozen=True)
class PlannerRoute:
    harness: str
    model: str
    effort: str

    def __post_init__(self) -> None:
        if self.harness not in {"codex", "claude_code"}:
            raise WorkerError(f"unsupported planner harness {self.harness!r}")
        if not self.model.strip() or not self.effort.strip():
            raise WorkerError("planner route requires exact model and effort selectors")

    @property
    def author_id(self) -> str:
        return f"{self.harness}:{self.model}"


@dataclass(frozen=True)
class WorkerResult:
    envelope: dict[str, Any]
    thread_id: str
    stdout: str
    stderr: str
    command: tuple[str, ...]
    attempt: int = 1
    attempts: tuple[dict[str, Any], ...] = ()
    aggregate_elapsed_seconds: float = 0.0


def should_rotate(*, revision_rounds: int, context_pressure: bool) -> bool:
    """Rotate the physical thread before its fourth revision round."""
    return context_pressure or revision_rounds >= 3


def feedback_relay(
    *,
    verbatim: str,
    owner_synthesis: str,
    anchors: list[str] | tuple[str, ...],
    contradiction: bool = False,
) -> str:
    """Build a lossless relay, stopping when the owner flags a contradiction."""
    if contradiction:
        raise WorkerError("verbatim feedback and owner synthesis conflict; ask for clarification")
    payload = {
        "verbatim": verbatim,
        "anchors": list(anchors),
        "owner_synthesis": owner_synthesis,
    }
    return (
        "USER FEEDBACK (VERBATIM)\n"
        + json.dumps(payload["verbatim"], ensure_ascii=False)
        + "\nANCHORS\n"
        + json.dumps(payload["anchors"], ensure_ascii=False)
        + "\nOWNER SYNTHESIS\n"
        + json.dumps(payload["owner_synthesis"], ensure_ascii=False)
    )


def validate_envelope(route: PlannerRoute, value: dict[str, Any]) -> dict[str, Any]:
    """Validate provider output and bind its author to the route the adapter launched."""
    try:
        envelope = planning_attempt.PlanEnvelope.from_mapping(value)
    except planning_attempt.AttemptError as exc:
        raise WorkerError(f"planner returned an invalid typed envelope: {exc}") from exc
    if (
        envelope.author.get("id") != route.author_id
        or envelope.author.get("harness") != route.harness
        or envelope.author.get("model") != route.model
    ):
        raise WorkerError("planner envelope author identity does not match the actual route")
    return envelope.to_mapping()


def rehydration_prompt(*, current_plan: dict[str, Any], feedback: list[dict[str, Any]]) -> str:
    """Return all canonical review state needed by a fresh physical worker."""
    relay = [
        feedback_relay(
            verbatim=str(item.get("verbatim", "")),
            anchors=list(item.get("anchors", [])),
            owner_synthesis=str(item.get("owner_synthesis", "")),
        )
        for item in feedback
    ]
    return (
        "Rehydrate the logical planner from this complete plan and feedback ledger. "
        "Return a complete typed plan envelope, never a prose delta.\nCURRENT PLAN\n"
        + json.dumps(current_plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\nFEEDBACK LEDGER\n"
        + "\n\n".join(relay)
    )


def build_command(
    route: PlannerRoute,
    prompt: str,
    *,
    schema_path: Path,
    thread_id: str | None = None,
    new_thread_id: str | None = None,
) -> list[str]:
    """Build the exact read-only command for one supported planner harness."""
    schema = str(schema_path.expanduser().resolve())
    if route.harness == "codex":
        command = ["codex", "exec"]
        if thread_id:
            command.append("resume")
        command.extend(
            [
                "--model",
                route.model,
                "-c",
                f'model_reasoning_effort="{route.effort}"',
            ]
        )
        if thread_id:
            command.extend(["-c", 'sandbox_mode="read-only"'])
        else:
            command.extend(["--sandbox", "read-only"])
        command.extend(["--json", "--output-schema", schema])
        if thread_id:
            command.append(thread_id)
        command.append(prompt)
        return command

    command = [
        "claude",
        "--print",
        "--model",
        route.model,
        "--effort",
        route.effort,
        "--permission-mode",
        "plan",
        "--output-format",
        "stream-json",
        "--json-schema",
        schema_path.read_text(encoding="utf-8"),
    ]
    if thread_id:
        command.extend(["--resume", thread_id])
    else:
        command.extend(["--session-id", new_thread_id or str(uuid.uuid4())])
    command.append(prompt)
    return command


def preflight(
    route: PlannerRoute,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = 5.0,
) -> dict[str, str]:
    """Probe executable, authentication, and required flags within a short bound."""
    executable = "codex" if route.harness == "codex" else "claude"
    resolved = shutil.which(executable)
    if resolved is None:
        raise WorkerError(f"planner executable {executable!r} is unavailable")
    auth_command = (
        [executable, "login", "status"] if executable == "codex" else [executable, "auth", "status"]
    )
    try:
        auth = runner(auth_command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerError(f"planner authentication probe failed: {exc}") from exc
    if auth.returncode != 0:
        detail = (auth.stderr or auth.stdout).strip()
        raise WorkerError(f"planner authentication is unavailable: {detail}")
    try:
        version = runner(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        help_commands = (
            [[executable, "exec", "--help"], [executable, "exec", "resume", "--help"]]
            if executable == "codex"
            else [[executable, "--help"]]
        )
        help_results = [
            runner(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            for command in help_commands
        ]
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerError(f"planner capability probe failed: {exc}") from exc
    if version.returncode != 0 or any(result.returncode != 0 for result in help_results):
        raise WorkerError("planner capability probe did not complete successfully")
    help_text = "\n".join(result.stdout + result.stderr for result in help_results)
    required = (
        ("--model", "--sandbox", "--output-schema", "--json")
        if executable == "codex"
        else ("--model", "--effort", "--permission-mode", "--json-schema")
    )
    missing = [flag for flag in required if flag not in help_text]
    if missing:
        raise WorkerError(f"planner CLI lacks required capabilities: {', '.join(missing)}")
    if executable == "codex":
        resume_text = help_results[1].stdout + help_results[1].stderr
        resume_missing = [
            flag
            for flag in ("--model", "--output-schema", "--json", "--config")
            if flag not in resume_text
        ]
        if resume_missing:
            raise WorkerError(
                "planner CLI resume lacks required capabilities: " + ", ".join(resume_missing)
            )
    return {
        "executable": resolved,
        "version": (version.stdout or version.stderr).strip(),
        "harness": route.harness,
    }


def _typed_result(stdout: str, stderr: str, *, command: list[str]) -> WorkerResult:
    objects: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    thread_id: str | None = None
    for value in objects:
        candidate = value.get("thread_id", value.get("session_id"))
        if isinstance(candidate, str) and candidate:
            thread_id = candidate
    for value in reversed(objects):
        payload = value.get("structured_output", value.get("result", value.get("output")))
        item = value.get("item")
        if payload is None and isinstance(item, dict) and item.get("type") == "agent_message":
            payload = item.get("text")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = None
        if isinstance(payload, dict) and isinstance(thread_id, str) and thread_id:
            return WorkerResult(
                envelope=payload,
                thread_id=thread_id,
                stdout=stdout,
                stderr=stderr,
                command=tuple(command),
            )
    raise WorkerError("planner output did not contain a typed planner result and thread id")


def _cli_error_detail(stdout: str, stderr: str) -> str:
    """Prefer actionable structured CLI errors over transport chatter."""
    messages: list[str] = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        candidates = [value.get("message")]
        error = value.get("error")
        if isinstance(error, dict):
            candidates.append(error.get("message"))
        elif isinstance(error, str):
            candidates.append(error)
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip() and candidate not in messages:
                messages.append(candidate.strip())
    if messages:
        return "; ".join(messages)
    fallback = [part.strip() for part in (stderr, stdout) if part.strip()]
    return "\n".join(fallback) or "no diagnostic output"


def _default_popen(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
    return subprocess.Popen(command, **kwargs)


def _terminate_process_group(
    process: ProcessLike,
    *,
    killpg: Callable[[int, int], None],
    grace: float,
) -> bool:
    with contextlib.suppress(OSError, ProcessLookupError):
        killpg(process.pid, signal.SIGTERM)
    if grace <= 0:
        return False
    try:
        process.communicate(timeout=grace)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError, ProcessLookupError):
            killpg(process.pid, signal.SIGKILL)
        try:
            process.communicate(timeout=grace)
        except subprocess.TimeoutExpired:
            return False
    return process.poll() is not None


def run_process(
    command: list[str],
    *,
    popen: Callable[..., Any] = _default_popen,
    killpg: Callable[[int, int], None] = os.killpg,
    soft_timeout: float = SOFT_TIMEOUT_SECONDS,
    hard_timeout: float = HARD_TIMEOUT_SECONDS,
    termination_grace: float = TERMINATION_GRACE_SECONDS,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    attempt_number: int = 1,
) -> WorkerResult:
    """Run one physical worker and fail closed at the hard deadline."""
    if soft_timeout <= 0 or hard_timeout <= soft_timeout:
        raise WorkerError("planner deadlines require 0 < soft < hard")
    process = popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    started = time.monotonic()
    deadline_events: list[str] = []
    try:
        stdout, stderr = process.communicate(timeout=soft_timeout)
    except subprocess.TimeoutExpired:
        deadline_events.append("soft_deadline")
        if on_event is not None:
            on_event(
                {
                    "type": "soft_deadline",
                    "attempt": attempt_number,
                    "elapsed_seconds": time.monotonic() - started,
                }
            )
        try:
            stdout, stderr = process.communicate(timeout=hard_timeout - soft_timeout)
        except subprocess.TimeoutExpired:
            deadline_events.append("hard_deadline")
            acknowledged = _terminate_process_group(process, killpg=killpg, grace=termination_grace)
            elapsed = max(0.0, time.monotonic() - started)
            metric = {
                "attempt": attempt_number,
                "outcome": "hard_timeout",
                "soft_budget_seconds": soft_timeout,
                "hard_budget_seconds": hard_timeout,
                "deadline_events": deadline_events,
                "elapsed_seconds": elapsed,
                "terminal_acknowledged": acknowledged,
            }
            if on_event is not None:
                on_event(
                    {
                        "type": "hard_deadline",
                        "attempt": attempt_number,
                        "elapsed_seconds": elapsed,
                        "terminal_acknowledged": acknowledged,
                    }
                )
            raise _HardTimeout(acknowledged, metric) from None
    elapsed = max(0.0, time.monotonic() - started)
    if process.returncode != 0:
        metric = {
            "attempt": attempt_number,
            "outcome": "cli_error",
            "soft_budget_seconds": soft_timeout,
            "hard_budget_seconds": hard_timeout,
            "deadline_events": deadline_events,
            "elapsed_seconds": elapsed,
            "terminal_acknowledged": True,
        }
        raise WorkerError(
            f"planner CLI exited {process.returncode}: {_cli_error_detail(stdout, stderr)}",
            attempts=(metric,),
        )
    metric = {
        "attempt": attempt_number,
        "outcome": "success",
        "soft_budget_seconds": soft_timeout,
        "hard_budget_seconds": hard_timeout,
        "deadline_events": deadline_events,
        "elapsed_seconds": elapsed,
        "terminal_acknowledged": True,
    }
    try:
        typed = _typed_result(stdout, stderr, command=command)
    except WorkerError as exc:
        metric["outcome"] = "invalid_output"
        raise WorkerError(str(exc), attempts=(metric,)) from exc
    return replace(
        typed,
        attempt=attempt_number,
        attempts=(metric,),
        aggregate_elapsed_seconds=elapsed,
    )


def run_with_retry(
    command_factory: Callable[[bool], list[str]],
    *,
    popen: Callable[..., Any] = _default_popen,
    killpg: Callable[[int, int], None] = os.killpg,
    soft_timeout: float = SOFT_TIMEOUT_SECONDS,
    hard_timeout: float = HARD_TIMEOUT_SECONDS,
    termination_grace: float = TERMINATION_GRACE_SECONDS,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> WorkerResult:
    """Permit one fresh retry only after confirmed terminal cancellation."""
    started = time.monotonic()
    attempts: list[dict[str, Any]] = []
    for attempt in (1, 2):
        try:
            result = run_process(
                command_factory(attempt == 2),
                popen=popen,
                killpg=killpg,
                soft_timeout=soft_timeout,
                hard_timeout=hard_timeout,
                termination_grace=termination_grace,
                on_event=on_event,
                attempt_number=attempt,
            )
            attempts.extend(result.attempts)
            aggregate = max(
                time.monotonic() - started,
                sum(float(item["elapsed_seconds"]) for item in attempts),
            )
            return replace(
                result,
                attempt=attempt,
                attempts=tuple(attempts),
                aggregate_elapsed_seconds=max(0.0, aggregate),
            )
        except _HardTimeout as exc:
            attempts.extend(exc.attempts)
            if not exc.terminal_acknowledged:
                raise WorkerError(
                    "planner cancellation lacks terminal acknowledgement; refusing overlap",
                    attempts=tuple(attempts),
                ) from exc
            if attempt == 2:
                raise WorkerError(
                    "planner exhausted its one fresh retry",
                    attempts=tuple(attempts),
                ) from exc
            if on_event is not None:
                on_event({"type": "fresh_retry", "attempt": 2})
    raise AssertionError("bounded planner retry loop escaped")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one exact read-only planner route.")
    parser.add_argument("--harness", choices=["codex", "claude_code"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--prompt-from", required=True)
    parser.add_argument("--fresh-prompt-from")
    parser.add_argument("--schema", required=True)
    parser.add_argument("--thread-id")
    parser.add_argument("--preflight-only", action="store_true")
    return parser


def cli_main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    route = PlannerRoute(args.harness, args.model, args.effort)
    try:
        capability = preflight(route)
        if args.preflight_only:
            result: object = capability
        else:
            prompt = Path(args.prompt_from).read_text(encoding="utf-8")
            if args.thread_id and not args.fresh_prompt_from:
                raise WorkerError(
                    "a resumed planner requires --fresh-prompt-from with a complete "
                    "fresh rehydration prompt"
                )
            fresh_prompt = (
                Path(args.fresh_prompt_from).read_text(encoding="utf-8")
                if args.fresh_prompt_from
                else prompt
            )
            initial_session_id = str(uuid.uuid4())
            retry_session_id = str(uuid.uuid4())

            def command_factory(fresh: bool) -> list[str]:
                return build_command(
                    route,
                    fresh_prompt if fresh else prompt,
                    schema_path=Path(args.schema),
                    thread_id=None if fresh else args.thread_id,
                    new_thread_id=retry_session_id if fresh else initial_session_id,
                )

            worker = run_with_retry(command_factory)
            executed_prompt = worker.command[-1]
            try:
                envelope = validate_envelope(route, worker.envelope)
            except WorkerError as exc:
                attempts = [dict(item) for item in worker.attempts]
                if attempts:
                    attempts[-1]["outcome"] = "invalid_output"
                raise WorkerError(str(exc), attempts=tuple(attempts)) from exc
            result = {
                "envelope": envelope,
                "thread_id": worker.thread_id,
                "attempt": worker.attempt,
                "physical_attempts": list(worker.attempts),
                "aggregate_wall_seconds": worker.aggregate_elapsed_seconds,
                "capability": capability,
                "command": [*worker.command[:-1], "<prompt>"],
                "acceptance": {
                    "request": {
                        "harness": route.harness,
                        "model": route.model,
                        "effort": route.effort,
                    },
                    "response": {
                        "accepted": True,
                        "harness": route.harness,
                        "model": route.model,
                        "effort": route.effort,
                        "transport": "cli",
                        "adapter_version": capability["version"],
                        "canonical_model": None,
                    },
                    "prompt_hash": hashlib.sha256(executed_prompt.encode()).hexdigest(),
                    "schema_hash": hashlib.sha256(Path(args.schema).read_bytes()).hexdigest(),
                },
            }
    except (OSError, WorkerError) as exc:
        if isinstance(exc, WorkerError) and exc.attempts:
            detail = {
                "error": str(exc),
                "physical_attempts": list(exc.attempts),
            }
            sys.stderr.write("planner-worker: " + json.dumps(detail, sort_keys=True) + "\n")
        else:
            sys.stderr.write(f"planner-worker: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "HARD_TIMEOUT_SECONDS",
    "SOFT_TIMEOUT_SECONDS",
    "PlannerRoute",
    "WorkerError",
    "WorkerResult",
    "build_command",
    "feedback_relay",
    "preflight",
    "rehydration_prompt",
    "run_process",
    "run_with_retry",
    "should_rotate",
    "validate_envelope",
]
