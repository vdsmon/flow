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
from model_resolve import OFF_VALUES

KNOWN_BACKENDS: tuple[str, ...] = ("jira", "beads")
KNOWN_FORGE_BACKENDS: tuple[str, ...] = ("github", "bitbucket")
_HANDLER_RE = re.compile(r"^(inline|none|subagent:[A-Za-z0-9_-]+|skill:[A-Za-z0-9_.-]+(?::.+)?)$")


@dataclass
class ValidationResult:
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def add(self, key_path: str, message: str, *, severity: str = "error") -> None:
        self.violations.append(f"{severity}: {key_path}: {message}")

    def warn(self, key_path: str, message: str) -> None:
        # non-fatal: surfaced on stderr but never flips `ok`, so the HARD GATE passes
        self.warnings.append(f"warning: {key_path}: {message}")


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


def _validate_forge_block(data: dict[str, Any], result: ValidationResult) -> None:
    """Validate the OPTIONAL `[forge]` block, only when present.

    Unlike `[tracker]`, an absent `[forge]` is NOT a violation (a workspace that
    keeps create_pr/review_loop at `none` needs no forge). github requires no
    sub-keys; bitbucket requires `workspace` + `repo_slug`.
    """
    forge = data.get("forge")
    if forge is None:
        return
    if not isinstance(forge, dict):
        result.add("forge", "not a table")
        return
    backend = forge.get("backend")
    if backend not in KNOWN_FORGE_BACKENDS:
        result.add("forge.backend", f"expected one of {KNOWN_FORGE_BACKENDS!r}, got {backend!r}")
        return
    if backend == "bitbucket":
        bb = forge.get("bitbucket")
        if not isinstance(bb, dict):
            result.add("forge.bitbucket", "missing or not a table")
        else:
            for key in ("workspace", "repo_slug"):
                if not isinstance(bb.get(key), str) or not bb[key]:
                    result.add(f"forge.bitbucket.{key}", "missing or not a non-empty string")


def _parse_stages(pipeline: dict[str, Any], result: ValidationResult) -> list[str] | None:
    stages_raw = pipeline.get("stages")
    if not isinstance(stages_raw, list) or not stages_raw:
        result.add("pipeline.stages", "must be a non-empty list[str]")
        return None
    stages: list[str] = []
    for i, s in enumerate(stages_raw):
        if not isinstance(s, str):
            result.add(f"pipeline.stages[{i}]", "entry is not a string")
            continue
        stages.append(s)
    return stages


def _check_stage_registration(
    stages: list[str],
    by_name: dict[str, StageEntry],
    registry: list[StageEntry],
    compounding: bool,
    result: ValidationResult,
) -> None:
    for s in stages:
        if s not in by_name:
            result.add(
                "pipeline.stages",
                f"stage {s!r} is not registered in stage-registry.toml",
            )

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


def _check_predecessors(
    stages: list[str],
    by_name: dict[str, StageEntry],
    result: ValidationResult,
) -> None:
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


def _parse_handlers(
    pipeline: dict[str, Any], stages: list[str], result: ValidationResult
) -> dict[str, str]:
    handlers_raw = pipeline.get("handlers")
    if not isinstance(handlers_raw, dict):
        result.add("pipeline.handlers", "missing or not a table")
        return {}
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
    return handlers


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

    stages = _parse_stages(pipeline, result)
    if stages is None:
        return [], {}

    by_name = {e.name: e for e in registry}
    _check_stage_registration(stages, by_name, registry, compounding, result)
    _check_predecessors(stages, by_name, result)
    return stages, _parse_handlers(pipeline, stages, result)


def _warn_inline_work_model(
    data: dict[str, Any], handlers: dict[str, str], result: ValidationResult
) -> None:
    """Warn (non-fatal) when `[models] work_model` is EXPLICITLY set but implement is inline.

    An inline stage runs on the session model and cannot be model-pinned, so an
    explicit `work_model` would silently not apply to it. Only an explicit, non-opt-out
    `work_model` warns (a config intent that won't take effect); the on-by-default case
    (no `[models]` block) does not, to keep validate quiet for the common setup.
    """
    models = data.get("models")
    if not isinstance(models, dict):
        return
    work_model = models.get("work_model")
    if not isinstance(work_model, str) or work_model.strip().lower() in OFF_VALUES:
        return
    if handlers.get("implement") == "inline":
        result.warn(
            "models.work_model",
            "implement handler is 'inline'; an inline stage cannot be model-pinned, "
            "so its code-writing runs on the session model and work_model is ignored for it",
        )


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
    _validate_forge_block(data, result)
    compounding = _validate_memory_block(data, result)

    registry = stage_registry or load_registry(_stage_registry_path())
    stages, handlers = _validate_pipeline_block(data, registry, compounding, result)

    _warn_inline_work_model(data, handlers, result)

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
    for line in result.warnings:
        sys.stderr.write(line + "\n")
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
