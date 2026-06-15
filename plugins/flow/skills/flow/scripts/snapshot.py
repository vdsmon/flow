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
  - engine: {branch, tree_hash} over the MAIN checkout's own skill tree
    (resolved via `git worktree list`, stage-registry.toml excluded), active
    only when that checkout sits on a protected branch — the marketplace-
    tracks-main window where a mid-run checkout advance swaps engine code.
    {} when inactive (feature branch, detached, or not a git repo).
  - master_hash: sha256 of the canonical-JSON of the four keys above.

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
_TREE_SUFFIXES = tuple(glob.lstrip("*") for glob in _TREE_GLOBS)
_SKILL_PREFIX = "skill:"
_STAGE_REGISTRY_NAME = "stage-registry.toml"


def _skill_root_from_script() -> Path:
    # __file__ = .../plugins/flow/skills/flow/scripts/snapshot.py
    return Path(__file__).resolve().parent.parent


def stage_registry_path(skill_root: Path) -> Path:
    return skill_root / _STAGE_REGISTRY_NAME


def _run_dir(workspace_root: Path, ticket: str, revision: str | None) -> Path:
    base = workspace_root / ".flow" / "runs" / ticket
    return base if revision is None else base / "revisions" / revision


def snapshot_json_path(workspace_root: Path, ticket: str, revision: str | None = None) -> Path:
    return _run_dir(workspace_root, ticket, revision) / "snapshot.json"


def snapshot_sha_path(workspace_root: Path, ticket: str, revision: str | None = None) -> Path:
    return _run_dir(workspace_root, ticket, revision) / "snapshot.sha"


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
    # Single tree walk instead of one rglob per glob (compute_snapshot runs on
    # the do-loop hot path). Grouping by suffix in _TREE_GLOBS order preserves
    # the old per-glob dedup priority for paths resolving to the same file, so
    # the hash stays byte-identical to the 4-glob implementation.
    matched: dict[str, list[Path]] = {suffix: [] for suffix in _TREE_SUFFIXES}
    for path in plugin_root.rglob("*"):
        for suffix in _TREE_SUFFIXES:
            if path.name.endswith(suffix):
                matched[suffix].append(path)
                break
    entries: list[tuple[str, str]] = []
    seen: set[Path] = set()
    for suffix in _TREE_SUFFIXES:
        for path in matched[suffix]:
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


_PROTECTED_BRANCHES = frozenset({"main", "master", "dev", "develop"})


def _engine_component(skill_root: Path) -> dict[str, str]:
    """{branch, tree_hash} over the MAIN checkout's skill tree; {} when inactive.

    Threat (flow-2pp): in the marketplace-tracks-main setup, a mid-run
    `git pull` + `claude plugin marketplace update` swaps dispatch_stage.py /
    state.py / reference docs under a running pipeline with no drift detection
    (the handlers component covers only external skill: bundles).

    Anchoring: `_skill_root_from_script()` is BISTABLE mid-run — the do-loop
    invokes engine scripts via the absolute installed path (main checkout) or a
    repo-relative path (the run's worktree copy) depending on how the agent
    typed the command (proven 2026-06-09, 12-transcript sweep on flow-2pp). So
    the component anchors on the MAIN checkout resolved via `git worktree list`
    — identical no matter which copy computes it — and hashes THAT engine tree.

    Branch gate: active only when the main checkout sits on a protected branch.
    machinery_edit refuses self-edits on protected branches, so the guard's
    active window is exactly the complement of the legitimate self-edit window:
    no false abort on a reflect self-heal, and no unguarded marketplace window.
    Worktree engine copies stay uncovered (run-private; only the run itself
    mutates them). Any resolution failure (not a git repo, git missing,
    detached HEAD, tree gone) deactivates the component rather than crashing —
    a bare/non-git install has no marketplace-advance window to guard.
    """
    import subprocess

    def _git_text(args: list[str], cwd: Path) -> str:
        res = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30
        )
        if res.returncode != 0:
            raise OSError(res.stderr.strip() or "git failed")
        return res.stdout

    try:
        if not skill_root.is_dir():
            return {}
        toplevel = Path(_git_text(["rev-parse", "--show-toplevel"], skill_root).strip()).resolve()
        porcelain = _git_text(["worktree", "list", "--porcelain"], skill_root)
        first_stanza = porcelain.split("\n\n", 1)[0].splitlines()
        main_root = Path(first_stanza[0].removeprefix("worktree ").strip()).resolve()
        branch_lines = [ln for ln in first_stanza if ln.startswith("branch ")]
        if not branch_lines:  # detached or bare main checkout
            return {}
        branch = branch_lines[0].removeprefix("branch refs/heads/").strip()
        if branch not in _PROTECTED_BRANCHES:
            return {}
        rel = skill_root.resolve().relative_to(toplevel)
        engine_root = main_root / rel
        if not engine_root.is_dir():
            return {}
        # Enumerate via git ls-files, not a filesystem walk: the main checkout
        # carries untracked machine-local trees (scripts/.venv, .pytest_cache,
        # editor scratch) whose churn is not an engine swap and must not abort
        # runs. A tracked file deleted mid-advance raises on read -> {} ->
        # master-hash mismatch -> abort (fail closed, same as any swap).
        listed = _git_text(["ls-files", "--", rel.as_posix()], main_root)
        entries: list[tuple[str, str]] = []
        for line in listed.splitlines():
            name = line.rsplit("/", 1)[-1]
            if name == _STAGE_REGISTRY_NAME or not name.endswith(_TREE_SUFFIXES):
                continue
            file_path = main_root / line
            relpath = file_path.relative_to(engine_root).as_posix()
            entries.append((relpath, hashlib.sha256(file_path.read_bytes()).hexdigest()))
        entries.sort()
        return {
            "branch": branch,
            "tree_hash": _sha256_text(_canonical_json({"tree": entries})),
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


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
    engine: dict[str, str],
) -> dict[str, Any]:
    return {
        "workspace_toml": workspace_toml_text,
        "stage_registry": stage_registry_text,
        "handlers": handlers,
        "engine": engine,
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
    engine = _engine_component(skill_root)
    payload = _payload(workspace_toml_text, stage_registry_text, handlers, engine)
    snapshot = dict(payload)
    snapshot["master_hash"] = _sha256_text(_canonical_json(payload))
    return snapshot


def write_snapshot(
    workspace_root: Path,
    ticket: str,
    *,
    skill_root: Path,
    search_roots: list[Path] | None = None,
    snapshot: dict[str, Any] | None = None,
    revision: str | None = None,
) -> Path:
    """Write snapshot.json (full dict) and snapshot.sha (master_hash); returns the json path.

    `snapshot` lets a caller reuse a dict it already computed (e.g. via
    classify_drift) instead of paying a second compute_snapshot. `revision`
    nests the paths under runs/<ticket>/revisions/<revision>/ for a revision
    sub-run's own baseline (default None = the ticket-level path).
    """
    if snapshot is None:
        snapshot = compute_snapshot(
            workspace_root, skill_root=skill_root, search_roots=search_roots
        )
    json_path = snapshot_json_path(workspace_root, ticket, revision)
    # sha before json: a partial-write survivor is then sha-present/json-absent, which
    # classify_drift fails CLOSED on, instead of the json-present/sha-absent state it
    # reads as "no snapshot to verify" (drift guard silently off).
    atomic_write_text(
        snapshot_sha_path(workspace_root, ticket, revision), str(snapshot["master_hash"]) + "\n"
    )
    atomic_write_text(json_path, json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    return json_path


def drifted_components(stored: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Ordered component labels that differ between stored and current snapshots.

    Labels: "workspace_toml", "stage_registry", "engine", and "handler <stage>"
    entries. Returns [] for the no-diff (inconclusive) case.
    """
    changed: list[str] = []
    if stored.get("workspace_toml") != current.get("workspace_toml"):
        changed.append("workspace_toml")
    if stored.get("stage_registry") != current.get("stage_registry"):
        changed.append("stage_registry")
    # .get defaults make a pre-engine stored snapshot (no key) equal to a
    # current inactive component only when both are falsy; an active engine vs
    # a missing key is a real mid-upgrade drift and SHOULD abort (fail closed).
    if (stored.get("engine") or {}) != (current.get("engine") or {}):
        changed.append("engine")

    stored_raw = stored.get("handlers")
    current_raw = current.get("handlers")
    stored_handlers: dict[str, Any] = stored_raw if isinstance(stored_raw, dict) else {}
    current_handlers: dict[str, Any] = current_raw if isinstance(current_raw, dict) else {}
    for stage in sorted(set(stored_handlers) | set(current_handlers)):
        if stored_handlers.get(stage) != current_handlers.get(stage):
            changed.append(f"handler {stage}")
    return changed


def _rel_or_none(path: Path, workspace_root: Path) -> str | None:
    if path.is_relative_to(workspace_root):
        return path.relative_to(workspace_root).as_posix()
    return None


def component_files(
    components: list[str],
    *,
    workspace_root: Path,
    skill_root: Path,
) -> dict[str, str | None]:
    """Map drifted component labels to a workspace-root-relative posix path.

    workspace_toml and stage_registry map to their path relative to
    workspace_root (or None when the file lives outside it — a separate skill
    checkout, so the edit cannot be a planned file of this run). A handler or
    engine tree component maps to None: a tree_hash names no single file, so
    those drifts are never owned (deliberate scope limit).
    """
    out: dict[str, str | None] = {}
    for component in components:
        if component == "workspace_toml":
            out[component] = _rel_or_none(workspace_toml_path(workspace_root), workspace_root)
        elif component == "stage_registry":
            out[component] = _rel_or_none(stage_registry_path(skill_root), workspace_root)
        else:
            out[component] = None
    return out


def _name_drift(stored: dict[str, Any], current: dict[str, Any]) -> str:
    """Compare stored snapshot.json components to current; name what changed."""
    comps = drifted_components(stored, current)
    if not comps:
        return "drift: master_hash mismatch (component diff inconclusive)"
    return "drift: " + ", ".join(comps)


def classify_drift(
    workspace_root: Path,
    ticket: str,
    *,
    skill_root: Path,
    search_roots: list[Path] | None = None,
    revision: str | None = None,
) -> tuple[bool, str, list[str], dict[str, Any] | None]:
    """Recompute and compare against the stored snapshot, naming drifted components.

    (True, "no snapshot to verify", [], None) when no snapshot.sha exists;
    (True, "match", [], current) on equality; otherwise (False, "drift: <what
    changed>", comps, current) where comps is the ordered list from
    drifted_components (empty when the diff is inconclusive or snapshot.json is
    missing/unreadable). The last element is the freshly computed snapshot
    (None when compute itself failed), so a caller that reconciles can pass it
    straight to write_snapshot instead of recomputing. `revision` reads the
    revision sub-run's own snapshot baseline (default None = ticket-level).
    """
    sha_path = snapshot_sha_path(workspace_root, ticket, revision)
    if not sha_path.exists():
        return True, "no snapshot to verify", [], None

    stored_hash = _read_text(sha_path).strip()
    try:
        current = compute_snapshot(workspace_root, skill_root=skill_root, search_roots=search_roots)
    except OSError as exc:
        return False, f"drift: tracked file vanished or unreadable mid-verify ({exc})", [], None
    if current["master_hash"] == stored_hash:
        return True, "match", [], current

    json_path = snapshot_json_path(workspace_root, ticket, revision)
    if json_path.exists():
        try:
            stored = json.loads(_read_text(json_path))
        except json.JSONDecodeError:
            stored = {}
        if isinstance(stored, dict):
            comps = drifted_components(stored, current)
            return False, _name_drift(stored, current), comps, current
    return False, "drift: master_hash mismatch", [], current


def verify_snapshot(
    workspace_root: Path,
    ticket: str,
    *,
    skill_root: Path,
    search_roots: list[Path] | None = None,
    revision: str | None = None,
) -> tuple[bool, str]:
    """Recompute and compare against the stored snapshot.

    (True, "no snapshot to verify") when no snapshot.sha exists. Otherwise
    recompute master_hash via compute_snapshot; (True, "match") on equality,
    else (False, "drift: <what changed>") naming the changed component(s) by
    diffing against snapshot.json when present. `revision` reads the revision
    sub-run's own baseline (default None = ticket-level).
    """
    ok, detail, _, _ = classify_drift(
        workspace_root, ticket, skill_root=skill_root, search_roots=search_roots, revision=revision
    )
    return ok, detail


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
    "classify_drift",
    "cli_main",
    "component_files",
    "compute_snapshot",
    "drifted_components",
    "snapshot_json_path",
    "snapshot_sha_path",
    "stage_registry_path",
    "verify_snapshot",
    "write_snapshot",
]
