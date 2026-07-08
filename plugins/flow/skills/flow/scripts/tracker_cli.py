"""CLI wrapper around the Tracker Protocol.

Library + thin CLI. Stdlib-only.

Lets reference-doc prose call tracker.<method>() from Bash. Each subcommand
maps to a Tracker Protocol method; output is JSON to stdout; errors go to
stderr with structured exit codes.

Subcommands:
  get --key FT-1                         tracker.get(key) -> JSON
  state --key FT-1                       tracker.state(key) -> JSON
  transition --key FT-1 --to-state in_progress [--field k=v ...]
  comment --key FT-1 --text "..."        tracker.comment(key, body)
  create --summary "..." --type task [--description "..." --parent K --label L --assignee A]
                                         tracker.create(...) -> {"key": new_key}
  is-shipped --key FT-1                  tracker.is_shipped(key) -> JSON
  download-attachments --key FT-1 --out <dir> [--max-bytes N]   download to <dir>

Workspace resolution: reads `.flow/workspace.toml` `[tracker]` block, flattens
the per-backend sub-block (`[tracker.jira]` or `[tracker.beads]`) into the
config dict that `tracker.make_tracker()` expects.

Exit codes:
  0 = success
  1 = transient/unknown tracker error (network / auth / retryable / unknown
      failure_kind / no such key)
  2 = workspace config invalid (no workspace.toml, malformed, missing block)
  3 = invalid CLI args (bad transition lookup, malformed --field)
  4 = hard transition failure (permission_denied / validator_failed / missing_required_field)
  5 = transition not applicable (wrong_source_state / ambiguous_transition)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import _workspace
import pending_mutations
from _timeutil import utcnow_iso
from tracker import NotSupported, TrackerError, make_tracker


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


# Several native states can normalize to in_progress (e.g. Jira's "Testing");
# prefer the one that reads as the actual in-progress state.
_IN_PROGRESS_HINTS = ("in progress", "doing", "in development")


# ─── Subcommand dispatch ─────────────────────────────────────────────────────


def _cmd_get(tracker_obj: Any, args: argparse.Namespace) -> int:
    ticket = tracker_obj.get(args.key)
    sys.stdout.write(json.dumps(ticket, indent=2, sort_keys=True, default=str) + "\n")
    return 0


def _cmd_state(tracker_obj: Any, args: argparse.Namespace) -> int:
    state = tracker_obj.state(args.key)
    sys.stdout.write(json.dumps(state, indent=2, sort_keys=True, default=str) + "\n")
    return 0


def _select_transition_id(transitions: list[dict[str, Any]], target: str) -> str | None:
    """Pick the id of the first available transition matching `target`.

    Falls back to an unavailable match when no available one exists: posting
    it lets the backend's rejection detail surface instead of a generic "no
    transition" error.
    """
    if target == "in_progress":
        # Boards can offer several to_normalized_state=="in_progress" transitions
        # (e.g. 'Testing' and 'In Progress'); break the tie toward the native one.
        for t in transitions:
            if not t.get("available", True):
                continue
            if t.get("to_normalized_state", "").lower() != "in_progress":
                continue
            hint_text = f"{t.get('to_state', '')} {t.get('name', '')}".lower()
            if any(hint in hint_text for hint in _IN_PROGRESS_HINTS):
                return t.get("id")
    unavailable_id: str | None = None
    for t in transitions:
        candidates = (
            t.get("to_normalized_state", "").lower(),
            t.get("to_state", "").lower(),
            t.get("name", "").lower(),
        )
        if target not in candidates:
            continue
        if t.get("available", True):
            return t.get("id")
        if unavailable_id is None:
            unavailable_id = t.get("id")
    return unavailable_id


def _cmd_transition(tracker_obj: Any, args: argparse.Namespace) -> int:
    transitions = tracker_obj.list_transitions(args.key)
    target = args.to_state.lower()
    selected_id = _select_transition_id(transitions, target)
    if selected_id is None:
        hint = ""
        if type(tracker_obj).__name__ == "BeadsAdapter":
            # beads synthesizes only open->[in_progress, blocked, closed]; other
            # real bd statuses (deferred) are reachable only via the raw CLI
            hint = f" — for a status bd accepts, use: bd update {args.key} --status {target}"
        sys.stderr.write(
            f"tracker-cli transition: no transition to {args.to_state!r} available "
            f"(have: {[t.get('name') for t in transitions]}){hint}\n"
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

    def _enqueue() -> None:
        if not args.enqueue_on_transient:
            return
        try:
            pending_mutations.append_mutation(
                Path(args.workspace_root).resolve(),
                ticket=args.key,
                op="transition",
                args={"transition_id": selected_id, "fields": fields or None},
                expected_postcondition={"normalized": args.to_state.lower()},
                intent_at=utcnow_iso(),
            )
        except Exception as exc:
            sys.stderr.write(f"tracker-cli transition: enqueue failed: {exc}\n")

    try:
        result = tracker_obj.transition(args.key, selected_id, fields=fields or None)
    except TrackerError as exc:
        _enqueue()
        sys.stderr.write(f"tracker-cli transition: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
    if result.get("success", False):
        return 0
    code = _FAILURE_KIND_EXIT.get(result.get("failure_kind"), 1)
    if code == 1:
        _enqueue()
    return code


def _cmd_comment(tracker_obj: Any, args: argparse.Namespace) -> int:
    body = {"body": args.text, "fmt": "md"}
    tracker_obj.comment(args.key, body)
    sys.stdout.write(json.dumps({"ok": True, "key": args.key}) + "\n")
    return 0


def _cmd_create(tracker_obj: Any, args: argparse.Namespace) -> int:
    summary = {"body": args.summary, "fmt": "md"}
    description = {"body": args.description, "fmt": "md"}
    new_key = tracker_obj.create(
        summary,
        description,
        args.type,
        parent=args.parent,
        labels=args.label,
        assignee=args.assignee,
    )
    sys.stdout.write(json.dumps({"key": new_key}) + "\n")
    return 0


def _cmd_is_shipped(tracker_obj: Any, args: argparse.Namespace) -> int:
    ship = tracker_obj.is_shipped(args.key)
    sys.stdout.write(json.dumps(ship, indent=2, sort_keys=True, default=str) + "\n")
    return 0


def _cmd_list_types(tracker_obj: Any, args: argparse.Namespace) -> int:
    del args
    types = tracker_obj.list_issue_types()
    sys.stdout.write(json.dumps(types, indent=2, sort_keys=True, default=str) + "\n")
    return 0


def _cmd_list_epics(tracker_obj: Any, args: argparse.Namespace) -> int:
    del args
    epics = tracker_obj.list_epics()
    sys.stdout.write(json.dumps(epics, indent=2, sort_keys=True, default=str) + "\n")
    return 0


def _cmd_list_sprints(tracker_obj: Any, args: argparse.Namespace) -> int:
    try:
        sprints = tracker_obj.list_sprints(args.project)
    except NotSupported:
        # Backends without sprints (beads): not an error, nothing to list.
        sys.stdout.write(json.dumps({"supported": False, "sprints": []}) + "\n")
        return 0
    sys.stdout.write(json.dumps(sprints, indent=2, sort_keys=True, default=str) + "\n")
    return 0


def _cmd_set_sprint(tracker_obj: Any, args: argparse.Namespace) -> int:
    try:
        tracker_obj.set_sprint(args.key, args.sprint_id)
    except NotSupported:
        sys.stdout.write(json.dumps({"supported": False, "key": args.key}) + "\n")
        return 0
    sys.stdout.write(json.dumps({"ok": True, "key": args.key, "sprint_id": args.sprint_id}) + "\n")
    return 0


def _safe_filename(name: str) -> str:
    """Strip directory components and unsafe chars from an attachment filename."""
    base = re.sub(r"[^\w.\-]+", "_", Path(name).name).strip("._")
    return base or "attachment"


def _cmd_download_attachments(tracker_obj: Any, args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    try:
        attachments = tracker_obj.get_attachments(args.key)
    except NotSupported:
        # Backends without attachments (beads): not an error, nothing to do.
        sys.stdout.write(json.dumps({"supported": False, "key": args.key, "downloaded": []}) + "\n")
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[dict[str, Any]] = []
    for att in attachments:
        size = int(att.get("size") or 0)
        fname = _safe_filename(att.get("filename") or att.get("id") or "attachment")
        if args.max_bytes and size > args.max_bytes:
            downloaded.append({"filename": fname, "size": size, "skipped": "exceeds-max-bytes"})
            continue
        data = tracker_obj.download_attachment(att)
        dest = out_dir / fname
        dest.write_bytes(data)
        downloaded.append({"filename": fname, "size": len(data), "path": str(dest)})
    sys.stdout.write(
        json.dumps({"supported": True, "key": args.key, "downloaded": downloaded}, indent=2) + "\n"
    )
    return 0


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CLI wrapper around the Tracker Protocol.")
    parser.add_argument("--workspace-root", default=".")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_get = sub.add_parser("get", help="tracker.get(key)")
    p_get.add_argument("--key", required=True)

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
    p_trans.add_argument(
        "--enqueue-on-transient",
        action="store_true",
        help="on a transient failure (exit 1), durably queue the transition to "
        ".flow/pending-mutations.jsonl for /flow sync to reconcile.",
    )

    p_comment = sub.add_parser("comment", help="tracker.comment(key, body)")
    p_comment.add_argument("--key", required=True)
    p_comment.add_argument("--text", required=True)

    p_create = sub.add_parser("create", help="tracker.create(summary, description, type, ...)")
    p_create.add_argument("--summary", required=True)
    p_create.add_argument("--description", default="")
    p_create.add_argument("--type", required=True)
    p_create.add_argument("--parent", default=None)
    p_create.add_argument("--label", action="append", default=None, help="label (repeatable).")
    p_create.add_argument("--assignee", default=None)

    p_types = sub.add_parser("list-types", help="tracker.list_issue_types()")
    del p_types  # no flags

    p_epics = sub.add_parser("list-epics", help="tracker.list_epics()")
    del p_epics  # no flags

    p_sprints = sub.add_parser("list-sprints", help="tracker.list_sprints(project)")
    p_sprints.add_argument("--project", default="", help="project key (beads scopes by it).")

    p_set_sprint = sub.add_parser("set-sprint", help="tracker.set_sprint(key, sprint_id)")
    p_set_sprint.add_argument("--key", required=True)
    p_set_sprint.add_argument("--sprint-id", required=True)

    p_ship = sub.add_parser("is-shipped", help="tracker.is_shipped(key)")
    p_ship.add_argument("--key", required=True)

    p_dl = sub.add_parser("download-attachments", help="download ticket attachments to a dir")
    p_dl.add_argument("--key", required=True)
    p_dl.add_argument("--out", required=True, help="destination directory")
    p_dl.add_argument(
        "--max-bytes",
        type=int,
        default=26_214_400,
        help="skip attachments larger than this (default 25 MiB).",
    )

    return parser.parse_args(argv)


_DISPATCH: dict[str, Any] = {
    "get": _cmd_get,
    "state": _cmd_state,
    "transition": _cmd_transition,
    "comment": _cmd_comment,
    "create": _cmd_create,
    "is-shipped": _cmd_is_shipped,
    "download-attachments": _cmd_download_attachments,
    "list-types": _cmd_list_types,
    "list-epics": _cmd_list_epics,
    "list-sprints": _cmd_list_sprints,
    "set-sprint": _cmd_set_sprint,
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
