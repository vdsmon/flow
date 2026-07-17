"""Planning compatibility surface over the common read-only capsule contract.

Only planning-specific behavior lives here: route-bound author validation. The exact CLI, private
clone, process supervision, journal, typed validation, Git guards, receipts, and disposal belong to
``cognitive_workers`` and are reached through one ``CognitiveWorkers.run`` call. The returned thread
identifier belongs only to the live owner and is never written to a Flow run or planning bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cognitive_workers
import planning_attempt

SOFT_TIMEOUT_SECONDS = cognitive_workers.SOFT_TIMEOUT_SECONDS
HARD_TIMEOUT_SECONDS = cognitive_workers.HARD_TIMEOUT_SECONDS

WorkerError = cognitive_workers.WorkerFailure


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

    def to_mapping(self) -> dict[str, str]:
        return {"harness": self.harness, "model": self.model, "effort": self.effort}


def validate_envelope(route: PlannerRoute, value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate provider output and bind its author to the route the adapter launched."""
    if value is None:
        raise WorkerError("planner returned no typed envelope")
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


def preflight(route: PlannerRoute, **kwargs: Any) -> dict[str, str]:
    """Probe executable, authentication, and required flags, including resume."""
    return cognitive_workers.preflight_route(route.to_mapping(), require_resume=True, **kwargs)


def _git(root: Path | None, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def _owner_proof() -> cognitive_workers.OwnerProof:
    harness = os.environ.get("FLOW_HARNESS", "").replace("-", "_")
    if harness not in {"codex", "claude_code"}:
        raise WorkerError("planner-worker requires FLOW_HARNESS to name the owner harness")
    return cognitive_workers.OwnerProof(owner_id="planner-worker", harness=harness)


def _work_order(args: argparse.Namespace, route: PlannerRoute) -> cognitive_workers.WorkOrder:
    missing = [
        flag
        for flag, value in (
            ("--attempt-id", args.attempt_id),
            ("--plan-version", args.plan_version),
            ("--route-digest", args.route_digest),
        )
        if value is None
    ]
    if missing:
        raise WorkerError(f"a planner launch requires {', '.join(missing)}")
    if args.thread_id and not args.fresh_prompt_from:
        raise WorkerError(
            "a resumed planner requires --fresh-prompt-from with a complete "
            "fresh rehydration prompt"
        )
    prompt_path = Path(args.prompt_from).expanduser().resolve()
    prompt = prompt_path.read_text(encoding="utf-8")
    fresh_prompt = (
        Path(args.fresh_prompt_from).expanduser().read_text(encoding="utf-8")
        if args.fresh_prompt_from
        else prompt
    )
    source_root = (
        Path(args.source_root).expanduser().resolve()
        if args.source_root
        else Path(_git(None, "rev-parse", "--show-toplevel")).resolve()
    )
    schema_path = Path(args.schema).expanduser().resolve()
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerError(f"cannot read the emitted planner schema: {exc}") from exc
    return cognitive_workers.WorkOrder(
        logical_invocation_id=f"planner:{args.attempt_id}:v{args.plan_version}",
        generation=1,
        profile="planner",
        source_root=str(source_root),
        source_sha=_git(source_root, "rev-parse", "HEAD"),
        route=route.to_mapping(),
        route_snapshot_digest=args.route_digest,
        input_bundle=str(prompt_path),
        input_digest=hashlib.sha256(prompt_path.read_bytes()).hexdigest(),
        facts={
            "attempt_id": args.attempt_id,
            "plan_version": args.plan_version,
            "mode": "resume" if args.thread_id else "initial",
        },
        result_schema=schema,
        provider_prompt=prompt,
        fresh_provider_prompt=fresh_prompt,
        session={
            "thread_id": args.thread_id,
            "initial_session_id": str(uuid.uuid4()),
            "fresh_session_id": str(uuid.uuid4()),
        },
    )


def _launch(args: argparse.Namespace, route: PlannerRoute) -> dict[str, Any]:
    """Run one logical planner invocation through the common capsule executor."""
    order = _work_order(args, route)
    owner = _owner_proof()
    ephemeral = args.invocation_root is None
    invocation_root = Path(
        args.invocation_root
        or tempfile.mkdtemp(prefix="flow-planner-worker-", dir=os.environ.get("TMPDIR"))
    ).resolve()
    outcome = cognitive_workers.CognitiveWorkers(
        artifact_root=invocation_root / "artifacts",
        capsule_root=invocation_root / "capsules",
    ).run(order, owner)
    receipts = outcome.receipts
    attempts = list(receipts["physical_attempts"])
    acceptance = dict(receipts["route_acceptance"])
    acceptance["cleanup"] = {**acceptance["cleanup"], "invocation_root": str(invocation_root)}
    acceptance["capsule"] = receipts["capsule"]
    if ephemeral:
        # The planner's durable state is the envelope it just returned. Its journal holds the
        # provider transcript, and the transcript holds the live thread id, which must never
        # outlive the owner conversation. A failed launch keeps its evidence.
        shutil.rmtree(invocation_root, ignore_errors=True)
        acceptance["cleanup"]["invocation_root_absent"] = not invocation_root.exists()
    return {
        "envelope": validate_envelope(route, outcome.result),
        "thread_id": receipts["route"]["worker_id"],
        "attempt": attempts[-1]["attempt"] if attempts else 1,
        "physical_attempts": attempts,
        "aggregate_wall_seconds": sum(float(item["elapsed_seconds"]) for item in attempts),
        "capability": receipts["capability"],
        "command": receipts["command"],
        "acceptance": acceptance,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one exact read-only planner route.")
    parser.add_argument("--harness", choices=["codex", "claude_code"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--prompt-from", required=True)
    parser.add_argument("--fresh-prompt-from")
    parser.add_argument("--schema", required=True)
    parser.add_argument("--thread-id")
    parser.add_argument("--attempt-id")
    parser.add_argument("--plan-version", type=int)
    parser.add_argument("--route-digest")
    parser.add_argument("--source-root")
    parser.add_argument("--invocation-root")
    parser.add_argument("--preflight-only", action="store_true")
    return parser


def cli_main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    route = PlannerRoute(args.harness, args.model, args.effort)
    try:
        result: object = preflight(route) if args.preflight_only else _launch(args, route)
    except (OSError, WorkerError) as exc:
        attempts = list(getattr(exc, "attempts", ()))
        if attempts:
            detail = {"error": str(exc), "physical_attempts": attempts}
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
    "cli_main",
    "preflight",
    "validate_envelope",
]
