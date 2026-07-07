"""Detect friction that recurred after a claimed MACHINERY fix.

Library + thin CLI. Stdlib-only, read-only.

Forward-joins `.flow/<namespace>/friction.jsonl` to MACHINERY-prefixed entries
in `.flow/<namespace>/knowledge.jsonl` (a MACHINERY entry is a knowledge entry
whose body starts with the literal "MACHINERY"; there is no MACHINERY type
value). Clusters the recurring friction two ways: `signature_classes` (a
single distinctive anchor token, cross-cutting stage/type) and
`structural_classes` (`(stage, type, anchor)`). Namespace resolved via
`_memory_paths.resolve_namespace`.

Pure function of the two files (no wall clock). Output carries evidence
(entry ids, run ids, counts, fix sha) for a downstream judge; this module
reports, it does not adjudicate.

Signature classes are single-anchor facets, not fully-unified root-cause
clusters: MACHINERY bodies are verbose enough that a union-find over anchors
chains everything into one blob (tried, proven bad here). Cross-anchor
unification is left to the downstream judge, aided by each class's
`related_anchors`. This shape is on-thesis for oe9.2/oe9.3/oe9.4, which
inherit it.

Exit codes:
  0 = ok.
  3 = OSError.
  4 = `_memory_paths._MemoryConfigError` (missing/invalid workspace.toml).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import _memory_paths
from _jsonl import read_jsonl_lenient

DF_LO = 2
DF_HI = 15
MACHINERY_PREFIX = "MACHINERY"

_FILE_ANCHOR_RE = re.compile(r"[a-z0-9_]+\.(?:py|md|toml)")
_SNAKE_RE = re.compile(r"[a-z]+(?:_[a-z]+)+")
_INLINE_SHA_RE = re.compile(r"commit ([0-9a-f]{7,40})")
_AT_U_TOKEN = "@{u}"

_Anchored = tuple[dict[str, Any], set[str]]


def anchors(text: str) -> set[str]:
    """Distinctive-anchor tokens in one entry's text (lowercased, deduped).

    Flags (`--foo`) and hyphen-kebab tokens are deliberately not captured:
    flags bridge everything into one blob, kebab grabs unrelated prose
    compounds. Both were tried and failed.
    """
    lowered = text.lower()
    file_anchors = set(_FILE_ANCHOR_RE.findall(lowered))
    snake_tokens = {
        tok for tok in _SNAKE_RE.findall(lowered) if not any(tok in fa for fa in file_anchors)
    }
    found = file_anchors | snake_tokens
    if _AT_U_TOKEN in lowered:
        found.add(_AT_U_TOKEN)
    return found


def _str_field(entry: dict[str, Any], key: str) -> str:
    value = entry.get(key)
    return value if isinstance(value, str) else ""


def entry_anchors(entry: dict[str, Any]) -> set[str]:
    """Anchor tokens from a knowledge/friction entry's `body` + `detail` text."""
    return anchors(f"{_str_field(entry, 'body')} {_str_field(entry, 'detail')}")


def _machinery_entries(knowledge: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in knowledge if _str_field(e, "body").startswith(MACHINERY_PREFIX)]


def document_frequencies(anchor_sets: list[set[str]]) -> dict[str, int]:
    df: dict[str, int] = {}
    for anchor_set in anchor_sets:
        for tok in anchor_set:
            df[tok] = df.get(tok, 0) + 1
    return df


def distinctive_anchors(
    df: dict[str, int], exempt: set[str] | frozenset[str] = frozenset()
) -> set[str]:
    """DF band [DF_LO, DF_HI]; anchors in `exempt` skip the ceiling.

    A fix-claimed anchor (one appearing in a MACHINERY entry) is the tracked
    object itself: high document frequency there is recurrence evidence, not
    ubiquitous noise, and a raw ceiling would drop the hottest recurring class
    from the report the moment its DF crossed DF_HI. Only the rarity floor
    applies to exempt anchors.
    """
    return {
        tok for tok, count in df.items() if count >= DF_LO and (count <= DF_HI or tok in exempt)
    }


def fix_sha(entry: dict[str, Any], workspace_root: Path, namespace: str) -> str | None:
    """MACHINERY entry's evidenced fix commit.

    Inline `commit <sha>` first, else the ticket's ship-event
    `evidence.commit_sha` (only 4/83 live bodies carry an inline sha; the
    ship-event fallback is what makes most fixes traceable).
    """
    match = _INLINE_SHA_RE.search(_str_field(entry, "body"))
    if match:
        return match.group(1)
    path = _memory_paths.ship_event_path(workspace_root, namespace, entry.get("ticket", ""))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    evidence = data.get("evidence")
    if not isinstance(evidence, dict):
        return None
    sha = evidence.get("commit_sha")
    return sha if isinstance(sha, str) else None


def _fix_dicts(
    fixes_entries: list[dict[str, Any]], workspace_root: Path, namespace: str
) -> list[dict[str, Any]]:
    return sorted(
        (
            {
                "id": m.get("id", ""),
                "ticket": m.get("ticket", ""),
                "ts": m.get("ts", ""),
                "fix_sha": fix_sha(m, workspace_root, namespace),
            }
            for m in fixes_entries
        ),
        key=lambda x: (x["ts"], x["id"]),
    )


def recurrence_dicts(friction_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (
            {
                "id": f.get("id", ""),
                "run_id": f.get("run_id", ""),
                "ticket": f.get("ticket", ""),
                "ts": f.get("ts", ""),
                "stage": f.get("stage", ""),
                "type": f.get("type", ""),
            }
            for f in friction_entries
        ),
        key=lambda x: (x["ts"], x["id"]),
    )


def signature_classes(
    friction: list[_Anchored],
    machinery: list[_Anchored],
    distinct: set[str],
    workspace_root: Path,
    namespace: str,
) -> list[dict[str, Any]]:
    """Key (b): one class per distinctive anchor, cross-cutting stage/type."""
    classes: list[dict[str, Any]] = []
    for anchor in distinct:
        fixes_entries = [m for m, ma in machinery if anchor in ma]
        friction_hits = [(f, fa) for f, fa in friction if anchor in fa]
        if not fixes_entries or not friction_hits:
            continue
        # ts is Z-suffixed UTC ms ISO8601; lexicographic compare == chronological.
        # A ts-less fix cannot anchor a forward join; an empty min would flag
        # every hit as post-fix.
        fix_ts_values = [m["ts"] for m in fixes_entries if isinstance(m.get("ts"), str) and m["ts"]]
        if not fix_ts_values:
            continue
        earliest_fix_ts = min(fix_ts_values)
        post_fix = [(f, fa) for f, fa in friction_hits if f.get("ts", "") > earliest_fix_ts]
        if not post_fix:
            continue
        recurrences = recurrence_dicts([f for f, _ in post_fix])
        related = sorted({tok for _, fa in post_fix for tok in fa if tok != anchor})
        classes.append(
            {
                "cluster_key": "signature",
                "anchor": anchor,
                "fixes": _fix_dicts(fixes_entries, workspace_root, namespace),
                "earliest_fix_ts": earliest_fix_ts,
                "class_size": len(friction_hits),
                "post_fix_count": len(post_fix),
                "recurrences": recurrences,
                "stages": sorted({r["stage"] for r in recurrences}),
                "types": sorted({r["type"] for r in recurrences}),
                "related_anchors": related,
            }
        )
    classes.sort(key=lambda c: (-c["post_fix_count"], c["cluster_key"], c["anchor"]))
    return classes


def structural_classes(
    friction: list[_Anchored],
    machinery: list[_Anchored],
    distinct: set[str],
    workspace_root: Path,
    namespace: str,
) -> list[dict[str, Any]]:
    """Key (a): one class per (stage, type, anchor) bucket.

    Structurally blind to a recurrence that crosses stage/type, unlike
    `signature_classes`.
    """
    classes: list[dict[str, Any]] = []
    for anchor in distinct:
        fixes_entries = [m for m, ma in machinery if anchor in ma]
        friction_hits = [f for f, fa in friction if anchor in fa]
        if not fixes_entries or not friction_hits:
            continue
        fix_ts_values = [m["ts"] for m in fixes_entries if isinstance(m.get("ts"), str) and m["ts"]]
        if not fix_ts_values:
            continue
        earliest_fix_ts = min(fix_ts_values)
        buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for f in friction_hits:
            buckets.setdefault((f.get("stage", ""), f.get("type", "")), []).append(f)
        for (stage, type_), bucket in buckets.items():
            post_fix = [f for f in bucket if f.get("ts", "") > earliest_fix_ts]
            if not post_fix:
                continue
            classes.append(
                {
                    "cluster_key": "structural",
                    "anchor": anchor,
                    "stage": stage,
                    "type": type_,
                    "fixes": _fix_dicts(fixes_entries, workspace_root, namespace),
                    "earliest_fix_ts": earliest_fix_ts,
                    "post_fix_count": len(post_fix),
                    "recurrences": recurrence_dicts(post_fix),
                }
            )
    classes.sort(
        key=lambda c: (-c["post_fix_count"], c["cluster_key"], c["anchor"], c["stage"], c["type"])
    )
    return classes


def analyze(workspace_root: Path, namespace: str) -> dict[str, Any]:
    """Pure read of the two jsonl files (no wall clock)."""
    friction = read_jsonl_lenient(_memory_paths.friction_path(workspace_root, namespace))
    knowledge = read_jsonl_lenient(_memory_paths.knowledge_path(workspace_root, namespace))
    machinery = _machinery_entries(knowledge)

    friction_anchored: list[_Anchored] = [(f, entry_anchors(f)) for f in friction]
    machinery_anchored: list[_Anchored] = [(m, entry_anchors(m)) for m in machinery]

    df = document_frequencies(
        [anchor_set for _, anchor_set in friction_anchored]
        + [anchor_set for _, anchor_set in machinery_anchored]
    )
    machinery_tokens: set[str] = set()
    for _, anchor_set in machinery_anchored:
        machinery_tokens |= anchor_set
    distinct = distinctive_anchors(df, machinery_tokens)

    return {
        "signature_classes": signature_classes(
            friction_anchored, machinery_anchored, distinct, workspace_root, namespace
        ),
        "structural_classes": structural_classes(
            friction_anchored, machinery_anchored, distinct, workspace_root, namespace
        ),
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect friction that recurred after a MACHINERY fix."
    )
    parser.add_argument("--workspace-root", default=".")
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    try:
        namespace = _memory_paths.resolve_namespace(workspace_root)
        payload = analyze(workspace_root, namespace)
    except _memory_paths._MemoryConfigError as exc:
        sys.stderr.write(f"friction-recurrence: {exc}\n")
        return 4
    except OSError as exc:
        sys.stderr.write(f"friction-recurrence: I/O error: {exc}\n")
        return 3
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "DF_HI",
    "DF_LO",
    "MACHINERY_PREFIX",
    "analyze",
    "anchors",
    "cli_main",
    "distinctive_anchors",
    "document_frequencies",
    "entry_anchors",
    "fix_sha",
    "recurrence_dicts",
    "signature_classes",
    "structural_classes",
]
