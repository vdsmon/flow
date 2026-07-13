"""FLOW workspace sync: reconcile failed tracker mutations against live tracker state.

Reads .flow/pending-mutations.jsonl (written by the commit-stage transition
chokepoint, `tracker_cli.py transition --enqueue-on-transient`, on a transient
failure), and for each entry: if its postcondition is already satisfied it is
dropped as applied-externally; if its pre-state no longer holds it is dropped as
superseded; otherwise the op is replayed. Reconciliation, not blind replay.

Transition reconciliation is read-before-replay (idempotent on target state).
For comment/link/create the probe-based dedup is deferred; those are replayed
best-effort and a successful replay drops the entry. An entry whose op no
adapter can replay (anything outside VALID_OPS, e.g. the retired generic edit)
is parked with a warning; drop it through the workspace facade's
`pending-mutations compact` command.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Protocol

import pending_mutations
from tracker import make_tracker
from tracker_cli import _read_tracker_config, _WorkspaceConfigError


class _Tracker(Protocol):
    def state(self, key: str) -> dict[str, Any]: ...
    def transition(
        self, key: str, transition_id: str, fields: dict[str, Any] | None = None
    ) -> Any: ...
    def comment(self, key: str, body: Any) -> None: ...
    def link(self, from_key: str, to_key: str, kind: str) -> None: ...
    def create(
        self,
        summary: Any,
        description: Any,
        type: str,
        parent: str | None = None,
        labels: list[str] | None = None,
        assignee: str | None = None,
    ) -> str: ...


def _state_matches(tracker: _Tracker, ticket: str, target: str) -> bool:
    # Case-insensitive: tracker_cli enqueues the lowercased --to-state, which may
    # be a native status name like "To Do".
    st = tracker.state(ticket)
    want = target.lower()
    return (st.get("normalized") or "").lower() == want or (
        st.get("native_status") or ""
    ).lower() == want


def _postcondition_met(tracker: _Tracker, entry: dict[str, Any]) -> bool:
    post = entry.get("expected_postcondition")
    if not isinstance(post, dict):
        return False
    if entry["op"] != "transition":
        return False
    target = post.get("normalized") or post.get("tracker_status")
    return bool(target) and _state_matches(tracker, entry["ticket"], str(target))


def _pre_state_superseded(tracker: _Tracker, entry: dict[str, Any]) -> bool:
    pre = entry.get("expected_pre_state")
    if not isinstance(pre, dict):
        return False
    target = pre.get("tracker_status") or pre.get("normalized")
    if not target:
        return False
    return not _state_matches(tracker, entry["ticket"], str(target))


def _invoke(tracker: _Tracker, entry: dict[str, Any]) -> bool:
    op = entry["op"]
    args = entry.get("args") or {}
    key = entry["ticket"]
    if op == "transition":
        res = tracker.transition(key, str(args.get("transition_id")), fields=args.get("fields"))
        return bool(res.get("success")) if isinstance(res, dict) else bool(res)
    if op == "comment":
        tracker.comment(key, args.get("body"))
        return True
    if op == "link":
        tracker.link(str(args.get("from_key", key)), str(args.get("to_key")), str(args.get("kind")))
        return True
    if op == "create":
        tracker.create(
            args.get("summary"),
            args.get("description"),
            str(args.get("type")),
            parent=args.get("parent"),
            labels=args.get("labels"),
            assignee=args.get("assignee"),
        )
        return True
    return False


def reconcile(workspace_root: Path, tracker: _Tracker) -> dict[str, Any]:
    applied: list[str] = []
    applied_externally: list[str] = []
    superseded: list[str] = []
    failed: list[str] = []
    parked: list[str] = []
    for entry in pending_mutations.list_mutations(workspace_root):
        key = entry["idempotency_key"]
        op = entry.get("op")
        if op not in pending_mutations.VALID_OPS:
            # No adapter can replay an unknown op; retrying would fail forever
            # and wedge sync at exit 1. Park the entry (kept on disk, warned,
            # excluded from the exit code) instead of dropping it silently.
            sys.stderr.write(
                f"sync: parked {key} (op={op} is not replayable; remove via "
                f".flow/runtime/flow pending-mutations --workspace-root . compact "
                f"--drop-keys {key})\n"
            )
            parked.append(key)
            continue
        try:
            if _postcondition_met(tracker, entry):
                applied_externally.append(key)
            elif _pre_state_superseded(tracker, entry):
                superseded.append(key)
            elif _invoke(tracker, entry):
                applied.append(key)
            else:
                failed.append(key)
        except Exception:
            failed.append(key)
    drop = set(applied) | set(applied_externally) | set(superseded)
    removed = pending_mutations.compact(workspace_root, drop)
    return {
        "applied": applied,
        "applied_externally": applied_externally,
        "superseded": superseded,
        "failed": failed,
        "parked": parked,
        "removed": removed,
    }


def _build_tracker(workspace_root: Path) -> Any:
    # _read_tracker_config threads workspace_root into the flattened config so
    # BeadsAdapter subprocesses run in the workspace, not the caller's cwd.
    return make_tracker(_read_tracker_config(workspace_root))


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="FLOW workspace sync: drain pending tracker mutations."
    )
    parser.add_argument("--workspace-root", default=".")
    args = parser.parse_args(argv)
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    try:
        tracker = _build_tracker(workspace_root)
    except _WorkspaceConfigError as exc:
        sys.stderr.write(f"sync: {exc}\n")
        return 2
    except Exception as exc:
        sys.stderr.write(f"sync: tracker unavailable: {exc}\n")
        return 2
    report = reconcile(workspace_root, tracker)
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main", "reconcile"]
