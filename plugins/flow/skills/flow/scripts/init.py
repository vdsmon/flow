"""Flow workspace setup: transactional workspace bootstrap.

Library + thin CLI. Stdlib-only (`tomllib` for reads, hand-written TOML for the
single small workspace.toml output).

Contract:

- Pure CLI. NO stdin. The Flow adapter collects user answers, then invokes
  init.py with everything as flags or via `--config <answers.json>`.
- Transactional. Writes `.flow/.initializing` BEFORE any mutation. Atomically
  renames to `.flow/.initialized` ONLY after all postconditions pass. Any
  failure leaves `.initializing` in place; re-run with `--resume`.
- `.flow/.init-progress` is an append-only JSONL of completed phases. `--resume`
  reads it and skips already-done phases.
- Pre-flight: `.flow/.initialized` present → refuse unless `--reconfigure`.
  `.flow/.initializing` present and no `--resume`/`--reconfigure` → refuse with
  recover hint.
- Bundle discovery via `bundle_discover.discover()`; no hardcoded skill names.
  `recommended` is offered only when discovered manifests cover the stages
  that bare leaves as `none`/`inline` (typically `code_review`, `create_pr`,
  `review_loop`). On stage-provider conflicts, `recommended` is refused;
  caller must use `--bundle custom` with explicit per-stage handler overrides.
- For backend=beads, runs `bd init --prefix <prefix>` then verifies
  `bd ready --json` returns parseable JSON. Subprocess runner is injectable
  (`bd_runner` constructor arg) so tests can mock without spawning bd.
- Appends one line to `~/.config/flow/checkpoint-manifest.jsonl` so the
  14-day-checkpoint metric in `recall.py` has an auditable participant ledger.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import flow_launcher
from _atomicio import atomic_write_bytes, atomic_write_text
from _registry import StageEntry, load_registry, parse_handler
from _runner import KwRunner as Runner
from _runner import kw_default_runner as _default_runner
from _timeutil import utcnow_iso
from bundle_discover import DiscoveryResult, HarnessError, flow_harness
from bundle_discover import discover as bundle_discover_run

# ─── Types ───────────────────────────────────────────────────────────────────

BackendLiteral = Literal["jira", "beads"]
BundleLiteral = Literal["bare", "recommended", "custom"]

PhaseLiteral = Literal[
    "validate_inputs",
    "bundle_compose",
    "mkdirs",
    "ensure_gitignore",
    "bd_init",
    "write_workspace_toml",
    "verify_postconditions",
    "append_checkpoint",
    "finalize",
]

# Ignore all transient .flow/ state (runtime, runs, locks, and memory); whitelist
# the config pair that stays tracked. Broad rule
# so new transient files are ignored without enumerating each. _GITIGNORE_MARKER
# is the idempotency probe. Its presence means we already seeded.
_GITIGNORE_MARKER = ".flow/*"
_GITIGNORE_BLOCK = (
    "# flow: ignore transient run state; keep config trackable\n"
    ".flow/*\n"
    "!.flow/workspace.toml\n"
    "!.flow/.initialized\n"
)
# The worktree pool lives under .claude/worktrees (flow-gh1u). Probed as its own
# line, independent of _GITIGNORE_MARKER, so repos seeded before the relocation
# still gain it on re-init instead of being skipped by the .flow marker.
_GITIGNORE_POOL_LINE = ".claude/worktrees/"


def _ensure_gitignore(root: Path) -> dict[str, Any] | None:
    """Append the `.flow/` ignore block and the `.claude/worktrees/` pool line
    to `<root>/.gitignore` unless already present. Marker-guarded + append-only,
    so `--resume` / re-init never duplicate the block or clobber the user's
    existing `.gitignore`."""
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    lines = existing.splitlines()
    wants_block = _GITIGNORE_MARKER not in lines
    wants_pool = _GITIGNORE_POOL_LINE not in lines
    if not wants_block and not wants_pool:
        return {"skipped": True, "reason": ".flow/ and the worktree pool already gitignored"}
    if existing and not existing.endswith("\n"):
        existing += "\n"
    block = ("\n" if existing else "") + (_GITIGNORE_BLOCK if wants_block else "")
    if wants_pool:
        block += _GITIGNORE_POOL_LINE + "\n"
    atomic_write_text(gitignore, existing + block)
    return None


# AGENTS.md is durable generic-harness guidance. Claude Code and Codex can load Flow natively;
# other harnesses may use this managed block as their entry point. Opt-in via
# `--agents-md`: a tracked root file, so default-off keeps native-only init byte-identical.
# Once present, the marker pair records durable opt-in and later reconfigures upgrade it.
_AGENTS_MARKER = "<!-- flow:begin -->"
_AGENTS_END_MARKER = "<!-- flow:end -->"
_AGENTS_STANZA = """<!-- flow:begin -->
## Flow state-aware ticket→PR delivery

This repository is initialized for Flow. Codex should prefer the installed
`$flow:flow` skill; Claude Code should use the Flow plugin. A generic harness must be
given the absolute Flow skill root (`FLOW_SKILL_DIR`) by its adapter, then read `SKILL.md` and
`references/harness.md` there. Do not search the machine for an installation.

1. Pass the request after the host trigger unchanged to Flow's `public-commands.toml`
   router. Static namespaces win over target parsing; unknown or removed forms stop.
   Keep the starting checkout as an absolute task root.
2. Bind the harness identity (`codex`, `claude-code`, or `generic`). Prefix every
   direct setup, runtime repair, and facade command with `FLOW_HARNESS=<identity>` in that same
   call; a shell export is never persistent state.
3. Before a workspace command, install or migrate runtime layout v2 from the loaded
   skill. Join tracker, run, lease, snapshot, revision, and forge evidence and obey the
   deterministic lifecycle result.
4. **Approval is not coding.** For a fresh target, perform read-only planning, present
   the plan and confidence evidence, then stop until the user explicitly approves.
   Use native Plan mode when available; otherwise this turn boundary is the gate.
5. After approval, create or adopt the Flow worktree. Set the returned absolute path as
   the run root and its `.flow/runtime/flow` executable as the absolute facade path.
6. Harness calls do not share shell state. Give every command an explicit workdir of
   the run root, or self-root that individual call when no workdir field exists. Invoke
   the facade absolutely, and root every read, edit, artifact, and subagent prompt
   there. A prior standalone `cd` or shell export is never persistent state.
7. Never relocate dirty main-checkout files automatically. Spill recovery requires
   confirmed agent-created provenance and an explicit recovery action.
<!-- flow:end -->"""


def _ensure_agents_md(root: Path, *, requested: bool) -> dict[str, Any] | None:
    """Add or upgrade the managed Flow block without touching surrounding text."""
    agents = root / "AGENTS.md"
    existing = agents.read_text(encoding="utf-8") if agents.exists() else ""
    begin_count = existing.count(_AGENTS_MARKER)
    end_count = existing.count(_AGENTS_END_MARKER)
    if begin_count != end_count or begin_count > 1:
        raise InitError(
            "AGENTS.md must contain either no Flow markers or exactly one ordered "
            f"{_AGENTS_MARKER} / {_AGENTS_END_MARKER} pair"
        )

    if begin_count == 0:
        if not requested:
            return {"skipped": True, "reason": "agents_md not requested"}
        prefix = existing
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix:
            prefix += "\n"
        updated = prefix + _AGENTS_STANZA + "\n"
    else:
        begin = existing.index(_AGENTS_MARKER)
        end = existing.index(_AGENTS_END_MARKER)
        if end < begin:
            raise InitError(
                f"AGENTS.md has {_AGENTS_END_MARKER} before {_AGENTS_MARKER}; fix the markers"
            )
        end += len(_AGENTS_END_MARKER)
        updated = existing[:begin] + _AGENTS_STANZA + existing[end:]

    if updated == existing:
        return {"skipped": True, "reason": "AGENTS.md Flow stanza is current"}
    atomic_write_text(agents, updated)
    return None


# Phases run in order. Phases skipped by backend (e.g. bd_init for jira) are
# still recorded as "completed" so --resume bookkeeping stays simple.


@dataclass(frozen=True)
class JiraConfig:
    cloud_id: str
    project_key: str
    assignee_account_id: str | None


@dataclass(frozen=True)
class BeadsConfig:
    prefix: str


@dataclass(frozen=True)
class InitConfig:
    """Resolved + validated answer set. The single input to `run_init`."""

    backend: BackendLiteral
    bundle: BundleLiteral
    workspace_root: Path
    jira: JiraConfig | None = None
    beads: BeadsConfig | None = None
    # Handler overrides: stage_name → handler_string. For bundle=custom, these
    # are user-supplied. For bundle=recommended, they are computed by phase
    # `bundle_compose` from discovered manifests. For bundle=bare, this is empty.
    handler_overrides: dict[str, str] = field(default_factory=dict)
    memory_namespace: str | None = None
    memory_compounding: bool = True
    # Override the default checkpoint-manifest location (tests).
    checkpoint_manifest_path: Path | None = None
    # personal | work | scratch. None -> derived from backend. The backend
    # alignment matrix is enforced (jira != personal, beads != work).
    checkpoint_mode: str | None = None
    # Override default search roots for bundle discovery (tests).
    bundle_search_roots: list[Path] | None = None
    # Opt-in: add the managed AGENTS.md fallback. Once present, reconfigure keeps
    # it current without requiring the flag again. See _ensure_agents_md.
    agents_md: bool = False


@dataclass
class InitResult:
    workspace_toml_path: Path
    handlers: dict[str, str]
    namespace: str
    discovery_warnings: list[str] = field(default_factory=list)


class InitError(Exception):
    """Surfaced at CLI level as exit-code 1 with stderr."""


class InitPreflightError(InitError):
    """Exit code 4: pre-existing marker without --resume/--reconfigure."""


class BundleConflictError(InitError):
    """Exit code 3: recommended bundle has a stage-provider conflict."""


def _install_launcher(root: Path) -> None:
    try:
        flow_launcher.install(root)
    except (OSError, flow_launcher.runtime_layout.RuntimeLayoutError) as exc:
        raise InitError(f"could not install .flow/runtime/flow launcher: {exc}") from exc


# ─── Stage-registry parsing ─────────────────────────────────────────────────


def _stage_registry_path() -> Path:
    # `__file__` points at scripts/init.py; registry lives at the skill root.
    return Path(__file__).resolve().parent.parent / "stage-registry.toml"


def _load_stage_registry(path: Path | None = None) -> list[StageEntry]:
    # Called outside run_init's try block (line ~752), so map the shared loader's
    # ValueError to InitError here to keep the CLI's "init failed" (rc=1) wording.
    try:
        return load_registry(path or _stage_registry_path())
    except ValueError as exc:
        raise InitError(str(exc)) from exc


def _default_pipeline_stages(registry: list[StageEntry], compounding: bool) -> list[str]:
    """All registered stages; drops reflect iff compounding=false.

    Day-1 simplest policy: include every stage. Workspaces prune at hand-edit
    time. Reflect is the only stage gated by `compounding`.
    """
    return [
        s.name
        for s in registry
        if s.name != "reflect" or compounding or s.required_when_compounding is False
    ]


# ─── Path helpers ───────────────────────────────────────────────────────────


def _flow_dir(root: Path) -> Path:
    return root / ".flow"


def _marker_initializing(root: Path) -> Path:
    return _flow_dir(root) / ".initializing"


def _marker_initialized(root: Path) -> Path:
    return _flow_dir(root) / ".initialized"


def _progress_path(root: Path) -> Path:
    return _flow_dir(root) / ".init-progress"


def _ensure_init_run_id(initializing: Path) -> str:
    """Create the `.initializing` marker with a run id, or read an existing one.

    The id is stable across `--resume` so checkpoint-append can detect a
    duplicate from a prior interrupted run.
    """
    initializing.parent.mkdir(parents=True, exist_ok=True)
    if initializing.exists():
        existing = initializing.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    run_id = uuid.uuid4().hex
    initializing.write_text(run_id + "\n", encoding="utf-8")
    return run_id


def _workspace_toml_path(root: Path) -> Path:
    return _flow_dir(root) / "workspace.toml"


def _default_checkpoint_manifest_path() -> Path:
    return Path.home() / ".config" / "flow" / "checkpoint-manifest.jsonl"


# ─── Slug derivation ────────────────────────────────────────────────────────


_SLUG_NONALPHA_RE = re.compile(r"[^a-z0-9]+")


def _derive_slug(name: str) -> str:
    """NFKC + lowercase + non-alphanumeric → '-'. Strips leading/trailing '-'."""
    normalized = unicodedata.normalize("NFKC", name).lower()
    return _SLUG_NONALPHA_RE.sub("-", normalized).strip("-")


def _derive_beads_prefix(workspace_root: Path) -> str:
    return _derive_slug(workspace_root.resolve().name) or "flow"


def _derive_default_namespace(config: InitConfig) -> str:
    if config.memory_namespace is not None:
        return config.memory_namespace
    if config.backend == "jira":
        assert config.jira is not None
        return config.jira.project_key
    if config.backend == "beads":
        assert config.beads is not None
        return _derive_slug(config.workspace_root.resolve().name) or config.beads.prefix
    raise InitError(f"unknown backend {config.backend!r}")


# ─── Progress tracking ──────────────────────────────────────────────────────


def _read_progress(root: Path) -> set[str]:
    path = _progress_path(root)
    if not path.exists():
        return set()
    completed: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        phase = entry.get("phase")
        if isinstance(phase, str):
            completed.add(phase)
    return completed


def _append_progress(root: Path, phase: PhaseLiteral, extra: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {
        "phase": phase,
        "ts": utcnow_iso(),
    }
    if extra:
        payload.update(extra)
    line = json.dumps(payload, sort_keys=True) + "\n"
    path = _progress_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


# ─── TOML emitter (hand-rolled) ─────────────────────────────────────────────


def _toml_escape(value: str) -> str:
    # Minimal TOML basic-string escape: backslash, double-quote, control chars.
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_str_array(values: list[str]) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(_toml_escape(v) for v in values) + "]"


def _render_workspace_toml(
    config: InitConfig,
    namespace: str,
    pipeline_stages: list[str],
    handlers: dict[str, str],
    *,
    routing_toml: str | None = None,
) -> str:
    lines: list[str] = []
    lines.append("# .flow/workspace.toml — generated by Flow workspace setup")
    lines.append(f"# generated_at = {utcnow_iso()!r}")
    lines.append("")
    lines.append("[tracker]")
    lines.append(f"backend = {_toml_escape(config.backend)}")
    lines.append("")

    if config.backend == "jira":
        assert config.jira is not None
        lines.append("[tracker.jira]")
        lines.append(f"cloud_id = {_toml_escape(config.jira.cloud_id)}")
        lines.append(f"project_key = {_toml_escape(config.jira.project_key)}")
        if config.jira.assignee_account_id is not None:
            lines.append(f"assignee_account_id = {_toml_escape(config.jira.assignee_account_id)}")
        lines.append("")
    elif config.backend == "beads":
        assert config.beads is not None
        lines.append("[tracker.beads]")
        lines.append(f"prefix = {_toml_escape(config.beads.prefix)}")
        lines.append("shared_server = true")
        lines.append("")

    lines.append("[pipeline]")
    lines.append(f"stages = {_toml_str_array(pipeline_stages)}")
    lines.append("")

    lines.append("[pipeline.handlers]")
    for stage in pipeline_stages:
        value = handlers.get(stage, "none")
        lines.append(f"{stage} = {_toml_escape(value)}")
    lines.append("")

    lines.append("[memory]")
    lines.append(f"namespace = {_toml_escape(namespace)}")
    lines.append("auto_recall = true")
    lines.append('recall_by = ["branch", "current-ticket", "ready-tickets"]')
    lines.append("recall_top_n = 5")
    lines.append("label_facets = []")
    lines.append(f"compounding = {str(config.memory_compounding).lower()}")
    lines.append("")
    # Optional semantic-recall overlay (flow-vuff). Off by default: recall stays
    # pure BM25 until opted in. Enabling on an existing workspace needs an explicit
    # bulk backfill (`recall.py --reindex --workspace-root .`) before the index is
    # populated; until then plan-phase recall is BM25-only. See inventory.md.
    lines.append("# [memory.semantic]")
    lines.append("# enabled = false")
    lines.append('# model = "BAAI/bge-small-en-v1.5"')
    lines.append("# threshold = 0.0  # low floor; candidates are selected by rank (top-K), not τ")
    lines.append(
        '# embedder = ""  # blank → default: uvx --with fastembed python embedder_fastembed.py'
    )
    lines.append("")
    if routing_toml is None:
        routing_toml = (
            _default_agent_routes_toml() if flow_harness() in {"claude-code", "codex"} else ""
        )
    if routing_toml:
        lines.extend(routing_toml.rstrip().splitlines())
        lines.append("")
    return "\n".join(lines)


def _default_agent_routes_toml() -> str:
    """Return the native setup defaults; generic setup intentionally omits them."""
    return """[agents.planner]
harness = "codex"
model = "gpt-5.6-sol"
effort = "xhigh"

[agents.plan_assessor]
harness = "claude_code"
model = "opus"
effort = "high"

[agents.implementer.by_owner.claude_code]
harness = "claude_code"
model = "sonnet"
effort = "high"

[agents.implementer.by_owner.codex]
harness = "codex"
model = "gpt-5.6-luna"
effort = "high"

[agents.e2e.by_owner.claude_code]
harness = "claude_code"
model = "sonnet"
effort = "medium"

[agents.e2e.by_owner.codex]
harness = "codex"
model = "gpt-5.6-luna"
effort = "medium"

[agents.diff_reviewer.by_owner.claude_code]
harness = "claude_code"
model = "opus"
effort = "high"

[agents.diff_reviewer.by_owner.codex]
harness = "codex"
model = "gpt-5.6-sol"
effort = "high"

[agents.guard_reviewer.by_owner.claude_code]
harness = "claude_code"
model = "opus"
effort = "high"

[agents.guard_reviewer.by_owner.codex]
harness = "codex"
model = "gpt-5.6-sol"
effort = "high"

[agents.revision_fixer.by_owner.claude_code]
harness = "claude_code"
model = "sonnet"
effort = "high"

[agents.revision_fixer.by_owner.codex]
harness = "codex"
model = "gpt-5.6-luna"
effort = "high"
"""


def _preserved_routing_toml(workspace_toml_text: str | None) -> str:
    """Render existing routing without changing legacy mode during reconfigure."""
    if not workspace_toml_text:
        return ""
    try:
        data = tomllib.loads(workspace_toml_text)
    except tomllib.TOMLDecodeError:
        return ""
    agents = data.get("agents")
    lines: list[str] = []
    if isinstance(agents, dict):
        for profile, raw in agents.items():
            if not isinstance(raw, dict):
                continue
            by_owner = raw.get("by_owner")
            routes = (
                [(f"agents.{profile}.by_owner.{owner}", route) for owner, route in by_owner.items()]
                if isinstance(by_owner, dict)
                else [(f"agents.{profile}", raw)]
            )
            for heading, route in routes:
                if not isinstance(route, dict):
                    continue
                lines.append(f"[{heading}]")
                for key in ("harness", "model", "effort"):
                    value = route.get(key)
                    if isinstance(value, str):
                        lines.append(f"{key} = {_toml_escape(value)}")
                lines.append("")
    models = data.get("models")
    if isinstance(models, dict):
        lines.append("[models]")
        lines.extend(
            f"{key} = {_toml_escape(value)}"
            for key, value in models.items()
            if isinstance(value, str)
        )
        lines.append("")
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


# ─── Handler composition ────────────────────────────────────────────────────


def _legal_handler_string(value: str) -> bool:
    return parse_handler(value) is not None


def _preserved_handlers(
    existing_handlers: dict[str, str] | None, defaults: dict[str, str]
) -> tuple[dict[str, str], list[str]]:
    """Prior handlers that differ from the current registry default (reconfigure
    preservation). Returns (preserved, warning lines that carry value + default)."""
    preserved = {
        stage: val
        for stage, val in (existing_handlers or {}).items()
        if stage in defaults and val != defaults[stage]
    }
    lines = [
        f"reconfigure preserved {stage}={val} (registry default: {defaults[stage]})"
        for stage, val in preserved.items()
    ]
    return preserved, lines


def _parse_existing_handlers(workspace_toml_text: str | None) -> dict[str, str]:
    """Parse the prior `[pipeline.handlers]` table for reconfigure preservation.

    Fail-safe: None, a TOML parse error, or a missing/non-dict table yields `{}`,
    so a malformed prior workspace never crashes reconfigure (it falls back to
    registry defaults, the pre-preservation behavior)."""
    if workspace_toml_text is None:
        return {}
    try:
        data = tomllib.loads(workspace_toml_text)
    except tomllib.TOMLDecodeError:
        return {}
    pipeline = data.get("pipeline")
    handlers = pipeline.get("handlers") if isinstance(pipeline, dict) else None
    if not isinstance(handlers, dict):
        return {}
    return {stage: val for stage, val in handlers.items() if isinstance(val, str)}


def _compose_handlers(
    config: InitConfig,
    registry: list[StageEntry],
    pipeline_stages: list[str],
    discovery: DiscoveryResult,
    existing_handlers: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Return (handlers, warnings).

    bare: defaults from stage-registry.toml.
    custom: defaults + user overrides; rejects illegal handler strings.
    recommended: defaults + auto-overrides from discovered manifests; rejects
                 conflicts (more than one provider for any stage).

    On reconfigure, `existing_handlers` carries the prior workspace's handlers.
    Any stage whose prior value differs from the current registry default is
    preserved. Precedence: --handler > existing customization > manifest >
    default. `existing_handlers` None or {} (fresh init) is a no-op.
    """
    handlers: dict[str, str] = {
        s.name: s.default_handler for s in registry if s.name in pipeline_stages
    }
    warnings: list[str] = []
    preserved, preserved_warnings = _preserved_handlers(existing_handlers, dict(handlers))

    if config.bundle == "bare":
        handlers.update(preserved)
        warnings.extend(preserved_warnings)
        return handlers, warnings

    if config.bundle == "custom":
        handlers.update(preserved)
        warnings.extend(preserved_warnings)
        for stage, value in config.handler_overrides.items():
            if stage not in pipeline_stages:
                raise InitError(f"--handler {stage}=... but {stage!r} is not in pipeline.stages")
            if not _legal_handler_string(value):
                raise InitError(
                    f"--handler {stage}={value!r} is not a legal handler string "
                    f"(expected inline|none|subagent:*|skill:*)"
                )
            handlers[stage] = value
        return handlers, warnings

    # recommended
    stage_providers: dict[str, list[tuple[str, str]]] = {}
    for manifest in discovery.valid:
        for skill in manifest.skills:
            if skill.stage in pipeline_stages:
                stage_providers.setdefault(skill.stage, []).append(
                    (manifest.bundle_name, skill.handler_string)
                )

    covered_stages = 0
    for stage, providers in stage_providers.items():
        if len(providers) > 1:
            raise BundleConflictError(
                f"stage {stage!r} has multiple providers: "
                f"{[p[0] for p in providers]!r}; use --bundle custom to disambiguate"
            )
        bundle_name, handler_string = providers[0]
        if not _legal_handler_string(handler_string):
            raise InitError(
                f"bundle {bundle_name!r} provides an illegal handler for stage {stage!r}: "
                f"{handler_string!r} (expected inline|none|subagent:*|skill:<name>)"
            )
        handlers[stage] = handler_string
        covered_stages += 1

    if covered_stages == 0:
        # No-silent-degrade: --bundle recommended that resolves to zero stages
        # is functionally identical to bare. Refuse so the caller picks bare or
        # custom explicitly instead of silently getting bare defaults.
        raise InitError(
            "--bundle=recommended found no discovered manifests covering any "
            "pipeline stage; use --bundle bare for defaults or --bundle custom "
            "with explicit --handler overrides"
        )

    warnings.extend(f"manifest {err.path}: {err.reason}" for err in discovery.invalid)

    handlers.update(preserved)
    warnings.extend(preserved_warnings)
    return handlers, warnings


# ─── Beads init + verify ────────────────────────────────────────────────────


def _run_bd_init(
    config: InitConfig,
    runner: Runner,
) -> None:
    assert config.beads is not None
    result = runner(
        ["bd", "init", "--prefix", config.beads.prefix, "--skip-agents", "--non-interactive"],
        cwd=config.workspace_root,
        check=False,
    )
    if result.returncode != 0:
        raise InitError(
            f"bd init --prefix {config.beads.prefix} failed (rc={result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _verify_bd_ready(config: InitConfig, runner: Runner) -> None:
    result = runner(
        ["bd", "ready", "--json"],
        cwd=config.workspace_root,
        check=False,
    )
    if result.returncode != 0:
        raise InitError(
            f"bd ready --json failed (rc={result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise InitError(f"bd ready --json output is not valid JSON: {exc}") from exc


# ─── Checkpoint manifest append ─────────────────────────────────────────────


def _append_checkpoint_manifest(
    config: InitConfig,
    namespace: str,
    init_run_id: str,
) -> None:
    path = config.checkpoint_manifest_path or _default_checkpoint_manifest_path()
    workspace_root = str(config.workspace_root.resolve())
    # Idempotent on --resume: a crash between this append and recording the
    # progress phase must not double-count this init in the ledger.
    if _checkpoint_already_recorded(path, workspace_root, init_run_id):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = utcnow_iso()
    entry = {
        "ts": ts,
        "initialized_at": ts,
        "workspace_root": workspace_root,
        "init_run_id": init_run_id,
        "backend": config.backend,
        "namespace": namespace,
        "compounding": config.memory_compounding,
        "checkpoint_mode": _resolve_checkpoint_mode(config.backend, config.checkpoint_mode),
    }
    line = json.dumps(entry, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


# ─── Postconditions ─────────────────────────────────────────────────────────


def _verify_workspace_toml(
    workspace_toml: Path,
    expected_backend: BackendLiteral,
    expected_stages: list[str],
) -> None:
    raw = workspace_toml.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise InitError(f"workspace.toml does not parse: {exc}") from exc
    tracker = data.get("tracker", {})
    if not isinstance(tracker, dict) or tracker.get("backend") != expected_backend:
        raise InitError(f"[tracker] backend mismatch: expected {expected_backend!r}")
    pipeline = data.get("pipeline", {})
    if not isinstance(pipeline, dict):
        raise InitError("[pipeline] block missing")
    if pipeline.get("stages") != expected_stages:
        raise InitError("[pipeline.stages] mismatch with computed stages")
    handlers = pipeline.get("handlers")
    if not isinstance(handlers, dict):
        raise InitError("[pipeline.handlers] block missing")
    for stage in expected_stages:
        if stage not in handlers:
            raise InitError(f"[pipeline.handlers] missing entry for {stage!r}")
    memory = data.get("memory", {})
    if not isinstance(memory, dict) or not isinstance(memory.get("namespace"), str):
        raise InitError("[memory] block missing or namespace not a string")


# ─── Input validation ───────────────────────────────────────────────────────


# Backend alignment matrix: a jira workspace cannot be "personal" (that would
# dodge the work time-to-PR gate); a beads workspace cannot be "work". "scratch"
# opts out of both gates and is allowed for either backend.
_CHECKPOINT_MODES: dict[str, tuple[str, ...]] = {
    "jira": ("work", "scratch"),
    "beads": ("personal", "scratch"),
}
_CHECKPOINT_MODE_DEFAULT: dict[str, str] = {"jira": "work", "beads": "personal"}


def _resolve_checkpoint_mode(backend: str, mode: str | None) -> str:
    allowed = _CHECKPOINT_MODES.get(backend, ())
    if mode is None:
        return _CHECKPOINT_MODE_DEFAULT.get(backend, "scratch")
    if mode not in allowed:
        raise InitError(
            f"checkpoint_mode={mode!r} not allowed for backend={backend!r}; "
            f"allowed: {list(allowed)}"
        )
    return mode


def _validate_config(config: InitConfig) -> None:
    """Validate the resolved answer set. No side effects, safe to re-run."""
    if config.backend not in ("jira", "beads"):
        raise InitError(f"unknown backend {config.backend!r}")
    _resolve_checkpoint_mode(config.backend, config.checkpoint_mode)
    if config.backend == "jira" and config.jira is None:
        raise InitError("--backend=jira requires --jira-cloud-id + --jira-project-key")
    if config.backend == "beads" and config.beads is None:
        raise InitError("--backend=beads requires --beads-prefix")
    try:
        flow_launcher.runtime_layout.validate_namespace(_derive_default_namespace(config))
    except flow_launcher.runtime_layout.RuntimeLayoutError as exc:
        raise InitError(str(exc)) from exc
    if config.bundle not in ("bare", "recommended", "custom"):
        raise InitError(f"unknown bundle {config.bundle!r}")
    if config.bundle == "custom" and not config.handler_overrides:
        raise InitError("--bundle=custom requires at least one --handler stage=value")


# ─── Reconfigure backup / restore ───────────────────────────────────────────


@dataclass
class _FileBackup:
    """Prior state of one generated or managed file."""

    existed: bool
    content: bytes = b""
    mode: int | None = None


def _backup_file(path: Path) -> _FileBackup:
    if not path.exists():
        return _FileBackup(existed=False)
    return _FileBackup(
        existed=True,
        content=path.read_bytes(),
        mode=path.stat().st_mode & 0o7777,
    )


def _restore_file(path: Path, backup: _FileBackup) -> None:
    if backup.existed:
        assert backup.mode is not None
        if (
            path.exists()
            and path.read_bytes() == backup.content
            and path.stat().st_mode & 0o7777 == backup.mode
        ):
            return
        atomic_write_bytes(path, backup.content, mode=backup.mode)
    elif path.exists():
        path.unlink()


@dataclass
class _ReconfigureBackup:
    """Snapshot of the prior valid workspace so a failed reconfigure restores it.

    `.initialized` is intentionally NOT unlinked up front; finalize swaps it
    atomically. On failure the prior config, launcher metadata, executable modes,
    managed guidance, and any pre-existing transient markers are restored.
    """

    workspace_toml: str | None
    launcher: _FileBackup
    skill_root: _FileBackup
    agents_md: _FileBackup
    initializing: _FileBackup
    progress: _FileBackup


def _backup_for_reconfigure(root: Path) -> _ReconfigureBackup:
    toml_path = _workspace_toml_path(root)
    return _ReconfigureBackup(
        workspace_toml=(toml_path.read_text(encoding="utf-8") if toml_path.exists() else None),
        launcher=_backup_file(root / ".flow" / "runtime" / "flow"),
        skill_root=_backup_file(root / ".flow" / "runtime" / "skill-root"),
        agents_md=_backup_file(root / "AGENTS.md"),
        initializing=_backup_file(_marker_initializing(root)),
        progress=_backup_file(_progress_path(root)),
    )


def _restore_reconfigure_backup(root: Path, backup: _ReconfigureBackup) -> None:
    """Roll the workspace back to its pre-reconfigure state on failure."""
    toml_path = _workspace_toml_path(root)
    if backup.workspace_toml is not None:
        atomic_write_text(toml_path, backup.workspace_toml)
    elif toml_path.exists():
        toml_path.unlink()
    _restore_file(root / ".flow" / "runtime" / "flow", backup.launcher)
    _restore_file(root / ".flow" / "runtime" / "skill-root", backup.skill_root)
    _restore_file(root / "AGENTS.md", backup.agents_md)
    _restore_file(_marker_initializing(root), backup.initializing)
    _restore_file(_progress_path(root), backup.progress)


# ─── Idempotency helpers (resume) ────────────────────────────────────────────


def _checkpoint_already_recorded(path: Path, workspace_root: str, init_run_id: str) -> bool:
    """True if the checkpoint manifest already has an entry for this run.

    Guards `--resume` from appending a duplicate line when a crash landed
    between writing the checkpoint and recording the progress phase.
    """
    if not path.exists():
        return False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            entry.get("workspace_root") == workspace_root
            and entry.get("init_run_id") == init_run_id
        ):
            return True
    return False


def _bd_already_initialized(config: InitConfig, runner: Runner) -> bool:
    """True if `bd ready --json` already returns parseable JSON.

    Lets `--resume` skip a second `bd init` when the prior run created the bead
    store but crashed before recording the phase.
    """
    result = runner(
        ["bd", "ready", "--json"],
        cwd=config.workspace_root,
        check=False,
    )
    if result.returncode != 0:
        return False
    try:
        json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return False
    return True


# ─── Main orchestration ─────────────────────────────────────────────────────


def run_init(
    config: InitConfig,
    *,
    runner: Runner | None = None,
    resume: bool = False,
    reconfigure: bool = False,
) -> InitResult:
    """Drive the transactional init sequence. Returns InitResult on success.

    On failure, raises InitError (or subclass). For a plain or `--resume` run,
    `.flow/.initializing` and `.flow/.init-progress` remain on disk for a later
    `--resume`. For a failed `--reconfigure`, the prior config, launcher files,
    managed guidance, and any pre-existing transient markers are restored.
    """
    root = config.workspace_root.resolve()
    flow_dir = _flow_dir(root)
    initialized = _marker_initialized(root)
    initializing = _marker_initializing(root)

    if initialized.exists() and not reconfigure:
        raise InitPreflightError(f"{initialized} exists; pass --reconfigure to re-initialize")
    if initializing.exists() and not resume and not reconfigure:
        raise InitPreflightError(
            f"{initializing} exists from a prior interrupted init; "
            f"pass --resume to continue or --reconfigure to start over"
        )

    # Validate inputs BEFORE any marker is created. A config that fails
    # validation must not leave a `.initializing` marker that would then refuse
    # a plain re-run with the corrected config.
    _validate_config(config)
    try:
        selected_harness = flow_harness()
    except HarnessError as exc:
        raise InitError(str(exc)) from exc

    # Upgrade a legacy workspace before reconfiguration snapshots launcher
    # metadata or creates the selected namespace. This is intentionally a
    # separate, forward-only data migration: a later configuration failure
    # restores configuration/runtime files but never moves memory back to v1.
    if (
        reconfigure
        and _workspace_toml_path(root).is_file()
        and not flow_launcher.runtime_layout.is_v2(root)
    ):
        _install_launcher(root)

    # Back up every file this run may replace. `.initialized` stays in place until finalize swaps
    # it; on failure the other prior files are restored.
    reconfigure_backup: _ReconfigureBackup | None = None
    if reconfigure:
        reconfigure_backup = _backup_for_reconfigure(root)

    try:
        if reconfigure:
            progress = _progress_path(root)
            if progress.exists():
                progress.unlink()
            if initializing.exists():
                initializing.unlink()
        existing_handlers = (
            _parse_existing_handlers(reconfigure_backup.workspace_toml)
            if reconfigure_backup
            else {}
        )
        existing_routing = (
            _preserved_routing_toml(reconfigure_backup.workspace_toml)
            if reconfigure_backup
            else None
        )

        completed = _read_progress(root) if resume else set()

        flow_dir.mkdir(parents=True, exist_ok=True)
        init_run_id = _ensure_init_run_id(initializing)

        runner = runner or _default_runner()
        registry = _load_stage_registry()
        namespace = _derive_default_namespace(config)
        pipeline_stages = _default_pipeline_stages(registry, config.memory_compounding)

        def _run_phase(name: PhaseLiteral, fn: Callable[[], dict[str, Any] | None]) -> None:
            if name in completed:
                return
            extra = fn() or {}
            _append_progress(root, name, extra=extra)
            if name == "finalize":
                progress_path = _progress_path(root)
                if progress_path.exists():
                    progress_path.unlink()

        return _run_init_phases(
            config=config,
            runner=runner,
            registry=registry,
            namespace=namespace,
            pipeline_stages=pipeline_stages,
            existing_handlers=existing_handlers,
            routing_toml=(
                existing_routing
                if reconfigure
                else (_default_agent_routes_toml() if selected_harness != "generic" else "")
            )
            or "",
            init_run_id=init_run_id,
            root=root,
            flow_dir=flow_dir,
            initializing=initializing,
            initialized=initialized,
            run_phase=_run_phase,
        )
    except Exception:
        if reconfigure_backup is not None:
            _restore_reconfigure_backup(root, reconfigure_backup)
        raise


def _discover_and_compose(
    config: InitConfig,
    registry: list[StageEntry],
    pipeline_stages: list[str],
    root: Path,
    existing_handlers: dict[str, str],
) -> tuple[DiscoveryResult, dict[str, str], list[str]]:
    try:
        d = bundle_discover_run(roots=config.bundle_search_roots, repo_root=root)
    except HarnessError as exc:
        raise InitError(str(exc)) from exc
    h, w = _compose_handlers(config, registry, pipeline_stages, d, existing_handlers)
    return d, h, w


def _run_init_phases(
    *,
    config: InitConfig,
    runner: Runner,
    registry: list[StageEntry],
    namespace: str,
    pipeline_stages: list[str],
    existing_handlers: dict[str, str],
    routing_toml: str,
    init_run_id: str,
    root: Path,
    flow_dir: Path,
    initializing: Path,
    initialized: Path,
    run_phase: Callable[[PhaseLiteral, Callable[[], dict[str, Any] | None]], None],
) -> InitResult:
    _run_phase = run_phase
    discovery = DiscoveryResult()
    handlers: dict[str, str] = {}
    warnings: list[str] = []

    # Phase: validate_inputs (already enforced before any marker; re-run is a
    # no-op so --resume bookkeeping stays simple).
    def _phase_validate_inputs() -> dict[str, Any] | None:
        _validate_config(config)
        return None

    _run_phase("validate_inputs", _phase_validate_inputs)

    def _phase_bundle_compose() -> dict[str, Any] | None:
        nonlocal discovery, handlers, warnings
        discovery, handlers, warnings = _discover_and_compose(
            config, registry, pipeline_stages, root, existing_handlers
        )
        return {
            "bundle": config.bundle,
            "discovered_count": len(discovery.valid),
            "invalid_count": len(discovery.invalid),
        }

    _run_phase("bundle_compose", _phase_bundle_compose)
    # If resume skipped the phase, we still need handlers populated to write
    # the toml later. Recompute deterministically.
    if not handlers:
        discovery, handlers, warnings = _discover_and_compose(
            config, registry, pipeline_stages, root, existing_handlers
        )

    def _phase_mkdirs() -> dict[str, Any] | None:
        (flow_dir / "runs").mkdir(parents=True, exist_ok=True)
        (flow_dir / "memory" / namespace).mkdir(parents=True, exist_ok=True)
        (flow_dir / "memory" / namespace / "ship-events").mkdir(parents=True, exist_ok=True)
        _ensure_agents_md(root, requested=config.agents_md)
        return None

    _run_phase("mkdirs", _phase_mkdirs)

    # Phase: ensure_gitignore, keep transient .flow/ state and the worktree
    # pool (.claude/worktrees/) out of the project's git status.
    _run_phase("ensure_gitignore", lambda: _ensure_gitignore(root))

    # Phase: bd_init (beads only; jira records a skip)
    def _phase_bd_init() -> dict[str, Any] | None:
        if config.backend != "beads":
            return {"skipped": True, "reason": "backend is not beads"}
        # Idempotent on --resume: if a prior interrupted run already created the
        # bead store, `bd ready --json` parses and we skip re-running bd init.
        if _bd_already_initialized(config, runner):
            return {"skipped": True, "reason": "bd already initialized"}
        _run_bd_init(config, runner)
        return None

    _run_phase("bd_init", _phase_bd_init)

    def _phase_write_workspace_toml() -> dict[str, Any] | None:
        for stage, value in handlers.items():
            if not _legal_handler_string(value):
                raise InitError(f"refusing to write illegal handler for stage {stage!r}: {value!r}")
        content = _render_workspace_toml(
            config,
            namespace,
            pipeline_stages,
            handlers,
            routing_toml=routing_toml,
        )
        atomic_write_text(_workspace_toml_path(root), content)
        return None

    _run_phase("write_workspace_toml", _phase_write_workspace_toml)

    def _phase_verify_postconditions() -> dict[str, Any] | None:
        _verify_workspace_toml(_workspace_toml_path(root), config.backend, pipeline_stages)
        if config.backend == "beads":
            _verify_bd_ready(config, runner)
        return None

    _run_phase("verify_postconditions", _phase_verify_postconditions)

    # This intentionally sits outside the resumable phase ledger. A resumed init must repair
    # launcher files even when its recorded mkdirs phase is skipped. Install before the durable
    # checkpoint: a broken facade is not a completed initialization and must not create an auditable
    # success record.
    _install_launcher(root)

    def _phase_append_checkpoint() -> dict[str, Any] | None:
        _append_checkpoint_manifest(config, namespace, init_run_id)
        return None

    _run_phase("append_checkpoint", _phase_append_checkpoint)

    # Phase: finalize, atomic rename .initializing → .initialized
    def _phase_finalize() -> dict[str, Any] | None:
        os.replace(initializing, initialized)
        return None

    _run_phase("finalize", _phase_finalize)

    return InitResult(
        workspace_toml_path=_workspace_toml_path(root),
        handlers=handlers,
        namespace=namespace,
        discovery_warnings=warnings,
    )


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_handler_overrides(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise InitError(f"--handler expects stage=value, got {raw!r}")
        stage, _, value = raw.partition("=")
        stage = stage.strip()
        value = value.strip()
        if not stage or not value:
            raise InitError(f"--handler stage and value must be non-empty: {raw!r}")
        out[stage] = value
    return out


def _coerce_search_roots(value: object) -> list[Path] | None:
    """Normalize --bundle-search-roots from CLI string OR --config JSON list.

    CLI passes a `:`-separated string; a --config file may already hand us a
    JSON list. Either way return list[Path] (or None when unset).
    """
    if value is None:
        return None
    if isinstance(value, list):
        return [Path(str(p)).expanduser() for p in value if str(p)]
    if isinstance(value, str):
        return [Path(p).expanduser() for p in value.split(":") if p]
    raise InitError(f"--bundle-search-roots must be a string or list, got {type(value).__name__}")


def _coerce_checkpoint_path(value: object) -> Path | None:
    """Normalize --checkpoint-manifest from a string or a single-element list."""
    if value is None:
        return None
    if isinstance(value, list):
        if len(value) != 1:
            raise InitError("--checkpoint-manifest list must hold exactly one path")
        value = value[0]
    if isinstance(value, str):
        return Path(value).resolve()
    raise InitError(f"--checkpoint-manifest must be a string, got {type(value).__name__}")


def _build_config_from_args(args: argparse.Namespace) -> InitConfig:
    workspace_root = Path(args.workspace_root or os.getcwd()).resolve()

    jira: JiraConfig | None = None
    beads: BeadsConfig | None = None

    if args.backend == "jira":
        if not args.jira_cloud_id or not args.jira_project_key:
            raise InitError("--backend=jira requires --jira-cloud-id and --jira-project-key")
        jira = JiraConfig(
            cloud_id=args.jira_cloud_id,
            project_key=args.jira_project_key,
            assignee_account_id=args.jira_assignee_account_id or None,
        )
    elif args.backend == "beads":
        prefix = args.beads_prefix or _derive_beads_prefix(workspace_root)
        beads = BeadsConfig(prefix=prefix)

    overrides = _parse_handler_overrides(args.handler or [])

    compounding = True
    if args.memory_compounding is not None:
        v = args.memory_compounding.lower()
        if v not in ("true", "false"):
            raise InitError("--memory-compounding must be 'true' or 'false'")
        compounding = v == "true"

    return InitConfig(
        backend=args.backend,
        bundle=args.bundle,
        workspace_root=workspace_root,
        jira=jira,
        beads=beads,
        handler_overrides=overrides,
        memory_namespace=args.memory_namespace or None,
        memory_compounding=compounding,
        checkpoint_mode=args.checkpoint_mode or None,
        checkpoint_manifest_path=_coerce_checkpoint_path(args.checkpoint_manifest),
        bundle_search_roots=_coerce_search_roots(args.bundle_search_roots),
        agents_md=args.agents_md,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flow workspace setup — transactional workspace bootstrap.",
    )
    parser.add_argument("--backend", choices=("jira", "beads"), required=False)
    parser.add_argument("--bundle", choices=("bare", "recommended", "custom"), required=False)
    parser.add_argument("--workspace-root", default=None)
    parser.add_argument(
        "--checkpoint-mode",
        choices=("personal", "work", "scratch"),
        default=None,
        help="14-day-gate participation mode; derived from backend if omitted.",
    )

    parser.add_argument("--jira-cloud-id", default=None)
    parser.add_argument("--jira-project-key", default=None)
    parser.add_argument("--jira-assignee-account-id", default=None)

    parser.add_argument("--beads-prefix", default=None)

    parser.add_argument(
        "--handler",
        action="append",
        help="stage=value (e.g. create_pr=skill:ship-it:create); repeatable",
    )

    parser.add_argument("--memory-namespace", default=None)
    parser.add_argument("--memory-compounding", default=None)

    parser.add_argument("--config", default=None, help="path to JSON file with all answers")
    parser.add_argument(
        "--checkpoint-manifest",
        default=None,
        help="override default checkpoint-manifest path (tests)",
    )
    parser.add_argument(
        "--bundle-search-roots",
        default=None,
        help="colon-separated dirs (overrides defaults; tests)",
    )

    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--reconfigure", action="store_true")
    parser.add_argument(
        "--agents-md",
        action="store_true",
        help="write the cross-harness AGENTS.md entry point (off by default; "
        "for repos run through Cursor/Windsurf/opencode/etc. — Claude Code does not need it)",
    )
    parser.add_argument(
        "--guidance-only",
        action="store_true",
        help="install or refresh managed AGENTS.md guidance in an initialized workspace",
    )
    return parser.parse_args(argv)


def _load_config_file(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise InitError(f"--config {path} root is not a JSON object")
    return data


def _merge_config_file(args: argparse.Namespace, config_data: dict[str, Any]) -> None:
    """CLI flags take precedence over --config file values."""
    for key, value in config_data.items():
        attr = key.replace("-", "_")
        if hasattr(args, attr) and getattr(args, attr) in (None, False, []):
            setattr(args, attr, value)


def cli_main(argv: list[str]) -> int:
    try:
        args = _parse_args(argv)

        if args.config:
            _merge_config_file(args, _load_config_file(Path(args.config).expanduser()))

        if args.guidance_only:
            if not args.workspace_root:
                sys.stderr.write("--workspace-root is required with --guidance-only\n")
                return 2
            root = Path(args.workspace_root).expanduser().resolve()
            if (
                not (root / ".flow" / ".initialized").is_file()
                or not (root / ".flow" / "workspace.toml").is_file()
            ):
                sys.stderr.write("guidance requires an initialized workspace\n")
                return 1
            outcome = _ensure_agents_md(root, requested=True)
            sys.stdout.write(
                json.dumps(
                    {
                        "changed": outcome is None,
                        "guidance": str(root / "AGENTS.md"),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            return 0

        if not args.backend:
            sys.stderr.write("--backend is required (jira | beads)\n")
            return 2
        if not args.bundle:
            sys.stderr.write("--bundle is required (bare | recommended | custom)\n")
            return 2

        config = _build_config_from_args(args)
        result = run_init(
            config,
            resume=args.resume,
            reconfigure=args.reconfigure,
        )
    except InitPreflightError as exc:
        sys.stderr.write(f"init pre-flight: {exc}\n")
        return 4
    except BundleConflictError as exc:
        sys.stderr.write(f"bundle conflict: {exc}\n")
        return 3
    except InitError as exc:
        sys.stderr.write(f"init failed: {exc}\n")
        return 1
    except Exception as exc:
        sys.stderr.write(f"init crashed: {type(exc).__name__}: {exc}\n")
        return 1

    payload = {
        "workspace_toml": str(result.workspace_toml_path),
        "handlers": result.handlers,
        "namespace": result.namespace,
        "warnings": result.discovery_warnings,
    }
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "BeadsConfig",
    "BundleConflictError",
    "InitConfig",
    "InitError",
    "InitPreflightError",
    "InitResult",
    "JiraConfig",
    "cli_main",
    "run_init",
]
