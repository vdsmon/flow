"""Schema validator for `.flow/workspace.toml`.

Library + thin CLI. Stdlib-only.

HARD GATE: dispatch-stage.py runs this on every `init` and every `next`.
Exit 0 = ok. Exit 1 = schema invalid (stderr carries one violation per line).

Validates (phase 7-mvp scope; capability cross-check + canonical snapshot are
phase 7-full):

1. `.flow/.initialized` marker present.
2. `[tracker]` block with `backend` ∈ {jira, beads}.
3. `[tracker.jira]` for jira backend with `cloud_id` + `project_key`.
4. `[tracker.beads]` for beads backend with `prefix`.
5. `[pipeline]`: `stages` non-empty list[str]; every stage registered in
   stage-registry.toml; `pipeline.handlers` covers every stage.
6. Per stage: handler-string parses as `inline | none | subagent:<type> |
   skill:<name>[:<args>]`.
7. Required predecessors precede the stage.
8. `required = true` stages appear.
9. `required_when_compounding = true` stages appear iff
   `[memory] compounding = true`.
10. `[memory]`: `namespace` string; `compounding` bool; `auto_recall` bool;
    `recall_by` list[str]; `recall_top_n` int.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import snapshot
from _registry import StageEntry, load_registry

KNOWN_BACKENDS: tuple[str, ...] = ("jira", "beads")
_HANDLER_RE = re.compile(r"^(inline|none|subagent:[A-Za-z0-9_-]+|skill:[A-Za-z0-9_.-]+(?::.+)?)$")


@dataclass
class ValidationResult:
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def add(self, key_path: str, message: str, *, severity: str = "error") -> None:
        self.violations.append(f"{severity}: {key_path}: {message}")


# ─── stage-registry loader ──────────────────────────────────────────────────


def _stage_registry_path() -> Path:
    return Path(__file__).resolve().parent.parent / "stage-registry.toml"


# ─── Workspace-toml shape validators ────────────────────────────────────────


def _validate_tracker_block(data: dict[str, Any], result: ValidationResult) -> str | None:
    tracker = data.get("tracker")
    if not isinstance(tracker, dict):
        result.add("tracker", "missing or not a table")
        return None
    backend = tracker.get("backend")
    if backend not in KNOWN_BACKENDS:
        result.add("tracker.backend", f"expected one of {KNOWN_BACKENDS!r}, got {backend!r}")
        return None
    if backend == "jira":
        jira = tracker.get("jira")
        if not isinstance(jira, dict):
            result.add("tracker.jira", "missing or not a table")
        else:
            for key in ("cloud_id", "project_key"):
                if not isinstance(jira.get(key), str) or not jira[key]:
                    result.add(f"tracker.jira.{key}", "missing or not a non-empty string")
    elif backend == "beads":
        beads = tracker.get("beads")
        if not isinstance(beads, dict):
            result.add("tracker.beads", "missing or not a table")
        elif not isinstance(beads.get("prefix"), str) or not beads["prefix"]:
            result.add("tracker.beads.prefix", "missing or not a non-empty string")
    return backend


def _validate_pipeline_block(
    data: dict[str, Any],
    registry: list[StageEntry],
    compounding: bool,
    result: ValidationResult,
) -> tuple[list[str], dict[str, str]]:
    pipeline = data.get("pipeline")
    if not isinstance(pipeline, dict):
        result.add("pipeline", "missing or not a table")
        return [], {}

    stages_raw = pipeline.get("stages")
    if not isinstance(stages_raw, list) or not stages_raw:
        result.add("pipeline.stages", "must be a non-empty list[str]")
        return [], {}
    stages: list[str] = []
    for i, s in enumerate(stages_raw):
        if not isinstance(s, str):
            result.add(f"pipeline.stages[{i}]", "entry is not a string")
            continue
        stages.append(s)

    by_name = {e.name: e for e in registry}
    for s in stages:
        if s not in by_name:
            result.add(
                "pipeline.stages",
                f"stage {s!r} is not registered in stage-registry.toml",
            )

    # Required (always)
    for entry in registry:
        if entry.required and entry.name not in stages:
            result.add(
                "pipeline.stages",
                f"stage {entry.name!r} is required but missing",
            )
        if entry.required_when_compounding and compounding and entry.name not in stages:
            result.add(
                "pipeline.stages",
                f"stage {entry.name!r} required when [memory] compounding=true",
            )

    # Predecessors
    stage_index = {name: i for i, name in enumerate(stages)}
    for name in stages:
        entry = by_name.get(name)
        if entry is None:
            continue
        for pred in entry.required_predecessors:
            if pred not in stage_index:
                continue  # predecessor not in pipeline; ok (stage's choice)
            if stage_index[pred] >= stage_index[name]:
                result.add(
                    "pipeline.stages",
                    f"stage {name!r} must follow predecessor {pred!r}",
                )

    # Handlers
    handlers_raw = pipeline.get("handlers")
    if not isinstance(handlers_raw, dict):
        result.add("pipeline.handlers", "missing or not a table")
        return stages, {}
    handlers: dict[str, str] = {}
    for stage in stages:
        value = handlers_raw.get(stage)
        if not isinstance(value, str):
            result.add(f"pipeline.handlers.{stage}", "missing or not a string")
            continue
        if not _HANDLER_RE.match(value):
            result.add(
                f"pipeline.handlers.{stage}",
                f"handler {value!r} does not match "
                f"inline|none|subagent:<type>|skill:<name>[:<args>]",
            )
            continue
        handlers[stage] = value
    return stages, handlers


def _validate_memory_block(data: dict[str, Any], result: ValidationResult) -> bool:
    memory = data.get("memory")
    if not isinstance(memory, dict):
        result.add("memory", "missing or not a table")
        return True  # default compounding=true so caller still gates on it
    if not isinstance(memory.get("namespace"), str) or not memory["namespace"]:
        result.add("memory.namespace", "missing or not a non-empty string")
    for key in ("auto_recall", "compounding"):
        if not isinstance(memory.get(key), bool):
            result.add(f"memory.{key}", "missing or not a bool")
    recall_by = memory.get("recall_by")
    if not isinstance(recall_by, list) or not all(isinstance(x, str) for x in recall_by):
        result.add("memory.recall_by", "missing or not a list[str]")
    if not isinstance(memory.get("recall_top_n"), int):
        result.add("memory.recall_top_n", "missing or not an int")
    root = memory.get("root")
    if root is not None:
        # Optional shared-store path. A relative root would break the cross-worktree
        # share guarantee, so reject it; a not-yet-existing absolute dir is fine.
        if not isinstance(root, str) or not root:
            result.add("memory.root", "present but not a non-empty string")
        elif not Path(root).expanduser().is_absolute():
            result.add("memory.root", "must be an absolute path")
    return bool(memory.get("compounding", True))


# ─── Public API ──────────────────────────────────────────────────────────────


@dataclass
class WorkspaceSnapshot:
    """Best-effort snapshot of validated workspace state for the dispatcher."""

    backend: str
    stages: list[str]
    handlers: dict[str, str]
    namespace: str
    compounding: bool


def validate(
    workspace_root: Path,
    stage_registry: list[StageEntry] | None = None,
) -> tuple[ValidationResult, WorkspaceSnapshot | None]:
    """Validate the workspace at `workspace_root`. Returns (result, snapshot|None).

    `snapshot` is populated when validation passes; None on failure.
    """
    result = ValidationResult()
    flow_dir = workspace_root / ".flow"
    workspace_toml = flow_dir / "workspace.toml"
    initialized = flow_dir / ".initialized"

    if not initialized.exists():
        result.add(".flow/.initialized", "marker missing; run /flow init first")
        return result, None

    if not workspace_toml.exists():
        result.add(".flow/workspace.toml", "missing")
        return result, None

    try:
        data = tomllib.loads(workspace_toml.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        result.add(".flow/workspace.toml", f"failed to parse: {exc}")
        return result, None

    backend = _validate_tracker_block(data, result)
    compounding = _validate_memory_block(data, result)

    registry = stage_registry or load_registry(_stage_registry_path())
    stages, handlers = _validate_pipeline_block(data, registry, compounding, result)

    if not result.ok or backend is None:
        return result, None

    memory_block = data.get("memory", {}) if isinstance(data.get("memory"), dict) else {}
    namespace = memory_block.get("namespace", "")
    snapshot = WorkspaceSnapshot(
        backend=backend,
        stages=stages,
        handlers=handlers,
        namespace=str(namespace),
        compounding=compounding,
    )
    return result, snapshot


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate .flow/ workspace schema.")
    parser.add_argument("--workspace-root", default=".")
    parser.add_argument("--ticket", default=None)
    parser.add_argument("--emit-canonical-snapshot", action="store_true")
    args = parser.parse_args(argv)
    if args.emit_canonical_snapshot and not args.ticket:
        parser.error("--emit-canonical-snapshot requires --ticket")
    return args


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    root = Path(args.workspace_root).expanduser().resolve()
    try:
        result, _ = validate(root)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"validate-workspace: {exc}\n")
        return 1
    if not result.ok:
        for line in result.violations:
            sys.stderr.write(line + "\n")
        return 1
    if args.emit_canonical_snapshot:
        # same skill_root resolution as snapshot.py emit and dispatch_stage init,
        # so all three paths hash identical on-disk content.
        skill_root = Path(__file__).resolve().parent.parent
        path = snapshot.write_snapshot(root, args.ticket, skill_root=skill_root)
        sys.stdout.write(str(path) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "KNOWN_BACKENDS",
    "ValidationResult",
    "WorkspaceSnapshot",
    "cli_main",
    "validate",
]
