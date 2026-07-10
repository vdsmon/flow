"""Freeze a ship event from a doomed worktree's state.json before the reap.

Library + thin CLI. Stdlib-only.

When a merged-and-closed run's worktree is torn down (the worktree_janitor sweep, or the
evolve-drain step-A orphan path), the reaper must read run_id/attribution out of the worktree's
`.flow/runs/<key>/state.json` and freeze the ship event BEFORE `reap_worktree` destroys that state.
This module is the whole observe-at-close sequence behind one seam: the janitor imports the lib, the
drain prose invokes the CLI.

Sequence (`observe_at_close`):
  1. Idempotence pre-check: a frozen `ship-events/<key>.json` already at the main store -> skip.
  2. is_shipped gate: only `not_yet_observed` observes. Anything else (shipped, not_shipped,
     indeterminate) skips. The gate is PR#277's measurement-integrity property; this path only calls
     it post-close, never loosens it. Closed-unmerged reads `indeterminate` and is never observed.
  3. Capture run_id from the doomed worktree's state.json (validated 16-hex).
  4. Gather tier / acceptance_invariant (from the tracker) + lane (from the worktree's ticket
     frontmatter), all best-effort.
  5. Synthesize shipped_at from the is_shipped evidence's `closed_at`, else now.
  6. Write via `observe_ship_event.observe`, stamping attribution from the doomed worktree's
     state.json (state_path override) while the event itself writes against the MAIN root's store.

Never raises: returns `{"action": "observed"|"skipped"|"failed", "reason", ...}`.

CLI:
  observe_at_close.py --workspace-root <main-root> --key <key> [--worktree <dir>]

`--worktree` is the doomed worktree root (the janitor passes it); omitted on the drain path, where
the worktree is auto-resolved from the pool. Prints the result dict as JSON. Exit 0 on
observed/skipped, 1 on failed (advisory only; the drain invokes with `|| true`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import _evolve_common
import _memory_paths
import _timeutil
import observe_ship_event
import ticket_frontmatter
from tracker import Tracker, TrackerError, make_tracker
from tracker_cli import _read_tracker_config, _WorkspaceConfigError

_RUN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_SHIPPED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ACCEPTANCE_STEM = "ACCEPTANCE-INVARIANT:"


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _resolve_run_dir(workspace_root: Path, key: str, worktree: Path | None) -> Path | None:
    """The run dir `.flow/runs/<key>` holding the doomed run's state.

    Explicit `worktree` (the janitor's `entry["worktree"]`) roots the run dir directly; the drain
    path omits it and resolves via the worktree pool.
    """
    if worktree is not None:
        return worktree / ".flow" / "runs" / key
    return _evolve_common.run_dir_for(workspace_root, key)


def _read_run_id(state_path: Path) -> str | None:
    """The dead run's own 16-hex run_id, or None if unreadable/malformed."""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict):
        return None
    run_id = state.get("run_id")
    if isinstance(run_id, str) and _RUN_ID_RE.match(run_id):
        return run_id
    return None


def _tracker_stamps(tracker: Tracker, key: str) -> tuple[str, str]:
    """`(tier, acceptance_invariant)` from the tracker's ticket, best-effort.

    tier is the first `tier:` label verbatim; acceptance_invariant is the first description line
    under the `ACCEPTANCE-INVARIANT:` stem. Both mirror the reflect stage's own capture
    (stage-reflect.md steps 5-6). Any tracker error yields empty strings; the ship event is still
    worth freezing without them.
    """
    try:
        ticket = tracker.get(key)
    except TrackerError:
        return "", ""
    labels = ticket.get("labels") or []
    tier = next((lbl for lbl in labels if isinstance(lbl, str) and lbl.startswith("tier:")), "")
    acceptance_invariant = ""
    for line in (ticket.get("description") or "").splitlines():
        if line.startswith(_ACCEPTANCE_STEM):
            acceptance_invariant = line[len(_ACCEPTANCE_STEM) :].strip()
            break
    return tier, acceptance_invariant


def _read_lane(worktree_root: Path, key: str) -> str:
    """The run's verification lane from `<wt>/.flow/tickets/<key>.md`, else empty."""
    try:
        fm = ticket_frontmatter.read(worktree_root / ".flow" / "tickets" / f"{key}.md")
    except Exception:
        return ""
    lane = fm.get("lane", "")
    return lane if isinstance(lane, str) else ""


def _synthesize_shipped_at(closed_at: object) -> str:
    """A UTC `...Z` seconds-precision shipped_at (observe_ship_event's exact shape).

    Pass an already-Z value through; normalize any other ISO string via parse_iso -> iso_z; absent
    or unparseable -> now.
    """
    if isinstance(closed_at, str) and _SHIPPED_AT_RE.match(closed_at):
        return closed_at
    parsed = _timeutil.parse_iso(closed_at)
    if parsed is not None:
        return _timeutil.iso_z(parsed)
    return _timeutil.utcnow_iso()


# ─── Public API ──────────────────────────────────────────────────────────────


def observe_at_close(
    workspace_root: Path, key: str, worktree: Path | None = None
) -> dict[str, Any]:
    """Observe the ship event for a merged run before its worktree is reaped.

    `workspace_root` is the MAIN root (the store lives there; a doomed worktree missing its
    `.flow/memory-root` sibling would otherwise resolve the store inside itself). `worktree` is the
    doomed worktree root when known.

    Returns `{"action": "observed"|"skipped"|"failed", ...}`, never raises.
    """
    workspace_root = Path(workspace_root)
    try:
        namespace = _memory_paths.resolve_namespace(workspace_root)
        frozen = _memory_paths.ship_event_path(workspace_root, namespace, key)
        if frozen.exists():
            return {"action": "skipped", "reason": "already_observed", "path": str(frozen)}

        config = _read_tracker_config(workspace_root)
        tracker = make_tracker(config)
        ship = tracker.is_shipped(key)
        state = ship.get("state")
        if state != "not_yet_observed":
            return {"action": "skipped", "reason": str(state)}

        run_dir = _resolve_run_dir(workspace_root, key, worktree)
        if run_dir is None:
            return {"action": "skipped", "reason": "no_run_state"}
        state_path = run_dir / "state.json"
        run_id = _read_run_id(state_path)
        if run_id is None:
            return {"action": "skipped", "reason": "no_run_state"}

        tier, acceptance_invariant = _tracker_stamps(tracker, key)
        lane = _read_lane(run_dir.parents[2], key)

        evidence = ship.get("evidence") or {}
        shipped_at = _synthesize_shipped_at(evidence.get("closed_at"))
        payload = {"ticket": key, "shipped_at": shipped_at, "evidence": evidence}

        path, is_dupe = observe_ship_event.observe(
            workspace_root,
            key,
            payload,
            run_id,
            tier=tier,
            acceptance_invariant=acceptance_invariant,
            lane=lane,
            state_path=state_path,
        )
        return {"action": "observed", "path": str(path), "is_dupe": is_dupe}
    except (_WorkspaceConfigError, TrackerError) as exc:
        return {"action": "failed", "reason": f"gate: {exc}"}
    except Exception as exc:
        return {"action": "failed", "reason": f"unexpected: {exc}"}


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Freeze the ship event before a merged run's worktree is reaped."
    )
    parser.add_argument("--workspace-root", required=True, help="the MAIN root (store owner).")
    parser.add_argument("--key", required=True)
    parser.add_argument(
        "--worktree",
        default=None,
        help="the doomed worktree root; omitted -> auto-resolve from the pool.",
    )
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    worktree = Path(args.worktree) if args.worktree else None
    result = observe_at_close(workspace_root, args.key, worktree)
    sys.stdout.write(json.dumps(result) + "\n")
    return 1 if result.get("action") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main", "observe_at_close"]
