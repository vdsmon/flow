"""/flow sync: reconcile failed tracker mutations against live tracker state.

Reads .flow/pending-mutations.jsonl (written by adapter failure paths), and for
each entry: if its postcondition is already satisfied it is dropped as
applied-externally; if its pre-state no longer holds it is dropped as superseded;
otherwise the op is replayed. Reconciliation, not blind replay.

Transition reconciliation is read-before-replay (idempotent on target state).
For comment/link/create/edit the probe-based dedup is deferred; those are
replayed best-effort and a successful replay drops the entry.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import pending_mutations
from _workspace import WorkspaceConfigError, load_workspace_toml


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Tracker(Protocol):
    def state(self, key: str) -> dict[str, Any]: ...
    def transition(self, key: str, transition_id: str, fields: dict | None = None) -> Any: ...
    def comment(self, key: str, body: Any) -> None: ...
    def link(self, from_key: str, to_key: str, kind: str) -> None: ...
    def edit(self, key: str, fields: dict) -> None: ...


def _state_matches(tracker: _Tracker, ticket: str, target: str) -> bool:
    st = tracker.state(ticket)
    return st.get("normalized") == target or st.get("native_status") == target


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
    if op == "edit":
        tracker.edit(key, args.get("fields") or {})
        return True
    return False


def reconcile(workspace_root: Path, tracker: _Tracker) -> dict[str, Any]:
    applied: list[str] = []
    applied_externally: list[str] = []
    superseded: list[str] = []
    failed: list[str] = []
    for entry in pending_mutations.list_mutations(workspace_root):
        key = entry["idempotency_key"]
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
        "removed": removed,
    }


def _build_tracker(workspace_root: Path) -> Any:
    data = load_workspace_toml(workspace_root)
    tracker_cfg = data.get("tracker")
    if not isinstance(tracker_cfg, dict):
        raise WorkspaceConfigError("workspace.toml missing [tracker] block")
    backend = tracker_cfg.get("backend")
    sub = tracker_cfg.get(backend) if backend else None
    cfg: dict[str, Any] = {"backend": backend}
    if isinstance(sub, dict):
        cfg.update(sub)
    import tracker as tracker_mod

    return tracker_mod.make_tracker(cfg)


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="/flow sync: drain pending tracker mutations.")
    parser.add_argument("--workspace-root", default=".")
    args = parser.parse_args(argv)
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    try:
        tracker = _build_tracker(workspace_root)
    except WorkspaceConfigError as exc:
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
