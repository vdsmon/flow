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

Exit codes:
  0 = ok.
  5 = at least one apply record errored (unknown supersede target).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import _memory_paths
import memory_append
import recall
from _jsonl import iter_jsonl

DEFAULT_TYPES = ("DECISION", "FACT")


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
        branch = str(rec.get("branch") or f"feature/{ticket}")
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

    args = parser.parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()

    if args.cmd == "propose":
        types = [t.strip() for t in args.types.split(",") if t.strip()]
        worklist = propose(workspace_root, types)
        sys.stdout.write(json.dumps(worklist, indent=2, sort_keys=True) + "\n")
        return 0

    # apply
    records = _parse_manifest(Path(args.manifest).read_text(encoding="utf-8"))
    summary = apply(workspace_root, records)
    sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 5 if summary["any_error"] else 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["DEFAULT_TYPES", "apply", "cli_main", "propose"]
