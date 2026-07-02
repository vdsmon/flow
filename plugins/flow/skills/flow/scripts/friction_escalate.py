"""Propose-only recurrence escalation: file a bead per over-threshold friction class.

Library + thin CLI. Stdlib-only.

Consumes the child-1 detector (`friction_recurrence.analyze`, untouched) and files
ONE deduped `recurrent`-labelled bead per friction class that recurred `>=K` times
after its LATEST claimed MACHINERY fix. The detector's own `post_fix_count` grades
against the EARLIEST fix and over-counts a class with several fix attempts (an
early miss inflates the count even after a later fix held); this module re-grades
each class against `max(fix.ts)` instead, so only a fix that genuinely did not
hold escalates.

Labels are `["recurrent"]` ONLY, never `evolve`: drain candidates come from
`bd ready -l evolve`, so a bead lacking that label is never auto-gated, keeping
this propose-only unconditionally. `bd list -l recurrent` surfaces them to the
maintainer.

Dedup key is the bare anchor (`recurrence-escalation-<anchor>`, no `::`
separator), so only `flow_beads_create`'s exact `evid:` dedup net fires, never
its fuzzy same-file pass: one bead per anchor, ever, across open/closed.

Auto-dormant outside maintainer mode (`flow_beads_create.resolve_maintainer_repo`
returns None): `escalate()` returns immediately with `maintainer: False` and
nothing filed, before any friction/knowledge read.

Exit codes:
  0 = ok (including the dormant no-op).
  3 = OSError.
  4 = `_memory_paths._MemoryConfigError` (missing/invalid workspace.toml).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import _memory_paths
import flow_beads_create
import friction_recurrence
from _runner import Runner
from _workspace import WorkspaceConfigError, load_workspace_toml

DEFAULT_K = 3
DEFAULT_EXEMPT = frozenset({"planned_files"})


def escalation_k(workspace_root: Path) -> int:
    """`[evolve] recurrence_escalation_k` from workspace.toml (int); default 3.

    Only an explicit int (excluding bool, TOML's `true`/`false`) overrides the
    default; an absent key/section/file, a read error, or any other type reads
    as the default.
    """
    try:
        config = load_workspace_toml(workspace_root)
    except WorkspaceConfigError:
        return DEFAULT_K
    section = config.get("evolve")
    if not isinstance(section, dict):
        return DEFAULT_K
    value = section.get("recurrence_escalation_k")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return DEFAULT_K


def exempt_anchors(workspace_root: Path) -> set[str]:
    """`[evolve] recurrence_exempt_anchors` from workspace.toml (list[str]).

    Default `{"planned_files"}`. Only an explicit list overrides it, and it is
    used verbatim (an explicit `[]` means "no exemptions", not "use the
    default"); an absent key/section/file, a read error, or any other type
    reads as the default.
    """
    try:
        config = load_workspace_toml(workspace_root)
    except WorkspaceConfigError:
        return set(DEFAULT_EXEMPT)
    section = config.get("evolve")
    if not isinstance(section, dict):
        return set(DEFAULT_EXEMPT)
    value = section.get("recurrence_exempt_anchors")
    if isinstance(value, list):
        return {v for v in value if isinstance(v, str)}
    return set(DEFAULT_EXEMPT)


def select_escalations(
    analyze_payload: dict[str, Any], k: int, exempt: set[str]
) -> list[dict[str, Any]]:
    """Pure: which `signature_classes` recurred `>=k` times since their LATEST fix.

    Per class: drop it if `anchor` is exempt, or it carries no timestamped fix
    (nothing to grade against). Otherwise grade `recurrences` against
    `max(fix.ts)`, not the class's own (earliest-anchored) `post_fix_count`.
    Returns escalation dicts (`anchor`, `count`, `latest_fix_ts`,
    `latest_fix_sha`, `since_latest`) sorted by descending count.
    """
    out: list[dict[str, Any]] = []
    for cls in analyze_payload.get("signature_classes", []):
        anchor = cls.get("anchor", "")
        if anchor in exempt:
            continue
        fixes = [fx for fx in cls.get("fixes", []) if fx.get("ts")]
        if not fixes:
            continue
        latest_fix = max(fixes, key=lambda fx: fx["ts"])
        latest_fix_ts = latest_fix["ts"]
        since_latest = [r for r in cls.get("recurrences", []) if r.get("ts", "") > latest_fix_ts]
        if len(since_latest) < k:
            continue
        out.append(
            {
                "anchor": anchor,
                "count": len(since_latest),
                "latest_fix_ts": latest_fix_ts,
                "latest_fix_sha": latest_fix.get("fix_sha"),
                "since_latest": since_latest,
            }
        )
    out.sort(key=lambda e: (-e["count"], e["anchor"]))
    return out


def _describe(candidate: dict[str, Any]) -> str:
    since = candidate["since_latest"]
    stages = sorted({r.get("stage", "") for r in since if r.get("stage")})
    types = sorted({r.get("type", "") for r in since if r.get("type")})
    tickets = sorted({r.get("ticket", "") for r in since if r.get("ticket")})
    run_ids = sorted({r.get("run_id", "") for r in since if r.get("run_id")})
    lines = [
        f"Friction class `{candidate['anchor']}` recurred {candidate['count']}x "
        "after its latest claimed fix.",
        f"Latest claimed fix: {candidate['latest_fix_ts']} "
        f"(commit {candidate['latest_fix_sha'] or 'unknown'}).",
        f"Stages hit: {', '.join(stages) or 'n/a'}. Types: {', '.join(types) or 'n/a'}.",
        f"Recurrence run_ids: {', '.join(run_ids) or 'n/a'}.",
        f"Recurrence tickets: {', '.join(tickets) or 'n/a'}.",
        "",
        "Propose-only: informational evidence for the maintainer, never auto-gated"
        " (this bead carries no `evolve` label, so the drain loop never picks it up).",
    ]
    return "\n".join(lines)


def escalate(workspace_root: Path, runner: Runner | None = None) -> dict[str, Any]:
    """File one `recurrent`-labelled bead per over-threshold friction class.

    Dormant outside maintainer mode (checked BEFORE any friction/knowledge read,
    so a normal user run never touches `.flow/<namespace>/*.jsonl` for this).
    """
    result: dict[str, Any] = {
        "maintainer": False,
        "k": 0,
        "exempt": [],
        "candidates": 0,
        "filed": [],
        "deduped": [],
        "errors": [],
    }
    if flow_beads_create.resolve_maintainer_repo(workspace_root) is None:
        return result

    k = escalation_k(workspace_root)
    exempt = exempt_anchors(workspace_root)
    namespace = _memory_paths.resolve_namespace(workspace_root)
    payload = friction_recurrence.analyze(workspace_root, namespace)
    candidates = select_escalations(payload, k, exempt)

    result["maintainer"] = True
    result["k"] = k
    result["exempt"] = sorted(exempt)
    result["candidates"] = len(candidates)

    for candidate in candidates:
        anchor = candidate["anchor"]
        dedup_key = f"recurrence-escalation-{anchor}"
        summary = (
            f"recurrence escalation: `{anchor}` recurred {candidate['count']}x "
            "after its latest claimed fix"
        )
        try:
            key = flow_beads_create.create_bead(
                workspace_root,
                summary,
                _describe(candidate),
                type="task",
                labels=["recurrent"],
                dedup_key=dedup_key,
                runner=runner,
            )
            result["filed"].append({"anchor": anchor, "key": key, "count": candidate["count"]})
        except flow_beads_create.DuplicateBead as exc:
            result["deduped"].append({"anchor": anchor, "existing_key": exc.existing_key})
        except flow_beads_create.BeadCreateError as exc:
            result["errors"].append({"anchor": anchor, "error": str(exc)})
    return result


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Propose-only recurrence escalation: file a bead per over-threshold "
        "friction class."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_escalate = sub.add_parser("escalate")
    p_escalate.add_argument("--workspace-root", default=".")
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    try:
        result = escalate(workspace_root)
    except _memory_paths._MemoryConfigError as exc:
        sys.stderr.write(f"friction-escalate: {exc}\n")
        return 4
    except OSError as exc:
        sys.stderr.write(f"friction-escalate: I/O error: {exc}\n")
        return 3
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "DEFAULT_EXEMPT",
    "DEFAULT_K",
    "cli_main",
    "escalate",
    "escalation_k",
    "exempt_anchors",
    "select_escalations",
]
