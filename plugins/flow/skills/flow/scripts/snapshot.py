"""Canonical run snapshot for TOCTOU defense across dispatch calls.

Library module (no CLI). Stdlib-only.

The dispatcher validates workspace.toml at run start, then makes several short
dispatch subprocess calls (init / next / finish / release) over the life of a
run. Between those calls a user could edit workspace.toml or the stage registry,
or the engine's own tree could advance underneath the run. A snapshot taken at run
start lets each later call recompute the same hash from current on-disk content and
refuse on mismatch.

Snapshot content (hashed via canonical JSON -> sha256):
  - workspace_toml: full text of <workspace_root>/.flow/workspace.toml
  - stage_registry: full text of <skill_root>/stage-registry.toml
  - engine: {branch, tree_hash} over the MAIN checkout's own skill tree (resolved via `git worktree
    list`, stage-registry.toml excluded), active only when that checkout sits on a protected branch
    (the marketplace-tracks-main window where a mid-run checkout advance swaps engine code). {} when
    inactive (feature branch, detached, or not a git repo).
  - master_hash: sha256 of the canonical-JSON of the three keys above.

verify recomputes via compute_snapshot (the single source of hashing), compares
master_hash to the stored snapshot.sha, and only consults snapshot.json to NAME
what drifted.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from _atomicio import atomic_write_text
from _workspace import workspace_toml_path

_TREE_GLOBS = ("*.py", "*.sh", "*.md", "*.toml")
_TREE_SUFFIXES = tuple(glob.lstrip("*") for glob in _TREE_GLOBS)
_STAGE_REGISTRY_NAME = "stage-registry.toml"


def _skill_root_from_script() -> Path:
    # this file lives at <skill root>/scripts/snapshot.py, two levels below the skill root
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


_PROTECTED_BRANCHES = frozenset({"main", "master", "dev", "develop"})


def _git_text(args: list[str], cwd: Path) -> str:
    res = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30, check=False
    )
    if res.returncode != 0:
        raise OSError(res.stderr.strip() or "git failed")
    return res.stdout


def _resolve_engine_root(skill_root: Path) -> tuple[Path, Path] | None:
    """Resolve the MAIN-checkout engine root for the active engine component.

    Returns (main_root, rel) when the engine component is ACTIVE: skill_root is
    inside a git repo whose first `git worktree list` stanza (the main checkout)
    sits on a protected branch and whose engine tree (main_root / rel) exists.
    Returns None on any resolution failure or inactive case (not a git repo,
    detached/bare, feature branch, tree gone, git missing). `rel` is skill_root
    relative to its own toplevel, reused under main_root so the cleanliness
    check (engine_tree_clean) reads the SAME tree the hash reads.
    """
    try:
        if not skill_root.is_dir():
            return None
        toplevel = Path(_git_text(["rev-parse", "--show-toplevel"], skill_root).strip()).resolve()
        porcelain = _git_text(["worktree", "list", "--porcelain"], skill_root)
        first_stanza = porcelain.split("\n\n", 1)[0].splitlines()
        main_root = Path(first_stanza[0].removeprefix("worktree ").strip()).resolve()
        branch_lines = [ln for ln in first_stanza if ln.startswith("branch ")]
        if not branch_lines:  # detached or bare main checkout
            return None
        branch = branch_lines[0].removeprefix("branch refs/heads/").strip()
        if branch not in _PROTECTED_BRANCHES:
            return None
        rel = skill_root.resolve().relative_to(toplevel)
        engine_root = main_root / rel
        if not engine_root.is_dir():
            return None
        return main_root, rel
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def engine_tree_clean(skill_root: Path) -> bool:
    """True only when the MAIN-checkout engine working tree is clean vs its HEAD.

    A committed advance (lagging-main / marketplace pull) leaves the engine
    working tree == HEAD; a transient concurrent-read race re-verifies clean.
    A DIRTY engine tree (an uncommitted mid-run mutation, the raw-Edit threat)
    returns False so the drift guard still fail-closes. Resolves via
    _resolve_engine_root (the SAME (main_root, rel) the hash uses); None
    resolution / dirty / any error all fail closed to False.
    """
    resolved = _resolve_engine_root(skill_root)
    if resolved is None:
        return False
    main_root, rel = resolved
    try:
        res = subprocess.run(
            ["git", "-C", str(main_root), "diff", "--quiet", "HEAD", "--", rel.as_posix()],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # rc 0 = clean; rc 1 = dirty; any other rc = git error. Only rc 0 is clean.
    # a "daemon terminated" stderr line with rc 0 is cosmetic fsmonitor noise.
    return res.returncode == 0


def _engine_component(skill_root: Path) -> dict[str, str]:
    """{branch, tree_hash} over the MAIN checkout's skill tree; {} when inactive.

    Threat (flow-2pp): in the marketplace-tracks-main setup, a mid-run
    `git pull` + `claude plugin marketplace update` swaps dispatch_stage.py /
    state.py / reference docs under a running pipeline with no drift detection.

    Anchoring: `_skill_root_from_script()` is BISTABLE mid-run. The do-loop invokes engine scripts
    via the absolute installed path (main checkout) or a repo-relative path (the run's worktree
    copy) depending on how the agent typed the command (proven 2026-06-09, 12-transcript sweep on
    flow-2pp). So the component anchors on the MAIN checkout resolved via `git worktree list`,
    identical no matter which copy computes it, and hashes THAT engine tree.

    Branch gate: active only when the main checkout sits on a protected branch. machinery_edit
    refuses self-edits on protected branches, so the guard's active window is exactly the complement
    of the legitimate self-edit window: no false abort on a reflect self-heal, and no unguarded
    marketplace window. Worktree engine copies stay uncovered (run-private; only the run itself
    mutates them). Any resolution failure (not a git repo, git missing, detached HEAD, tree gone)
    deactivates the component rather than crashing. A bare/non-git install has no
    marketplace-advance window to guard.
    """
    resolved = _resolve_engine_root(skill_root)
    if resolved is None:
        return {}
    main_root, rel = resolved
    engine_root = main_root / rel
    try:
        # branch is a label on the output dict; the topology/protected gate that
        # made the component active was already settled by _resolve_engine_root.
        branch = _git_text(["rev-parse", "--abbrev-ref", "HEAD"], main_root).strip()
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


def _payload(
    workspace_toml_text: str,
    stage_registry_text: str,
    engine: dict[str, str],
) -> dict[str, Any]:
    return {
        "workspace_toml": workspace_toml_text,
        "stage_registry": stage_registry_text,
        "engine": engine,
    }


def compute_snapshot(
    workspace_root: Path,
    *,
    skill_root: Path,
) -> dict[str, Any]:
    """Compute the full snapshot dict from current on-disk content.

    Returns {workspace_toml, stage_registry, engine, master_hash}. The single
    source of all serialization + hashing; verify_snapshot re-runs this rather
    than re-deriving any hash itself.
    """
    workspace_toml_text = _read_text(workspace_toml_path(workspace_root))
    stage_registry_text = _read_text(stage_registry_path(skill_root))
    engine = _engine_component(skill_root)
    payload = _payload(workspace_toml_text, stage_registry_text, engine)
    snapshot = dict(payload)
    snapshot["master_hash"] = _sha256_text(_canonical_json(payload))
    return snapshot


def write_snapshot(
    workspace_root: Path,
    ticket: str,
    *,
    skill_root: Path,
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
        snapshot = compute_snapshot(workspace_root, skill_root=skill_root)
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

    Labels: "workspace_toml", "stage_registry", "engine". Returns [] for the
    no-diff (inconclusive) case.
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

    workspace_toml and stage_registry map to their path relative to workspace_root (or None when the
    file lives outside it, a separate skill checkout, so the edit cannot be a planned file of this
    run). The engine tree component maps to None: a tree_hash names no single file, so that drift is
    never owned (deliberate scope limit).
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
        current = compute_snapshot(workspace_root, skill_root=skill_root)
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
        workspace_root, ticket, skill_root=skill_root, revision=revision
    )
    return ok, detail


__all__ = [
    "classify_drift",
    "component_files",
    "compute_snapshot",
    "drifted_components",
    "engine_tree_clean",
    "snapshot_json_path",
    "snapshot_sha_path",
    "stage_registry_path",
    "verify_snapshot",
    "write_snapshot",
]
