"""Select + partition the next batch of evolve beads to launch (the drainer's core).

Pure selection over flow's OWN backlog, no side effects. Given the ready evolve
beads plus the in-flight branches/PRs, decide which keys to fan out as
`/flow <key> --auto` runs. The `/flow evolve --ship` verb consumes this and does
the actual launching.

Partition is best-effort coarse, NOT a disjointness guarantee — planning is
post-launch (the headless Plan subagent runs after `claude --bg` fires), so the
selector never knows a bead's real file set. It serializes on the two signals it
does have (the `hot` label + a primary-file anchor parsed from the bead's BLAST
RADIUS line) and relies on the keystone gate: each run is worktree/lease-isolated,
so any residual file overlap surfaces as a merge conflict at human review —
friction, never corruption. Keep CONCURRENCY low so that stays rare.

Selection inputs (all read-only, queryable):
  - `bd ready -l evolve --json` — open, dependency-unblocked candidates (bd ready
    already excludes blocked beads and carries structured `labels` incl. `hot`).
  - `gh pr list` + `git for-each-ref` — in-flight join by branch name
    `feature/<key>-*` (drop already-running beads; count open PRs for backpressure).

CLI:
  evolve_select.py --workspace-root <dir> [--cap N] [--concurrency N]

Exit codes:
  0 = ok (prints the selection JSON)
  2 = tool error (bd/git/gh failed; stderr propagated)
  4 = not a maintainer setup (dormant; nothing selected)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from _workspace import WorkspaceConfigError, load_workspace_toml
from maintainer import resolve_maintainer_repo

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]

DEFAULT_CAP = 5
DEFAULT_CONCURRENCY = 3
_EVOLVE_STATUSES = "open,in_progress,blocked,deferred,closed"
_BRANCH_PREFIX = "feature/"
_FLOW_KEY_RE = re.compile(r"^feature/(flow-[a-z0-9]+(?:\.\d+)?)(?:-.*)?$", re.IGNORECASE)
_BLAST_RE = re.compile(r"^\s*BLAST[ _]RADIUS:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


class NotMaintainer(Exception):
    """Raised when the run is not in maintainer mode. Exit 4."""


class ToolError(Exception):
    """Raised when an injected tool (bd/git/gh) fails. Exit 2."""


def _default_runner(repo: Path) -> Runner:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=str(repo), capture_output=True, text=True, check=False)

    return run


def _ok(result: subprocess.CompletedProcess[str], what: str) -> str:
    if result.returncode != 0:
        raise ToolError(f"{what} failed: {result.stderr.strip()}")
    return result.stdout or ""


def _loads(raw: str) -> list:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        items = payload.get("issues") or payload.get("prs") or []
        return items if isinstance(items, list) else []
    return []


def primary_anchor(description: str) -> str | None:
    """First file path on the bead's `BLAST RADIUS:` line, else None.

    Best-effort: the line is free-text prose written by the audit. A missing or
    unparseable line just means this bead carries no anchor and falls back to
    hot-only serialization.
    """
    m = _BLAST_RE.search(description or "")
    if not m:
        return None
    first = m.group(1).split(",")[0].strip()
    return first or None


def _key_from_ref(ref: str) -> str | None:
    ref = ref.removeprefix("origin/")
    m = _FLOW_KEY_RE.match(ref)
    return m.group(1) if m else None


def _is_inflight(key: str, refs: set[str]) -> bool:
    """A key is in-flight when a branch/PR head is `feature/<key>` or `feature/<key>-*`."""
    exact = f"{_BRANCH_PREFIX}{key}"
    pre = f"{exact}-"
    return any(r == exact or r.startswith(pre) for r in refs)


def partition(
    candidates: list[dict],
    inflight_keys: set[str],
    hot_inflight: bool,
    open_pr_count: int,
    cap: int = DEFAULT_CAP,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> dict:
    """Pure core: decide the launch batch from already-extracted inputs.

    candidates: parsed `bd ready -l evolve` items (id, priority, labels, issue_type,
    description). Epics are skipped (you launch a run on leaf work, not a container).
    Proposal beads (label `proposal`, filed by `/flow evolve --generative`) are held
    for the maintainer to triage and never auto-launched — the judgment side of the
    vision's auto-vs-propose line.
    """
    held_proposal = [
        c["id"] for c in candidates if c.get("id") and "proposal" in (c.get("labels") or [])
    ]
    skipped_in_flight = [c["id"] for c in candidates if c.get("id") in inflight_keys]

    active = [
        c
        for c in candidates
        if c.get("id")
        and c.get("issue_type") != "epic"
        and "proposal" not in (c.get("labels") or [])
        and c["id"] not in inflight_keys
    ]
    active.sort(key=lambda c: (c.get("priority", 99), str(c.get("id"))))

    if open_pr_count >= cap:
        return {
            "launch": [],
            "skipped_in_flight": skipped_in_flight,
            "held_backpressure": True,
            "held_hot": [],
            "held_anchor": [],
            "held_proposal": held_proposal,
        }

    budget = min(cap - open_pr_count, concurrency)
    launch: list[str] = []
    held_hot: list[str] = []
    held_anchor: list[str] = []
    used_anchors: set[str] = set()
    hot_used = hot_inflight  # a hot PR already open consumes the single hot slot

    for c in active:
        key = c["id"]
        labels = c.get("labels") or []
        is_hot = "hot" in labels
        anchor = primary_anchor(c.get("description", ""))
        if is_hot and hot_used:
            held_hot.append(key)
            continue
        if anchor and anchor in used_anchors:
            held_anchor.append(key)
            continue
        if len(launch) >= budget:
            break
        launch.append(key)
        if is_hot:
            hot_used = True
        if anchor:
            used_anchors.add(anchor)

    return {
        "launch": launch,
        "skipped_in_flight": skipped_in_flight,
        "held_backpressure": False,
        "held_hot": held_hot,
        "held_anchor": held_anchor,
        "held_proposal": held_proposal,
    }


def _gather_refs(runner: Runner) -> tuple[set[str], int]:
    """Return (in-flight head refs, open evolve-PR count)."""
    pr_raw = _ok(
        runner(["gh", "pr", "list", "--state", "open", "--json", "headRefName", "--limit", "200"]),
        "gh pr list",
    )
    pr_refs = {
        str(p.get("headRefName"))
        for p in _loads(pr_raw)
        if isinstance(p, dict) and p.get("headRefName")
    }
    branch_raw = _ok(
        runner(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes"]),
        "git for-each-ref",
    )
    branch_refs = {
        line.strip().removeprefix("origin/") for line in branch_raw.splitlines() if line.strip()
    }
    refs = pr_refs | branch_refs
    open_pr_count = sum(1 for r in pr_refs if r.startswith(f"{_BRANCH_PREFIX}flow-"))
    return refs, open_pr_count


def _hot_inflight(runner: Runner, refs: set[str]) -> bool:
    """True if any in-flight `feature/flow-*` ref maps to a hot evolve bead."""
    inflight_flow_keys = {k for r in refs if (k := _key_from_ref(r))}
    if not inflight_flow_keys:
        return False
    raw = _ok(
        runner(["bd", "list", "-l", "evolve", "--status", _EVOLVE_STATUSES, "--json"]),
        "bd list",
    )
    hot_keys = {
        str(b["id"])
        for b in _loads(raw)
        if isinstance(b, dict) and b.get("id") and "hot" in (b.get("labels") or [])
    }
    return bool(inflight_flow_keys & hot_keys)


def select(
    workspace_root: Path, *, cap: int, concurrency: int, runner: Runner | None = None
) -> dict:
    repo = resolve_maintainer_repo(workspace_root)
    if repo is None:
        raise NotMaintainer("not a flow maintainer setup; nothing to select")
    run = runner or _default_runner(repo)

    candidates = _loads(_ok(run(["bd", "ready", "-l", "evolve", "--json"]), "bd ready"))
    refs, open_pr_count = _gather_refs(run)
    inflight_keys = {c["id"] for c in candidates if c.get("id") and _is_inflight(c["id"], refs)}
    hot_inflight = _hot_inflight(run, refs)

    result = partition(
        candidates, inflight_keys, hot_inflight, open_pr_count, cap=cap, concurrency=concurrency
    )
    result["cap"] = cap
    result["concurrency"] = concurrency
    result["open_pr_count"] = open_pr_count
    return result


def _config_defaults(workspace_root: Path) -> tuple[int, int]:
    try:
        config = load_workspace_toml(workspace_root)
    except WorkspaceConfigError:
        return DEFAULT_CAP, DEFAULT_CONCURRENCY
    section = config.get("evolve")
    if not isinstance(section, dict):
        return DEFAULT_CAP, DEFAULT_CONCURRENCY
    cap = section.get("cap")
    conc = section.get("concurrency")
    return (
        cap if isinstance(cap, int) and cap > 0 else DEFAULT_CAP,
        conc if isinstance(conc, int) and conc > 0 else DEFAULT_CONCURRENCY,
    )


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Select the next batch of evolve beads to launch.")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--cap", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=None)
    args = parser.parse_args(argv)

    ws = Path(args.workspace_root)
    cfg_cap, cfg_conc = _config_defaults(ws)
    cap = args.cap if args.cap is not None else cfg_cap
    concurrency = args.concurrency if args.concurrency is not None else cfg_conc

    try:
        result = select(ws, cap=cap, concurrency=concurrency)
    except NotMaintainer as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
