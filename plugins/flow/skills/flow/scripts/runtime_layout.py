"""Collision-proof Flow runtime layout and journaled v1-to-v2 migration.

Layout v2 separates executable metadata from durable memory::

    .flow/runtime/{flow,skill-root,memory-root,layout-version}
    .flow/memory/<namespace>/

An unstamped workspace is treated as v1 until :func:`ensure_layout` runs.  The
migration fails toward preserving data: a non-empty source/destination conflict,
a live (or unreadable) run lease, or a hash mismatch never removes either copy.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import lease
from _atomicio import atomic_write_text
from _locking import flock_blocking
from _timeutil import utcnow_iso

LAYOUT_VERSION = 2
_LOCAL_MEMORY_ROOT = ".flow/memory"
# Every non-hidden first component Flow owns (or historically owned) directly
# under a v1 `.flow/` root. `flow` is intentionally absent: its launcher
# collision has a data-preserving special case below.
_RESERVED_NAMESPACES = frozenset(
    {
        "config.toml",
        "e2e-recipes.md",
        "fleet",
        "launch-ledger",
        "memory",
        "memory-root",
        "pending-mutations.jsonl",
        "recall-pending.jsonl",
        "runs",
        "runtime",
        "skill_dir",
        "tickets",
        "workspace.toml",
        "worktrees",
    }
)
_RESERVED_NAMESPACE_PREFIXES = ("pending-mutations.jsonl.", "recall-pending.jsonl.")
_JOURNAL_STAGES = frozenset({"prepared", "moved", "verified", "published", "complete"})
_JOURNAL_KEYS = frozenset(
    {
        "version",
        "stage",
        "workspace_root",
        "namespace",
        "source",
        "destination",
        "destination_base",
        "backup",
        "manifest",
    }
)


class RuntimeLayoutError(RuntimeError):
    """Base for safe migration refusals."""


class MemoryConflictError(RuntimeLayoutError):
    """Both the v1 and v2 memory stores contain data."""


class LiveLeaseError(RuntimeLayoutError):
    """A run might still be writing paths covered by the migration."""


class DataIntegrityError(RuntimeLayoutError):
    """The copied or moved store does not match its pre-migration manifest."""


@dataclass(frozen=True)
class RuntimeLayout:
    workspace_root: Path
    flow_dir: Path
    runtime_dir: Path
    launcher: Path
    skill_root_file: Path
    memory_root_file: Path
    version_file: Path
    memory_base: Path
    version: int = LAYOUT_VERSION


def _runtime_dir(workspace_root: Path) -> Path:
    return workspace_root / ".flow" / "runtime"


def is_v2(workspace_root: Path) -> bool:
    """Return whether the workspace carries a valid layout-v2 stamp."""
    try:
        return int((_runtime_dir(workspace_root) / "layout-version").read_text().strip()) == 2
    except (OSError, ValueError):
        return False


def _workspace_data(root: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads((root / ".flow" / "workspace.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _namespace(root: Path) -> str:
    memory = _workspace_data(root).get("memory")
    namespace = memory.get("namespace") if isinstance(memory, dict) else None
    if not isinstance(namespace, str) or not namespace:
        raise RuntimeLayoutError("workspace.toml missing or empty memory.namespace")
    return validate_namespace(namespace)


def validate_namespace(namespace: str) -> str:
    """Return a path-safe namespace that cannot collide with Flow-owned v1 roots."""
    if (
        not namespace
        or namespace in {".", ".."}
        or namespace.startswith(".")
        or Path(namespace).name != namespace
        or namespace.casefold() in _RESERVED_NAMESPACES
        or namespace.casefold().startswith(_RESERVED_NAMESPACE_PREFIXES)
    ):
        raise RuntimeLayoutError(
            f"unsafe or reserved memory namespace {namespace!r}; choose a project-specific name"
        )
    return namespace


def _read_absolute_path(path: Path) -> Path | None:
    try:
        raw = path.read_text(encoding="utf-8").strip("\r\n")
    except (OSError, UnicodeError):
        return None
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    return candidate.resolve() if candidate.is_absolute() else None


def _read_v2_memory_base(root: Path) -> Path:
    """Read a v2 pointer without guessing past missing or malformed metadata."""
    path = _runtime_dir(root) / "memory-root"
    try:
        raw = path.read_text(encoding="utf-8").strip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise RuntimeLayoutError(f"cannot read layout-v2 memory root at {path}: {exc}") from exc
    if raw == _LOCAL_MEMORY_ROOT:
        return (root / ".flow" / "memory").resolve()
    if not raw:
        raise RuntimeLayoutError(f"layout-v2 memory root at {path} is empty")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise RuntimeLayoutError(
            f"layout-v2 memory root at {path} must be {_LOCAL_MEMORY_ROOT!r} or an absolute path"
        )
    return candidate.resolve()


def _configured_legacy_base(root: Path) -> Path:
    """Resolve v1's base directory, before the implicit ``memory/`` segment."""
    flow = root / ".flow"
    sibling = _read_absolute_path(flow / "memory-root")
    if sibling is not None:
        return sibling
    memory = _workspace_data(root).get("memory")
    configured = memory.get("root") if isinstance(memory, dict) else None
    if isinstance(configured, str) and configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
    return flow.resolve()


def _default_v2_base(root: Path) -> Path:
    return _configured_legacy_base(root) / "memory"


def _tree_nonempty(path: Path) -> bool:
    if not path.exists():
        return False
    if not path.is_dir():
        return True
    return next(path.iterdir(), None) is not None


def _tree_manifest(path: Path) -> dict[str, Any]:
    files: list[dict[str, object]] = []
    directories: list[str] = []
    for candidate in sorted(path.rglob("*")):
        relative = candidate.relative_to(path).as_posix()
        if candidate.is_dir():
            directories.append(relative)
            continue
        if not candidate.is_file():
            raise DataIntegrityError(f"memory store contains unsupported entry: {candidate}")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        files.append({"path": relative, "size": candidate.stat().st_size, "sha256": digest})
    return {"files": files, "directories": directories}


def _verify_manifest(path: Path, expected: dict[str, Any]) -> None:
    if not path.is_dir():
        raise DataIntegrityError(f"migrated memory store is missing at {path}")
    actual = _tree_manifest(path)
    if actual != expected:
        raise DataIntegrityError(
            f"migrated memory store at {path} failed byte/hash verification; "
            "the migration backup was preserved"
        )


def _journal_path(root: Path) -> Path:
    return _runtime_dir(root) / "migration-journal.json"


def _write_journal(path: Path, journal: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(journal, indent=2, sort_keys=True) + "\n")


def _advance_journal(
    path: Path,
    journal: dict[str, Any],
    stage: str,
    stage_hook: Callable[[str], None] | None,
) -> None:
    journal["stage"] = stage
    _write_journal(path, journal)
    if stage_hook is not None:
        stage_hook(stage)


def _load_journal(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeLayoutError(
            f"cannot resume corrupt migration journal at {path}: {exc}"
        ) from exc
    if not isinstance(data, dict) or data.get("version") != LAYOUT_VERSION:
        raise RuntimeLayoutError(f"invalid migration journal at {path}")
    return data


def _safe_manifest_relative(raw: object) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    candidate = Path(raw)
    return (
        not candidate.is_absolute() and ".." not in candidate.parts and candidate.as_posix() != "."
    )


def _validate_manifest_schema(manifest: object, journal_path: Path) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != {"files", "directories"}:
        raise RuntimeLayoutError(f"invalid manifest in migration journal at {journal_path}")
    manifest_dict = cast(dict[str, Any], manifest)
    files = manifest_dict.get("files")
    directories = manifest_dict.get("directories")
    if not isinstance(files, list) or not isinstance(directories, list):
        raise RuntimeLayoutError(f"invalid manifest in migration journal at {journal_path}")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise RuntimeLayoutError(
                f"invalid file manifest in migration journal at {journal_path}"
            )
        rel = entry.get("path")
        size = entry.get("size")
        digest = entry.get("sha256")
        if (
            not isinstance(rel, str)
            or not _safe_manifest_relative(rel)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)
            or rel in seen
        ):
            raise RuntimeLayoutError(
                f"invalid file manifest in migration journal at {journal_path}"
            )
        seen.add(rel)
    for rel in directories:
        if not isinstance(rel, str) or not _safe_manifest_relative(rel) or rel in seen:
            raise RuntimeLayoutError(
                f"invalid directory manifest in migration journal at {journal_path}"
            )
        seen.add(rel)
    return manifest_dict


def _validated_journal_paths(
    root: Path, journal: dict[str, Any]
) -> tuple[Path, Path, Path, dict[str, Any]]:
    """Closed-validate a journal before trusting any path for mutation."""
    journal_path = _journal_path(root)
    if set(journal) != _JOURNAL_KEYS:
        raise RuntimeLayoutError(f"invalid migration journal schema at {journal_path}")
    stage = journal.get("stage")
    if stage not in _JOURNAL_STAGES:
        raise RuntimeLayoutError(f"invalid migration stage {stage!r} at {journal_path}")
    raw_journal_root = journal.get("workspace_root")
    if not isinstance(raw_journal_root, str) or not Path(raw_journal_root).is_absolute():
        raise RuntimeLayoutError(f"migration journal has an invalid workspace root: {journal_path}")
    journal_root = Path(raw_journal_root).resolve()
    if stage != "complete" and journal_root != root:
        raise RuntimeLayoutError(
            f"migration journal belongs to a different workspace: {journal_path}"
        )
    namespace = journal.get("namespace")
    if not isinstance(namespace, str):
        raise RuntimeLayoutError(f"migration journal has no namespace at {journal_path}")
    validate_namespace(namespace)

    raw_paths = [
        journal.get(key) for key in ("source", "destination", "destination_base", "backup")
    ]
    if not all(isinstance(raw, str) and Path(raw).is_absolute() for raw in raw_paths):
        raise RuntimeLayoutError(
            f"migration journal contains a non-absolute path at {journal_path}"
        )
    source, destination, destination_base, backup = (Path(str(raw)).resolve() for raw in raw_paths)
    # Completed journals are historical, non-mutating receipts and remain valid
    # when the whole checkout moves. Every resumable stage is pinned to `root`.
    backup_root = journal_root if stage == "complete" else root
    expected_backup = (_runtime_dir(backup_root) / "migration-backup" / namespace).resolve()
    if backup != expected_backup:
        raise RuntimeLayoutError(
            f"migration backup escapes this workspace runtime at {journal_path}: {backup}"
        )
    if destination != destination_base / namespace:
        raise RuntimeLayoutError(f"migration destination does not match its base at {journal_path}")
    if source.name != namespace or destination.name != namespace or source == destination:
        raise RuntimeLayoutError(
            f"migration journal contains inconsistent namespace paths at {journal_path}"
        )
    manifest = _validate_manifest_schema(journal.get("manifest"), journal_path)
    return source, destination, backup, manifest


def _live_leases(root: Path) -> list[str]:
    runs = root / ".flow" / "runs"
    if not runs.is_dir():
        return []
    now = utcnow_iso()
    boot = lease.boot_id()
    host = socket.gethostname()
    blockers: list[str] = []
    for lock in sorted(runs.glob("**/run.lock")):
        ticket_dir = lock.parent
        info = lease.classify(ticket_dir, now, current_boot=boot, hostname=host)
        if info.get("state") in {"live", "corrupt"}:
            blockers.append(ticket_dir.relative_to(runs).as_posix())
    return blockers


def _linked_worktree_roots(root: Path) -> list[Path]:
    """Enumerate linked git worktrees, failing closed when repository metadata is ambiguous."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain", "-z"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiveLeaseError(f"cannot prove shared-memory lease safety: {exc}") from exc
    if result.returncode != 0:
        # A directory with no git metadata cannot have linked git worktrees. A
        # present-but-unreadable .git entry is ambiguous and must block migration.
        if not (root / ".git").exists():
            return [root]
        detail = result.stderr.strip() or result.stdout.strip() or f"git exited {result.returncode}"
        raise LiveLeaseError(f"cannot enumerate linked worktrees for lease safety: {detail}")

    roots: list[Path] = []
    for field in result.stdout.split("\0"):
        if not field.startswith("worktree "):
            continue
        raw = field.removeprefix("worktree ")
        candidate = Path(raw)
        if not raw or not candidate.is_absolute():
            raise LiveLeaseError(
                f"cannot prove shared-memory lease safety: invalid git worktree path {raw!r}"
            )
        roots.append(candidate.resolve())
    if not roots or root not in roots:
        raise LiveLeaseError(
            "cannot prove shared-memory lease safety: git omitted the current worktree"
        )
    return list(dict.fromkeys(roots))


def _discovery_legacy_base(root: Path) -> Path:
    """Resolve a linked worktree's v1 base without swallowing corrupt configuration."""
    flow = root / ".flow"
    sibling = flow / "memory-root"
    if sibling.exists() or sibling.is_symlink():
        parsed = _read_absolute_path(sibling)
        if parsed is None:
            raise LiveLeaseError(
                f"cannot prove shared-memory lease safety: invalid pointer at {sibling}"
            )
        return parsed

    workspace = flow / "workspace.toml"
    if not workspace.exists():
        return flow.resolve()
    try:
        data = tomllib.loads(workspace.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise LiveLeaseError(
            f"cannot prove shared-memory lease safety: unreadable config at {workspace}: {exc}"
        ) from exc
    memory = data.get("memory") if isinstance(data, dict) else None
    configured = memory.get("root") if isinstance(memory, dict) else None
    if configured is None or configured == "":
        return flow.resolve()
    if not isinstance(configured, str):
        raise LiveLeaseError(
            f"cannot prove shared-memory lease safety: invalid memory.root at {workspace}"
        )
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        raise LiveLeaseError(
            f"cannot prove shared-memory lease safety: relative memory.root at {workspace}"
        )
    return candidate.resolve()


def _shared_base_lease_blockers(root: Path, legacy_base: Path) -> list[str]:
    """Find live/corrupt leases in every linked v1 checkout using ``legacy_base``."""
    blockers: list[str] = []
    for worktree in _linked_worktree_roots(root):
        if is_v2(worktree):
            continue
        if _discovery_legacy_base(worktree) != legacy_base.resolve():
            continue
        blockers.extend(f"{worktree}:{ticket}" for ticket in _live_leases(worktree))
    return blockers


def _backup_store(source: Path, backup: Path, manifest: dict[str, Any]) -> None:
    preparing = backup.with_name(backup.name + ".preparing")
    if preparing.exists():
        shutil.rmtree(preparing)
    if backup.exists():
        _verify_manifest(backup, manifest)
        return
    preparing.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, preparing, symlinks=True)
    _verify_manifest(preparing, manifest)
    os.replace(preparing, backup)


def _remove_empty_directory(path: Path) -> None:
    if path.is_dir() and not _tree_nonempty(path):
        path.rmdir()


def _remove_legacy_metadata(flow: Path) -> None:
    for path in (flow / "skill_dir", flow / "memory-root"):
        if path.is_file() or path.is_symlink():
            path.unlink()
    launcher = flow / "flow"
    if launcher.is_file() or launcher.is_symlink():
        launcher.unlink()


def _layout(root: Path, memory_base: Path) -> RuntimeLayout:
    flow = root / ".flow"
    runtime = flow / "runtime"
    return RuntimeLayout(
        workspace_root=root,
        flow_dir=flow,
        runtime_dir=runtime,
        launcher=runtime / "flow",
        skill_root_file=runtime / "skill-root",
        memory_root_file=runtime / "memory-root",
        version_file=runtime / "layout-version",
        memory_base=memory_base,
    )


def _publish_layout(layout: RuntimeLayout, *, create_memory: bool = True) -> None:
    if create_memory:
        layout.memory_base.mkdir(parents=True, exist_ok=True)
    local = (layout.flow_dir / "memory").resolve()
    pointer = (
        _LOCAL_MEMORY_ROOT
        if layout.memory_base.resolve() == local
        else str(layout.memory_base.resolve())
    )
    atomic_write_text(layout.memory_root_file, pointer + "\n")
    atomic_write_text(layout.version_file, f"{LAYOUT_VERSION}\n")


def _resume_data_migration(
    root: Path,
    journal: dict[str, Any],
    *,
    stage_hook: Callable[[str], None] | None,
) -> None:
    journal_path = _journal_path(root)
    source, destination, backup, manifest = _validated_journal_paths(root, journal)

    stage = str(journal.get("stage", "prepared"))
    if stage == "prepared":
        if _tree_nonempty(source) and _tree_nonempty(destination):
            raise MemoryConflictError(
                f"legacy memory at {source} and v2 memory at {destination} both contain data; "
                "preserving both for manual reconciliation"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        _remove_empty_directory(destination)
        if source.is_dir():
            os.replace(source, destination)
        elif not destination.exists():
            if not backup.is_dir():
                raise DataIntegrityError(
                    f"migration source and destination are missing and no backup exists at {backup}"
                )
            shutil.copytree(backup, destination, symlinks=True)
        _advance_journal(journal_path, journal, "moved", stage_hook)
        stage = "moved"

    if stage == "moved":
        if not destination.exists() and backup.is_dir():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(backup, destination, symlinks=True)
        _verify_manifest(destination, manifest)
        _advance_journal(journal_path, journal, "verified", stage_hook)
        stage = "verified"

    if stage == "verified":
        layout = _layout(root, destination.parent)
        _publish_layout(layout)
        _advance_journal(journal_path, journal, "published", stage_hook)
        stage = "published"

    if stage == "published":
        layout = _layout(root, destination.parent)
        _remove_legacy_metadata(layout.flow_dir)
        if backup.exists():
            shutil.rmtree(backup)
        backup_parent = backup.parent
        if backup_parent.is_dir() and not _tree_nonempty(backup_parent):
            backup_parent.rmdir()
        _advance_journal(journal_path, journal, "complete", stage_hook)


def ensure_layout(
    workspace_root: Path,
    *,
    memory_base: Path | None = None,
    stage_hook: Callable[[str], None] | None = None,
) -> RuntimeLayout:
    """Install or migrate runtime layout v2 and return its resolved paths.

    ``memory_base`` is the *v2* base (the directory containing namespaces) and
    is used by worktree bootstrap to bind directly to the main workspace's
    already-migrated store.
    """
    root = workspace_root.expanduser().resolve()
    namespace = _namespace(root)
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    runtime = flow / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)

    journal = _load_journal(_journal_path(root))
    journal_paths = _validated_journal_paths(root, journal) if journal is not None else None
    if is_v2(root) and journal is not None and journal.get("stage") != "complete":
        assert journal_paths is not None
        source = journal_paths[0]
        with flock_blocking(source.parent / ".layout-v2-migration.lock"):
            blockers = _shared_base_lease_blockers(root, source.parent)
            if blockers:
                raise LiveLeaseError(
                    "refusing runtime migration while run leases may be live: "
                    + ", ".join(blockers)
                )
            _resume_data_migration(root, journal, stage_hook=stage_hook)

    if is_v2(root):
        recorded = _read_v2_memory_base(root)
        selected = memory_base.expanduser().resolve() if memory_base is not None else recorded
        if selected != recorded and _tree_nonempty(recorded / namespace):
            raise MemoryConflictError(
                f"refusing to rebind layout-v2 memory from {recorded} to {selected}; "
                f"the existing {namespace!r} namespace contains data"
            )
        if not selected.is_dir():
            raise RuntimeLayoutError(
                f"layout-v2 memory root is missing or not a directory: {selected}; "
                "refusing to create a replacement that could hide existing memory"
            )
        layout = _layout(root, selected)
        _publish_layout(layout, create_memory=False)
        return layout

    legacy_base = _configured_legacy_base(root)
    selected_base = (
        memory_base.expanduser().resolve() if memory_base is not None else _default_v2_base(root)
    )
    layout = _layout(root, selected_base)
    source = legacy_base / namespace
    destination = selected_base / namespace
    lock_path = legacy_base / ".layout-v2-migration.lock"

    with flock_blocking(lock_path):
        # Another workspace sharing this base may have completed the move while
        # this process waited. Its destination is authoritative when our source
        # is now absent.
        journal_path = _journal_path(root)
        journal = _load_journal(journal_path)
        if journal is not None and journal.get("stage") != "complete":
            _validated_journal_paths(root, journal)
            blockers = _shared_base_lease_blockers(root, legacy_base)
            if blockers:
                raise LiveLeaseError(
                    "refusing runtime migration while run leases may be live: "
                    + ", ".join(blockers)
                )
            _resume_data_migration(root, journal, stage_hook=stage_hook)
            return _layout(root, Path(str(journal["destination"])).parent)

        blockers = _shared_base_lease_blockers(root, legacy_base)
        if blockers:
            raise LiveLeaseError(
                "refusing runtime migration while run leases may be live: " + ", ".join(blockers)
            )

        if _tree_nonempty(source) and _tree_nonempty(destination):
            raise MemoryConflictError(
                f"legacy memory at {source} and v2 memory at {destination} both contain data; "
                "preserving both for manual reconciliation"
            )

        if source.exists() and not source.is_dir():
            # A namespace named "flow" collided with the legacy launcher file
            # before it could create memory. Treat the file as launcher metadata.
            if source.parent == flow and source.name.casefold() == "flow":
                source.unlink()
            else:
                raise RuntimeLayoutError(f"legacy memory path is not a directory: {source}")

        if source.is_dir() and _tree_nonempty(source):
            manifest = _tree_manifest(source)
            backup = runtime / "migration-backup" / namespace
            _backup_store(source, backup, manifest)
            journal = {
                "version": LAYOUT_VERSION,
                "stage": "prepared",
                "workspace_root": str(root),
                "namespace": namespace,
                "source": str(source),
                "destination": str(destination),
                "destination_base": str(selected_base),
                "backup": str(backup),
                "manifest": manifest,
            }
            _advance_journal(journal_path, journal, "prepared", stage_hook)
            _resume_data_migration(root, journal, stage_hook=stage_hook)
            return _layout(root, selected_base)

        _remove_empty_directory(source)
        selected_base.mkdir(parents=True, exist_ok=True)
        _publish_layout(layout)
        _remove_legacy_metadata(flow)
        return layout


__all__ = [
    "LAYOUT_VERSION",
    "DataIntegrityError",
    "LiveLeaseError",
    "MemoryConflictError",
    "RuntimeLayout",
    "RuntimeLayoutError",
    "ensure_layout",
    "is_v2",
    "validate_namespace",
]
