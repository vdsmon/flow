"""TTL launch ledger — bridges the launch→init blind window for the drain selector.

A run launched by `claude --bg "/flow <key> --auto"` is invisible to BOTH of
`evolve_select`'s in-flight detectors during its plan+bootstrap phase: it has no
branch/PR ref yet (`_gather_refs`) and no pre-PR lease yet (`_live_run_keys`) —
minutes pass before either registers. In that window the selector re-emits the
just-launched key AND can offer a SECOND concurrent hot bead, breaking the
one-hot-per-pass isolation invariant.

This ledger closes the window: the drain orchestrator writes a per-key marker at
`claude --bg` time, and the selector reads it as a third in-flight channel until
the run's real lease/branch registers or the marker self-expires (TTL). The
markers live in the MAIN checkout's `.flow/launch-ledger/` (resolved via
`resolve_maintainer_repo`), shared between the orchestrator and the selector.

CLI:
  launch_ledger.py add  --key <K> --workspace-root <dir>   # record a launch
  launch_ledger.py prune --workspace-root <dir>            # drop expired markers
  launch_ledger.py list  --workspace-root <dir> [--json]   # print live keys

Exit codes:
  0 = ok
  4 = not a maintainer setup (dormant; nothing to do)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import lease
from maintainer import resolve_maintainer_repo

# bounds the launch→init window: a dead launch's marker self-expires after this,
# so the key becomes re-launchable rather than stuck forever in-flight.
LAUNCH_TTL_SECONDS = 1800


class NotMaintainer(Exception):
    """Raised when the run is not in maintainer mode. Exit 4."""


def _ledger_dir(repo: Path) -> Path:
    return repo / ".flow" / "launch-ledger"


def _age_seconds(now: str, ts: str) -> float | None:
    now_dt = lease.parse_iso(now)
    ts_dt = lease.parse_iso(ts)
    if now_dt is None or ts_dt is None:
        return None
    return (now_dt - ts_dt).total_seconds()


def add(repo: Path, key: str, *, now: str | None = None) -> None:
    """Write the launch marker for `key` (mkdir -p the ledger dir)."""
    now = now or lease._utcnow_iso()
    d = _ledger_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    (d / key).write_text(now, encoding="utf-8")


def live_keys(repo: Path, *, now: str | None = None) -> set[str]:
    """Keys with a non-expired launch marker (age < TTL).

    Robust to a missing ledger dir (returns empty) and to an unparseable/empty
    marker (skipped — never counted live).
    """
    now = now or lease._utcnow_iso()
    d = _ledger_dir(repo)
    live: set[str] = set()
    for marker in d.glob("*"):
        if not marker.is_file():
            continue
        try:
            ts = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        age = _age_seconds(now, ts)
        if age is not None and age < LAUNCH_TTL_SECONDS:
            live.add(marker.name)
    return live


def prune(repo: Path, *, now: str | None = None) -> list[str]:
    """Delete expired marker files. Returns the pruned key list (for reporting)."""
    now = now or lease._utcnow_iso()
    d = _ledger_dir(repo)
    pruned: list[str] = []
    for marker in d.glob("*"):
        if not marker.is_file():
            continue
        try:
            ts = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        age = _age_seconds(now, ts)
        if age is not None and age >= LAUNCH_TTL_SECONDS:
            marker.unlink(missing_ok=True)
            pruned.append(marker.name)
    return pruned


def _resolve(workspace_root: Path) -> Path:
    repo = resolve_maintainer_repo(workspace_root)
    if repo is None:
        raise NotMaintainer("not a flow maintainer setup; no launch ledger")
    return repo


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="TTL launch ledger for the evolve drain selector.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="record a launch marker for a key")
    p_add.add_argument("--key", required=True)
    p_add.add_argument("--workspace-root", default=".")

    p_prune = sub.add_parser("prune", help="delete expired markers")
    p_prune.add_argument("--workspace-root", default=".")

    p_list = sub.add_parser("list", help="print the live (non-expired) keys")
    p_list.add_argument("--workspace-root", default=".")
    p_list.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    try:
        repo = _resolve(Path(args.workspace_root))
    except NotMaintainer as exc:
        print(str(exc), file=sys.stderr)
        return 4

    if args.cmd == "add":
        add(repo, args.key)
        print(args.key)
        return 0
    if args.cmd == "prune":
        pruned = prune(repo)
        print(json.dumps(sorted(pruned)) if args.json else "\n".join(sorted(pruned)))
        return 0
    # list
    keys = sorted(live_keys(repo))
    print(json.dumps(keys) if args.json else "\n".join(keys))
    return 0


__all__ = ["LAUNCH_TTL_SECONDS", "NotMaintainer", "add", "cli_main", "live_keys", "prune"]


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
