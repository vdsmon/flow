"""Execute exact routed cognition inside isolated read-only capsules.

The public seam intentionally stays small: callers submit a closed ``WorkOrder``
and an ``OwnerProof`` to ``CognitiveWorkers.run`` or cancel the logical
invocation. Route resolution, prompts, provider commands, process lifecycle,
typed validation, Git guards, journals, and capsule disposal remain private to
this module.

Read-only roles and the disposable E2E writer are active. The E2E writer runs in a
write-capable capsule seeded with the ticket's uncommitted working state, captures the
recipe's own mutations as report evidence, imports nothing, and discards the capsule. The
four importing writer policies are present so route snapshots stay complete, but requests
for them fail before a capsule directory is allocated.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from _atomicio import atomic_write_bytes, atomic_write_text
from _locking import LockContention, flock_blocking, flock_retry

PROMPTS_PATH = Path(__file__).with_name("cognitive_worker_prompts.json")
WORK_ORDER_SCHEMA = "flow.cognitive-work-order/v1"
OUTCOME_SCHEMA = "flow.cognitive-work-outcome/v1"
REVIEW_BUNDLE_SCHEMA = "flow.review-input-bundle/v1"
STAGE_OUTCOMES_SCHEMA = "flow.cognitive-stage-outcomes/v1"
CHANGE_RECEIPT_SCHEMA = "flow.cognitive-change-receipt/v1"
WRITER_IMPORT_LOCK_NAME = "flow-writer-import.lock"
JOURNAL_SCHEMA = "flow.cognitive-worker-journal/v1"
SEED_BASELINE_REF = "refs/flow/seed-baseline"
SOFT_TIMEOUT_SECONDS = 10 * 60
HARD_TIMEOUT_SECONDS = 40 * 60
TERMINATION_GRACE_SECONDS = 5.0

Authority = Literal["read_only", "capsule_writer", "disposable_writer"]


class WorkerFailure(RuntimeError):
    """A work order cannot be executed without weakening its contract."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_order",
        attempts: tuple[dict[str, Any], ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.attempts = attempts


@dataclass(frozen=True)
class RolePolicy:
    profile: str
    authority: Authority
    active: bool
    prompt_builder: str
    result_schema: str
    retry_limit: int


_READERS = {
    "planner": ("planner", "plan-envelope/v1"),
    "plan_assessor": ("plan_assessor", "plan-assessment/v1"),
    "code_reviewer": ("code_reviewer", "review-findings/v1"),
    "diff_reviewer": ("diff_reviewer", "review-findings/v1"),
    "guard_reviewer": ("guard_reviewer", "guard-verdict/v1"),
    "review_brief_author": ("review_brief_author", "review-brief-content/v1"),
    "reflector": ("reflector", "reflection-actions/v1"),
}
_WRITERS = {
    "implementer": "implementation-report/v1",
    "review_fixer": "fix-report/v1",
    "revision_fixer": "revision-report/v1",
    "machinery_fixer": "machinery-fix-report/v1",
}

ROLE_CATALOG: dict[str, RolePolicy] = {
    name: RolePolicy(name, "read_only", True, builder, schema, 1)
    for name, (builder, schema) in _READERS.items()
}
ROLE_CATALOG.update(
    {
        name: RolePolicy(name, "capsule_writer", False, name, schema, 0)
        for name, schema in _WRITERS.items()
    }
)
ROLE_CATALOG["e2e"] = RolePolicy("e2e", "disposable_writer", True, "e2e", "e2e-report/v1", 0)
# The implementer, review_fixer, and revision_fixer are activated importing writers: each
# validated capsule patch is imported into the authoritative worktree under the sole-writer
# claim. machinery_fixer stays shadowed (active=False) for Phase 5.
ROLE_CATALOG["implementer"] = RolePolicy(
    "implementer", "capsule_writer", True, "implementer", "implementation-report/v1", 0
)
ROLE_CATALOG["review_fixer"] = RolePolicy(
    "review_fixer", "capsule_writer", True, "review_fixer", "fix-report/v1", 0
)
ROLE_CATALOG["revision_fixer"] = RolePolicy(
    "revision_fixer", "capsule_writer", True, "revision_fixer", "revision-report/v1", 0
)

# Preserve the public profile order used by agent_routes.py.
ROLE_CATALOG = {
    name: ROLE_CATALOG[name]
    for name in (
        "planner",
        "plan_assessor",
        "implementer",
        "e2e",
        "code_reviewer",
        "diff_reviewer",
        "guard_reviewer",
        "review_fixer",
        "revision_fixer",
        "review_brief_author",
        "reflector",
        "machinery_fixer",
    )
}
ACTIVE_READ_ONLY_PROFILES = tuple(name for name, policy in ROLE_CATALOG.items() if policy.active)


def _resolve_authority(
    profile: str, authority: str, allowed: tuple[str, ...]
) -> tuple[str, tuple[str, ...]]:
    """Pin an order's authority to its profile and vet its writer mutation paths.

    An empty authority resolves to the catalog value; a non-empty one that disagrees is a
    forgery. Allowed paths must be empty for a read_only order, and every writer entry must be
    a repo-relative POSIX path with no leading slash and no ``..`` segment. Paths are never
    resolved or touched on disk here.
    """
    expected = ROLE_CATALOG[profile].authority
    resolved = authority or expected
    if resolved != expected:
        raise WorkerFailure(
            f"work-order authority {authority!r} does not match profile {profile!r}"
        )
    paths = tuple(allowed)
    if resolved == "read_only" and paths:
        raise WorkerFailure("a read-only work order cannot allow mutation paths")
    for entry in paths:
        if not isinstance(entry, str) or not entry or entry.startswith("/"):
            raise WorkerFailure(f"allowed mutation path {entry!r} is not repo-relative")
        if ".." in entry.split("/"):
            raise WorkerFailure(f"allowed mutation path {entry!r} escapes the repository")
    return resolved, paths


def _validate_seed(authority: str, seed_patch: str | None, seed_digest: str | None) -> None:
    """Vet an optional seed patch reference against its authority; nothing is read here.

    Only a writer order may seed its capsule with the ticket's uncommitted working state. The
    patch path must be absolute and pinned by an exact digest, so the seed sealed at dispatch
    cannot be swapped before the executor applies it.
    """
    if seed_patch is None:
        if seed_digest is not None:
            raise WorkerFailure("work-order seed digest requires a seed patch")
        return
    if authority == "read_only":
        raise WorkerFailure("a read-only work order cannot carry a seed patch")
    if not Path(seed_patch).is_absolute():
        raise WorkerFailure("work-order seed patch path must be absolute")
    if not isinstance(seed_digest, str) or len(seed_digest) != 64:
        raise WorkerFailure("a seeded work order requires an exact seed digest")


@dataclass(frozen=True)
class OwnerProof:
    owner_id: str
    harness: str
    run_id: str | None = None
    lease_fence: str | None = None

    def __post_init__(self) -> None:
        if not self.owner_id.strip() or self.harness not in {"codex", "claude_code"}:
            raise WorkerFailure("owner proof requires an ID and a supported harness")


@dataclass(frozen=True)
class WorkOrder:
    logical_invocation_id: str
    generation: int
    profile: str
    source_root: str
    source_sha: str
    route: dict[str, str]
    route_snapshot_digest: str
    input_bundle: str
    input_digest: str
    facts: dict[str, Any]
    authority: str = ""
    allowed_mutation_paths: tuple[str, ...] = ()
    seed_patch: str | None = None
    seed_digest: str | None = None
    schema: str = WORK_ORDER_SCHEMA
    run_id: str | None = None
    stage: str | None = None
    substep: str | None = None
    stage_generation: int = 0
    expected_state: str | None = None
    lease_fence: str | None = None
    challenge_digest: str | None = None
    result_schema: dict[str, Any] | None = None
    provider_prompt: str | None = None
    fresh_provider_prompt: str | None = None
    session: dict[str, str | None] | None = None

    def __post_init__(self) -> None:
        if self.schema != WORK_ORDER_SCHEMA:
            raise WorkerFailure(f"unsupported work-order schema {self.schema!r}")
        if not self.logical_invocation_id.strip() or self.generation < 1:
            raise WorkerFailure("work order requires an invocation ID and positive generation")
        if self.profile not in ROLE_CATALOG:
            raise WorkerFailure(f"unknown cognitive profile {self.profile!r}")
        authority, paths = _resolve_authority(
            self.profile, self.authority, self.allowed_mutation_paths
        )
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "allowed_mutation_paths", paths)
        if not Path(self.source_root).is_absolute() or not Path(self.input_bundle).is_absolute():
            raise WorkerFailure("work-order source and input paths must be absolute")
        _validate_seed(authority, self.seed_patch, self.seed_digest)
        if len(self.source_sha) != 40 or len(self.route_snapshot_digest) != 64:
            raise WorkerFailure("work order requires exact source and route digests")
        if len(self.input_digest) != 64:
            raise WorkerFailure("work order requires an exact input digest")
        if set(self.route) != {"harness", "model", "effort"}:
            raise WorkerFailure("work-order route must be an exact harness/model/effort triple")
        if self.route["harness"] not in {"codex", "claude_code"}:
            raise WorkerFailure("work-order route names an unsupported harness")
        if self.provider_prompt is not None and self.profile != "planner":
            raise WorkerFailure("only the planner compatibility order may bind a provider prompt")
        if self.session is not None:
            if self.profile != "planner" or set(self.session) != {
                "thread_id",
                "initial_session_id",
                "fresh_session_id",
            }:
                raise WorkerFailure("planner session fields do not match the closed contract")
            if not self.session.get("initial_session_id") or not self.session.get(
                "fresh_session_id"
            ):
                raise WorkerFailure("planner session requires initial and fresh session IDs")
            if self.session.get("thread_id") and not self.fresh_provider_prompt:
                raise WorkerFailure("a resumed planner requires a complete fresh prompt")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> WorkOrder:
        try:
            fields = {
                key: value[key]
                for key in (
                    "logical_invocation_id",
                    "generation",
                    "profile",
                    "source_root",
                    "source_sha",
                    "route",
                    "route_snapshot_digest",
                    "input_bundle",
                    "input_digest",
                    "facts",
                )
            }
        except KeyError as exc:
            raise WorkerFailure(f"work order missing {exc.args[0]!r}") from exc
        optional = {
            key: value[key]
            for key in (
                "authority",
                "allowed_mutation_paths",
                "seed_patch",
                "seed_digest",
                "schema",
                "run_id",
                "stage",
                "substep",
                "stage_generation",
                "expected_state",
                "lease_fence",
                "challenge_digest",
                "result_schema",
                "provider_prompt",
                "fresh_provider_prompt",
                "session",
            )
            if key in value
        }
        return cls(**fields, **optional)

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PromptMaterial:
    builder_id: str
    template_digest: str
    facts_digest: str
    artifact_digests: dict[str, str]
    schema_digest: str
    prompt: str
    prompt_digest: str

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass(frozen=True)
class WorkOutcome:
    logical_invocation_id: str
    generation: int
    profile: str
    status: Literal["succeeded", "needs_input", "failed", "cancelled"]
    result: dict[str, Any] | None
    receipts: dict[str, Any]
    failure: dict[str, Any] | None = None
    run_id: str | None = None
    stage: str | None = None
    substep: str | None = None
    stage_generation: int = 0
    route_snapshot_digest: str | None = None
    source_sha: str | None = None
    lease_fence: str | None = None
    input_bundle: str | None = None
    input_digest: str | None = None
    schema: str = OUTCOME_SCHEMA
    digest: str = field(default="", compare=False)

    def to_mapping(self) -> dict[str, Any]:
        body = asdict(self)
        body.pop("digest", None)
        body["digest"] = _digest(body)
        return body


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _input_digest(path: Path) -> str:
    if path.is_file():
        return _file_digest(path)
    manifest_path = path / "manifest.json"
    if not path.is_dir() or not manifest_path.is_file():
        raise WorkerFailure("work-order input is neither a file nor an immutable bundle")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerFailure(f"work-order input manifest is invalid: {exc}") from exc
    body = {key: value for key, value in manifest.items() if key != "digest"}
    if manifest.get("digest") != _digest(body):
        raise WorkerFailure("work-order input manifest digest is invalid")
    # A self-consistent manifest says nothing about the bytes the reviewer will read. Verify
    # every raw capture and blob against the digest the manifest recorded for it.
    expected: list[tuple[Path, str]] = [
        (path / "raw" / name, str(receipt["sha256"]))
        for name, receipt in cast(dict[str, Any], manifest.get("raw", {})).items()
    ]
    expected.extend(
        (path / str(item["blob"]), str(item["sha256"]))
        for item in cast(list[dict[str, Any]], manifest.get("untracked", []))
        if item.get("blob")
    )
    for target, digest in expected:
        try:
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise WorkerFailure(f"review bundle content is not its recorded {target.name}")
        except OSError as exc:
            raise WorkerFailure(f"review bundle is missing {target.name}: {exc}") from exc
    return str(manifest["digest"])


def _atomic_json(path: Path, value: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(mode)


def _prompt_entries() -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerFailure(f"cannot load cognitive prompt catalog: {exc}") from exc
    if value.get("schema") != "flow.cognitive-worker-prompts/v1":
        raise WorkerFailure("cognitive prompt catalog has the wrong schema")
    entries = value.get("entries")
    if not isinstance(entries, dict):
        raise WorkerFailure("cognitive prompt catalog has no entries")
    return cast(dict[str, dict[str, Any]], entries)


_FACT_KEYS: dict[str, frozenset[str]] = {
    "planner": frozenset(
        {
            "stage_plan",
            "ticket",
            "base_sha",
            "route",
            "current_envelope",
            "feedback_ledger",
            "version_requirements",
            "approved_design_digest",
            "mode",
        }
    ),
    "plan_assessor": frozenset(
        {
            "ticket",
            "base_sha",
            "route_digest",
            "candidate_plan",
            "planner_receipt",
            "assessment_rubric",
        }
    ),
    "code_reviewer": frozenset(
        {"stage_code_review", "ticket", "accepted_plan", "source_sha", "review_bundle"}
    ),
    "diff_reviewer": frozenset({"source_sha", "review_bundle", "review_rubric"}),
    "guard_reviewer": frozenset({"probe", "guard_diff", "guard_properties"}),
    "review_brief_author": frozenset(
        {"ticket", "plan", "pr", "review", "e2e", "ci", "content_contract"}
    ),
    "reflector": frozenset({"reflection_input", "stage_reflect", "action_contract"}),
    "e2e": frozenset({"stage_e2e", "ticket", "source_sha", "e2e_recipe", "evidence_contract"}),
    "implementer": frozenset(
        {"stage_implement", "ticket", "source_sha", "plan", "planned_files", "report_contract"}
    ),
    "review_fixer": frozenset(
        {
            "stage_review_loop",
            "ticket",
            "source_sha",
            "review_findings",
            "planned_files",
            "report_contract",
        }
    ),
    "revision_fixer": frozenset(
        {
            "stage_review_loop",
            "ticket",
            "source_sha",
            "revision_instruction",
            "planned_files",
            "report_contract",
        }
    ),
}


def _build_prompt(profile: str, facts: Mapping[str, Any]) -> PromptMaterial:
    expected = _FACT_KEYS[profile]
    unknown = set(facts) - expected
    missing = expected - set(facts)
    if unknown:
        raise WorkerFailure(f"{profile} prompt has unknown facts: {', '.join(sorted(unknown))}")
    if missing:
        raise WorkerFailure(f"{profile} prompt is missing facts: {', '.join(sorted(missing))}")
    entry = _prompt_entries().get(profile)
    if not isinstance(entry, dict) or not isinstance(entry.get("instruction"), str):
        raise WorkerFailure(f"prompt catalog is missing the {profile!r} entry")
    version = entry.get("version")
    if not isinstance(version, int) or version < 1:
        raise WorkerFailure(f"prompt entry {profile!r} has an invalid version")
    schema = provider_schema(profile)
    instruction = entry["instruction"].strip()
    canonical_facts = {key: facts[key] for key in sorted(facts)}
    prompt = (
        f"FLOW COGNITIVE ROLE: {profile}\n"
        f"PROMPT VERSION: {version}\n"
        f"{instruction}\n\nIMMUTABLE FACTS\n"
        + json.dumps(canonical_facts, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    )
    artifacts = {
        key: _digest(value)
        for key, value in canonical_facts.items()
        if key.endswith(("_plan", "_receipt", "_bundle", "_input", "_diff"))
    }
    return PromptMaterial(
        builder_id=f"{profile}/v{version}",
        template_digest=_digest(entry),
        facts_digest=_digest(canonical_facts),
        artifact_digests=artifacts,
        schema_digest=_digest(schema),
        prompt=prompt,
        prompt_digest=hashlib.sha256(prompt.encode()).hexdigest(),
    )


def build_planner_prompt(facts: Mapping[str, Any]) -> PromptMaterial:
    return _build_prompt("planner", facts)


def build_plan_assessor_prompt(facts: Mapping[str, Any]) -> PromptMaterial:
    return _build_prompt("plan_assessor", facts)


def build_code_reviewer_prompt(facts: Mapping[str, Any]) -> PromptMaterial:
    return _build_prompt("code_reviewer", facts)


def build_diff_reviewer_prompt(facts: Mapping[str, Any]) -> PromptMaterial:
    return _build_prompt("diff_reviewer", facts)


def build_guard_reviewer_prompt(facts: Mapping[str, Any]) -> PromptMaterial:
    return _build_prompt("guard_reviewer", facts)


def build_review_brief_author_prompt(facts: Mapping[str, Any]) -> PromptMaterial:
    return _build_prompt("review_brief_author", facts)


def build_reflector_prompt(facts: Mapping[str, Any]) -> PromptMaterial:
    return _build_prompt("reflector", facts)


def build_e2e_prompt(facts: Mapping[str, Any]) -> PromptMaterial:
    return _build_prompt("e2e", facts)


def build_implementer_prompt(facts: Mapping[str, Any]) -> PromptMaterial:
    return _build_prompt("implementer", facts)


def build_review_fixer_prompt(facts: Mapping[str, Any]) -> PromptMaterial:
    return _build_prompt("review_fixer", facts)


def build_revision_fixer_prompt(facts: Mapping[str, Any]) -> PromptMaterial:
    return _build_prompt("revision_fixer", facts)


def _bound_provider_prompt(
    prompt: str, *, facts: Mapping[str, Any], schema: Mapping[str, Any]
) -> PromptMaterial:
    """Bind the historical planner prompt without letting callers append to it.

    ``planner-worker`` predates the closed fact builders and its public surface accepts
    one complete prompt file. The compatibility wrapper may bind that whole file, but
    it cannot add a suffix or select a different schema after the work order is sealed.
    """
    if not prompt.strip():
        raise WorkerFailure("planner provider prompt must not be empty")
    prompt_digest = hashlib.sha256(prompt.encode()).hexdigest()
    return PromptMaterial(
        builder_id="planner-worker-compat/v1",
        template_digest=prompt_digest,
        facts_digest=_digest(facts),
        artifact_digests={"provider_prompt": prompt_digest},
        schema_digest=_digest(schema),
        prompt=prompt,
        prompt_digest=prompt_digest,
    )


PROMPT_BUILDERS: dict[str, Callable[[Mapping[str, Any]], PromptMaterial]] = {
    "planner": build_planner_prompt,
    "plan_assessor": build_plan_assessor_prompt,
    "code_reviewer": build_code_reviewer_prompt,
    "diff_reviewer": build_diff_reviewer_prompt,
    "guard_reviewer": build_guard_reviewer_prompt,
    "review_brief_author": build_review_brief_author_prompt,
    "reflector": build_reflector_prompt,
    "e2e": build_e2e_prompt,
    "implementer": build_implementer_prompt,
    "review_fixer": build_review_fixer_prompt,
    "revision_fixer": build_revision_fixer_prompt,
}


def _closed_object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_TEXT = {"type": "string", "minLength": 1}
_FINDING = _closed_object(
    {
        "id": _TEXT,
        "severity": {"type": "string", "enum": ["minor", "major", "critical"]},
        "title": _TEXT,
        "body": _TEXT,
        "file": {"type": ["string", "null"]},
        "line": {"type": ["integer", "null"], "minimum": 1},
    },
    ["id", "severity", "title", "body", "file", "line"],
)


def provider_schema(profile: str) -> dict[str, Any]:
    """Return the closed provider-facing schema for one active profile."""
    if profile == "planner":
        import planning_attempt

        return planning_attempt.envelope_json_schema()
    if profile == "plan_assessor":
        return _closed_object(
            {
                "verdict": {"type": "string", "enum": ["approve", "revise"]},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "summary": _TEXT,
                "findings": {"type": "array", "items": _FINDING},
                "assessed_plan_digest": _TEXT,
            },
            ["verdict", "confidence", "summary", "findings", "assessed_plan_digest"],
        )
    if profile in {"code_reviewer", "diff_reviewer"}:
        return _closed_object(
            {
                "verdict": {"type": "string", "enum": ["clean", "findings"]},
                "summary": _TEXT,
                "findings": {"type": "array", "items": _FINDING},
                "input_digest": _TEXT,
            },
            ["verdict", "summary", "findings", "input_digest"],
        )
    if profile == "guard_reviewer":
        return _closed_object(
            {
                "verdict": {"type": "string", "enum": ["clean", "block"]},
                "summary": _TEXT,
                "findings": {"type": "array", "items": _FINDING},
                "guard_digest": _TEXT,
            },
            ["verdict", "summary", "findings", "guard_digest"],
        )
    if profile == "e2e":
        # The disposable E2E writer authors only the recipe verdict, its evidence, and a binding
        # source SHA. Flow, not the model, captures the capsule mutation summary and attaches it
        # to the result after validation, so it is deliberately absent from this model-facing
        # schema (see _run_locked's disposable_writer branch).
        return _closed_object(
            {
                "verdict": {"type": "string", "enum": ["pass", "fail"]},
                "summary": _TEXT,
                "evidence": _TEXT,
                "source_sha": _TEXT,
            },
            ["verdict", "summary", "evidence", "source_sha"],
        )
    if profile == "implementer":
        # The importing implementer authors only a report: a summary, the evidence of what it
        # built (files, tests, green result), and a binding source SHA. The change receipt (patch
        # digest, touched paths, import result) is Flow-captured by _import_after_validation, never
        # model-authored, so it is deliberately absent from this model-facing schema.
        return _closed_object(
            {
                "summary": _TEXT,
                "evidence": _TEXT,
                "source_sha": _TEXT,
            },
            ["summary", "evidence", "source_sha"],
        )
    if profile in {"review_fixer", "revision_fixer"}:
        # An importing fixer (a review finding fix or a revision instruction) authors only a
        # report: a summary, the evidence of what it changed, and a binding source SHA. Flow, not
        # the model, captures the change receipt, so it stays out of this model-facing schema, the
        # same closed report shape as the implementer.
        return _closed_object(
            {
                "summary": _TEXT,
                "evidence": _TEXT,
                "source_sha": _TEXT,
            },
            ["summary", "evidence", "source_sha"],
        )
    if profile == "review_brief_author":
        import review_brief

        return review_brief.provider_schema()
    if profile == "reflector":
        action = _closed_object(
            {
                "id": _TEXT,
                "kind": {
                    "type": "string",
                    "enum": [
                        "knowledge",
                        "supersession",
                        "recall_usage",
                        "project_rule_proposal",
                        "ship_event",
                        "machinery_finding",
                    ],
                },
                "payload": _closed_object(
                    {
                        "type": {"type": ["string", "null"]},
                        "body": {"type": ["string", "null"]},
                        "target_id": {"type": ["string", "null"]},
                        "superseded_by": {"type": "array", "items": _TEXT},
                        "used_ids": {"type": "array", "items": _TEXT},
                        "proposal": {"type": ["string", "null"]},
                        "evidence": {"type": ["string", "null"]},
                        "status": {"type": ["string", "null"]},
                    },
                    [],
                ),
            },
            ["id", "kind", "payload"],
        )
        return _closed_object(
            {"summary": _TEXT, "actions": {"type": "array", "items": action}},
            ["summary", "actions"],
        )
    raise WorkerFailure(f"profile {profile!r} has no active provider schema")


def validate_typed_result(profile: str, value: object) -> dict[str, Any]:  # noqa: C901
    """Apply semantic checks that provider JSON schemas cannot express."""
    if not isinstance(value, dict):
        raise WorkerFailure("worker result must be an object", code="invalid_result")
    if profile == "planner":
        import planning_attempt

        try:
            return planning_attempt.PlanEnvelope.from_mapping(
                cast(dict[str, Any], value)
            ).to_mapping()
        except planning_attempt.AttemptError as exc:
            raise WorkerFailure(f"planner result is invalid: {exc}", code="invalid_result") from exc
    contracts: dict[str, tuple[set[str], dict[str, set[str]]]] = {
        "plan_assessor": (
            {"verdict", "confidence", "summary", "findings", "assessed_plan_digest"},
            {"verdict": {"approve", "revise"}, "confidence": {"low", "medium", "high"}},
        ),
        "code_reviewer": (
            {"verdict", "summary", "findings", "input_digest"},
            {"verdict": {"clean", "findings"}},
        ),
        "diff_reviewer": (
            {"verdict", "summary", "findings", "input_digest"},
            {"verdict": {"clean", "findings"}},
        ),
        "guard_reviewer": (
            {"verdict", "summary", "findings", "guard_digest"},
            {"verdict": {"clean", "block"}},
        ),
        "reflector": ({"summary", "actions"}, {}),
        "e2e": (
            {"verdict", "summary", "evidence", "source_sha"},
            {"verdict": {"pass", "fail"}},
        ),
        "implementer": ({"summary", "evidence", "source_sha"}, {}),
        "review_fixer": ({"summary", "evidence", "source_sha"}, {}),
        "revision_fixer": ({"summary", "evidence", "source_sha"}, {}),
    }
    contract = contracts.get(profile)
    if contract is not None:
        expected, enums = contract
        if set(value) != expected:
            raise WorkerFailure(
                f"{profile} result fields do not match its closed contract",
                code="invalid_result",
            )
        for key, allowed in enums.items():
            if value.get(key) not in allowed:
                raise WorkerFailure(f"{profile} result has invalid {key}", code="invalid_result")
    if profile in {"plan_assessor", "code_reviewer", "diff_reviewer", "guard_reviewer"}:
        findings = value.get("findings")
        if not isinstance(findings, list):
            raise WorkerFailure("worker result findings must be an array", code="invalid_result")
        ids = [item.get("id") for item in findings if isinstance(item, dict)]
        if len(ids) != len(findings) or any(not isinstance(item, str) for item in ids):
            raise WorkerFailure("every finding requires a string ID", code="invalid_result")
        if len(set(ids)) != len(ids):
            raise WorkerFailure(
                "worker result contains duplicate finding IDs", code="invalid_result"
            )
        digest_field = {
            "plan_assessor": "assessed_plan_digest",
            "code_reviewer": "input_digest",
            "diff_reviewer": "input_digest",
            "guard_reviewer": "guard_digest",
        }[profile]
        digest_value = value.get(digest_field)
        if not isinstance(digest_value, str) or len(digest_value) != 64:
            raise WorkerFailure(f"{profile} result has an invalid digest", code="invalid_result")
        for finding in findings:
            if not isinstance(finding, dict) or set(finding) != {
                "id",
                "severity",
                "title",
                "body",
                "file",
                "line",
            }:
                raise WorkerFailure(
                    "finding fields do not match the closed contract", code="invalid_result"
                )
    if profile in {"e2e", "implementer", "review_fixer", "revision_fixer"}:
        source_sha = value.get("source_sha")
        if not isinstance(source_sha, str) or len(source_sha) != 40:
            raise WorkerFailure(
                f"{profile} result must cite its 40-char source SHA", code="invalid_result"
            )
    if profile == "reflector":
        actions = value.get("actions")
        if not isinstance(actions, list):
            raise WorkerFailure("reflection actions must be an array", code="invalid_result")
        ids = [item.get("id") for item in actions if isinstance(item, dict)]
        if len(ids) != len(actions) or len(set(ids)) != len(ids):
            raise WorkerFailure(
                "reflection action IDs must be present and unique", code="invalid_result"
            )
        allowed_kinds = {
            "knowledge",
            "supersession",
            "recall_usage",
            "project_rule_proposal",
            "ship_event",
            "machinery_finding",
        }
        for action in actions:
            if (
                not isinstance(action, dict)
                or set(action) != {"id", "kind", "payload"}
                or action.get("kind") not in allowed_kinds
                or not isinstance(action.get("payload"), dict)
            ):
                raise WorkerFailure(
                    "reflection action is outside the closed contract", code="invalid_result"
                )
    if profile == "review_brief_author":
        import review_brief

        try:
            return review_brief.validate_content(value)
        except review_brief.ValidationError as exc:
            raise WorkerFailure(
                f"review brief result is invalid: {exc}", code="invalid_result"
            ) from exc
    return cast(dict[str, Any], value)


class CliAdapter(Protocol):
    harness: str

    def command(
        self,
        route: Mapping[str, str],
        prompt: str,
        schema_path: Path,
        capsule: Path,
        authority: str = "read_only",
    ) -> list[str]: ...

    def session_command(
        self,
        route: Mapping[str, str],
        prompt: str,
        schema_path: Path,
        *,
        thread_id: str | None,
        new_thread_id: str | None,
    ) -> list[str]: ...

    def preflight(
        self, route: Mapping[str, str], authority: str = "read_only"
    ) -> dict[str, str]: ...


def preflight_route(
    route: Mapping[str, str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = 5.0,
    require_resume: bool = False,
    authority: str = "read_only",
) -> dict[str, str]:
    """Probe one exact provider route, including planner resume when requested.

    The probe runs in the same narrowed environment the worker will get. Probing the owner's
    full environment instead would green-light an authentication the launch cannot reproduce.
    """
    if route["harness"] == "claude_code" and authority == "disposable_writer":
        raise WorkerFailure(
            "claude has no confined write-exec sandbox headless: --permission-mode "
            "bypassPermissions escapes the OS sandbox, and the claude sandbox settings deny "
            "all writes including the allowlisted directory. A disposable_writer (e2e) route "
            "must run its recipe under codex --sandbox workspace-write.",
            code="unsupported_writer_harness",
        )
    executable = "codex" if route["harness"] == "codex" else "claude"
    resolved = shutil.which(executable)
    if resolved is None:
        raise WorkerFailure(
            f"worker executable {executable!r} is unavailable", code="route_unavailable"
        )
    environment = worker_environment()
    auth_command = (
        [executable, "login", "status"] if executable == "codex" else [executable, "auth", "status"]
    )
    try:
        auth = runner(
            auth_command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
        version = runner(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
        help_commands = (
            [[executable, "exec", "--help"], [executable, "exec", "resume", "--help"]]
            if executable == "codex" and require_resume
            else [
                [executable, "exec", "--help"] if executable == "codex" else [executable, "--help"]
            ]
        )
        help_results = [
            runner(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=environment,
            )
            for command in help_commands
        ]
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerFailure(
            f"worker capability probe failed: {exc}", code="capability_missing"
        ) from exc
    if auth.returncode != 0:
        detail = (auth.stderr or auth.stdout).strip()
        raise WorkerFailure(
            f"worker authentication is unavailable: {detail}", code="auth_unavailable"
        )
    help_text = "\n".join(result.stdout + result.stderr for result in help_results)
    flags = (
        ("--model", "--sandbox", "--output-schema", "--json")
        if executable == "codex"
        else ("--model", "--effort", "--permission-mode", "--json-schema", "--verbose")
    )
    missing = [flag for flag in flags if flag not in help_text]
    if authority != "read_only":
        # A writer route is only provable if the CLI also documents its writable mode. The
        # read-only probe above cannot green-light a workspace-write launch.
        writable = "workspace-write" if executable == "codex" else "acceptEdits"
        if writable not in help_text:
            missing.append(writable)
    if version.returncode or any(result.returncode for result in help_results) or missing:
        raise WorkerFailure(
            "worker CLI lacks required capabilities"
            + (f": {', '.join(missing)}" if missing else ""),
            code="capability_missing",
        )
    if executable == "codex" and require_resume:
        resume_text = help_results[1].stdout + help_results[1].stderr
        resume_missing = [
            flag
            for flag in ("--model", "--output-schema", "--json", "--config")
            if flag not in resume_text
        ]
        if resume_missing:
            raise WorkerFailure(
                "worker CLI resume lacks required capabilities: " + ", ".join(resume_missing),
                code="capability_missing",
            )
    return {
        "executable": resolved,
        "version": (version.stdout or version.stderr).strip(),
        "harness": route["harness"],
    }


def _probe_cli(
    executable: str,
    route: Mapping[str, str],
    flags: tuple[str, ...],
    authority: str = "read_only",
) -> dict[str, str]:
    del executable, flags
    return preflight_route(route, authority=authority)


class CodexCliAdapter:
    harness = "codex"

    def command(
        self,
        route: Mapping[str, str],
        prompt: str,
        schema_path: Path,
        capsule: Path,
        authority: str = "read_only",
    ) -> list[str]:
        sandbox = "read-only" if authority == "read_only" else "workspace-write"
        return [
            "codex",
            "exec",
            "--model",
            route["model"],
            "-c",
            f'model_reasoning_effort="{route["effort"]}"',
            "--sandbox",
            sandbox,
            "--json",
            "--output-schema",
            str(schema_path.resolve()),
            "-C",
            str(capsule.resolve()),
            prompt,
        ]

    def session_command(
        self,
        route: Mapping[str, str],
        prompt: str,
        schema_path: Path,
        *,
        thread_id: str | None,
        new_thread_id: str | None,
    ) -> list[str]:
        return build_planner_command(
            route,
            prompt,
            schema_path=schema_path,
            thread_id=thread_id,
            new_thread_id=new_thread_id,
        )

    def preflight(self, route: Mapping[str, str], authority: str = "read_only") -> dict[str, str]:
        return _probe_cli(
            "codex",
            route,
            ("--model", "--sandbox", "--output-schema", "--json", "--cd"),
            authority=authority,
        )


class ClaudeCodeCliAdapter:
    harness = "claude_code"

    def command(
        self,
        route: Mapping[str, str],
        prompt: str,
        schema_path: Path,
        capsule: Path,
        authority: str = "read_only",
    ) -> list[str]:
        # acceptEdits lets a claude capsule_writer edit via Edit/Write but blocks bash-exec; a
        # writer that must run a shell recipe with writes confined (disposable_writer/e2e) has no
        # such mode on claude headless and is refused at preflight, routing to codex instead.
        permission = "plan" if authority == "read_only" else "acceptEdits"
        return [
            "claude",
            "--print",
            "--model",
            route["model"],
            "--effort",
            route["effort"],
            "--permission-mode",
            permission,
            "--output-format",
            "stream-json",
            # --print with stream-json is rejected outright unless --verbose is also present.
            "--verbose",
            "--json-schema",
            schema_path.read_text(encoding="utf-8"),
            "--no-session-persistence",
            prompt,
        ]

    def session_command(
        self,
        route: Mapping[str, str],
        prompt: str,
        schema_path: Path,
        *,
        thread_id: str | None,
        new_thread_id: str | None,
    ) -> list[str]:
        return build_planner_command(
            route,
            prompt,
            schema_path=schema_path,
            thread_id=thread_id,
            new_thread_id=new_thread_id,
        )

    def preflight(self, route: Mapping[str, str], authority: str = "read_only") -> dict[str, str]:
        return _probe_cli(
            "claude",
            route,
            (
                "--model",
                "--effort",
                "--permission-mode",
                "--json-schema",
                "--no-session-persistence",
            ),
            authority=authority,
        )


ADAPTERS: dict[str, CliAdapter] = {
    "codex": CodexCliAdapter(),
    "claude_code": ClaudeCodeCliAdapter(),
}


def build_planner_command(
    route: Mapping[str, str],
    prompt: str,
    *,
    schema_path: Path,
    thread_id: str | None = None,
    new_thread_id: str | None = None,
) -> list[str]:
    """Build the session-aware command used by the planner compatibility facade."""
    schema = str(schema_path.expanduser().resolve())
    if route["harness"] == "codex":
        command = ["codex", "exec"]
        if thread_id:
            command.append("resume")
        command.extend(
            [
                "--model",
                route["model"],
                "-c",
                f'model_reasoning_effort="{route["effort"]}"',
            ]
        )
        if thread_id:
            command.extend(["-c", 'sandbox_mode="read-only"'])
        else:
            command.extend(["--sandbox", "read-only"])
        command.extend(["--json", "--output-schema", schema])
        if thread_id:
            command.append(thread_id)
        command.append(prompt)
        return command
    if route["harness"] != "claude_code":
        raise WorkerFailure("planner route names an unsupported harness")
    command = [
        "claude",
        "--print",
        "--model",
        route["model"],
        "--effort",
        route["effort"],
        "--permission-mode",
        "plan",
        "--output-format",
        "stream-json",
        # --print with stream-json is rejected outright unless --verbose is also present.
        "--verbose",
        "--json-schema",
        schema_path.read_text(encoding="utf-8"),
    ]
    if thread_id:
        command.extend(["--resume", thread_id])
    else:
        command.extend(["--session-id", new_thread_id or str(uuid.uuid4())])
    command.append(prompt)
    return command


def worker_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the narrow environment shared with provider subprocesses.

    The proxy variables are not optional comfort. A sandboxed owner reaches the network
    only through an injected loopback proxy, and Claude Code sandboxes Bash by default on
    macOS. Dropping them leaves the provider CLI attempting a direct connection that the
    sandbox denies, so every routed worker fails to reach its API. A corporate egress
    proxy behaves the same way.
    """
    allowed = {
        "HOME",
        "PATH",
        "SHELL",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "TERM",
        "USER",
        "XDG_CONFIG_HOME",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
    result = {key: value for key, value in os.environ.items() if key in allowed}
    if extra:
        for key, value in extra.items():
            if not key.startswith("FLOW_WORKER_"):
                raise WorkerFailure(f"worker environment refuses {key!r}")
            result[key] = value
    return result


def _git_bytes(root: Path, *args: str, allow_returncodes: tuple[int, ...] = (0,)) -> bytes:
    environment = dict(os.environ)
    # Disable optional index refreshes so evidence capture is itself read-only.
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", *args], cwd=root, env=environment, capture_output=True, check=False
    )
    if result.returncode not in allow_returncodes:
        detail = result.stderr.decode(errors="replace").strip()
        raise WorkerFailure(f"git {' '.join(args)} failed: {detail}", code="artifact_failure")
    return result.stdout


_RUNTIME_POINTERS = frozenset({"skill-root", "memory-root", "layout-version"})


def _runtime_surface(root: Path) -> list[list[Any]]:
    """Digest the executable surface of the gitignored .flow/runtime/ directory.

    `status --untracked-files=all` never lists ignored paths, and .gitignore ignores
    `**/.flow/runtime/`, which holds the `flow` facade every prose command executes plus the
    pointers deciding which code that facade loads. Without this, a worker could rewrite the
    facade, redirect skill-root at its own tree, or flip an executable bit and still pass the
    read-only postcondition. Only executables and pointers are digested: the same directory
    carries per-run envelopes and locks that prose writes while a reader is in flight, so a
    whole-directory digest would report a violation for the run's own bookkeeping.

    A listed path that vanishes before its stat or its read is skipped: live writers publish
    into this directory through mkstemp + os.replace, so the listing can name a temporary the
    rename removes a moment later. A guarded file a worker deleted still changes the receipt,
    because the before-receipt carries the entry the after-receipt now lacks.
    """
    runtime = root / ".flow" / "runtime"
    entries: list[list[Any]] = []
    for path in sorted(runtime.rglob("*")) if runtime.is_dir() else []:
        with contextlib.suppress(FileNotFoundError):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            if not (info.st_mode & 0o111 or path.name in _RUNTIME_POINTERS):
                continue
            entries.append([path.relative_to(root).as_posix(), info.st_mode, _file_digest(path)])
    return entries


_HARNESS_CONFIG = (".claude/settings.json", ".claude/settings.local.json")


def _harness_surface(root: Path) -> list[list[Any]]:
    """Digest the harness settings files, which are gitignored yet declare executable hooks.

    Claude Code runs the hook commands declared in these two files, so a worker that appends a
    PreToolUse hook reaches arbitrary code execution in the parent harness on its next tool call.
    Both are ignored in this repository, so `status --untracked-files=all` never lists them and
    the read-only postcondition passes clean. Only these two paths are digested: the rest of
    .claude/ (todos, statsig, shell snapshots, worktrees) churns under a live session and would
    report the harness's own bookkeeping as a violation.
    """
    entries: list[list[Any]] = []
    for relative in _HARNESS_CONFIG:
        path = root / relative
        if not path.is_file():
            continue
        entries.append([relative, path.lstat().st_mode, _file_digest(path)])
    return entries


_UNTRACKED_DIGEST_MAX_FILE_BYTES = 8 * 1024 * 1024


def _untracked_content(root: Path) -> list[list[Any]]:
    """Digest the content of untracked, non-ignored files.

    `status --porcelain=v2` lists an untracked path by name alone and `git diff` hashes tracked
    content only, so rewriting an existing untracked file moved no other field of this receipt.
    Ignored paths stay out: a blanket digest churns on caches, virtualenvs, and build output.
    A file over the cap carries its size instead of a content hash, which keeps a receipt taken
    twice per bundle capture and four times per worker invocation from reading an unbounded
    artifact. Size is a function of content, unlike mtime, so a size-only entry is not a
    false-positive source, but a same-size rewrite of an over-cap file does escape the guard.
    """
    raw = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
    entries: list[list[Any]] = []
    for name in sorted(item for item in raw.split(b"\0") if item):
        path = root / os.fsdecode(name)
        encoded = _encoded_path(name)
        try:
            info = path.lstat()
        except OSError:
            continue
        content: str
        if stat.S_ISLNK(info.st_mode):
            content = hashlib.sha256(os.readlink(path).encode(errors="surrogateescape")).hexdigest()
        elif stat.S_ISREG(info.st_mode) and info.st_size <= _UNTRACKED_DIGEST_MAX_FILE_BYTES:
            try:
                content = _file_digest(path)
            except OSError:
                continue
        else:
            content = f"size:{info.st_size}"
        entries.append(
            [encoded["path"], encoded["path_encoding"], info.st_mode, info.st_size, content]
        )
    return entries


def git_receipt(root: Path) -> dict[str, Any]:
    """Capture source, index, worktree, untracked, submodule, and Git metadata."""
    resolved = root.resolve()
    git_dir_raw = _git_bytes(resolved, "rev-parse", "--absolute-git-dir").strip()
    common_raw = _git_bytes(resolved, "rev-parse", "--git-common-dir").strip()
    head = _git_bytes(resolved, "rev-parse", "HEAD").strip().decode()
    branch = (
        _git_bytes(resolved, "symbolic-ref", "-q", "HEAD", allow_returncodes=(0, 1))
        .strip()
        .decode(errors="replace")
    )
    status_bytes = _git_bytes(resolved, "status", "--porcelain=v2", "-z", "--untracked-files=all")
    index_bytes = _git_bytes(resolved, "ls-files", "--stage", "-z")
    # Per-entry index flags, which neither ls-files --stage nor status reveals. Without them,
    # `update-index --assume-unchanged` (or --skip-worktree) hides an arbitrary tracked-file
    # rewrite from the whole guard. This listing is stable across a stat-cache refresh, so it
    # restores the coverage the raw .git/index hash gave without its false positives.
    index_flags = _git_bytes(resolved, "ls-files", "-v", "-z")
    # Neither status --porcelain=v2 nor ls-files --stage carries a worktree content hash, so a
    # tracked file rewritten in place keeps both digests equal while its bytes change. The
    # unstaged diff hashes that content through Git and stays byte-identical across a stat-cache
    # refresh, which is why the raw .git/index below is still excluded.
    worktree_diff = _git_bytes(
        resolved, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv"
    )
    submodules = _git_bytes(resolved, "submodule", "status", "--recursive")
    hooks = sorted(
        (entry.name, entry.stat().st_mode, hashlib.sha256(entry.read_bytes()).hexdigest())
        for entry in (Path(os.fsdecode(git_dir_raw)) / "hooks").glob("*")
        if entry.is_file() and not entry.name.endswith(".sample")
    )
    git_dir = Path(os.fsdecode(git_dir_raw))
    if not git_dir.is_absolute():
        git_dir = resolved / git_dir
    # The raw .git/index file is deliberately excluded: Git rewrites its stat cache for
    # racily-clean entries, so its bytes change without any repository change. The index
    # is covered semantically by ls-files --stage and status --porcelain below.
    metadata: dict[str, Any] = {}
    for relative in ("HEAD", "config", "packed-refs"):
        path = git_dir / relative
        if path.is_file():
            data = path.read_bytes()
            metadata[relative] = {"length": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        else:
            metadata[relative] = None
    # HEAD/config/packed-refs miss a loose ref, and the writer seed machinery records the seeded
    # tree under refs/flow/seed-baseline. Scoped to refs/flow/ so a stray such ref in the
    # AUTHORITATIVE repo trips the read-only postcondition; that namespace is empty there, so
    # before==after and no live behavior changes. A full-refs digest would false-trip on
    # refs/remotes and refs/dolt that legitimately move during a run.
    flow_refs = _git_bytes(resolved, "for-each-ref", "refs/flow/")
    body = {
        "schema": "flow.git-receipt/v1",
        "root": str(resolved),
        "head": head,
        "head_ref": branch or None,
        "git_dir": str(git_dir.resolve()),
        "common_dir": os.fsdecode(common_raw),
        "status": {"length": len(status_bytes), "sha256": hashlib.sha256(status_bytes).hexdigest()},
        "index": {"length": len(index_bytes), "sha256": hashlib.sha256(index_bytes).hexdigest()},
        "index_flags": {
            "length": len(index_flags),
            "sha256": hashlib.sha256(index_flags).hexdigest(),
        },
        "worktree_diff": {
            "length": len(worktree_diff),
            "sha256": hashlib.sha256(worktree_diff).hexdigest(),
        },
        "submodules": {"length": len(submodules), "sha256": hashlib.sha256(submodules).hexdigest()},
        "flow_refs": {"length": len(flow_refs), "sha256": hashlib.sha256(flow_refs).hexdigest()},
        "metadata": metadata,
        "hooks": hooks,
        "runtime_surface": _runtime_surface(resolved),
        "harness_surface": _harness_surface(resolved),
        "untracked_content": _untracked_content(resolved),
    }
    return {**body, "digest": _digest(body)}


def _load_seed(order: WorkOrder) -> bytes:
    """Read and digest-verify the order's sealed seed patch; empty when the order carries none.

    The seed is only needed when a fresh capsule is cloned, so it is read here rather than at
    order validation, and a completed invocation replayed from its durable outcome never touches
    it. A digest mismatch means the sealed seed drifted since dispatch and is a hard failure.
    """
    if order.seed_patch is None:
        return b""
    try:
        seed = Path(order.seed_patch).resolve().read_bytes()
    except OSError as exc:
        raise WorkerFailure(
            f"work-order seed patch is unreadable: {exc}", code="invalid_order"
        ) from exc
    if hashlib.sha256(seed).hexdigest() != order.seed_digest:
        raise WorkerFailure("work-order seed digest does not match", code="invalid_order")
    return seed


def _seed_capsule_working_state(capsule: Path, seed_patch: bytes) -> str | None:
    """Apply a captured working-state delta into a pristine capsule; non-empty seeds only.

    The capsule is a fresh clone at the base SHA and the patch was captured against that same
    SHA, so it applies deterministically; a patch that will not apply raises ``seed_apply_failed``
    and is never silently dropped. The seeded tree is recorded under SEED_BASELINE_REF (no
    commit, so HEAD stays the base SHA) so later evidence capture measures the recipe's own
    mutations against base+seed. Reusable by any writer that must run against the ticket's
    uncommitted working tree; returns the seeded tree oid, or None for an empty seed.
    """
    if not seed_patch:
        return None
    root = capsule.resolve()
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}

    def git(*args: str, stdin: bytes | None = None) -> bytes:
        result = subprocess.run(
            ["git", *args], cwd=root, input=stdin, capture_output=True, check=False, env=env
        )
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip()
            raise WorkerFailure(
                f"capsule seed failed: git {' '.join(args)}: {detail}", code="seed_apply_failed"
            )
        return result.stdout

    git("apply", "--binary", "--whitespace=nowarn", stdin=seed_patch)
    git("add", "-A")
    seed_tree = git("write-tree").strip().decode()
    git("update-ref", SEED_BASELINE_REF, seed_tree)
    return seed_tree


def create_private_clone(
    source_root: Path, source_sha: str, capsule: Path, seed: bytes = b""
) -> dict[str, Any]:
    """Create a standalone exact-SHA clone without linked mutable Git metadata.

    The clone is built beside its final path and installed with one rename. A crash mid-clone
    would otherwise leave a partial repository at the capsule path, and recovery reads that
    path as an existing capsule it cannot inspect, wedging the invocation for good. A non-empty
    ``seed`` is applied into the staging clone before the rename, so an installed capsule is
    always fully seeded and recovery never sees a half-seeded tree.
    """
    source = source_root.resolve()
    target = capsule.resolve()
    if target.exists():
        raise WorkerFailure(f"capsule already exists: {target}", code="execution_busy")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    build = staging / "capsule"
    result = subprocess.run(
        ["git", "clone", "--no-hardlinks", "--no-checkout", "--quiet", str(source), str(build)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        raise WorkerFailure(
            f"private clone failed: {result.stderr.strip()}", code="artifact_failure"
        )
    try:
        checkout = subprocess.run(
            ["git", "checkout", "--detach", "--quiet", source_sha],
            cwd=build,
            capture_output=True,
            text=True,
            check=False,
        )
        if checkout.returncode != 0:
            raise WorkerFailure(
                f"private clone cannot checkout {source_sha}: {checkout.stderr.strip()}",
                code="artifact_failure",
            )
        git_entry = build / ".git"
        alternates = git_entry / "objects" / "info" / "alternates"
        common = _git_bytes(build, "rev-parse", "--git-common-dir").strip()
        head = _git_bytes(build, "rev-parse", "HEAD").strip().decode()
        if not git_entry.is_dir() or alternates.exists() or common not in {b".git", b"./.git"}:
            raise WorkerFailure(
                "private clone shares mutable Git metadata", code="artifact_failure"
            )
        if head != source_sha:
            raise WorkerFailure(
                "private clone did not resolve the exact source SHA", code="artifact_failure"
            )
        _seed_capsule_working_state(build, seed)
        os.replace(build, target)
        body = {
            "schema": "flow.cognitive-capsule/v1",
            "source_root": str(source),
            "source_sha": source_sha,
            "capsule": str(target),
            "git_receipt": git_receipt(target),
            "standalone": True,
        }
        return {**body, "digest": _digest(body)}
    except Exception:
        with contextlib.suppress(OSError):
            shutil.rmtree(target)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _encoded_path(raw: bytes) -> dict[str, str]:
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        decoded = ""
    if decoded and "\n" not in decoded and "\r" not in decoded and "\x00" not in decoded:
        return {"path": decoded, "path_encoding": "utf8"}
    return {
        "path": base64.b64encode(raw).decode("ascii"),
        "path_encoding": "base64",
    }


def _parse_raw_diff(raw: bytes) -> list[dict[str, Any]]:
    """Parse ``git diff --raw -z`` without decoding repository path bytes."""
    chunks = raw.split(b"\0")
    changes: list[dict[str, Any]] = []
    index = 0
    while index < len(chunks):
        header = chunks[index]
        index += 1
        if not header:
            continue
        fields = header.split()
        if len(fields) != 5 or not fields[0].startswith(b":"):
            raise WorkerFailure("git raw diff has an invalid record", code="artifact_failure")
        if index >= len(chunks) or not chunks[index]:
            raise WorkerFailure("git raw diff is missing a path", code="artifact_failure")
        path = chunks[index]
        index += 1
        status_value = fields[4].decode("ascii", errors="strict")
        item: dict[str, Any] = {
            "old_mode": fields[0][1:].decode("ascii"),
            "new_mode": fields[1].decode("ascii"),
            "old_oid": fields[2].decode("ascii"),
            "new_oid": fields[3].decode("ascii"),
            "status": status_value,
            "path": _encoded_path(path),
        }
        if status_value.startswith(("R", "C")):
            if index >= len(chunks) or not chunks[index]:
                raise WorkerFailure(
                    "git rename/copy record is missing its destination", code="artifact_failure"
                )
            item["destination"] = _encoded_path(chunks[index])
            index += 1
        changes.append(item)
    return changes


def _write_blob(blobs: Path, data: bytes) -> dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    path = blobs / digest
    if not path.exists():
        path.write_bytes(data)
        path.chmod(0o400)
    return {"sha256": digest, "length": len(data), "blob": f"blobs/{digest}"}


def _path_payload(root: Path, raw: bytes, blobs: Path, budget: int) -> dict[str, Any]:
    path = root / os.fsdecode(raw)
    item: dict[str, Any] = _encoded_path(raw)
    info = path.lstat()
    mode = info.st_mode
    item["mode"] = stat.S_IMODE(mode)
    # Size first: reading a huge artifact into memory to then reject it is how the limit OOMs.
    if info.st_size > budget:
        raise WorkerFailure("review bundle exceeds its byte limit", code="artifact_failure")
    if stat.S_ISREG(mode):
        item["kind"] = "file"
        item.update(_write_blob(blobs, path.read_bytes()))
    elif stat.S_ISLNK(mode):
        item["kind"] = "symlink"
        item.update(_write_blob(blobs, os.readlink(path).encode(errors="surrogateescape")))
    else:
        raise WorkerFailure(
            f"review bundle refuses special file {os.fsdecode(raw)!r}", code="artifact_failure"
        )
    return item


def _freeze_tree(root: Path) -> None:
    for directory, dirs, files in os.walk(root):
        for name in files:
            (Path(directory) / name).chmod(0o400)
        for name in dirs:
            (Path(directory) / name).chmod(0o500)
    root.chmod(0o500)


def build_review_input_bundle(
    source_root: Path,
    output: Path,
    *,
    max_files: int = 20_000,
    max_bytes: int = 512 * 1024 * 1024,
) -> dict[str, Any]:
    """Publish immutable binary-safe review evidence without applying a patch."""
    source = source_root.resolve()
    target = output.resolve()
    if target.exists():
        raise WorkerFailure(f"review bundle already exists: {target}", code="artifact_failure")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        blobs = temporary / "blobs"
        blobs.mkdir()
        before = git_receipt(source)
        status = _git_bytes(source, "status", "--porcelain=v2", "-z", "--untracked-files=all")
        index = _git_bytes(source, "ls-files", "--stage", "-z")
        staged = _git_bytes(
            source, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--cached"
        )
        worktree = _git_bytes(
            source, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv"
        )
        staged_raw = _git_bytes(source, "diff", "--raw", "-z", "--cached")
        worktree_raw = _git_bytes(source, "diff", "--raw", "-z")
        untracked_raw = _git_bytes(source, "ls-files", "--others", "--exclude-standard", "-z")
        paths = [item for item in untracked_raw.split(b"\0") if item]
        if len(paths) > max_files:
            raise WorkerFailure(
                "review bundle exceeds the untracked-file limit", code="artifact_failure"
            )
        budget = max_bytes - len(staged) - len(worktree)
        if budget < 0:
            raise WorkerFailure("review bundle exceeds its byte limit", code="artifact_failure")
        untracked: list[dict[str, Any]] = []
        for raw in paths:
            item = _path_payload(source, raw, blobs, budget)
            budget -= int(item["length"])
            untracked.append(item)
        raw_dir = temporary / "raw"
        raw_dir.mkdir()
        raw_files = {
            "status": status,
            "index": index,
            "staged.patch": staged,
            "worktree.patch": worktree,
            "staged.raw": staged_raw,
            "worktree.raw": worktree_raw,
        }
        raw_receipts: dict[str, Any] = {}
        for name, content in raw_files.items():
            path = raw_dir / name
            path.write_bytes(content)
            raw_receipts[name] = {
                "length": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        after = git_receipt(source)
        if before["digest"] != after["digest"]:
            raise WorkerFailure(
                "repository changed while review evidence was captured", code="baseline_mismatch"
            )
        body = {
            "schema": REVIEW_BUNDLE_SCHEMA,
            "source_root": str(source),
            "head": before["head"],
            "git_before": before,
            "git_after": after,
            "layers": {
                "staged": {
                    "patch_digest": raw_receipts["staged.patch"]["sha256"],
                    "raw_digest": raw_receipts["staged.raw"]["sha256"],
                    "changes": _parse_raw_diff(staged_raw),
                },
                "worktree": {
                    "patch_digest": raw_receipts["worktree.patch"]["sha256"],
                    "raw_digest": raw_receipts["worktree.raw"]["sha256"],
                    "changes": _parse_raw_diff(worktree_raw),
                },
            },
            "raw": raw_receipts,
            "untracked": untracked,
        }
        manifest = {**body, "digest": _digest(body)}
        _atomic_json(temporary / "manifest.json", manifest)
        os.replace(temporary, target)
        _freeze_tree(target)
        return {
            "schema": REVIEW_BUNDLE_SCHEMA,
            "path": str(target),
            "digest": manifest["digest"],
            "git_digest": before["digest"],
        }
    except Exception:
        with contextlib.suppress(OSError):
            shutil.rmtree(temporary)
        raise


def _normalize_repo_path(raw: str) -> str:
    """Normalize a repo-relative POSIX path for byte-exact ownership comparison.

    Rejects the odd forms _resolve_authority accepts but never consumes: an empty or absolute
    path, a backslash (a separator that would read as one component here), a NUL, and a "." or
    ".." segment. Redundant separators collapse and a trailing slash is dropped so a directory
    prefix compares identically on both the allowed and the touched side.
    """
    if not isinstance(raw, str) or not raw or raw.startswith("/") or "\\" in raw or "\x00" in raw:
        raise WorkerFailure(
            f"mutation path {raw!r} is not a safe repo-relative path", code="ownership_violation"
        )
    parts = [segment for segment in raw.split("/") if segment and segment != "."]
    if not parts or any(segment == ".." for segment in parts):
        raise WorkerFailure(
            f"mutation path {raw!r} escapes the repository", code="ownership_violation"
        )
    return "/".join(parts)


def _within_allowed(touched: str, allowed: frozenset[str]) -> bool:
    """True when a touched path equals or sits under an allowed path or directory."""
    return touched in allowed or any(touched.startswith(f"{prefix}/") for prefix in allowed)


def _change_metadata(changes: list[dict[str, Any]], patch: bytes) -> dict[str, Any]:
    """Summarize the touched paths' binary, rename, delete, add, and mode facts for the receipt."""
    additions = deletions = renames = copies = mode_changes = 0
    entries: list[dict[str, Any]] = []
    for change in changes:
        status = str(change["status"])
        old_mode = str(change["old_mode"])
        new_mode = str(change["new_mode"])
        entry: dict[str, Any] = {
            "status": status,
            "path": change["path"],
            "old_mode": old_mode,
            "new_mode": new_mode,
        }
        if "destination" in change:
            entry["destination"] = change["destination"]
        entries.append(entry)
        if status.startswith("A"):
            additions += 1
        elif status.startswith("D"):
            deletions += 1
        elif status.startswith("R"):
            renames += 1
        elif status.startswith("C"):
            copies += 1
        if old_mode not in {new_mode, "000000"} and new_mode != "000000":
            mode_changes += 1
    return {
        "binary": b"GIT binary patch" in patch,
        "additions": additions,
        "deletions": deletions,
        "renames": renames,
        "copies": copies,
        "mode_changes": mode_changes,
        "changes": entries,
    }


def _capture_capsule_patch(
    capsule: Path, source_sha: str, allowed_mutation_paths: Sequence[str]
) -> dict[str, Any]:
    """Stage every capsule change and produce one binary-safe patch against the writer's baseline.

    The capsule is disposable, so ``git add -A`` is safe and lets a single diff carry tracked
    edits, additions, deletions, renames, and mode changes in one replayable patch. The baseline
    is the seeded tree when the capsule carries one (SEED_BASELINE_REF): a seeded capsule_writer
    runs on top of the ticket's uncommitted working state, which the authoritative worktree
    already holds, so diffing against the seed yields ONLY the writer's own delta. Diffing against
    source_sha would double-count the seed and collide on import (patch_import_conflict, flow-wtm4).
    An unseeded writer (a clean base, e.g. the implementer) has no ref and falls back to source SHA.
    Every touched path (both sides of a rename) must fall inside allowed_mutation_paths or the
    capture is an ownership_violation; a git failure is a patch_capture_failed and the caller keeps
    the capsule as recovery evidence.
    """
    root = capsule.resolve()
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}

    def git(*args: str, allow: tuple[int, ...] = (0,)) -> bytes:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False, env=env)
        if result.returncode not in allow:
            detail = result.stderr.decode(errors="replace").strip()
            raise WorkerFailure(
                f"capsule patch capture failed: git {' '.join(args)}: {detail}",
                code="patch_capture_failed",
            )
        return result.stdout

    seed_tree = git("rev-parse", "--verify", "--quiet", SEED_BASELINE_REF, allow=(0, 1))
    baseline = seed_tree.strip().decode() or source_sha
    git("add", "-A")
    patch = git(
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        "-M",
        "--cached",
        baseline,
    )
    raw = git("diff", "--raw", "-z", "-M", "--cached", baseline)
    changes = _parse_raw_diff(raw)
    allowed = frozenset(_normalize_repo_path(entry) for entry in allowed_mutation_paths)
    touched: set[str] = set()
    for change in changes:
        sides = [change["path"]]
        if "destination" in change:
            sides.append(change["destination"])
        for side in sides:
            if side["path_encoding"] != "utf8":
                raise WorkerFailure(
                    "capsule mutated a non-UTF-8 path outside its allowed set",
                    code="ownership_violation",
                )
            normalized = _normalize_repo_path(side["path"])
            if not _within_allowed(normalized, allowed):
                raise WorkerFailure(
                    f"capsule mutated {normalized!r} outside its allowed paths",
                    code="ownership_violation",
                )
            touched.add(normalized)
    return {
        "patch": patch,
        "patch_digest": hashlib.sha256(patch).hexdigest(),
        "touched": sorted(touched),
        "allowed": sorted(allowed),
        "metadata": _change_metadata(changes, patch),
        "empty": not patch,
    }


def _capsule_mutation_summary(capsule: Path, source_sha: str) -> dict[str, Any]:
    """Summarize every disposable-capsule change as E2E evidence, with no ownership enforcement.

    A disposable writer discards its whole capsule and imports nothing, so every touched path is
    evidence rather than an owned change: unlike _capture_capsule_patch this enforces no allowed
    set and never raises ownership_violation. The baseline is the seeded tree when the capsule
    carries one (SEED_BASELINE_REF), so a seeded run reports only what the recipe wrote rather
    than conflating the recipe's mutations with the ticket's in-flight changes; an unseeded
    capsule falls back to the bare source SHA. The patch is captured only to derive the diffstat
    and digest, then dropped with the capsule; the bytes are never retained or replayed.
    """
    root = capsule.resolve()
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}

    def git(*args: str, allow: tuple[int, ...] = (0,)) -> bytes:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False, env=env)
        if result.returncode not in allow:
            detail = result.stderr.decode(errors="replace").strip()
            raise WorkerFailure(
                f"capsule evidence capture failed: git {' '.join(args)}: {detail}",
                code="patch_capture_failed",
            )
        return result.stdout

    seed_tree = git("rev-parse", "--verify", "--quiet", SEED_BASELINE_REF, allow=(0, 1))
    baseline = seed_tree.strip().decode() or source_sha
    git("add", "-A")
    patch = git(
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        "-M",
        "--cached",
        baseline,
    )
    raw = git("diff", "--raw", "-z", "-M", "--cached", baseline)
    changes = _parse_raw_diff(raw)
    touched: set[str] = set()
    for change in changes:
        sides = [change["path"], *([change["destination"]] if "destination" in change else [])]
        touched.update(side["path"] for side in sides)
    return {
        "schema": "flow.e2e-capsule-mutations/v1",
        "touched": sorted(touched),
        "diffstat": _change_metadata(changes, patch),
        "patch_digest": hashlib.sha256(patch).hexdigest(),
        "empty": not patch,
        "seeded": bool(seed_tree.strip()),
    }


def _capture_working_delta(source_root: Path, source_sha: str) -> bytes:
    """Capture the authoritative worktree's uncommitted delta as one applicable seed patch.

    Tracked edits, new untracked files, deletions, and mode changes are staged into a throwaway
    index (GIT_INDEX_FILE), so the authoritative worktree's real index is never touched and this
    dispatch-time capture stays read-only on the worktree. The returned bytes apply to a pristine
    checkout at source_sha with ``git apply --binary``; an empty delta returns empty bytes.
    """
    root = source_root.resolve()
    handle, index_path = tempfile.mkstemp(prefix="flow-seed-index.")
    os.close(handle)
    try:
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_INDEX_FILE": index_path}

        def git(*args: str) -> bytes:
            result = subprocess.run(
                ["git", *args], cwd=root, capture_output=True, check=False, env=env
            )
            if result.returncode != 0:
                detail = result.stderr.decode(errors="replace").strip()
                raise WorkerFailure(
                    f"seed capture failed: git {' '.join(args)}: {detail}", code="artifact_failure"
                )
            return result.stdout

        git("read-tree", source_sha)
        git("add", "-A")
        return git(
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "--cached",
            source_sha,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(index_path)


def _tracked_capturable_paths(root: Path) -> set[str]:
    """Repo-relative POSIX paths ``git add -A`` would stage: tracked plus untracked-non-ignored.

    The owned-file baseline must enumerate the same set the captured patch can carry. ``git add
    -A`` skips gitignored files, so hashing them (a nested .flow/runtime, __pycache__, build
    output under an allowed dir) would drift the baseline against a patch that never carries them.
    """
    listing = _git_bytes(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return {name for name in listing.decode(errors="surrogateescape").split("\0") if name}


def _owned_baseline_digest(root: Path, allowed_mutation_paths: Sequence[str]) -> str:
    """Digest the current content of the owned paths in the authoritative worktree.

    A change to an owned file by anything other than this import drifts the digest, so the CAS
    refuses rather than clobbering an external edit. Symlinks are hashed by their target string,
    not followed, so an owned symlink cannot smuggle outside content into the baseline. The file
    set is exactly what ``git add -A`` stages, so gitignored churn the patch cannot carry never
    drifts the baseline.
    """
    resolved = root.resolve()
    allowed = sorted(frozenset(_normalize_repo_path(entry) for entry in allowed_mutation_paths))
    listed = _tracked_capturable_paths(resolved)
    hasher = hashlib.sha256()
    for entry in allowed:
        owned = sorted(p for p in listed if p == entry or p.startswith(f"{entry}/"))
        if not owned:
            hasher.update(f"{entry}\0absent\0".encode())
            continue
        for relative in owned:
            path = resolved / relative
            if not path.is_symlink() and not path.exists():
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                content = os.readlink(path).encode(errors="surrogateescape")
            elif stat.S_ISDIR(info.st_mode):
                # A submodule gitlink lists as a directory path; its commit pointer is not part of
                # the patch the writer can carry, so it never belongs in this baseline.
                continue
            else:
                content = path.read_bytes()
            hasher.update(relative.encode())
            hasher.update(b"\0")
            hasher.update(hashlib.sha256(content).digest())
    return hasher.hexdigest()


def _patch_application_state(target: Path, patch: bytes) -> str:
    """Classify a captured patch as unapplied, applied, or partial at the authoritative target.

    ``git apply --index --check`` succeeds only when the whole patch still applies; the same
    check reversed succeeds only when it is already fully applied. Neither succeeding on a
    non-empty patch is a partial or conflicting tree that must never be silently re-baselined.
    """
    if not patch:
        return "applied"
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}

    def check(*flags: str) -> bool:
        result = subprocess.run(
            ["git", "apply", "--index", "--check", *flags],
            cwd=target,
            input=patch,
            capture_output=True,
            check=False,
            env=env,
        )
        return result.returncode == 0

    if check():
        return "unapplied"
    if check("--reverse"):
        return "applied"
    return "partial"


_CAS_FIELDS = (
    "head",
    "index",
    "owned_baseline",
    "dispatch_generation",
    "route_snapshot",
    "lease_fence",
)
# On an already-applied resume the index and owned files legitimately reflect this import, so
# only the invariants an external actor could still move are re-checked.
_EXTERNAL_CAS = ("head", "dispatch_generation", "route_snapshot", "lease_fence")


def _cas_refusals(
    expected: Mapping[str, Any], observed: Mapping[str, Any], fields: Sequence[str] = _CAS_FIELDS
) -> tuple[str, ...]:
    """Return the CAS fields that drifted between order issuance and import.

    Any drift means the authoritative worktree is no longer the one the order was validated
    against, so importing would re-baseline over an external change.
    """
    return tuple(field for field in fields if expected.get(field) != observed.get(field))


_JOURNAL_ORDER = {
    "prepared": 0,
    "cloning": 1,
    "running": 2,
    "cancelling": 3,
    "terminal": 4,
    "validated": 5,
    "importing": 6,
    "completed": 7,
    "blocked": 7,
    "quarantined": 7,
}


class InvocationJournal:
    """Durable monotonic state for one logical invocation."""

    def __init__(self, path: Path, logical_invocation_id: str) -> None:
        self.path = path.resolve()
        self.logical_invocation_id = logical_invocation_id

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkerFailure(
                f"invocation journal is unreadable: {exc}", code="recovery_required"
            ) from exc
        if (
            value.get("schema") != JOURNAL_SCHEMA
            or value.get("logical_invocation_id") != self.logical_invocation_id
        ):
            raise WorkerFailure("invocation journal identity is invalid", code="recovery_required")
        body = {key: item for key, item in value.items() if key != "digest"}
        if value.get("digest") != _digest(body):
            raise WorkerFailure("invocation journal digest is invalid", code="recovery_required")
        return cast(dict[str, Any], value)

    def transition(self, state: str, **fields: Any) -> dict[str, Any]:
        if state not in _JOURNAL_ORDER:
            raise WorkerFailure(f"unknown invocation journal state {state!r}")
        current = self.read()
        if current is not None:
            previous = str(current["state"])
            if _JOURNAL_ORDER[state] < _JOURNAL_ORDER[previous]:
                raise WorkerFailure(f"invocation journal cannot move {previous} -> {state}")
            if previous in {"completed", "blocked", "quarantined"} and state != previous:
                raise WorkerFailure(
                    f"terminal invocation journal cannot move {previous} -> {state}"
                )
            body = {key: item for key, item in current.items() if key != "digest"}
        else:
            if state != "prepared":
                raise WorkerFailure("a new invocation journal must begin in prepared")
            body = {
                "schema": JOURNAL_SCHEMA,
                "logical_invocation_id": self.logical_invocation_id,
                "created_at": time.time(),
            }
        body.update(fields)
        body["state"] = state
        body["updated_at"] = time.time()
        value = {**body, "digest": _digest(body)}
        _atomic_json(self.path, value)
        return value


@dataclass(frozen=True)
class ProcessEvidence:
    pid: int
    returncode: int
    stdout: str
    stderr: str
    child_reaped: bool
    process_group_absent: bool
    stdout_eof: bool
    stderr_eof: bool
    elapsed_seconds: float
    soft_deadline: bool

    @property
    def terminal_acknowledged(self) -> bool:
        return (
            self.child_reaped and self.process_group_absent and self.stdout_eof and self.stderr_eof
        )


@dataclass(frozen=True)
class ProviderExecution:
    payload: dict[str, Any]
    worker_id: str | None
    process: ProcessEvidence
    command: tuple[str, ...]
    attempt: int
    attempts: tuple[dict[str, Any], ...]
    aggregate_elapsed_seconds: float


class _ProviderHardTimeout(WorkerFailure):
    def __init__(
        self,
        terminal_acknowledged: bool,
        metric: dict[str, Any],
    ) -> None:
        super().__init__(
            "worker reached its hard deadline",
            code="hard_timeout" if terminal_acknowledged else "termination_unconfirmed",
            attempts=(metric,),
        )
        self.terminal_acknowledged = terminal_acknowledged


def _cli_error_detail(stdout: str, stderr: str) -> str:
    """Prefer actionable structured CLI errors over transport chatter."""
    messages: list[str] = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        candidates = [value.get("message")]
        error = value.get("error")
        if isinstance(error, dict):
            candidates.append(error.get("message"))
        elif isinstance(error, str):
            candidates.append(error)
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip() and candidate not in messages:
                messages.append(candidate.strip())
    if messages:
        return "; ".join(messages)
    fallback = [part.strip() for part in (stderr, stdout) if part.strip()]
    return "\n".join(fallback) or "no diagnostic output"


def _process_group_absent(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _default_popen(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
    return subprocess.Popen(command, **kwargs)


def run_provider_process(
    command: list[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    popen: Callable[..., Any] = _default_popen,
    killpg: Callable[[int, int], None] = os.killpg,
    group_absent: Callable[[int], bool] = _process_group_absent,
    soft_timeout: float = SOFT_TIMEOUT_SECONDS,
    hard_timeout: float = HARD_TIMEOUT_SECONDS,
    grace: float = TERMINATION_GRACE_SECONDS,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    attempt_number: int = 1,
) -> ProviderExecution:
    """Run one provider process group and retain typed output plus terminal proof."""
    if soft_timeout <= 0 or hard_timeout <= soft_timeout:
        raise WorkerFailure("worker deadlines require 0 < soft < hard")
    process = popen(
        command,
        cwd=cwd,
        env=dict(environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    started = time.monotonic()
    soft = False
    deadline_events: list[str] = []
    try:
        stdout, stderr = process.communicate(timeout=soft_timeout)
    except subprocess.TimeoutExpired:
        soft = True
        deadline_events.append("soft_deadline")
        if on_event is not None:
            on_event(
                {
                    "type": "soft_deadline",
                    "attempt": attempt_number,
                    "elapsed_seconds": time.monotonic() - started,
                }
            )
        try:
            stdout, stderr = process.communicate(timeout=hard_timeout - soft_timeout)
        except subprocess.TimeoutExpired:
            deadline_events.append("hard_deadline")
            with contextlib.suppress(OSError, ProcessLookupError):
                killpg(process.pid, signal.SIGTERM)
            stdout = ""
            stderr = ""
            streams_closed = False
            try:
                stdout, stderr = process.communicate(timeout=grace)
                streams_closed = True
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError, ProcessLookupError):
                    killpg(process.pid, signal.SIGKILL)
                try:
                    stdout, stderr = process.communicate(timeout=grace)
                    streams_closed = True
                except subprocess.TimeoutExpired:
                    streams_closed = False
            elapsed = max(0.0, time.monotonic() - started)
            child_reaped = process.poll() is not None
            absent = group_absent(process.pid)
            acknowledged = child_reaped and absent and streams_closed
            metric = {
                "attempt": attempt_number,
                "outcome": "hard_timeout",
                "soft_budget_seconds": soft_timeout,
                "hard_budget_seconds": hard_timeout,
                "deadline_events": deadline_events,
                "elapsed_seconds": elapsed,
                "terminal_acknowledged": acknowledged,
            }
            if on_event is not None:
                on_event(
                    {
                        "type": "hard_deadline",
                        "attempt": attempt_number,
                        "elapsed_seconds": elapsed,
                        "terminal_acknowledged": acknowledged,
                    }
                )
            raise _ProviderHardTimeout(acknowledged, metric) from None
    elapsed = max(0.0, time.monotonic() - started)
    returncode = process.returncode
    # The provider CLI can outlive its own exit through a helper that never held the output
    # pipes. Give the group a bounded chance to drain, then end it, before calling the
    # termination ambiguous: a snap judgement here quarantines a successful invocation.
    absent = group_absent(process.pid)
    if returncode is not None and not absent:
        deadline = time.monotonic() + grace
        while not absent and time.monotonic() < deadline:
            time.sleep(0.05)
            absent = group_absent(process.pid)
        if not absent:
            with contextlib.suppress(OSError, ProcessLookupError):
                killpg(process.pid, signal.SIGKILL)
            deadline = time.monotonic() + grace
            while not absent and time.monotonic() < deadline:
                time.sleep(0.05)
                absent = group_absent(process.pid)
    if returncode is None or not absent:
        raise WorkerFailure(
            "worker termination lacks child or process-group acknowledgement",
            code="termination_unconfirmed",
            attempts=(
                {
                    "attempt": attempt_number,
                    "outcome": "termination_unconfirmed",
                    "soft_budget_seconds": soft_timeout,
                    "hard_budget_seconds": hard_timeout,
                    "deadline_events": deadline_events,
                    "elapsed_seconds": elapsed,
                    "terminal_acknowledged": False,
                },
            ),
        )
    evidence = ProcessEvidence(
        pid=process.pid,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        child_reaped=True,
        process_group_absent=absent,
        stdout_eof=True,
        stderr_eof=True,
        elapsed_seconds=elapsed,
        soft_deadline=soft,
    )
    metric = {
        "attempt": attempt_number,
        "outcome": "success" if returncode == 0 else "cli_error",
        "soft_budget_seconds": soft_timeout,
        "hard_budget_seconds": hard_timeout,
        "deadline_events": deadline_events,
        "elapsed_seconds": elapsed,
        "terminal_acknowledged": evidence.terminal_acknowledged,
    }
    if returncode != 0:
        raise WorkerFailure(
            f"worker CLI exited {returncode}: {_cli_error_detail(stdout, stderr)}",
            code="worker_exited",
            attempts=(metric,),
        )
    try:
        payload, worker_id = _extract_typed_result(stdout)
    except WorkerFailure as exc:
        metric["outcome"] = "invalid_output"
        raise WorkerFailure(str(exc), code=exc.code, attempts=(metric,)) from exc
    return ProviderExecution(
        payload=payload,
        worker_id=worker_id,
        process=evidence,
        command=tuple(command),
        attempt=attempt_number,
        attempts=(metric,),
        aggregate_elapsed_seconds=elapsed,
    )


def run_provider_with_retry(
    command_factory: Callable[[bool], list[str]],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    retry_limit: int,
    popen: Callable[..., Any] = _default_popen,
    killpg: Callable[[int, int], None] = os.killpg,
    group_absent: Callable[[int], bool] = _process_group_absent,
    soft_timeout: float = SOFT_TIMEOUT_SECONDS,
    hard_timeout: float = HARD_TIMEOUT_SECONDS,
    grace: float = TERMINATION_GRACE_SECONDS,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> ProviderExecution:
    """Run one logical provider request with at most one acknowledged fresh retry."""
    if retry_limit not in {0, 1}:
        raise WorkerFailure("provider retry limit must be zero or one")
    started = time.monotonic()
    metrics: list[dict[str, Any]] = []
    attempts = 1 + retry_limit
    for attempt in range(1, attempts + 1):
        try:
            result = run_provider_process(
                command_factory(attempt > 1),
                cwd=cwd,
                environment=environment,
                popen=popen,
                killpg=killpg,
                group_absent=group_absent,
                soft_timeout=soft_timeout,
                hard_timeout=hard_timeout,
                grace=grace,
                on_event=on_event,
                attempt_number=attempt,
            )
        except _ProviderHardTimeout as exc:
            metrics.extend(exc.attempts)
            if not exc.terminal_acknowledged:
                raise WorkerFailure(
                    "worker cancellation lacks terminal acknowledgement; refusing overlap",
                    code="termination_unconfirmed",
                    attempts=tuple(metrics),
                ) from exc
            if attempt == attempts:
                raise WorkerFailure(
                    "worker exhausted its fresh retry budget",
                    code="hard_timeout",
                    attempts=tuple(metrics),
                ) from exc
            if on_event is not None:
                on_event({"type": "fresh_retry", "attempt": attempt + 1})
            continue
        metrics.extend(result.attempts)
        return ProviderExecution(
            payload=result.payload,
            worker_id=result.worker_id,
            process=result.process,
            command=result.command,
            attempt=attempt,
            attempts=tuple(metrics),
            aggregate_elapsed_seconds=max(
                time.monotonic() - started,
                sum(float(item["elapsed_seconds"]) for item in metrics),
            ),
        )
    raise AssertionError("bounded provider retry loop escaped")


def supervise_process(
    command: list[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    soft_timeout: float = SOFT_TIMEOUT_SECONDS,
    hard_timeout: float = HARD_TIMEOUT_SECONDS,
    grace: float = TERMINATION_GRACE_SECONDS,
) -> ProcessEvidence:
    """Compatibility seam returning only terminal evidence for one provider call."""
    return run_provider_process(
        command,
        cwd=cwd,
        environment=environment,
        soft_timeout=soft_timeout,
        hard_timeout=hard_timeout,
        grace=grace,
    ).process


def _extract_typed_result(stdout: str) -> tuple[dict[str, Any], str | None]:
    objects: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    worker_id: str | None = None
    for value in objects:
        candidate = value.get("thread_id", value.get("session_id"))
        if isinstance(candidate, str) and candidate:
            worker_id = candidate
    for value in reversed(objects):
        payload = value.get("structured_output", value.get("result", value.get("output")))
        item = value.get("item")
        if payload is None and isinstance(item, dict) and item.get("type") == "agent_message":
            payload = item.get("text")
        if isinstance(payload, str):
            with contextlib.suppress(json.JSONDecodeError):
                payload = json.loads(payload)
        if isinstance(payload, dict):
            return payload, worker_id
    raise WorkerFailure("worker output contained no typed result", code="invalid_result")


def _capsule_postcondition_ok(
    authority: str, before: Mapping[str, Any], after: Mapping[str, Any]
) -> bool:
    """Decide whether the capsule receipts satisfy an authority's mutation contract.

    A read_only worker must leave its capsule byte-identical. A capsule_writer or
    disposable_writer is expected to mutate its capsule, so a changed capsule is not a
    violation. The authoritative-source guard is enforced separately for every authority.
    """
    if authority == "read_only":
        return before["digest"] == after["digest"]
    return True


def _writer_import_lock_path(source_root: Path) -> Path:
    """Locate the sole-writer import lock for a mutation domain.

    The mutation domain is the authoritative worktree; the lock lives in its git-common-dir so
    two processes importing into the same worktree serialize, and the lock file never appears in
    the worktree's own git status.
    """
    resolved = source_root.resolve()
    common = _git_bytes(resolved, "rev-parse", "--git-common-dir").strip()
    common_path = Path(os.fsdecode(common))
    if not common_path.is_absolute():
        common_path = resolved / common_path
    return common_path.resolve() / WRITER_IMPORT_LOCK_NAME


class DispatchObserver(Protocol):
    """Reads the run's live dispatch generation, route-snapshot digest, and lease fence.

    Injected so a test can drive divergence with a fake; the default reads the authoritative run
    state at import time. Narrow by design so CognitiveWorkers never couples to dispatch internals.
    """

    def __call__(self, order: WorkOrder, source: Path) -> Mapping[str, Any]: ...


# A live dispatch value that cannot be read must never compare equal to the order's frozen value,
# so an unlocatable or unreadable run state fails the CAS closed rather than importing over drift.
_UNOBSERVED: Any = object()


def observe_live_dispatch(order: WorkOrder, source: Path) -> dict[str, Any]:
    """Observe the run's current dispatch generation, route-snapshot digest, and lease fence.

    The order freezes these at issue time; the import runs well after the worker did, by when the
    dispatcher may have bumped the stage generation, rotated the route snapshot, or moved the
    lease. These live values become the observed side of the CAS, so any that drifted from the
    order refuses the import. The run is located by run_id under the worktree's .flow/runs tree;
    a missing, unreadable, or .bak-recovered state fails the generation dimension closed, since a
    value recovered from a stale backup may pre-date a re-dispatch bump. state.read heals a corrupt
    state.json from its newest .bak as it reads; that recovery is state's own flock-serialized
    path, and the recovered generation is refused regardless, so no healed-from-stale value ever
    passes the CAS.
    """
    unobserved = {
        "dispatch_generation": _UNOBSERVED,
        "route_snapshot": _UNOBSERVED,
        "lease_fence": _UNOBSERVED,
    }
    if order.run_id is None or order.stage is None:
        return unobserved
    import agent_routes
    import lease
    import state

    try:
        candidates = sorted((source / ".flow" / "runs").rglob("state.json"))
    except OSError:
        return unobserved
    matched_dir: Path | None = None
    matched_state: Any = None
    matched_exit = 0
    for state_path in candidates:
        try:
            ticket_state, exit_code = state.read(state_path.parent)
        except OSError:
            continue
        if ticket_state is not None and ticket_state.run_id == order.run_id:
            matched_dir, matched_state, matched_exit = state_path.parent, ticket_state, exit_code
            break
    if matched_dir is None or matched_state is None:
        return unobserved
    record = matched_state.stages.get(order.stage)
    try:
        run_lease = lease.read_lease(matched_dir)
    except lease.LeaseError:
        run_lease = None
    try:
        route_snapshot = agent_routes.load_snapshot(matched_dir / "route-snapshot.json")["digest"]
    except agent_routes.RouteError:
        route_snapshot = _UNOBSERVED
    # A non-zero exit_code means state.read served a value recovered from a stale .bak (or could
    # not read at all), so the generation may pre-date a re-dispatch bump; leave it unobserved so
    # the CAS fails closed rather than passing over recovered drift.
    generation = record.generation if record is not None and matched_exit == 0 else _UNOBSERVED
    return {
        "dispatch_generation": generation,
        "route_snapshot": route_snapshot,
        "lease_fence": run_lease.session_nonce if run_lease is not None else _UNOBSERVED,
    }


def _import_capsule_patch(
    source: Path,
    *,
    order: WorkOrder,
    capture: Mapping[str, Any],
    patch_path: Path,
    expected: Mapping[str, Any],
    observed_external: Mapping[str, Any],
    journal: InvocationJournal,
    lock_path: Path | None = None,
) -> dict[str, Any]:
    """Compare-and-swap the captured patch into the authoritative worktree.

    Runs under the mutation domain's sole-writer lock (contention is writer_busy, never a
    full-run block).

    A fresh import (journal at validated) refuses with baseline_mismatch when any of the six
    order invariants drifted, then applies the patch all-or-nothing with ``git apply --index``.
    git apply validates every hunk before writing, so a rejected hunk leaves the worktree
    byte-identical and is patch_import_conflict, and the capsule and patch survive as recovery
    evidence.

    Resuming an interrupted import (journal already at importing) never re-invokes the model and
    is deterministic: an already-applied tree finalizes (re-checking only the external HEAD,
    generation, route, and lease invariants, since the index and owned files legitimately reflect
    this import), an unapplied tree rolls forward under the full CAS, and a genuinely partial tree
    is indeterminate_write and is never re-baselined.
    """
    source = source.resolve()
    patch = capture["patch"] if isinstance(capture["patch"], bytes) else bytes(capture["patch"])
    lock = lock_path or _writer_import_lock_path(source)
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    resuming = str((journal.read() or {}).get("state")) == "importing"
    before = git_receipt(source)

    def apply_patch() -> None:
        if not patch:
            return
        applied = subprocess.run(
            ["git", "apply", "--index"],
            cwd=source,
            input=patch,
            capture_output=True,
            check=False,
            env=env,
        )
        if applied.returncode != 0:
            raise WorkerFailure(
                "captured patch does not apply cleanly: "
                f"{applied.stderr.decode(errors='replace').strip()}",
                code="patch_import_conflict",
            )

    try:
        with flock_retry(lock):
            journal.transition(
                "importing",
                import_lock=str(lock),
                patch=str(patch_path),
                capture={
                    key: capture[key]
                    for key in ("patch_digest", "touched", "allowed", "metadata", "empty")
                },
            )
            observed = {
                "head": _git_bytes(source, "rev-parse", "HEAD").strip().decode(),
                "index": git_receipt(source)["index"]["sha256"],
                "owned_baseline": _owned_baseline_digest(source, order.allowed_mutation_paths),
                **{key: observed_external.get(key) for key in observed_external},
            }
            if not resuming:
                drift = _cas_refusals(expected, observed)
                if drift:
                    raise WorkerFailure(
                        f"authoritative {drift[0]} drifted since the order was issued",
                        code="baseline_mismatch",
                    )
                apply_patch()
                import_result = "applied"
            else:
                state = _patch_application_state(source, patch)
                if state == "partial":
                    raise WorkerFailure(
                        "authoritative worktree holds a partially applied import",
                        code="indeterminate_write",
                    )
                fields = _EXTERNAL_CAS if state == "applied" else _CAS_FIELDS
                drift = _cas_refusals(expected, observed, fields)
                if drift:
                    raise WorkerFailure(
                        f"authoritative {drift[0]} drifted since the order was issued",
                        code="baseline_mismatch",
                    )
                if state == "applied":
                    import_result = "resumed"
                else:
                    apply_patch()
                    import_result = "applied"
            after = git_receipt(source)
            imported = subprocess.run(
                [
                    "git",
                    "diff",
                    "--binary",
                    "--full-index",
                    "--no-ext-diff",
                    "--no-textconv",
                    "-M",
                    "--cached",
                    order.source_sha,
                ],
                cwd=source,
                capture_output=True,
                check=False,
                env=env,
            )
            receipt_body = {
                "schema": CHANGE_RECEIPT_SCHEMA,
                "baseline_digest": order.source_sha,
                "patch": {
                    "path": str(patch_path),
                    "sha256": capture["patch_digest"],
                    "length": len(patch),
                },
                "allowed_paths": list(capture["allowed"]),
                "touched_paths": list(capture["touched"]),
                "metadata": capture["metadata"],
                "import_target": {"head_before": before["head"], "head_after": after["head"]},
                "import_result": import_result,
                "authoritative_diff_digest": hashlib.sha256(imported.stdout).hexdigest(),
            }
            return {**receipt_body, "digest": _digest(receipt_body)}
    except LockContention as exc:
        raise WorkerFailure(
            "another writer holds the sole-writer import lock for this mutation domain",
            code="writer_busy",
        ) from exc


class CognitiveWorkers:
    """Deep execution boundary for independently routed cognitive roles."""

    def __init__(
        self,
        *,
        artifact_root: Path,
        capsule_root: Path,
        adapters: Mapping[str, CliAdapter] | None = None,
        dispatch_observer: DispatchObserver | None = None,
    ) -> None:
        self.artifact_root = artifact_root.expanduser().resolve()
        self.capsule_root = capsule_root.expanduser().resolve()
        self.adapters = dict(adapters or ADAPTERS)
        self._dispatch_observer: DispatchObserver = dispatch_observer or observe_live_dispatch

    def _invocation_dir(self, logical_id: str) -> Path:
        token = hashlib.sha256(logical_id.encode()).hexdigest()
        return self.artifact_root / "invocations" / token

    def _invocation_lock(self, logical_id: str) -> Path:
        return self._invocation_dir(logical_id).with_suffix(".lock")

    def _dispose_failed_capsule(self, capsule: Path, *, quarantine: bool) -> dict[str, Any]:
        """Remove a dead capsule, or set an ambiguous one aside for an operator."""
        if not capsule.exists():
            return {"capsule": str(capsule), "absent": True, "quarantined": False}
        if not quarantine:
            shutil.rmtree(capsule, ignore_errors=True)
            return {"capsule": str(capsule), "absent": not capsule.exists(), "quarantined": False}
        held = self.capsule_root / "quarantine" / capsule.name
        held.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            if held.exists():
                shutil.rmtree(held, ignore_errors=True)
            os.replace(capsule, held)
        return {
            "capsule": str(capsule),
            "absent": not capsule.exists(),
            "quarantined": True,
            "quarantine_path": str(held),
        }

    def _import_after_validation(
        self, order: WorkOrder, source: Path, capsule: Path, journal: InvocationJournal
    ) -> dict[str, Any]:
        """Capture the capsule patch and compare-and-swap it into the authoritative worktree.

        The captured patch is persisted next to the journal so an importing-state resume replays
        the same bytes without the capsule; the commit-gate consumption of the returned change
        receipt is a later phase, this only emits it.
        """
        invocation = self._invocation_dir(order.logical_invocation_id)
        patch_path = invocation / "capsule-patch.bin"
        current = journal.read() or {}
        stored = current.get("capture")
        if isinstance(stored, dict) and patch_path.exists():
            capture = {**stored, "patch": patch_path.read_bytes()}
        else:
            capture = _capture_capsule_patch(
                capsule, order.source_sha, order.allowed_mutation_paths
            )
            atomic_write_bytes(patch_path, capture["patch"], mode=0o400)
        authoritative_before = cast(dict[str, Any], current.get("authoritative_before") or {})
        expected = {
            "head": order.source_sha,
            "index": cast(dict[str, Any], authoritative_before.get("index", {})).get("sha256"),
            "owned_baseline": current.get("owned_baseline_before"),
            "dispatch_generation": order.stage_generation,
            "route_snapshot": order.route_snapshot_digest,
            "lease_fence": order.lease_fence,
        }
        # The dispatch generation, route snapshot, and lease fence are observed LIVE at import
        # time (the injected DispatchObserver), not read back off the order that froze them: the
        # dispatcher may have bumped the generation, rotated the route, or moved the lease between
        # order issue and import, and importing over any of those would re-baseline external drift.
        observed_external = self._dispatch_observer(order, source)
        return _import_capsule_patch(
            source,
            order=order,
            capture=capture,
            patch_path=patch_path,
            expected=expected,
            observed_external=observed_external,
            journal=journal,
        )

    def run(self, order: WorkOrder, owner: OwnerProof) -> WorkOutcome:
        """Execute or recover one idempotent logical invocation.

        The whole body runs under an exclusive lock on the invocation, so the durable-outcome
        check, the journal read, the capsule clone, and the capsule disposal are one critical
        section. Two concurrent calls for the same logical invocation would otherwise both read
        an empty journal, reuse the one generation-keyed capsule, and delete it under the live
        provider process.
        """
        with flock_blocking(self._invocation_lock(order.logical_invocation_id)):
            return self._run_locked(order, owner)

    def _run_locked(self, order: WorkOrder, owner: OwnerProof) -> WorkOutcome:  # noqa: C901
        policy = ROLE_CATALOG[order.profile]
        if not policy.active:
            raise WorkerFailure(
                f"cognitive profile {order.profile!r} is not active yet",
                code="capability_missing",
            )
        source = Path(order.source_root).resolve()
        input_path = Path(order.input_bundle).resolve()
        if _input_digest(input_path) != order.input_digest:
            raise WorkerFailure("work-order input digest does not match", code="invalid_order")
        source_head = _git_bytes(source, "rev-parse", "HEAD").strip().decode()
        if source_head != order.source_sha:
            raise WorkerFailure("work-order source SHA is stale", code="stale_order")
        if order.run_id is not None and owner.run_id != order.run_id:
            raise WorkerFailure("owner proof does not match the work-order run", code="lost_owner")
        if order.lease_fence is not None and owner.lease_fence != order.lease_fence:
            raise WorkerFailure("owner proof does not match the lease fence", code="lost_owner")

        invocation = self._invocation_dir(order.logical_invocation_id)
        outcome_path = invocation / "outcome.json"
        if outcome_path.is_file():
            value = json.loads(outcome_path.read_text(encoding="utf-8"))
            body = {key: item for key, item in value.items() if key != "digest"}
            if value.get("digest") != _digest(body):
                raise WorkerFailure(
                    "durable worker outcome has an invalid digest", code="recovery_required"
                )
            if (
                value.get("schema") != OUTCOME_SCHEMA
                or value.get("logical_invocation_id") != order.logical_invocation_id
                or value.get("generation") != order.generation
                or value.get("profile") != order.profile
                or value.get("route_snapshot_digest") != order.route_snapshot_digest
                or value.get("source_sha") != order.source_sha
            ):
                raise WorkerFailure(
                    "durable worker outcome does not match the work order",
                    code="recovery_required",
                )
            return WorkOutcome(
                logical_invocation_id=str(value["logical_invocation_id"]),
                generation=int(value["generation"]),
                profile=str(value["profile"]),
                status=cast(
                    Literal["succeeded", "needs_input", "failed", "cancelled"],
                    value["status"],
                ),
                result=cast(dict[str, Any] | None, value.get("result")),
                receipts=cast(dict[str, Any], value["receipts"]),
                failure=cast(dict[str, Any] | None, value.get("failure")),
                run_id=cast(str | None, value.get("run_id")),
                stage=cast(str | None, value.get("stage")),
                substep=cast(str | None, value.get("substep")),
                stage_generation=int(value.get("stage_generation", 0)),
                route_snapshot_digest=cast(str | None, value.get("route_snapshot_digest")),
                source_sha=cast(str | None, value.get("source_sha")),
                lease_fence=cast(str | None, value.get("lease_fence")),
                input_bundle=cast(str | None, value.get("input_bundle")),
                input_digest=cast(str | None, value.get("input_digest")),
                digest=str(value["digest"]),
            )

        invocation.mkdir(parents=True, exist_ok=True, mode=0o700)
        journal = InvocationJournal(invocation / "journal.json", order.logical_invocation_id)
        existing = journal.read()
        recovery_state = str(existing["state"]) if existing is not None else "prepared"
        if existing is not None:
            if recovery_state in {"running", "cancelling"}:
                raise WorkerFailure(
                    f"invocation recovery requires supervision from state {recovery_state}",
                    code="recovery_required",
                )
            if recovery_state in {"blocked", "quarantined", "completed"}:
                raise WorkerFailure(
                    f"invocation is terminally {recovery_state}", code="termination_unconfirmed"
                )
        else:
            journal.transition(
                "prepared",
                order_digest=_digest(order.to_mapping()),
                owner_digest=_digest(asdict(owner)),
                launch_nonce=secrets.token_hex(32),
            )

        adapter = self.adapters.get(order.route["harness"])
        if adapter is None:
            raise WorkerFailure("work-order harness has no exact adapter", code="route_unavailable")
        schema = order.result_schema or provider_schema(order.profile)
        if order.profile == "planner" and isinstance(
            adapter, (CodexCliAdapter, ClaudeCodeCliAdapter)
        ):
            capability = preflight_route(order.route, require_resume=True)
        else:
            capability = adapter.preflight(order.route, order.authority)
        prompt = (
            _bound_provider_prompt(order.provider_prompt, facts=order.facts, schema=schema)
            if order.provider_prompt is not None
            else PROMPT_BUILDERS[order.profile](order.facts)
        )
        schema_path = invocation / "provider-schema.json"
        _atomic_json(schema_path, schema, mode=0o400)
        capsule = (
            self.capsule_root
            / hashlib.sha256(
                f"{order.logical_invocation_id}:{order.generation}".encode()
            ).hexdigest()
        )

        def guarded_receipt(path: Path) -> dict[str, Any]:
            try:
                return git_receipt(path)
            except OSError as exc:
                # A receipt that cannot be read cannot prove the read-only postcondition, and a
                # bare OSError would escape every WorkerFailure handler below: no journal
                # transition, no capsule disposal, and a spurious artifact_failure at the CLI.
                journal.transition(
                    "quarantined",
                    failure={"code": "artifact_failure", "message": str(exc)},
                    disposal=self._dispose_failed_capsule(capsule, quarantine=True),
                )
                raise WorkerFailure(f"git receipt failed: {exc}", code="artifact_failure") from exc

        if recovery_state in {"prepared", "cloning"}:
            journal.transition("cloning", capsule=str(capsule))
        if capsule.exists():
            capsule_git = guarded_receipt(capsule)
            if capsule_git["head"] != order.source_sha:
                journal.transition("quarantined", failure={"code": "baseline_mismatch"})
                raise WorkerFailure(
                    "recovered capsule has the wrong source SHA", code="baseline_mismatch"
                )
            capsule_receipt = cast(dict[str, Any], (existing or {}).get("capsule_receipt"))
            if not capsule_receipt:
                capsule_body = {
                    "schema": "flow.cognitive-capsule/v1",
                    "source_root": str(source),
                    "source_sha": order.source_sha,
                    "capsule": str(capsule),
                    "git_receipt": capsule_git,
                    "standalone": True,
                }
                capsule_receipt = {**capsule_body, "digest": _digest(capsule_body)}
        else:
            capsule_receipt = create_private_clone(
                source, order.source_sha, capsule, seed=_load_seed(order)
            )

        current = journal.read() or {}
        execution: ProviderExecution | None = None
        if recovery_state in {"terminal", "validated", "importing"}:
            process_raw = current.get("process")
            if not isinstance(process_raw, dict):
                journal.transition("quarantined", failure={"code": "recovery_required"})
                raise WorkerFailure(
                    "terminal journal has no process evidence", code="recovery_required"
                )
            process = ProcessEvidence(**process_raw)
            authoritative_before = cast(dict[str, Any], current.get("authoritative_before"))
            capsule_before = cast(dict[str, Any], current.get("capsule_before"))
            if not authoritative_before or not capsule_before or not process.terminal_acknowledged:
                journal.transition("quarantined", failure={"code": "termination_unconfirmed"})
                raise WorkerFailure(
                    "terminal journal cannot prove lifecycle or Git guards",
                    code="termination_unconfirmed",
                )
        else:
            authoritative_before = guarded_receipt(source)
            capsule_before = guarded_receipt(capsule)

            def build_command(fresh: bool) -> list[str]:
                if order.provider_prompt is not None:
                    session = order.session or {}
                    selected_prompt = (
                        order.fresh_provider_prompt or order.provider_prompt
                        if fresh
                        else order.provider_prompt
                    )
                    return adapter.session_command(
                        order.route,
                        selected_prompt,
                        schema_path,
                        thread_id=None if fresh else session.get("thread_id"),
                        new_thread_id=(
                            session.get("fresh_session_id")
                            if fresh
                            else session.get("initial_session_id")
                        ),
                    )
                return adapter.command(
                    order.route, prompt.prompt, schema_path, capsule, order.authority
                )

            first_command = build_command(False)

            def command_factory(fresh: bool) -> list[str]:
                return build_command(True) if fresh else first_command

            running_fields: dict[str, Any] = {
                "command_digest": _digest(first_command),
                "authoritative_before": authoritative_before,
                "capsule_before": capsule_before,
                "capsule_receipt": capsule_receipt,
            }
            if policy.authority == "capsule_writer":
                # The owned-file baseline is the CAS reference the import re-checks; capture it at
                # launch, before the worker runs, so a later external edit to an owned file drifts.
                running_fields["owned_baseline_before"] = _owned_baseline_digest(
                    source, order.allowed_mutation_paths
                )
            journal.transition("running", **running_fields)
            try:
                execution = run_provider_with_retry(
                    command_factory,
                    cwd=capsule,
                    environment=worker_environment(
                        {"FLOW_WORKER_INVOCATION": order.logical_invocation_id}
                    ),
                    retry_limit=policy.retry_limit,
                )
            except WorkerFailure as exc:
                quarantine = exc.code == "termination_unconfirmed"
                state = "quarantined" if quarantine else "blocked"
                # A blocked invocation is provably terminal, so its capsule is dead weight:
                # both states are terminal, and nothing would ever come back to remove it.
                # Only a possibly-live process earns a retained capsule, and it is moved
                # aside so quarantine stays inspectable and bounded.
                disposal = self._dispose_failed_capsule(capsule, quarantine=quarantine)
                journal.transition(
                    state,
                    failure={
                        "code": exc.code,
                        "message": str(exc),
                        "physical_attempts": list(exc.attempts),
                    },
                    disposal=disposal,
                )
                raise
            process = execution.process
            journal.transition(
                "terminal",
                process=asdict(process),
                physical_attempts=list(execution.attempts),
                command=[*execution.command[:-1], "<prompt>"],
                worker_id=execution.worker_id,
            )

        current = journal.read() or {}
        if recovery_state in {"validated", "importing"}:
            raw_result = current.get("result")
            if not isinstance(raw_result, dict):
                journal.transition("quarantined", failure={"code": "recovery_required"})
                raise WorkerFailure(
                    "validated journal has no typed result", code="recovery_required"
                )
            result = validate_typed_result(order.profile, raw_result)
            raw_worker_id = current.get("worker_id")
            worker_id = raw_worker_id if isinstance(raw_worker_id, str) else None
            authoritative_after = cast(dict[str, Any], current.get("authoritative_after"))
            if not authoritative_after:
                journal.transition("quarantined", failure={"code": "recovery_required"})
                raise WorkerFailure(
                    "validated journal has no authoritative Git receipt",
                    code="recovery_required",
                )
        else:
            if execution is not None:
                result, worker_id = execution.payload, execution.worker_id
            else:
                result, worker_id = _extract_typed_result(process.stdout)
            try:
                result = validate_typed_result(order.profile, result)
                # A reviewer's verdict is only meaningful over the evidence it was actually
                # given. Without this the schema accepts any 64-hex string and a clean verdict
                # could have been reached over a stale or empty bundle.
                if (
                    order.profile in {"code_reviewer", "diff_reviewer"}
                    and result.get("input_digest") != order.input_digest
                ):
                    raise WorkerFailure(
                        f"{order.profile} verdict does not cite the exact review bundle",
                        code="invalid_result",
                    )
                if (
                    order.profile in {"e2e", "implementer", "review_fixer", "revision_fixer"}
                    and result.get("source_sha") != order.source_sha
                ):
                    raise WorkerFailure(
                        f"{order.profile} result does not cite the exact capsule source SHA",
                        code="invalid_result",
                    )
            except WorkerFailure as exc:
                journal.transition(
                    "blocked",
                    failure={"code": exc.code, "message": str(exc)},
                    disposal=self._dispose_failed_capsule(capsule, quarantine=False),
                )
                raise
            authoritative_after = guarded_receipt(source)
            capsule_after = guarded_receipt(capsule)
            if authoritative_before["digest"] != authoritative_after["digest"]:
                journal.transition(
                    "quarantined",
                    failure={"code": "read_only_violation"},
                    disposal=self._dispose_failed_capsule(capsule, quarantine=True),
                )
                raise WorkerFailure(
                    "worker invocation changed the authoritative repository",
                    code="read_only_violation",
                )
            if not _capsule_postcondition_ok(order.authority, capsule_before, capsule_after):
                journal.transition(
                    "quarantined",
                    failure={"code": "read_only_violation"},
                    disposal=self._dispose_failed_capsule(capsule, quarantine=True),
                )
                raise WorkerFailure(
                    "read-only worker changed its capsule", code="read_only_violation"
                )
            validated_fields: dict[str, Any] = {
                "result_digest": _digest(result),
                "result": result,
                "worker_id": worker_id,
                "authoritative_after": authoritative_after,
            }
            if policy.authority == "disposable_writer":
                # Capture the disposable capsule's mutations before disposal and persist them here:
                # a crash before completion re-creates a CLEAN capsule at the source SHA (the clone
                # branch above), so a later re-capture would falsely read zero mutations. This
                # summary is the only durable evidence of what the recipe wrote.
                validated_fields["capsule_mutations"] = _capsule_mutation_summary(
                    capsule, order.source_sha
                )
            journal.transition("validated", **validated_fields)
        if order.session is not None and not worker_id:
            journal.transition(
                "blocked",
                failure={"code": "invalid_result"},
                disposal=self._dispose_failed_capsule(capsule, quarantine=False),
            )
            raise WorkerFailure(
                "planner output carried no worker session id", code="invalid_result"
            )
        change_receipt: dict[str, Any] | None = None
        if policy.authority == "capsule_writer":
            # Dormant until a writer profile activates: the active gate above refuses every writer
            # before this runs. On a capture/import failure the helper leaves the journal in
            # importing (or validated) and we raise before disposal, so the capsule and patch
            # survive as recovery evidence and a repeat run resumes without re-invoking the model.
            change_receipt = self._import_after_validation(order, source, capsule, journal)
        elif policy.authority == "disposable_writer":
            # E2E imports nothing and takes no writer lock: the capsule mutation summary captured
            # at the validated transition becomes report evidence, while the capsule and every
            # source mutation in it are discarded below. The authoritative worktree is proven
            # untouched by the read_only_violation guard above, which fires for every authority.
            result = {
                **result,
                "capsule_mutations": (journal.read() or {}).get("capsule_mutations"),
            }
        if capsule.exists():
            shutil.rmtree(capsule)
        disposal = {
            "capsule": str(capsule),
            "absent": not capsule.exists(),
            "quarantined": False,
        }
        if not disposal["absent"]:
            journal.transition("quarantined", failure={"code": "artifact_failure"})
            raise WorkerFailure("validated capsule disposal failed", code="artifact_failure")
        physical_attempts = cast(
            list[dict[str, Any]], (journal.read() or {}).get("physical_attempts", [])
        )
        route_receipt_body = {
            "schema": "flow.cognitive-route-receipt/v1",
            "desired": order.route,
            "effective": order.route,
            "activation": "active",
            "transport": "cli",
            "adapter": adapter.__class__.__name__,
            "adapter_version": capability["version"],
            "worker_id": worker_id,
            "prompt": asdict(prompt),
            "schema_digest": _digest(schema),
            "physical_pid": process.pid,
            "physical_attempts": physical_attempts,
        }
        route_receipt = {**route_receipt_body, "digest": _digest(route_receipt_body)}
        route_acceptance = {
            "request": order.route,
            "response": {
                "accepted": True,
                **order.route,
                "transport": "cli",
                "adapter_version": capability["version"],
                "canonical_model": None,
                "worker_id": worker_id,
            },
            "prompt_hash": prompt.prompt_digest,
            "schema_hash": _digest(schema),
            "physical_attempt": {
                "pid": process.pid,
                "terminal_acknowledged": process.terminal_acknowledged,
                "attempts": physical_attempts,
            },
            "cleanup": {
                "capsule_absent": disposal["absent"],
                "quarantined": disposal["quarantined"],
            },
        }
        receipts: dict[str, Any] = {
            "route": route_receipt,
            "route_acceptance": route_acceptance,
            "capability": capability,
            "capsule": capsule_receipt,
            "process": asdict(process),
            "physical_attempts": physical_attempts,
            "command": cast(list[str], (journal.read() or {}).get("command", [])),
            "authoritative_before": authoritative_before,
            "authoritative_after": authoritative_after,
            "disposal": disposal,
        }
        if change_receipt is not None:
            receipts["change"] = change_receipt
        outcome = WorkOutcome(
            logical_invocation_id=order.logical_invocation_id,
            generation=order.generation,
            profile=order.profile,
            status="succeeded",
            result=result,
            receipts=receipts,
            run_id=order.run_id,
            stage=order.stage,
            substep=order.substep,
            stage_generation=order.stage_generation,
            route_snapshot_digest=order.route_snapshot_digest,
            source_sha=order.source_sha,
            lease_fence=order.lease_fence,
            input_bundle=order.input_bundle,
            input_digest=order.input_digest,
        )
        mapping = outcome.to_mapping()
        _atomic_json(outcome_path, mapping, mode=0o400)
        journal.transition("completed", outcome_digest=mapping["digest"])
        return WorkOutcome(**{**asdict(outcome), "digest": mapping["digest"]})

    def cancel(self, logical_invocation_id: str, owner: OwnerProof, reason: str) -> dict[str, Any]:
        """Idempotently cancel a known invocation or refuse ambiguous recovery.

        Cancellation waits for the invocation lock only within the bounded retry budget: it exists
        to stop a live run, so blocking for that run's whole duration would be indistinguishable
        from a hang. A caller that loses the race is told the invocation is busy and can retry.
        """
        try:
            with flock_retry(self._invocation_lock(logical_invocation_id)):
                return self._cancel_locked(logical_invocation_id, owner, reason)
        except LockContention as exc:
            raise WorkerFailure(
                "invocation is executing under another run; cancellation cannot mutate its journal",
                code="execution_busy",
            ) from exc

    def _cancel_locked(
        self, logical_invocation_id: str, owner: OwnerProof, reason: str
    ) -> dict[str, Any]:
        del owner
        invocation = self._invocation_dir(logical_invocation_id)
        journal = InvocationJournal(invocation / "journal.json", logical_invocation_id)
        value = journal.read()
        if value is None:
            raise WorkerFailure("cannot cancel an unknown invocation", code="invalid_order")
        if value["state"] in {"completed", "blocked", "quarantined"}:
            return {
                "schema": "flow.cognitive-cancellation/v1",
                "logical_invocation_id": logical_invocation_id,
                "state": value["state"],
                "idempotent": True,
            }
        # A recovered PID without live pipe ownership cannot prove output EOF.
        journal.transition(
            "quarantined", failure={"code": "termination_unconfirmed", "reason": reason}
        )
        raise WorkerFailure(
            "cannot prove terminal output closure after owner recovery",
            code="termination_unconfirmed",
        )


def _load_order(path: Path) -> WorkOrder:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerFailure(f"cannot read work order: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkerFailure("work order must be an object")
    return WorkOrder.from_mapping(value)


def _sealed_planned_files(sealed: Mapping[str, Any]) -> tuple[str, ...]:
    """Read the ticket's planned files (a writer's allowed-mutation boundary) from baseline.json.

    The dispatcher seals the run's ticket_dir into every cognitive substep; the implement stage's
    records_diff_baseline pre-hook has written baseline.json by the time this order is prepared, so
    its planned_files is exactly the set the content-ownership commit gate re-scans. A missing or
    malformed baseline yields an empty set, which fails the writer's touched-subset-of-allowed check
    closed rather than importing an unbounded change.
    """
    ticket_dir = sealed.get("ticket_dir")
    if not isinstance(ticket_dir, str) or not ticket_dir:
        return ()
    try:
        raw = json.loads((Path(ticket_dir) / "baseline.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    planned = raw.get("planned_files") if isinstance(raw, dict) else None
    if not isinstance(planned, list):
        return ()
    ordered: dict[str, None] = {}
    for entry in planned:
        normalized = str(entry).removeprefix("./").replace("\\", "/").strip()
        if normalized:
            ordered.setdefault(normalized, None)
    return tuple(ordered)


def prepare_work_order(
    descriptor: Mapping[str, Any],
    *,
    substep: str,
    source_root: Path,
    input_bundle: Path,
    facts: dict[str, Any],
    output: Path,
) -> WorkOrder:
    """Materialize a closed work order from one dispatch-sealed substep."""
    substeps = descriptor.get("cognitive_substeps")
    sealed = substeps.get(substep) if isinstance(substeps, dict) else None
    if not isinstance(sealed, dict):
        raise WorkerFailure(f"dispatch descriptor has no cognitive substep {substep!r}")
    if sealed.get("activation") != "pending":
        raise WorkerFailure(f"cognitive substep {substep!r} is not active")
    route = sealed.get("desired_route")
    if not isinstance(route, dict):
        raise WorkerFailure(f"cognitive substep {substep!r} has no exact route")
    source = source_root.expanduser().resolve()
    head = _git_bytes(source, "rev-parse", "HEAD").strip().decode()
    if head != sealed.get("source_sha"):
        raise WorkerFailure("dispatch cognitive source SHA is stale", code="stale_order")
    bundle = input_bundle.expanduser().resolve()
    order_path = output.expanduser().resolve()
    authority = ROLE_CATALOG[str(sealed["profile"])].authority
    # An importing writer may touch only the ticket's planned files: seal that set as the order's
    # allowed_mutation_paths so the capture's touched-subset-of-allowed check refuses an import of
    # any path the commit gate would reject. A disposable writer (E2E) imports nothing, so it keeps
    # the empty set and enforces no ownership boundary.
    allowed_mutation_paths: tuple[str, ...] = (
        _sealed_planned_files(sealed) if authority == "capsule_writer" else ()
    )
    seed_patch_path: str | None = None
    seed_digest: str | None = None
    # A pre-commit writer (E2E today) must run the recipe against the ticket's real code, whose
    # implement/code_review edits are still uncommitted at dispatch. Seal that delta as an
    # immutable, digest-bound patch so the seed is fixed at capture time and cannot drift.
    if authority != "read_only":
        seed = _capture_working_delta(source, head)
        if seed:
            seed_file = order_path.with_name(f"{order_path.stem}.seed.patch")
            atomic_write_bytes(seed_file, seed, mode=0o400)
            seed_patch_path = str(seed_file)
            seed_digest = hashlib.sha256(seed).hexdigest()
    order = WorkOrder(
        logical_invocation_id=str(sealed["logical_invocation_id"]),
        generation=int(sealed["stage_generation"]),
        profile=str(sealed["profile"]),
        source_root=str(source),
        source_sha=head,
        route={key: str(route[key]) for key in ("harness", "model", "effort")},
        route_snapshot_digest=str(sealed["route_snapshot_digest"]),
        input_bundle=str(bundle),
        input_digest=_input_digest(bundle),
        facts=facts,
        allowed_mutation_paths=allowed_mutation_paths,
        seed_patch=seed_patch_path,
        seed_digest=seed_digest,
        run_id=str(sealed["run_id"]),
        stage=str(sealed["stage"]),
        substep=str(sealed["substep"]),
        stage_generation=int(sealed["stage_generation"]),
        expected_state="in_progress",
        lease_fence=cast(str | None, sealed.get("lease_fence")),
    )
    _atomic_json(order_path, order.to_mapping(), mode=0o400)
    return order


def _owner_harness() -> str:
    return os.environ.get("FLOW_HARNESS", "").replace("-", "_")


def run_stage(
    descriptor: Mapping[str, Any],
    inputs: Mapping[str, Any],
    *,
    source_root: Path,
    artifact_root: Path,
    capsule_root: Path,
    owner_id: str,
    owner_harness: str,
    workers: CognitiveWorkers | None = None,
) -> dict[str, Any]:
    """Execute every cognitive substep the dispatcher sealed as active for one stage.

    Shadow, legacy, and historical substeps are never launched: only a substep the frozen
    snapshot recorded as ``pending`` may run, and a conditional one may instead carry a
    reasoned deterministic skip. The typed result of each launch is published separately
    so the existing deterministic renderers and appliers consume cognition without
    inheriting any of its authority.
    """
    sealed = descriptor.get("cognitive_substeps")
    if not isinstance(sealed, dict):
        raise WorkerFailure("stage descriptor carries no sealed cognitive substeps")
    outcomes: dict[str, Any] = {}
    skips: dict[str, Any] = {}
    results: dict[str, str] = {}
    for substep in sorted(sealed):
        facts_of = sealed[substep]
        if not isinstance(facts_of, dict) or facts_of.get("activation") != "pending":
            continue
        # The dispatcher sealed where this receipt has to land; it reads the outcome back
        # from exactly that path, so the caller does not get to choose it.
        sealed_root = facts_of.get("artifact_root")
        root = Path(sealed_root) if isinstance(sealed_root, str) else artifact_root
        executor = workers or CognitiveWorkers(artifact_root=root, capsule_root=capsule_root)
        entry = inputs.get(substep)
        if not isinstance(entry, dict):
            raise WorkerFailure(
                f"activated cognitive substep {substep!r} has no immutable input entry"
            )
        skip = entry.get("skip")
        if skip is not None:
            reason = str(skip.get("reason", "")).strip() if isinstance(skip, dict) else ""
            if not facts_of.get("conditional") or not reason:
                raise WorkerFailure(
                    f"cognitive substep {substep!r} needs a conditional route and an exact "
                    "reason to skip"
                )
            skips[substep] = {
                "substep": substep,
                "stage_generation": facts_of["stage_generation"],
                "reason": reason,
            }
            continue
        facts = entry.get("facts")
        bundle = entry.get("input_bundle")
        if not isinstance(facts, dict) or not isinstance(bundle, str):
            raise WorkerFailure(
                f"cognitive substep {substep!r} requires closed facts and an immutable bundle"
            )
        order = prepare_work_order(
            descriptor,
            substep=substep,
            source_root=source_root,
            input_bundle=Path(bundle),
            facts=facts,
            output=root / "orders" / f"{substep}.json",
        )
        owner = OwnerProof(
            owner_id=owner_id,
            harness=owner_harness,
            run_id=order.run_id,
            lease_fence=order.lease_fence,
        )
        outcome = executor.run(order, owner).to_mapping()
        outcomes[substep] = outcome
        result_path = root / "results" / f"{substep}.json"
        _atomic_json(result_path, outcome["result"], mode=0o400)
        results[substep] = str(result_path)
    return {
        "schema": STAGE_OUTCOMES_SCHEMA,
        "stage": descriptor.get("stage"),
        "stage_generation": descriptor.get("generation"),
        "cognitive_outcomes": outcomes,
        "cognitive_skips": skips,
        "results": results,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run exact routed cognition in a capsule.")
    sub = parser.add_subparsers(dest="operation", required=True)
    run = sub.add_parser("run")
    run.add_argument("--work-order", required=True)
    run.add_argument("--artifact-root", required=True)
    run.add_argument("--capsule-root", required=True)
    run.add_argument("--owner-id", default="facade-owner")
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--descriptor-from", required=True)
    prepare_parser.add_argument("--substep", required=True)
    prepare_parser.add_argument("--source-root", required=True)
    prepare_parser.add_argument("--input-bundle", required=True)
    prepare_parser.add_argument("--facts-from", required=True)
    prepare_parser.add_argument("--output", required=True)
    stage_parser = sub.add_parser("run-stage")
    stage_parser.add_argument("--descriptor-from", required=True)
    stage_parser.add_argument("--inputs-from", required=True)
    stage_parser.add_argument("--source-root", required=True)
    stage_parser.add_argument("--artifact-root", required=True)
    stage_parser.add_argument("--capsule-root", required=True)
    stage_parser.add_argument("--output", required=True)
    stage_parser.add_argument("--owner-id", default="facade-owner")
    bundle_parser = sub.add_parser("bundle-review")
    bundle_parser.add_argument("--source-root", required=True)
    bundle_parser.add_argument("--output", required=True)
    return parser


def cli_main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.operation == "bundle-review":
            result = build_review_input_bundle(Path(args.source_root), Path(args.output))
            sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
            return 0
        if args.operation == "prepare":
            descriptor = json.loads(Path(args.descriptor_from).read_text(encoding="utf-8"))
            facts = json.loads(Path(args.facts_from).read_text(encoding="utf-8"))
            if not isinstance(descriptor, dict) or not isinstance(facts, dict):
                raise WorkerFailure("descriptor and cognitive facts must be objects")
            order = prepare_work_order(
                descriptor,
                substep=args.substep,
                source_root=Path(args.source_root),
                input_bundle=Path(args.input_bundle),
                facts=facts,
                output=Path(args.output),
            )
            sys.stdout.write(json.dumps(order.to_mapping(), indent=2, sort_keys=True) + "\n")
            return 0
        if args.operation == "run-stage":
            descriptor = json.loads(Path(args.descriptor_from).read_text(encoding="utf-8"))
            inputs = json.loads(Path(args.inputs_from).read_text(encoding="utf-8"))
            if not isinstance(descriptor, dict) or not isinstance(inputs, dict):
                raise WorkerFailure("stage descriptor and cognitive inputs must be objects")
            body = run_stage(
                descriptor,
                inputs,
                source_root=Path(args.source_root),
                artifact_root=Path(args.artifact_root).expanduser().resolve(),
                capsule_root=Path(args.capsule_root).expanduser().resolve(),
                owner_id=args.owner_id,
                owner_harness=_owner_harness(),
            )
            _atomic_json(Path(args.output).expanduser().resolve(), body)
            sys.stdout.write(json.dumps(body, indent=2, sort_keys=True) + "\n")
            return 0
        order = _load_order(Path(args.work_order).expanduser().resolve())
        owner = OwnerProof(
            owner_id=args.owner_id,
            harness=_owner_harness(),
            run_id=order.run_id,
            lease_fence=order.lease_fence,
        )
        workers = CognitiveWorkers(
            artifact_root=Path(args.artifact_root), capsule_root=Path(args.capsule_root)
        )
        result = workers.run(order, owner).to_mapping()
    except (OSError, WorkerFailure) as exc:
        code = exc.code if isinstance(exc, WorkerFailure) else "artifact_failure"
        sys.stderr.write(
            "cognitive-worker: "
            + json.dumps({"error": str(exc), "code": code}, sort_keys=True)
            + "\n"
        )
        return 2
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "ACTIVE_READ_ONLY_PROFILES",
    "PROMPT_BUILDERS",
    "ROLE_CATALOG",
    "ClaudeCodeCliAdapter",
    "CodexCliAdapter",
    "CognitiveWorkers",
    "DispatchObserver",
    "InvocationJournal",
    "OwnerProof",
    "PromptMaterial",
    "RolePolicy",
    "WorkOrder",
    "WorkOutcome",
    "WorkerFailure",
    "build_code_reviewer_prompt",
    "build_diff_reviewer_prompt",
    "build_e2e_prompt",
    "build_guard_reviewer_prompt",
    "build_implementer_prompt",
    "build_plan_assessor_prompt",
    "build_planner_prompt",
    "build_reflector_prompt",
    "build_review_brief_author_prompt",
    "build_review_fixer_prompt",
    "build_review_input_bundle",
    "build_revision_fixer_prompt",
    "cli_main",
    "create_private_clone",
    "git_receipt",
    "observe_live_dispatch",
    "prepare_work_order",
    "provider_schema",
    "run_stage",
    "supervise_process",
    "validate_typed_result",
    "worker_environment",
]
