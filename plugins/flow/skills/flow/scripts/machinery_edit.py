"""Concurrency-safe applier for reflect lens-B machinery fixes to flow's OWN source.

The reflect stage (lens B, machinery=ON) lets the agent fix flow's own
`scripts/*.py` and `references/*.md` mid-run. With a fleet of parallel /flow
runs, two agents can reach reflect at once. The raw Edit tool has no
cross-process serialization, so two concurrent machinery edits to the same file
race: a lost update (last writer wins, one fix vanishes) or a torn read (a third
run importing the half-written module hits SyntaxError and aborts).

This tool closes both holes for the WRITER side:

- A single blocking flock on `<skill-root>/.machinery.lock` spans the whole
  read -> replace -> write, so concurrent machinery writers serialize. The flock
  is an OS advisory lock released on process exit, so it cannot leak across a
  crash (no stale-lease takeover needed, unlike the per-ticket run lease).
- The write goes through atomic_write_text (temp + fsync + os.replace), so any
  concurrent READER (a sibling run importing the module) sees old-or-new, never
  a torn file. Readers do not need to take the lock.

It also enforces the reflect doc's snapshot caveat at the tool level: it refuses
to touch `stage-registry.toml` (it IS in the run's canonical snapshot; editing it
mid-run trips the drift guard on the closing advance) and refuses any path
outside the skill tree.

Idempotency mirrors the doc's "anchor not found usually means already fixed":
if `old` is absent but `new` is already present, the fix is reported
already_applied (exit 0), not a failure.

Payload (JSON, via --payload <file> or stdin):
    {"file": "<path rel to skill-root, or absolute>", "old": "...", "new": "..."}

Exit codes:
    0 = applied, or already_applied (idempotent no-op).
    1 = usage / I/O error (bad payload, missing file, empty `old`, old==new).
    2 = refused (path outside skill tree, or snapshot-pinned stage-registry.toml).
    3 = anchor_not_found (old absent AND new absent — agent must re-derive).
    4 = ambiguous (old occurs more than once — not a unique anchor).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _atomicio import atomic_write_text
from _locking import flock_blocking

# In the run's canonical snapshot — editing mid-run trips the drift guard.
_SNAPSHOT_PINNED = {"stage-registry.toml"}


def _emit(obj: dict, code: int) -> int:
    print(json.dumps(obj, indent=2))
    return code


def apply_edit(skill_root: Path, target: Path, old: str, new: str) -> tuple[dict, int]:
    """Apply a single unique-anchor replacement under the machinery write lock.

    Pure of argparse so the test suite can drive it directly.
    """
    skill_root = skill_root.resolve()
    if not target.is_absolute():
        target = skill_root / target
    target = target.resolve()

    try:
        target.relative_to(skill_root)
    except ValueError:
        return {
            "status": "refused",
            "file": str(target),
            "reason": "path is outside the skill tree",
        }, 2
    if target.name in _SNAPSHOT_PINNED:
        return {
            "status": "refused",
            "file": str(target),
            "reason": f"{target.name} is in the canonical snapshot; "
            "propose+record or reload-snapshot instead",
        }, 2
    if not old:
        return {"status": "error", "file": str(target), "reason": "`old` is empty"}, 1
    if old == new:
        return {"status": "error", "file": str(target), "reason": "`old` equals `new` (no-op)"}, 1

    lock_path = skill_root / ".machinery.lock"
    with flock_blocking(lock_path):
        if not target.is_file():
            return {"status": "error", "file": str(target), "reason": "file does not exist"}, 1
        text = target.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 1:
            atomic_write_text(target, text.replace(old, new, 1))
            return {"status": "applied", "file": str(target), "occurrences": 1}, 0
        if count > 1:
            return {
                "status": "ambiguous",
                "file": str(target),
                "occurrences": count,
                "reason": "`old` is not a unique anchor; narrow it",
            }, 4
        # count == 0
        if new and new in text:
            return {
                "status": "already_applied",
                "file": str(target),
                "reason": "`new` already present; nothing to do",
            }, 0
        return {
            "status": "anchor_not_found",
            "file": str(target),
            "reason": "`old` not found and `new` absent; re-derive the anchor",
        }, 3


def _load_payload(payload_path: str | None) -> dict:
    raw = Path(payload_path).read_text(encoding="utf-8") if payload_path else sys.stdin.read()
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    ap = sub.add_parser("apply", help="apply one machinery edit under the write lock")
    ap.add_argument(
        "--skill-root",
        required=True,
        help="flow skill root (dir containing scripts/ and references/)",
    )
    ap.add_argument("--payload", help="path to JSON {file, old, new}; reads stdin if omitted")
    args = parser.parse_args(argv)

    try:
        payload = _load_payload(args.payload)
    except (OSError, json.JSONDecodeError) as exc:
        return _emit({"status": "error", "reason": f"bad payload: {exc}"}, 1)

    missing = [k for k in ("file", "old", "new") if k not in payload]
    if missing:
        return _emit({"status": "error", "reason": f"payload missing keys: {missing}"}, 1)

    result, code = apply_edit(
        Path(args.skill_root), Path(payload["file"]), payload["old"], payload["new"]
    )
    return _emit(result, code)


if __name__ == "__main__":
    raise SystemExit(main())
