"""Read-only JSON facade for the deterministic bare-FLOW cockpit."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Never, override

from cockpit import (
    CockpitInput,
    DeferredItem,
    FeedbackItem,
    MaintenanceNotice,
    PendingMutation,
    build_cockpit,
    render_cockpit,
)


class EvidenceError(ValueError):
    """A host evidence file is absent, malformed, or not normalized."""


class _EvidenceParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> Never:
        raise EvidenceError(message)


def _error(message: str) -> None:
    payload = {"error": {"code": "invalid_evidence", "message": message}}
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
    unknown = payload.keys() - {"runs", "deferred", "pending", "feedback", "maintenance"}
    if unknown:
        raise EvidenceError(f"unknown evidence field: {sorted(unknown)[0]}")
    return {str(key): value for key, value in payload.items()}


def _entries(payload: dict[str, object], name: str) -> list[dict[str, object]]:
    value = payload.get(name, [])
    if not isinstance(value, list):
        raise EvidenceError(f"{name} must be an array of objects")
    entries: list[dict[str, object]] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise EvidenceError(f"{name} must be an array of objects")
        entries.append({str(key): item for key, item in entry.items()})
    return entries


def _construct(cls: type, entry: dict[str, object], section: str) -> object:
    try:
        return cls(**entry)
    except TypeError as exc:
        raise EvidenceError(f"invalid {section} item: {exc}") from exc


def _validate_scalar_types(evidence: CockpitInput) -> None:
    for item in evidence.deferred:
        if not all(isinstance(value, str) for value in (item.target, item.question, item.state)):
            raise EvidenceError("deferred fields must be strings")
    for item in evidence.pending:
        if not all(isinstance(value, str) for value in (item.target, item.operation)):
            raise EvidenceError("pending fields must be strings")
    for item in evidence.feedback:
        if not isinstance(item.target, str) or not isinstance(item.pr, str):
            raise EvidenceError("feedback target and pr must be strings")
        if isinstance(item.actionable_count, bool) or not isinstance(item.actionable_count, int):
            raise EvidenceError("feedback actionable_count must be an integer")
        if item.actionable_count < 0:
            raise EvidenceError("feedback actionable_count must be non-negative")
    for item in evidence.maintenance:
        if not all(
            isinstance(value, str) for value in (item.label, item.detail, item.next_command)
        ):
            raise EvidenceError("maintenance fields must be strings")


def _evidence(payload: dict[str, object]) -> CockpitInput:
    runs = tuple(_entries(payload, "runs"))
    deferred = tuple(
        _construct(DeferredItem, entry, "deferred") for entry in _entries(payload, "deferred")
    )
    pending = tuple(
        _construct(PendingMutation, entry, "pending") for entry in _entries(payload, "pending")
    )
    feedback = tuple(
        _construct(FeedbackItem, entry, "feedback") for entry in _entries(payload, "feedback")
    )
    maintenance = tuple(
        _construct(MaintenanceNotice, entry, "maintenance")
        for entry in _entries(payload, "maintenance")
    )
    evidence = CockpitInput(
        runs=runs,
        deferred=tuple(item for item in deferred if isinstance(item, DeferredItem)),
        pending=tuple(item for item in pending if isinstance(item, PendingMutation)),
        feedback=tuple(item for item in feedback if isinstance(item, FeedbackItem)),
        maintenance=tuple(item for item in maintenance if isinstance(item, MaintenanceNotice)),
    )
    _validate_scalar_types(evidence)
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = _EvidenceParser(description="Render normalized Flow cockpit evidence.")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    render_parser = subparsers.add_parser("render")
    render_parser.add_argument("--evidence", required=True)
    render_parser.add_argument("--json", action="store_true")
    return parser


def cli_main(argv: list[str]) -> int:
    try:
        args = _parser().parse_args(argv)
        snapshot = build_cockpit(_evidence(_load_object(args.evidence)))
    except EvidenceError as exc:
        _error(str(exc))
        return 2
    if args.json:
        sys.stdout.write(json.dumps(asdict(snapshot), sort_keys=True, separators=(",", ":")) + "\n")
    else:
        sys.stdout.write(render_cockpit(snapshot) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["EvidenceError", "cli_main"]
