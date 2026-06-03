"""Canonical run snapshot for TOCTOU defense across dispatch calls.

Library + thin CLI. Stdlib-only.

The dispatcher validates workspace.toml at run start, then makes several short
dispatch subprocess calls (init / next / finish / release) over the life of a
run. Between those calls a user could edit workspace.toml, a plugin reinstall
could swap a handler's code, or a manifest could be rewritten. A snapshot taken
at run start lets each later call recompute the same hash from current on-disk
content and refuse on mismatch.

Snapshot content (hashed via canonical JSON -> sha256):
  - workspace_toml: full text of <workspace_root>/.flow/workspace.toml
  - stage_registry: full text of <skill_root>/stage-registry.toml
  - handlers: for each pipeline.handlers entry resolving to "skill:<name>...",
    a {stage: {manifest, tree_hash}} record. manifest is the matching
    .flow-bundle.toml text; tree_hash is a content hash over every *.py/*.sh/
    *.md/*.toml under the plugin_root. Bare workspaces have an empty dict here.
  - master_hash: sha256 of the canonical-JSON of the three keys above.

verify recomputes via compute_snapshot (the single source of hashing), compares
master_hash to the stored snapshot.sha, and only consults snapshot.json to NAME
what drifted.

CLI:
  snapshot.py emit   --ticket T --workspace-root R [--skill-root S]  (exit 0)
  snapshot.py verify --ticket T --workspace-root R [--skill-root S]
      exit 0 match-or-absent, 1 drift.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

import resolve_handler
from _atomicio import atomic_write_text
from _workspace import workspace_toml_path

_TREE_GLOBS = ("*.py", "*.sh", "*.md", "*.toml")
_SKILL_PREFIX = "skill:"
_STAGE_REGISTRY_NAME = "stage-registry.toml"


def _skill_root_from_script() -> Path:
    # __file__ = .../plugins/flow/skills/flow/scripts/snapshot.py
    return Path(__file__).resolve().parent.parent


def stage_registry_path(skill_root: Path) -> Path:
    return skill_root / _STAGE_REGISTRY_NAME


def snapshot_json_path(workspace_root: Path, ticket: str) -> Path:
    return workspace_root / ".flow" / "runs" / ticket / "snapshot.json"


def snapshot_sha_path(workspace_root: Path, ticket: str) -> Path:
    return workspace_root / ".flow" / "runs" / ticket / "snapshot.sha"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tree_hash(plugin_root: Path) -> str:
    """Content hash over sorted (relpath, sha256(bytes)) for tracked files.

    Tracked = *.py / *.sh / *.md / *.toml under plugin_root. The .toml glob
    excludes nothing relevant; compiled .pyc live in __pycache__ and are not
    matched. snapshot.json lives under workspace_root, never plugin_root, so
    writing it can't perturb this hash.
    """
    entries: list[tuple[str, str]] = []
    seen: set[Path] = set()
    for glob in _TREE_GLOBS:
        for path in plugin_root.rglob(glob):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            relpath = path.relative_to(plugin_root).as_posix()
            entries.append((relpath, hashlib.sha256(path.read_bytes()).hexdigest()))
    entries.sort()
    return _sha256_text(_canonical_json({"tree": entries}))


def _handler_strings_by_stage(workspace_toml_text: str) -> dict[str, str]:
    """Pull pipeline.handlers from raw workspace.toml text.

    Reads the table directly rather than via validate_workspace so a snapshot
    can be computed without the full schema gate (compute must not crash on a
    minimal workspace). Non-string values are skipped.
    """
    try:
        data = tomllib.loads(workspace_toml_text)
    except tomllib.TOMLDecodeError:
        return {}
    pipeline = data.get("pipeline")
    if not isinstance(pipeline, dict):
        return {}
    handlers = pipeline.get("handlers")
    if not isinstance(handlers, dict):
        return {}
    return {stage: value for stage, value in handlers.items() if isinstance(value, str)}


def _handlers_component(
    workspace_toml_text: str,
    search_roots: list[Path] | None,
) -> dict[str, dict[str, str]]:
    """Build {stage: {manifest, tree_hash}} for every skill: handler.

    An unresolved handler (not installed, or no plugin_root) is recorded with
    empty manifest + tree_hash rather than crashing; the validate gate normally
    prevents this, so the marker is minimal.
    """
    out: dict[str, dict[str, str]] = {}
    for stage, handler_string in _handler_strings_by_stage(workspace_toml_text).items():
        if not handler_string.startswith(_SKILL_PREFIX):
            continue
        resolution = resolve_handler.resolve(handler_string, search_roots=search_roots)
        plugin_root = resolution.plugin_root
        if not resolution.installed or plugin_root is None:
            out[stage] = {"manifest": "", "tree_hash": ""}
            continue
        root = Path(plugin_root)
        manifest_path = root / ".flow-bundle.toml"
        manifest_text = _read_text(manifest_path) if manifest_path.exists() else ""
        out[stage] = {"manifest": manifest_text, "tree_hash": _tree_hash(root)}
    return out


def _payload(
    workspace_toml_text: str,
    stage_registry_text: str,
    handlers: dict[str, dict[str, str]],
) -> dict[str, Any]:
    return {
        "workspace_toml": workspace_toml_text,
        "stage_registry": stage_registry_text,
        "handlers": handlers,
    }


def compute_snapshot(
    workspace_root: Path,
    *,
    skill_root: Path,
    search_roots: list[Path] | None = None,
) -> dict[str, Any]:
    """Compute the full snapshot dict from current on-disk content.

    Returns {workspace_toml, stage_registry, handlers, master_hash}. The single
    source of all serialization + hashing; verify_snapshot re-runs this rather
    than re-deriving any hash itself.
    """
    workspace_toml_text = _read_text(workspace_toml_path(workspace_root))
    stage_registry_text = _read_text(stage_registry_path(skill_root))
    handlers = _handlers_component(workspace_toml_text, search_roots)
    payload = _payload(workspace_toml_text, stage_registry_text, handlers)
    snapshot = dict(payload)
    snapshot["master_hash"] = _sha256_text(_canonical_json(payload))
    return snapshot


def write_snapshot(
    workspace_root: Path,
    ticket: str,
    *,
    skill_root: Path,
    search_roots: list[Path] | None = None,
) -> Path:
    """Write snapshot.json (full dict) and snapshot.sha (master_hash); returns the json path."""
    snapshot = compute_snapshot(workspace_root, skill_root=skill_root, search_roots=search_roots)
    json_path = snapshot_json_path(workspace_root, ticket)
    atomic_write_text(json_path, json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    atomic_write_text(
        snapshot_sha_path(workspace_root, ticket), str(snapshot["master_hash"]) + "\n"
    )
    return json_path


def _name_drift(stored: dict[str, Any], current: dict[str, Any]) -> str:
    """Compare stored snapshot.json components to current; name what changed."""
    changed: list[str] = []
    if stored.get("workspace_toml") != current.get("workspace_toml"):
        changed.append("workspace_toml")
    if stored.get("stage_registry") != current.get("stage_registry"):
        changed.append("stage_registry")

    stored_raw = stored.get("handlers")
    current_raw = current.get("handlers")
    stored_handlers: dict[str, Any] = stored_raw if isinstance(stored_raw, dict) else {}
    current_handlers: dict[str, Any] = current_raw if isinstance(current_raw, dict) else {}
    for stage in sorted(set(stored_handlers) | set(current_handlers)):
        if stored_handlers.get(stage) != current_handlers.get(stage):
            changed.append(f"handler {stage}")

    if not changed:
        return "drift: master_hash mismatch (component diff inconclusive)"
    return "drift: " + ", ".join(changed)


def verify_snapshot(
    workspace_root: Path,
    ticket: str,
    *,
    skill_root: Path,
    search_roots: list[Path] | None = None,
) -> tuple[bool, str]:
    """Recompute and compare against the stored snapshot.

    (True, "no snapshot to verify") when no snapshot.sha exists. Otherwise
    recompute master_hash via compute_snapshot; (True, "match") on equality,
    else (False, "drift: <what changed>") naming the changed component(s) by
    diffing against snapshot.json when present.
    """
    sha_path = snapshot_sha_path(workspace_root, ticket)
    if not sha_path.exists():
        return True, "no snapshot to verify"

    stored_hash = _read_text(sha_path).strip()
    current = compute_snapshot(workspace_root, skill_root=skill_root, search_roots=search_roots)
    if current["master_hash"] == stored_hash:
        return True, "match"

    json_path = snapshot_json_path(workspace_root, ticket)
    if json_path.exists():
        try:
            stored = json.loads(_read_text(json_path))
        except json.JSONDecodeError:
            stored = {}
        if isinstance(stored, dict):
            return False, _name_drift(stored, current)
    return False, "drift: master_hash mismatch"


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ticket", required=True)
    common.add_argument("--workspace-root", required=True)
    common.add_argument("--skill-root", default=None)

    parser = argparse.ArgumentParser(description="Emit / verify the canonical run snapshot.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("emit", parents=[common])
    sub.add_parser("verify", parents=[common])
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    skill_root = (
        Path(args.skill_root).expanduser().resolve()
        if args.skill_root
        else _skill_root_from_script()
    )

    if args.command == "emit":
        path = write_snapshot(workspace_root, args.ticket, skill_root=skill_root)
        sys.stdout.write(str(path) + "\n")
        return 0

    ok, detail = verify_snapshot(workspace_root, args.ticket, skill_root=skill_root)
    sys.stdout.write(detail + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "cli_main",
    "compute_snapshot",
    "snapshot_json_path",
    "snapshot_sha_path",
    "stage_registry_path",
    "verify_snapshot",
    "write_snapshot",
]
