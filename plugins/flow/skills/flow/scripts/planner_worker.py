"""Planning compatibility surface over the common read-only capsule contract.

Only planning-specific behavior lives here: route-bound author validation, feedback
relay, thread rotation, and rehydration. The exact CLI, private clone, process
supervision, journal, typed validation, Git guards, receipts, and disposal belong to
``cognitive_workers`` and are reached through one ``CognitiveWorkers.run`` call. The
returned thread identifier belongs only to the live owner and is never written to a
Flow run or planning bundle.
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
import time
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

# Retention window for the failed-launch evidence _launch reaps under ~/.cache.
_EPHEMERAL_REAP_AGE_S = 7 * 86400


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
        raise WorkerError(
            "planner envelope author identity does not match the actual route; "
            f"the launched route requires author id {route.author_id!r}"
        )
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
    return cognitive_workers.build_planner_command(
        route.to_mapping(),
        prompt,
        schema_path=schema_path,
        thread_id=thread_id,
        new_thread_id=new_thread_id,
    )


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


def _explicit_source_root(args: argparse.Namespace) -> Path:
    if not args.source_root:
        raise WorkerError(
            "planner-worker requires an explicit --source-root: the invoking checkout is "
            "typically the shared cockpit checkout, which concurrent actors mutate between "
            "the before/after receipt captures; pass a dedicated pristine mirror clone"
        )
    return Path(args.source_root).expanduser().resolve()


def _assessor_order(
    args: argparse.Namespace, route: PlannerRoute, source_root: Path
) -> cognitive_workers.WorkOrder:
    missing = [
        flag
        for flag, value in (
            ("--attempt-dir", args.attempt_dir),
            ("--route-digest", args.route_digest),
            ("--facts-from", args.facts_from),
        )
        if value is None
    ]
    if missing:
        raise WorkerError(f"a plan-assessor launch requires {', '.join(missing)}")
    try:
        attempt = planning_attempt.PlanningAttempt.load_bundle(Path(args.attempt_dir))
    except planning_attempt.AttemptError as exc:
        raise WorkerError(f"cannot load the planning attempt bundle: {exc}") from exc
    if attempt.route_digest != args.route_digest:
        raise WorkerError("--route-digest does not match the planning attempt bundle")
    current = attempt.current
    if current is None:
        raise WorkerError("planning attempt has no current complete plan to assess")
    planner_receipt = attempt.planner_launch_receipts.get(current.digest)
    if planner_receipt is None:
        raise WorkerError("planning attempt is missing the current planner launch receipt")
    facts_path = Path(args.facts_from).expanduser().resolve()
    try:
        supplied = json.loads(facts_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerError(f"cannot read the assessor facts file: {exc}") from exc
    if not isinstance(supplied, dict) or set(supplied) != {"ticket", "assessment_rubric"}:
        raise WorkerError("assessor facts must supply exactly ticket and assessment_rubric")
    return cognitive_workers.WorkOrder(
        logical_invocation_id=f"plan-assessor:{attempt.attempt_id}:v{current.version}",
        generation=1,
        profile="plan_assessor",
        source_root=str(source_root),
        source_sha=_git(source_root, "rev-parse", "HEAD"),
        route=route.to_mapping(),
        route_snapshot_digest=args.route_digest,
        input_bundle=str(facts_path),
        input_digest=hashlib.sha256(facts_path.read_bytes()).hexdigest(),
        facts={
            "ticket": supplied["ticket"],
            "base_sha": attempt.base_sha,
            "route_digest": attempt.route_digest,
            "candidate_plan": current.to_mapping(),
            "planner_receipt": planner_receipt,
            "assessment_rubric": supplied["assessment_rubric"],
        },
    )


def _work_order(args: argparse.Namespace, route: PlannerRoute) -> cognitive_workers.WorkOrder:
    source_root = _explicit_source_root(args)
    if args.profile == "plan_assessor":
        return _assessor_order(args, route, source_root)
    missing = [
        flag
        for flag, value in (
            ("--prompt-from", args.prompt_from),
            ("--schema", args.schema),
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
    schema_path = Path(args.schema).expanduser().resolve()
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerError(f"cannot read the emitted planner schema: {exc}") from exc
    if isinstance(schema, dict):
        # The claude --json-schema path rejects the draft marker the emitted schema carries;
        # the worker owns this strip so no driver rewrites the emitted file by hand.
        schema.pop("$schema", None)
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


def _reap_stale_ephemeral_siblings(cache_parent: Path) -> None:
    """Best-effort removal of failed-launch evidence older than _EPHEMERAL_REAP_AGE_S."""
    try:
        now = time.time()
        for sibling in cache_parent.glob("flow-planner-worker-*"):
            try:
                if now - sibling.stat().st_mtime > _EPHEMERAL_REAP_AGE_S:
                    shutil.rmtree(sibling)
            except OSError:
                continue
    except OSError:
        pass


def _launch(args: argparse.Namespace, route: PlannerRoute) -> dict[str, Any]:
    """Run one logical pre-approval invocation through the common capsule executor."""
    order = _work_order(args, route)
    owner = _owner_proof()
    ephemeral = args.invocation_root is None
    if ephemeral:
        # mise's trusted_config_paths is ['~/'], so the cloned capsule's mise.toml must land under
        # HOME; TMPDIR (e.g. macOS /var/folders) is untrusted and must not be used here. A failed
        # launch keeps its invocation root here as forensic evidence (see below), so siblings past
        # _EPHEMERAL_REAP_AGE_S are best-effort reaped on every ephemeral launch to bound growth
        # while keeping recent forensics.
        cache_parent = Path.home() / ".cache" / "flow-planner-worker"
        cache_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _reap_stale_ephemeral_siblings(cache_parent)
        invocation_root = Path(
            tempfile.mkdtemp(prefix="flow-planner-worker-", dir=str(cache_parent))
        ).resolve()
    else:
        invocation_root = Path(args.invocation_root).resolve()
    outcome = cognitive_workers.CognitiveWorkers(
        artifact_root=invocation_root / "artifacts",
        capsule_root=invocation_root / "capsules",
    ).run(order, owner)
    receipts = outcome.receipts
    attempts = list(receipts["physical_attempts"])
    acceptance = dict(receipts["route_acceptance"])
    acceptance["cleanup"] = {**acceptance["cleanup"], "invocation_root": str(invocation_root)}
    acceptance["capsule"] = receipts["capsule"]
    worker_id = receipts["route"]["worker_id"]
    if args.profile == "plan_assessor":
        if not worker_id:
            raise WorkerError(
                "plan-assessor output carried no worker session id; assess --require-fresh "
                "needs the receipt's distinct worker id as the assessor identity"
            )
        typed: dict[str, Any] = {"assessment": outcome.result}
    else:
        typed = {"envelope": validate_envelope(route, outcome.result)}
    result = {
        **typed,
        "thread_id": worker_id,
        "attempt": attempts[-1]["attempt"] if attempts else 1,
        "physical_attempts": attempts,
        "aggregate_wall_seconds": sum(float(item["elapsed_seconds"]) for item in attempts),
        "capability": receipts["capability"],
        "command": receipts["command"],
        "acceptance": acceptance,
    }
    if args.result_output:
        # Persisted before the ephemeral disposal so a failed write keeps the invocation root as
        # evidence. The planner copy carries the live session id nowhere: worker_id doubles as the
        # thread id and rides top-level thread_id, acceptance.response.worker_id, and the command
        # argv (resume/session flags embed it), so all three stay out of the file. The assessor
        # copy keeps its worker_id, the durable attested identity that assess --require-fresh
        # consumes; a one-shot assessor has no live owner conversation to protect.
        if args.profile == "plan_assessor":
            persisted = {key: value for key, value in result.items() if key != "thread_id"}
        else:
            persisted = {
                key: value for key, value in result.items() if key not in {"thread_id", "command"}
            }
            persisted["acceptance"] = {
                **acceptance,
                "response": {
                    key: value
                    for key, value in acceptance["response"].items()
                    if key != "worker_id"
                },
            }
        planning_attempt.atomic_write_text(
            Path(args.result_output).expanduser().resolve(),
            json.dumps(persisted, indent=2, sort_keys=True) + "\n",
        )
    if ephemeral:
        # The planner's durable state is the envelope it returned. Its journal holds the provider
        # transcript, and the transcript holds the live thread id, which must never outlive the
        # owner conversation. A failed launch keeps its evidence.
        shutil.rmtree(invocation_root, ignore_errors=True)
        acceptance["cleanup"]["invocation_root_absent"] = not invocation_root.exists()
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one exact read-only pre-approval route.")
    parser.add_argument("--harness", choices=["codex", "claude_code"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--profile", choices=["planner", "plan_assessor"], default="planner")
    parser.add_argument("--prompt-from")
    parser.add_argument("--fresh-prompt-from")
    parser.add_argument("--schema")
    parser.add_argument("--thread-id")
    parser.add_argument("--attempt-id")
    parser.add_argument("--plan-version", type=int)
    parser.add_argument("--route-digest")
    parser.add_argument("--attempt-dir")
    parser.add_argument("--facts-from")
    parser.add_argument("--source-root")
    parser.add_argument("--invocation-root")
    parser.add_argument("--result-output")
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
    "build_command",
    "cli_main",
    "feedback_relay",
    "preflight",
    "rehydration_prompt",
    "should_rotate",
    "validate_envelope",
]
