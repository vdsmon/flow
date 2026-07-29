"""Schema validator for `.flow/workspace.toml`.

Library + thin CLI. Stdlib-only.

HARD GATE: dispatch-stage.py runs this on every `init` and every `next`.
Exit 0 = ok. Exit 1 = schema invalid (stderr carries one violation per line).

Validates:

1. `.flow/.initialized` marker present.
2. `[tracker]` block with `backend` ∈ {jira, beads}.
3. `[tracker.jira]` for jira backend with `cloud_id` + `project_key`.
4. `[tracker.beads]` for beads backend with `prefix`.
5. `[pipeline]`: `stages` non-empty list[str]; every stage registered in
   stage-registry.toml; `pipeline.handlers` covers every stage.
6. Per stage: handler-string parses as `inline | none | subagent:<type> |
   subagent:<plugin>:<type>`.
7. Required predecessors precede the stage.
8. `required_when_compounding = true` stages must appear when
   `[memory] compounding = true` (one direction; presence without
   compounding is legal).
9. `[memory]`: `namespace` string; `compounding` bool.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _registry import (
    CODEX_HANDLERS,
    HANDLER_RE,
    LAUNCH_CALLER,
    LAUNCH_HANDLER,
    LAUNCH_KINDS,
    StageEntry,
    load_registry,
)
from model_resolve import OFF_VALUES

KNOWN_BACKENDS: tuple[str, ...] = ("jira", "beads")
KNOWN_FORGE_BACKENDS: tuple[str, ...] = ("github", "bitbucket")


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


def _validate_code_review_block(data: dict[str, Any], result: ValidationResult) -> None:
    """Hard-fail a `[code_review]` table: reviewer tuning moved into `[models]`."""
    if data.get("code_review") is not None:
        result.add(
            "code_review",
            "removed; reviewer tuning lives in [models.code_review] now — "
            'reviewer = { model = "<model>", effort = "<effort>" }',
        )


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
        if not HANDLER_RE.match(value):
            result.add(
                f"pipeline.handlers.{stage}",
                f"handler {value!r} does not match inline|none|subagent:<type>",
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


def _warn_inline_stage_model(
    data: dict[str, Any], handlers: dict[str, str], result: ValidationResult
) -> None:
    """Warn (non-fatal) when a stage has an EXPLICIT model pin but runs inline.

    An inline stage runs on the session model and cannot be model-pinned, so an
    explicit pin would silently not apply to it. Scoped to `implement` and `e2e`:
    those stages are dispatched directly as subagents. Inline review stages may
    launch their own fresh native reviewer, so their hints remain meaningful.
    """
    models = data.get("models")
    if not isinstance(models, dict):
        return
    for stage in ("implement", "e2e"):
        per_stage = models.get(stage)
        if isinstance(per_stage, str):
            field = f"models.{stage}"
            if per_stage.strip().lower() in OFF_VALUES:
                continue
        elif isinstance(per_stage, dict) and per_stage:
            field = f"models.{stage}"
        else:
            continue
        if handlers.get(stage) == "inline":
            result.warn(
                field,
                f"{stage} handler is 'inline'; an inline stage cannot be model-pinned, "
                f"so it runs on the session model and its model pin is ignored",
            )


def _warn_unusable_effort_hint(
    data: dict[str, Any], handlers: dict[str, str], result: ValidationResult
) -> None:
    """Warn when an EXPLICIT effort hint lands on a launcher that cannot carry it.

    Only a Codex launcher spends effort (`-c model_reasoning_effort=...`). A host-native
    launch has no lever, because Flow dispatches through the Agent tool, whose call takes
    no effort parameter — the agent's own frontmatter is the only native lever.

    Scoped to what the operator WROTE, never to a shipped registry default. A warning
    exists to say "the thing you configured will not take effect"; a default landing
    somewhere it cannot be used is Flow's problem to get right, and warning about it would
    put a standing complaint in every workspace's console about config nobody typed.
    """
    models = data.get("models")
    if not isinstance(models, dict):
        return
    for stage, entry in models.items():
        if not isinstance(entry, dict):
            continue
        for role, value in entry.items():
            if not isinstance(value, dict):
                continue
            effort = value.get("effort")
            if not isinstance(effort, str) or effort.strip().lower() in OFF_VALUES:
                continue
            kind = LAUNCH_KINDS.get(str(stage), {}).get(str(role), "")
            if kind == LAUNCH_CALLER:
                continue  # the caller may launch Codex; it decides at runtime
            codex = kind == LAUNCH_HANDLER and handlers.get(str(stage), "") in CODEX_HANDLERS
            if not codex:
                result.warn(
                    f"models.{stage}.{role}.effort",
                    "this role launches a host-native agent, which has no effort lever "
                    "(the Agent tool call carries no effort parameter), so the hint is "
                    "resolved and then dropped; pin effort in the agent's frontmatter",
                )


# The stages that actually launch an agent, and the roles they launch. This is the
# liveness truth for [models] keys: a hint for a stage outside this map (or a role a
# stage never launches) would silently do nothing, so it is a violation, not a no-op.
# Sources: delivery-plan.md (plan assessor), stage-registry.toml subagent handlers
# (implement, e2e), stage-code_review.md (reviewer + fixer), stage-review_loop.md §3
# (fixer), stage-review_brief.md (author).
# Derived from _registry.LAUNCH_KINDS so the launch map has ONE definition. That map also
# records HOW each role is launched (handler / native / caller), which model_resolve needs
# and this validator does not.
_LAUNCH_SITES: dict[str, frozenset[str]] = {
    stage: frozenset(roles) for stage, roles in LAUNCH_KINDS.items()
}

_HINT_FIELDS = frozenset({"model", "effort"})


def _validate_model_hints(
    data: dict[str, Any], handlers: dict[str, str], result: ValidationResult
) -> None:
    """Validate `[models]`: stage keys must name a launch site; a stage entry is a
    bare model string (stage-wide) or a role-keyed table whose values are a model
    string or an inline `{ model, effort }` table. Values are type-checked only —
    their vocabulary belongs to whatever launches the agent.

    Liveness is judged against THIS workspace: a `subagent:`-wired stage launches by
    construction (string hints allowed even off the map), while an inline/none stage
    launches only when its prose does (_LAUNCH_SITES)."""
    if "agents" in data:
        result.add(
            "agents",
            "provider route tables are no longer supported; delete [agents] and use optional "
            "[models] hints",
        )
    models = data.get("models")
    if models is None:
        return
    if not isinstance(models, dict):
        result.add("models", "must be a table keyed by stage")
        return
    for stage, entry in models.items():
        if stage not in handlers:
            result.add(f"models.{stage}", "stage is not in [pipeline].stages here")
            continue
        _validate_stage_hint(stage, entry, handlers[stage], result)


def _validate_stage_hint(stage: str, entry: Any, handler: str, result: ValidationResult) -> None:
    roles = _LAUNCH_SITES.get(stage)
    handler_launches = handler.startswith("subagent:")
    if roles is None and handler_launches and isinstance(entry, str):
        return
    if roles is None:
        detail = (
            "role tables need a stage with named roles; use a string hint"
            if handler_launches
            else f"stage launches no agent here; valid stages: {sorted(_LAUNCH_SITES)}"
        )
        result.add(f"models.{stage}", detail)
        return
    if isinstance(entry, str):
        return
    if not isinstance(entry, dict):
        result.add(f"models.{stage}", "must be a model string or a role-keyed table")
        return
    for role, value in entry.items():
        if role not in roles:
            result.add(
                f"models.{stage}.{role}",
                f"stage launches no such role; valid roles: {sorted(roles)}",
            )
            continue
        if isinstance(value, str):
            continue
        if not isinstance(value, dict):
            result.add(
                f"models.{stage}.{role}",
                'must be a model string or { model = "...", effort = "..." }',
            )
            continue
        for field_name, field_value in value.items():
            if field_name not in _HINT_FIELDS:
                result.add(
                    f"models.{stage}.{role}.{field_name}",
                    f"unknown field; valid fields: {sorted(_HINT_FIELDS)}",
                )
            elif not isinstance(field_value, str):
                result.add(f"models.{stage}.{role}.{field_name}", "must be a string")


def _validate_memory_block(data: dict[str, Any], result: ValidationResult) -> bool:
    memory = data.get("memory")
    if not isinstance(memory, dict):
        result.add("memory", "missing or not a table")
        return True  # default compounding=true so caller still gates on it
    if not isinstance(memory.get("namespace"), str) or not memory["namespace"]:
        result.add("memory.namespace", "missing or not a non-empty string")
    if not isinstance(memory.get("compounding"), bool):
        result.add("memory.compounding", "missing or not a bool")
    root = memory.get("root")
    if root is not None:
        # Optional shared-store path. A relative root would break the cross-worktree
        # share guarantee, so reject it; a not-yet-existing absolute dir is fine.
        if not isinstance(root, str) or not root:
            result.add("memory.root", "present but not a non-empty string")
        elif not Path(root).expanduser().is_absolute():
            result.add("memory.root", "must be an absolute path")
    # Optional facet-tagging convention (which recall.py --label to write).
    # Validated only when present, same "no default enforcement" pattern as root.
    label_facets = memory.get("label_facets")
    if label_facets is not None and (
        not isinstance(label_facets, list) or not all(isinstance(x, str) for x in label_facets)
    ):
        result.add("memory.label_facets", "present but not a list[str]")
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
        result.add(".flow/.initialized", "marker missing; run FLOW workspace setup first")
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
    _validate_code_review_block(data, result)
    compounding = _validate_memory_block(data, result)

    registry = stage_registry or load_registry(_stage_registry_path())
    stages, handlers = _validate_pipeline_block(data, registry, compounding, result)
    _validate_model_hints(data, handlers, result)

    _warn_inline_stage_model(data, handlers, result)
    _warn_unusable_effort_hint(data, handlers, result)

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
    return parser.parse_args(argv)


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
