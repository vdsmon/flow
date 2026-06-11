"""Select + partition the next batch of evolve beads to launch (drain's select core).

Pure selection over flow's OWN backlog, no side effects. Given the ready evolve
beads plus the in-flight branches/PRs, decide which keys to fan out as
`/flow <key> --auto` runs. The `/flow evolve drain` loop consumes this (via
`evolve_drain.py`, which adds in-flight lease liveness) and does the launching.

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
import sys
from pathlib import Path

import launch_ledger
from _evolve_common import ACTIVE_STATUSES as _ACTIVE_STATUSES
from _evolve_common import BRANCH_PREFIX as _BRANCH_PREFIX
from _evolve_common import NotMaintainer, ToolError, bead_labels, primary_anchor
from _evolve_common import gather_refs as _gather_refs_common
from _evolve_common import is_inflight as _is_inflight
from _evolve_common import key_from_ref as _key_from_ref
from _evolve_common import live_run_keys as _live_run_keys
from _evolve_common import loads as _loads
from _evolve_common import ok as _ok
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from _workspace import WorkspaceConfigError, load_workspace_toml
from maintainer import resolve_maintainer_repo

DEFAULT_CAP = 5
DEFAULT_CONCURRENCY = 3


def partition(
    candidates: list[dict],
    inflight_keys: set[str],
    hot_inflight: bool,
    open_pr_count: int,
    cap: int = DEFAULT_CAP,
    concurrency: int = DEFAULT_CONCURRENCY,
    include_proposals: bool = False,
) -> dict:
    """Pure core: decide the launch batch from already-extracted inputs.

    candidates: parsed `bd ready -l evolve` items (id, priority, labels, issue_type,
    description). Epics are skipped (you launch a run on leaf work, not a container).
    Generative proposals now live in a separate (non-`evolve`) backlog and only
    reach drain at all if mislabeled; the `proposal`-exclusion filter in `active`
    is retained as a defensive guard (judgment work never auto-launches), no
    longer surfaced as `held_proposal`.

    `include_proposals` is the DANGEROUS opt-in: it drops the proposal-exclusion
    guard so judgment beads auto-launch alongside audit work, bypassing the human
    spec-plan accept gate. The caller also has to feed the proposal candidates in
    (see `select`); flipping this alone over an evolve-only candidate set is a no-op.
    """
    skipped_in_flight = [c["id"] for c in candidates if c.get("id") in inflight_keys]

    active = [
        c
        for c in candidates
        if c.get("id")
        and c.get("issue_type") != "epic"
        and (include_proposals or "proposal" not in (c.get("labels") or []))
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
    }


def _gather_refs(runner: Runner) -> tuple[set[str], set[str], int]:
    """Return (in-flight head refs, open-PR head refs, GLOBAL open flow-PR count)."""
    refs, pr_refs = _gather_refs_common(runner)
    open_pr_count = sum(1 for r in pr_refs if r.startswith(f"{_BRANCH_PREFIX}flow-"))
    return refs, pr_refs, open_pr_count


def _hot_inflight(
    runner: Runner,
    refs: set[str],
    *,
    include_proposals: bool = False,
    extra_keys: frozenset[str] | set[str] = frozenset(),
) -> bool:
    """True if any in-flight `feature/flow-*` ref maps to a hot evolve bead.

    Under `include_proposals` the hot slot also serializes hot *proposal* beads, so
    a hot proposal already in flight blocks the next hot launch (the `proposal`
    label can carry `hot` too — see references/verb-evolve.md §propose).

    `extra_keys` seeds the in-flight set with keys known live by another channel
    (e.g. a pre-PR lease that has no ref/PR yet), so a hot pre-PR run blocks the
    next hot launch.
    """
    inflight_flow_keys = {k for r in refs if (k := _key_from_ref(r))} | set(extra_keys)
    if not inflight_flow_keys:
        return False
    labels = bead_labels(include_proposals)
    hot_keys: set[str] = set()
    for label in labels:
        raw = _ok(
            runner(["bd", "list", "-l", label, "--status", _ACTIVE_STATUSES, "--json"]),
            "bd list",
        )
        hot_keys |= {
            str(b["id"])
            for b in _loads(raw)
            if isinstance(b, dict) and b.get("id") and "hot" in (b.get("labels") or [])
        }
    return bool(inflight_flow_keys & hot_keys)


def _ready_candidates(run: Runner, include_proposals: bool) -> list[dict]:
    """Ready evolve beads, plus the `proposal` backlog when explicitly opted in.

    Two label-scoped `bd ready` calls merged by id (not `-l evolve,proposal`, whose
    AND/OR semantics are ambiguous); a bead carrying both labels is kept once.
    """
    cands = _loads(_ok(run(["bd", "ready", "-l", "evolve", "--json"]), "bd ready"))
    if include_proposals:
        seen = {c.get("id") for c in cands}
        props = _loads(_ok(run(["bd", "ready", "-l", "proposal", "--json"]), "bd ready"))
        cands += [p for p in props if p.get("id") and p.get("id") not in seen]
    return cands


def select(
    workspace_root: Path,
    *,
    cap: int,
    concurrency: int,
    runner: Runner | None = None,
    include_proposals: bool = False,
) -> dict:
    repo = resolve_maintainer_repo(workspace_root)
    if repo is None:
        raise NotMaintainer("not a flow maintainer setup; nothing to select")
    run = runner or _default_runner(repo)

    candidates = _ready_candidates(run, include_proposals)
    refs, pr_refs, open_pr_count = _gather_refs(run)
    live_keys = _live_run_keys(repo)
    launched_keys = launch_ledger.live_keys(repo)  # pre-init launch->init window
    inflight_pre = live_keys | launched_keys
    inflight_keys = {
        c["id"] for c in candidates if c.get("id") and _is_inflight(c["id"], refs)
    } | inflight_pre
    hot_inflight = _hot_inflight(
        run, refs, include_proposals=include_proposals, extra_keys=inflight_pre
    )

    result = partition(
        candidates,
        inflight_keys,
        hot_inflight,
        open_pr_count,
        cap=cap,
        concurrency=concurrency,
        include_proposals=include_proposals,
    )
    result["cap"] = cap
    result["concurrency"] = concurrency
    result["open_pr_count"] = open_pr_count
    # the flow keys behind the open PRs, so evolve_drain reuses this gather
    # instead of re-running `gh pr list`
    result["open_pr_keys"] = sorted({k for r in pr_refs if (k := _key_from_ref(r))})
    result["include_proposals"] = include_proposals
    result["live_runs"] = sorted(live_keys)
    result["launched_pending"] = sorted(launched_keys)
    labels_by_id = {c["id"]: (c.get("labels") or []) for c in candidates if c.get("id")}
    worker_model = _worker_model(workspace_root)
    model_per_key: dict[str, str] = {}
    for key in result["launch"]:
        labels = labels_by_id.get(key, [])
        if "hot" in labels:
            continue
        if "tier:trivial" in labels or "tier:light" in labels:
            model_per_key[key] = "sonnet"
        elif worker_model:
            model_per_key[key] = worker_model
    result["model_per_key"] = model_per_key
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


def _worker_model(workspace_root: Path) -> str | None:
    try:
        config = load_workspace_toml(workspace_root)
    except WorkspaceConfigError:
        return None
    section = config.get("evolve")
    if not isinstance(section, dict):
        return None
    val = section.get("worker_model")
    return val if isinstance(val, str) and val else None


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
