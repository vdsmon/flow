"""CLI wrapper around the Tracker Protocol.

Library + thin CLI. Stdlib-only.

Lets reference-doc prose call tracker.<method>() from Bash. Each subcommand
maps to a Tracker Protocol method; output is JSON to stdout; errors go to
stderr with structured exit codes.

Subcommands:
  get --key FT-1                         tracker.get(key) -> JSON
  list-assigned [--filter open]          tracker.list_assigned() -> JSON array
  state --key FT-1                       tracker.state(key) -> JSON
  transition --key FT-1 --to-state in_progress [--field k=v ...]
  comment --key FT-1 --text "..."        tracker.comment(key, body)
  is-shipped --key FT-1                  tracker.is_shipped(key) -> JSON

Workspace resolution: reads `.flow/workspace.toml` `[tracker]` block, flattens
the per-backend sub-block (`[tracker.jira]` or `[tracker.beads]`) into the
config dict that `tracker.make_tracker()` expects.

Exit codes:
  0 = success
  1 = transient/unknown tracker error (network / auth / retryable / unknown failure_kind)
  2 = workspace config invalid (no workspace.toml, malformed, missing block)
  3 = invalid CLI args (no such key, bad transition lookup, malformed --field)
  4 = hard transition failure (permission_denied / validator_failed / missing_required_field)
  5 = transition not applicable (wrong_source_state / ambiguous_transition)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import _workspace
from tracker import TrackerError, make_tracker


class _WorkspaceConfigError(Exception):
    """Workspace.toml is missing, malformed, or lacks [tracker]. Exit code 2."""


def _read_tracker_config(workspace_root: Path) -> dict[str, Any]:
    """Read `.flow/workspace.toml` and return the flattened tracker config dict.

    The result is suitable for passing directly to `tracker.make_tracker()`.
    Sub-block fields (`tracker.jira.*` or `tracker.beads.*`) are lifted into
    the top level. The `backend` field is preserved.
    """
    try:
        data = _workspace.load_workspace_toml(workspace_root)
    except _workspace.WorkspaceConfigError as exc:
        raise _WorkspaceConfigError(str(exc)) from exc
    tracker = data.get("tracker")
    if not isinstance(tracker, dict):
        raise _WorkspaceConfigError("workspace.toml missing [tracker] block")
    backend = tracker.get("backend")
    if backend not in ("jira", "beads"):
        raise _WorkspaceConfigError(f"unknown tracker.backend {backend!r}")
    flat: dict[str, Any] = {"backend": backend}
    sub = tracker.get(backend)
    if isinstance(sub, dict):
        flat.update(sub)
    # Beads adapter also reads workspace_root from config.
    flat["workspace_root"] = str(workspace_root)
    return flat


def _parse_field(field_arg: str) -> tuple[str, str]:
    if "=" not in field_arg:
        raise ValueError(f"--field value {field_arg!r} missing '='")
    key, _, value = field_arg.partition("=")
    return key, value


# Transition failure_kind -> exit code. Unmapped/unknown kinds fall through to 1
# (transient/unknown). The commit reference doc depends on these exact codes.
_FAILURE_KIND_EXIT: dict[str | None, int] = {
    "permission_denied": 4,
    "validator_failed": 4,
    "missing_required_field": 4,
    "wrong_source_state": 5,
    "ambiguous_transition": 5,
}


# ─── Subcommand dispatch ─────────────────────────────────────────────────────


def _cmd_get(tracker_obj: Any, args: argparse.Namespace) -> int:
    ticket = tracker_obj.get(args.key)
    sys.stdout.write(json.dumps(ticket, indent=2, sort_keys=True, default=str) + "\n")
    return 0


def _cmd_list_assigned(tracker_obj: Any, args: argparse.Namespace) -> int:
    tickets = tracker_obj.list_assigned(args.filter)
    sys.stdout.write(json.dumps(tickets, indent=2, sort_keys=True, default=str) + "\n")
    return 0


def _cmd_state(tracker_obj: Any, args: argparse.Namespace) -> int:
    state = tracker_obj.state(args.key)
    sys.stdout.write(json.dumps(state, indent=2, sort_keys=True, default=str) + "\n")
    return 0


def _cmd_transition(tracker_obj: Any, args: argparse.Namespace) -> int:
    transitions = tracker_obj.list_transitions(args.key)
    target = args.to_state.lower()
    selected_id: str | None = None
    for t in transitions:
        candidates = (
            t.get("to_normalized_state", "").lower(),
            t.get("to_state", "").lower(),
            t.get("name", "").lower(),
        )
        if target in candidates:
            selected_id = t.get("id")
            break
    if selected_id is None:
        sys.stderr.write(
            f"tracker-cli transition: no transition to {args.to_state!r} available "
            f"(have: {[t.get('name') for t in transitions]})\n"
        )
        return 3
    fields: dict[str, Any] = {}
    if args.field:
        for raw in args.field:
            try:
                k, v = _parse_field(raw)
            except ValueError as exc:
                sys.stderr.write(f"tracker-cli transition: {exc}\n")
                return 3
            fields[k] = v
    result = tracker_obj.transition(args.key, selected_id, fields=fields or None)
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
    if result.get("success", False):
        return 0
    return _FAILURE_KIND_EXIT.get(result.get("failure_kind"), 1)


def _cmd_comment(tracker_obj: Any, args: argparse.Namespace) -> int:
    body = {"body": args.text, "fmt": "md"}
    tracker_obj.comment(args.key, body)
    sys.stdout.write(json.dumps({"ok": True, "key": args.key}) + "\n")
    return 0


def _cmd_is_shipped(tracker_obj: Any, args: argparse.Namespace) -> int:
    ship = tracker_obj.is_shipped(args.key)
    sys.stdout.write(json.dumps(ship, indent=2, sort_keys=True, default=str) + "\n")
    return 0


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CLI wrapper around the Tracker Protocol.")
    parser.add_argument("--workspace-root", default=".")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_get = sub.add_parser("get", help="tracker.get(key)")
    p_get.add_argument("--key", required=True)

    p_list = sub.add_parser("list-assigned", help="tracker.list_assigned(filter)")
    p_list.add_argument("--filter", default="open")

    p_state = sub.add_parser("state", help="tracker.state(key)")
    p_state.add_argument("--key", required=True)

    p_trans = sub.add_parser("transition", help="tracker.transition(key, id, fields)")
    p_trans.add_argument("--key", required=True)
    p_trans.add_argument(
        "--to-state",
        required=True,
        help="target state (matched against to_normalized_state / to_state / name).",
    )
    p_trans.add_argument(
        "--field",
        action="append",
        default=None,
        help="k=v pair (repeatable).",
    )

    p_comment = sub.add_parser("comment", help="tracker.comment(key, body)")
    p_comment.add_argument("--key", required=True)
    p_comment.add_argument("--text", required=True)

    p_ship = sub.add_parser("is-shipped", help="tracker.is_shipped(key)")
    p_ship.add_argument("--key", required=True)

    return parser.parse_args(argv)


_DISPATCH: dict[str, Any] = {
    "get": _cmd_get,
    "list-assigned": _cmd_list_assigned,
    "state": _cmd_state,
    "transition": _cmd_transition,
    "comment": _cmd_comment,
    "is-shipped": _cmd_is_shipped,
}


def cli_main(
    argv: list[str],
    tracker_factory: Any = None,
) -> int:
    """Dispatch a subcommand. `tracker_factory` is injectable for tests
    (default: real `make_tracker`)."""
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    try:
        config = _read_tracker_config(workspace_root)
    except _WorkspaceConfigError as exc:
        sys.stderr.write(f"tracker-cli: {exc}\n")
        return 2
    factory = tracker_factory or make_tracker
    try:
        tracker_obj = factory(config)
    except Exception as exc:
        sys.stderr.write(f"tracker-cli: factory error: {exc}\n")
        return 2
    handler = _DISPATCH.get(args.cmd)
    if handler is None:
        sys.stderr.write(f"tracker-cli: unknown subcommand {args.cmd!r}\n")
        return 3
    try:
        return handler(tracker_obj, args)
    except TrackerError as exc:
        sys.stderr.write(f"tracker-cli: tracker error: {exc}\n")
        return 1
    except (KeyError, ValueError) as exc:
        sys.stderr.write(f"tracker-cli: invalid argument: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main"]
