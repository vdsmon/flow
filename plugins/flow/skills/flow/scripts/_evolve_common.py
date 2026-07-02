"""Shared helpers for the evolve + queue drain cluster (lib, no CLI).

Definitions duplicated verbatim across evolve_reap / evolve_select /
evolve_drain / evolve_session_cleanup / queue_select live here once: the
tool-call wrappers, the `feat/<key>` branch regex, the bead-label query
sets, the worktree-pool run-dir resolution, and the selector primitives
(in-flight join, ref gather, pre-PR lease scan, BLAST-RADIUS anchor).
"""

from __future__ import annotations

import contextlib
import glob
import json
import re
import subprocess
from pathlib import Path

import fleet
import lease
from _runner import CwdRunner as Runner
from _timeutil import utcnow_iso

# detection accepts both the current `feat/` prefix and the legacy `feature/` so
# branches/PRs opened before the rename stay in-flight through the transition.
FLOW_KEY_RE = re.compile(r"^feat(?:ure)?/(flow-[a-z0-9]+(?:\.\d+)?)(?:-.*)?$", re.IGNORECASE)
BRANCH_PREFIX = "feat/"
BRANCH_PREFIXES = ("feat/", "feature/")
# worktree-dir form (branch `/` becomes `-`); both accepted while legacy dirs survive
WORKTREE_PREFIXES = ("feat-", "feature-")
# a CLOSED or DEFERRED bead is never in flight regardless of a leaked feat/<key>-* branch
ACTIVE_STATUSES = "open,in_progress,blocked"
_BLAST_RE = re.compile(r"^\s*BLAST[ _]RADIUS:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


class NotMaintainer(Exception):
    """Raised when the run is not in maintainer mode. Exit 4."""


class ToolError(Exception):
    """Raised when an injected tool (bd/git/gh) fails. Exit 2."""


def ok(result: subprocess.CompletedProcess[str], what: str) -> str:
    if result.returncode != 0:
        raise ToolError(f"{what} failed: {result.stderr.strip()}")
    return result.stdout or ""


def loads(raw: str) -> list:
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


def key_from_ref(ref: str) -> str | None:
    m = FLOW_KEY_RE.match(ref.removeprefix("origin/"))
    return m.group(1) if m else None


def bead_labels(include_proposals: bool) -> list[str]:
    """The bd label set an evolve query spans (`proposal` only when opted in)."""
    return ["evolve", "proposal"] if include_proposals else ["evolve"]


def primary_anchor(description: str | None) -> str | None:
    """First file path on the bead's `BLAST RADIUS:` line, else None.

    Best-effort: the line is free-text prose written by the audit. A missing or
    unparseable line means this bead carries no anchor and skips anchor-dedup
    serialization.
    """
    m = _BLAST_RE.search(description or "")
    if not m:
        return None
    first = m.group(1).split(",")[0].strip()
    return first or None


def is_inflight(key: str, refs: set[str]) -> bool:
    """A key is in-flight when a branch/PR head is `feat/<key>` or `feat/<key>-*`
    (legacy `feature/` too)."""
    exacts = {f"{p}{key}" for p in BRANCH_PREFIXES}
    pres = tuple(f"{p}{key}-" for p in BRANCH_PREFIXES)
    return any(r in exacts or r.startswith(pres) for r in refs)


def gather_refs(runner: Runner) -> tuple[set[str], set[str]]:
    """Return (in-flight head refs incl. branches, open-PR head refs)."""
    pr_raw = ok(
        runner(["gh", "pr", "list", "--state", "open", "--json", "headRefName", "--limit", "200"]),
        "gh pr list",
    )
    pr_refs = {
        str(p.get("headRefName"))
        for p in loads(pr_raw)
        if isinstance(p, dict) and p.get("headRefName")
    }
    branch_raw = ok(
        runner(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes"]),
        "git for-each-ref",
    )
    branch_refs = {
        line.strip().removeprefix("origin/") for line in branch_raw.splitlines() if line.strip()
    }
    return pr_refs | branch_refs, pr_refs


def live_run_keys(repo: Path) -> set[str]:
    """Ticket keys with a LIVE (unexpired) pre-PR lease in the worktree pool.

    Globs `<repo>/.flow/worktrees/feat-*/.flow/runs/*` (legacy `feature-*` too;
    mirrors run_dir_for's layout) and keeps only run dirs whose lease classifies
    `live`. Live-only by design: an expired/absent lease contributes nothing,
    so an orphan still reads `done`/parked exactly as before.
    """
    base = repo / ".flow" / "worktrees"
    now = utcnow_iso()
    live: set[str] = set()
    for p in WORKTREE_PREFIXES:
        for run_dir in glob.glob(str(base / f"{p}*" / ".flow" / "runs" / "*")):
            key = Path(run_dir).name
            if lease.classify(Path(run_dir), now).get("state") == "live":
                live.add(key)
    return live


def fleet_live_keys(repo: Path) -> set[str]:
    """The reconciled liveness authority (epic flow-8by2.3): a key is live if its
    pre-PR lease is live (live_run_keys) OR the fleet ledger has a fresh heartbeat.

    The lease covers an active run's long stages via its per-stage TTL refresh;
    the fleet heartbeat fires only at stage transitions, so fleet alone would age a
    live long-stage run out (the merge-stage CI re-wait, flow-72d9); reconciling
    against the lease closes that. The fleet adds the launch->init blind window and
    a cross-process snapshot, collapsing the per-site L/M unions to one definition.

    Fail-open: a ledger read error degrades to the lease-only set (the pre-cutover
    behavior), so a fleet fault never breaks the drain. fleet stays additive here;
    launch_ledger still backstops the launch window until child-5 retires it.
    """
    keys = live_run_keys(repo)
    with contextlib.suppress(Exception):
        keys = keys | fleet.live_keys(fleet.resolve_fleet_dir(repo), now=utcnow_iso())
    return keys


def run_dir_for(repo: Path, key: str) -> Path | None:
    """The in-flight run's ticket dir under the worktree pool for `key`.

    Worktrees live at `<repo>/.flow/worktrees/feat-<key>-<slug>/` (legacy
    `feature-<key>-<slug>/` too; see flow_worktree._worktree_path); the run state
    is `.flow/runs/<key>/`. Absent = no lease to read (a leaked branch with no
    worktree, or the common post-reap case), so the caller treats it as non-live
    rather than waiting on it forever.
    """
    base = repo / ".flow" / "worktrees"
    for p in WORKTREE_PREFIXES:
        for wt in sorted(glob.glob(str(base / f"{p}{key}*"))):
            run_dir = Path(wt) / ".flow" / "runs" / key
            if run_dir.exists():
                return run_dir
    return None
