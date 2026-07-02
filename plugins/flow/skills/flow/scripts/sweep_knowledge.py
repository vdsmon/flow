"""Maintainer-gated retro-curation sweep over `.flow/<namespace>/knowledge.jsonl`.

PROPOSE-ONLY: this engine NEVER auto-decides supersession. `propose` emits a
read-only worklist of curatable DECISION/FACT entries; a maintainer (or an agent
under maintainer supervision) cross-checks each against current code + merged PRs
and authors a manifest of confirmed supersessions; `apply` then writes one
append-only tombstone record per confirmed entry through the `memory_append`
seam (`--supersedes`). The standing producer for future rot is the reflect stage
(flow-ufvu.2); this is one-shot backlog cleanup.

`propose` (read-only): worklist of non-superseded entries of the given types.
`apply --manifest <file>`: append a superseding tombstone per manifest record;
idempotent (a record whose target is already dead is skipped), and refuses an
unknown target id (the record errors; the batch continues; the run exits
non-zero if any record errored).

Consolidation lane (density, not just staleness): `cluster` deterministically
groups live, same-type, sidecar-indexed entries into near-duplicate clusters by
complete-linkage cosine (over the `memory_embed` index; read-only, never writes).
A maintainer authors ONE canonical body per confirmed cluster into a manifest;
`apply-cluster --manifest <file>` then collapses each cluster to a single live
entry via a list-valued `memory_append --supersedes`, mirroring `apply`'s
idempotency + unknown-id discipline.

Exit codes:
  0 = ok.
  5 = at least one apply / apply-cluster record errored (unknown supersede target).
"""

from __future__ import annotations

import argparse
import json
import sys
from itertools import combinations
from pathlib import Path
from typing import Any

import _memory_paths
import memory_append
import memory_embed
import recall
from _evolve_common import BRANCH_PREFIX as _BRANCH_PREFIX
from _jsonl import iter_jsonl

DEFAULT_TYPES = ("DECISION", "FACT")
# Calibrated against the live corpus (353 vectors, flow-ro3w): 0.93+ surfaces zero
# groups (this corpus's entries are deliberately distinct, not verbatim dupes);
# 0.90 surfaces 4 tight pairs, each a later entry refining/extending an earlier one
# on the exact same narrow mechanism (a genuine consolidation candidate); 0.87
# starts admitting merely-related-not-redundant pairs (two different facts about
# the same PR). 0.90 is the floor that still surfaces real candidates without
# proposing junk a maintainer would reject.
DEFAULT_CLUSTER_THRESHOLD = 0.90


def _ts_token() -> str:
    return memory_append._ts_token()


def _load_entries(workspace_root: Path) -> list[dict[str, Any]]:
    namespace = _memory_paths.resolve_namespace(workspace_root)
    kpath = _memory_paths.knowledge_path(workspace_root, namespace)
    if not kpath.exists():
        return []
    sidecar = kpath.with_name(f"{kpath.name}.quarantine.{_ts_token()}")
    return list(iter_jsonl(kpath, sidecar))


def propose(workspace_root: Path, types: list[str]) -> list[dict[str, Any]]:
    """Read-only worklist of non-superseded entries matching `types`, in file order."""
    entries = recall.filter_superseded(_load_entries(workspace_root))
    type_set = set(types)
    return [
        {
            "id": e.get("id"),
            "ticket": e.get("ticket"),
            "ts": e.get("ts"),
            "type": e.get("type"),
            "body": e.get("body"),
        }
        for e in entries
        if e.get("type") in type_set
    ]


def _parse_manifest(text: str) -> list[dict[str, Any]]:
    """Tolerant parse: a JSON array, else JSONL (one object per non-blank line)."""
    stripped = text.strip()
    if stripped:
        try:
            whole = json.loads(stripped)
        except json.JSONDecodeError:
            whole = None
        if isinstance(whole, list):
            return [r for r in whole if isinstance(r, dict)]
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if isinstance(rec, dict):
            records.append(rec)
    return records


def apply(workspace_root: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply each manifest record. Returns a per-record results summary."""
    dead = recall.superseded_ids(_load_entries(workspace_root))
    results: list[dict[str, Any]] = []
    any_error = False
    for rec in records:
        superseded_id = str(rec.get("superseded_id", ""))
        ticket = str(rec.get("superseding_ticket", ""))
        rationale = str(rec.get("rationale", ""))
        branch = str(rec.get("branch") or f"{_BRANCH_PREFIX}{ticket}")
        # memory_append treats a falsy supersedes as "no supersede" and appends
        # normally, so an empty target must be rejected here or the record
        # writes a non-superseding junk entry and reports applied.
        if not superseded_id:
            any_error = True
            results.append(
                {
                    "superseded_id": superseded_id,
                    "result": "error",
                    "detail": "empty superseded_id",
                }
            )
            continue
        if superseded_id in dead:
            results.append({"superseded_id": superseded_id, "result": "skipped"})
            continue
        try:
            entry = memory_append.append(
                workspace_root,
                type_="DECISION",
                body=rationale,
                branch=branch,
                ticket=ticket,
                supersedes=superseded_id,
            )
        except memory_append._UnknownSupersedeTarget:
            any_error = True
            results.append(
                {
                    "superseded_id": superseded_id,
                    "result": "error",
                    "detail": "unknown supersede target",
                }
            )
            continue
        dead.add(superseded_id)
        results.append({"superseded_id": superseded_id, "new_id": entry["id"], "result": "applied"})
    return {"results": results, "any_error": any_error}


def cluster(
    workspace_root: Path,
    types: list[str],
    threshold: float = DEFAULT_CLUSTER_THRESHOLD,
) -> list[dict[str, Any]]:
    """Deterministic complete-linkage clustering of live, same-type, indexed entries.

    Read-only, never writes. An entry without a sidecar vector never seeds or
    joins a group; a missing/empty sidecar yields `[]`. Clusters WITHIN a type
    only (`types` processed in the given order; never mixes e.g. DECISION with
    FACT). Complete linkage: within one type, entries are walked in file order;
    each unclustered entry seeds a group, then a later unclustered entry joins
    ONLY if its cosine similarity is >= `threshold` against EVERY current member
    (not just the seed) — this rejects an A~B, B~C, A!~C chain ever landing in one
    group. Only groups of size >= 2 are emitted, sorted by `cluster_id`.
    """
    namespace = _memory_paths.resolve_namespace(workspace_root)
    _, indexed = memory_embed.load_index(workspace_root, namespace)
    if not indexed:
        return []
    entries = recall.filter_superseded(_load_entries(workspace_root))

    groups: list[dict[str, Any]] = []
    for type_ in types:
        pool = [e for e in entries if e.get("type") == type_ and e.get("id") in indexed]
        clustered: set[str] = set()
        for i, seed in enumerate(pool):
            seed_id = seed["id"]
            if seed_id in clustered:
                continue
            members = [seed]
            member_ids = {seed_id}
            for cand in pool[i + 1 :]:
                cand_id = cand["id"]
                if cand_id in clustered:
                    continue
                cand_vec = indexed[cand_id]
                if all(recall._cosine(cand_vec, indexed[m_id]) >= threshold for m_id in member_ids):
                    members.append(cand)
                    member_ids.add(cand_id)
            if len(members) < 2:
                continue
            clustered.update(member_ids)
            min_cosine = min(
                recall._cosine(indexed[a], indexed[b]) for a, b in combinations(member_ids, 2)
            )
            groups.append(
                {
                    "cluster_id": seed_id,
                    "type": type_,
                    "min_cosine": round(min_cosine, 6),
                    "members": [
                        {
                            "id": m.get("id"),
                            "ticket": m.get("ticket"),
                            "ts": m.get("ts"),
                            "type": m.get("type"),
                            "body": m.get("body"),
                        }
                        for m in members
                    ],
                }
            )
    groups.sort(key=lambda g: g["cluster_id"])
    return groups


def apply_cluster(workspace_root: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply each confirmed cluster-consolidation record. Returns a results summary.

    Each record collapses `member_ids` to ONE canonical live entry via a
    list-valued `memory_append --supersedes`. Idempotent: a record whose members
    are ALL already dead is skipped (a member id superseded individually in the
    meantime is not re-litigated here). Refuses an unknown member id (the record
    errors; the batch continues; the run exits non-zero if any record errored).
    """
    dead = recall.superseded_ids(_load_entries(workspace_root))
    results: list[dict[str, Any]] = []
    any_error = False
    for rec in records:
        member_ids = [str(m) for m in rec.get("member_ids", [])]
        ticket = str(rec.get("canonical_ticket", ""))
        body = str(rec.get("canonical_body", ""))
        type_ = str(rec.get("type") or "DECISION")
        branch = str(rec.get("branch") or f"{_BRANCH_PREFIX}{ticket}")
        # Same guard as apply(): supersedes=[] appends a normal entry, so a
        # wholly-empty member list would inject a canonical that merges nothing.
        if not member_ids:
            any_error = True
            results.append(
                {"member_ids": member_ids, "result": "error", "detail": "empty member_ids"}
            )
            continue
        if all(m in dead for m in member_ids):
            results.append({"member_ids": member_ids, "result": "skipped"})
            continue
        try:
            entry = memory_append.append(
                workspace_root,
                type_=type_,
                body=body,
                branch=branch,
                ticket=ticket,
                supersedes=member_ids,
            )
        except memory_append._UnknownSupersedeTarget as exc:
            any_error = True
            results.append(
                {
                    "member_ids": member_ids,
                    "result": "error",
                    "detail": f"unknown supersede target: {exc}",
                }
            )
            continue
        except memory_append._DuplicateId:
            results.append({"member_ids": member_ids, "result": "skipped"})
            continue
        dead.update(member_ids)
        results.append(
            {
                "member_ids": member_ids,
                "new_id": entry["id"],
                "merged": len(member_ids),
                "result": "applied",
            }
        )
    return {"results": results, "any_error": any_error}


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Maintainer-gated retro-curation sweep over knowledge.jsonl (propose-only)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_propose = sub.add_parser("propose", help="emit a read-only curation worklist")
    p_propose.add_argument(
        "--type",
        dest="types",
        default=",".join(DEFAULT_TYPES),
        help="comma-separated entry types to include (default DECISION,FACT).",
    )
    p_propose.add_argument("--workspace-root", default=".")

    p_apply = sub.add_parser("apply", help="apply a confirmed-supersession manifest")
    p_apply.add_argument("--manifest", required=True)
    p_apply.add_argument("--workspace-root", default=".")

    p_cluster = sub.add_parser(
        "cluster",
        help="deterministic near-duplicate grouping over indexed live entries (read-only)",
    )
    p_cluster.add_argument(
        "--type",
        dest="types",
        default=",".join(DEFAULT_TYPES),
        help="comma-separated entry types to cluster within (default DECISION,FACT).",
    )
    p_cluster.add_argument("--threshold", type=float, default=DEFAULT_CLUSTER_THRESHOLD)
    p_cluster.add_argument("--workspace-root", default=".")

    p_apply_cluster = sub.add_parser(
        "apply-cluster", help="apply a confirmed cluster-consolidation manifest"
    )
    p_apply_cluster.add_argument("--manifest", required=True)
    p_apply_cluster.add_argument("--workspace-root", default=".")

    args = parser.parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()

    if args.cmd == "propose":
        types = [t.strip() for t in args.types.split(",") if t.strip()]
        worklist = propose(workspace_root, types)
        sys.stdout.write(json.dumps(worklist, indent=2, sort_keys=True) + "\n")
        return 0

    if args.cmd == "cluster":
        types = [t.strip() for t in args.types.split(",") if t.strip()]
        groups = cluster(workspace_root, types, threshold=args.threshold)
        sys.stdout.write(json.dumps(groups, indent=2, sort_keys=True) + "\n")
        return 0

    if args.cmd == "apply-cluster":
        records = _parse_manifest(Path(args.manifest).read_text(encoding="utf-8"))
        summary = apply_cluster(workspace_root, records)
        sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        return 5 if summary["any_error"] else 0

    # apply
    records = _parse_manifest(Path(args.manifest).read_text(encoding="utf-8"))
    summary = apply(workspace_root, records)
    sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 5 if summary["any_error"] else 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "DEFAULT_CLUSTER_THRESHOLD",
    "DEFAULT_TYPES",
    "apply",
    "apply_cluster",
    "cli_main",
    "cluster",
    "propose",
]
