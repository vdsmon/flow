"""Read-only JSON facade for the pure target lifecycle reducer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Never, cast, override

from lifecycle import (
    GroupabilityEvidence,
    GroupTargetEvidence,
    InvalidLifecycleRequest,
    LeaseState,
    LifecycleAction,
    LifecycleEvidence,
    MultiTargetChoice,
    PrState,
    RunState,
    TicketState,
    UnknownTargetError,
    coordinate_targets,
    reduce_lifecycle,
)

_TICKET_STATES = frozenset({"open", "deferred", "blocked", "done", "cancelled"})
_RUN_STATES = frozenset({"none", "healthy", "failed", "stale", "drifted", "corrupt", "completed"})
_LEASE_STATES = frozenset({"free", "owned", "live_foreign", "stale_foreign"})
_PR_STATES = frozenset({"none", "open", "merged", "closed"})
_REQUIRED = frozenset({"target_exists", "ticket_state", "run_state", "lease_state", "pr_state"})
_OPTIONAL = frozenset(
    {
        "request",
        "scope_approved",
        "stored_question",
        "actionable_feedback",
        "ship_event_corrupt",
        "contradictions",
    }
)


class EvidenceError(ValueError):
    """A host evidence file is absent, malformed, or not normalized."""


class _EvidenceParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> Never:
        raise EvidenceError(message)


def _error(code: str, message: str) -> None:
    payload = {"error": {"code": code, "message": message}}
    sys.stderr.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def _load_object(raw_path: str) -> dict[str, object]:
    path = Path(raw_path)
    if not path.is_absolute():
        raise EvidenceError("--evidence must be an absolute path")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read normalized evidence: {exc}") from exc
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        raise EvidenceError("evidence root must be a JSON object")
    return {str(key): value for key, value in payload.items()}


def _state(payload: dict[str, object], name: str, allowed: frozenset[str]) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or value not in allowed:
        raise EvidenceError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return value


def _boolean(payload: dict[str, object], name: str, default: bool = False) -> bool:
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise EvidenceError(f"{name} must be a boolean")
    return value


def _evidence(payload: dict[str, object]) -> LifecycleEvidence:
    missing = _REQUIRED - payload.keys()
    if missing:
        raise EvidenceError(f"missing evidence field: {sorted(missing)[0]}")
    unknown = payload.keys() - (_REQUIRED | _OPTIONAL)
    if unknown:
        raise EvidenceError(f"unknown evidence field: {sorted(unknown)[0]}")
    contradictions = payload.get("contradictions", [])
    if not isinstance(contradictions, list) or not all(
        isinstance(item, str) for item in contradictions
    ):
        raise EvidenceError("contradictions must be an array of strings")
    return LifecycleEvidence(
        target_exists=_boolean(payload, "target_exists"),
        ticket_state=cast(TicketState, _state(payload, "ticket_state", _TICKET_STATES)),
        run_state=cast(RunState, _state(payload, "run_state", _RUN_STATES)),
        lease_state=cast(LeaseState, _state(payload, "lease_state", _LEASE_STATES)),
        pr_state=cast(PrState, _state(payload, "pr_state", _PR_STATES)),
        request=_boolean(payload, "request"),
        scope_approved=_boolean(payload, "scope_approved"),
        stored_question=_boolean(payload, "stored_question"),
        actionable_feedback=_boolean(payload, "actionable_feedback"),
        ship_event_corrupt=_boolean(payload, "ship_event_corrupt"),
        contradictions=tuple(str(item) for item in contradictions),
    )


def _groupability(raw_path: str) -> GroupabilityEvidence:
    payload = _load_object(raw_path)
    unknown = payload.keys() - {"targets", "coupling_verified"}
    if unknown:
        raise EvidenceError(f"unknown groupability field: {sorted(unknown)[0]}")
    if "targets" not in payload or "coupling_verified" not in payload:
        raise EvidenceError("groupability evidence requires targets and coupling_verified")
    coupling = payload["coupling_verified"]
    if not isinstance(coupling, bool):
        raise EvidenceError("coupling_verified must be a boolean")
    raw_targets = payload["targets"]
    if not isinstance(raw_targets, list):
        raise EvidenceError("groupability targets must be an array")
    targets: list[GroupTargetEvidence] = []
    for item in raw_targets:
        if not isinstance(item, dict):
            raise EvidenceError("each groupability target requires only key, live, and epic")
        entry: dict[str, object] = {str(key): value for key, value in item.items()}
        if set(entry) != {"key", "live", "epic"}:
            raise EvidenceError("each groupability target requires only key, live, and epic")
        key = entry["key"]
        live = entry["live"]
        epic = entry["epic"]
        if not isinstance(key, str) or not key:
            raise EvidenceError("groupability target key must be a non-empty string")
        if not isinstance(live, bool) or not isinstance(epic, bool):
            raise EvidenceError("groupability target live and epic must be booleans")
        targets.append(GroupTargetEvidence(key=key, live=live, epic=epic))
    return GroupabilityEvidence(targets=tuple(targets), coupling_verified=coupling)


def _parser() -> argparse.ArgumentParser:
    parser = _EvidenceParser(description="Reduce normalized Flow lifecycle evidence.")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    reduce_parser = subparsers.add_parser("reduce")
    reduce_parser.add_argument("--evidence", required=True)
    coordinate_parser = subparsers.add_parser("coordinate")
    coordinate_parser.add_argument(
        "--action",
        action="append",
        required=True,
        choices=[action.value for action in LifecycleAction],
    )
    coordinate_parser.add_argument("--together", action="store_true")
    coordinate_parser.add_argument("--unattended", action="store_true")
    coordinate_parser.add_argument(
        "--choice", choices=[choice.value for choice in MultiTargetChoice]
    )
    coordinate_parser.add_argument("--groupability-evidence")
    return parser


def cli_main(argv: list[str]) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.operation == "coordinate":
            choice = MultiTargetChoice(args.choice) if args.choice else None
            disposition = coordinate_targets(
                [LifecycleAction(action) for action in args.action],
                together=args.together,
                unattended=args.unattended,
                choice=choice,
                groupability=(
                    _groupability(args.groupability_evidence)
                    if args.groupability_evidence
                    else None
                ),
            )
            sys.stdout.write(
                json.dumps({"disposition": disposition.value}, separators=(",", ":")) + "\n"
            )
            return 0
        evidence = _evidence(_load_object(args.evidence))
        action = reduce_lifecycle(evidence)
    except EvidenceError as exc:
        _error("invalid_evidence", str(exc))
        return 2
    except UnknownTargetError as exc:
        _error("unknown_target", str(exc))
        return 3
    except InvalidLifecycleRequest as exc:
        _error("invalid_request", str(exc))
        return 4
    sys.stdout.write(json.dumps({"action": action.value}, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["EvidenceError", "cli_main"]
