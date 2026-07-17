"""Canonical state machine for Flow's read-only planning attempts.

Planning attempts exist before a delivery run. They retain complete review artifacts
while excluding live worker-session identifiers, then produce one exact approval
receipt that the existing post-approval bootstrap can verify.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import _locking
import agent_routes
from _atomicio import atomic_write_bytes, atomic_write_text

ENVELOPE_SCHEMA = "flow.plan-envelope/v1"
ATTEMPT_SCHEMA = "flow.planning-attempt/v1"
VERDICT_SCHEMA = "flow.plan-assessment/v1"
REVALIDATION_SCHEMA = "flow.plan-revalidation/v1"
GATE_SCHEMA = "flow.plan-gate/v1"
APPROVAL_SCHEMA = "flow.plan-approval/v1"

_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_STATUSES = frozenset({"NEEDS_INPUT", "PLAN_READY"})
_DISPOSITIONS = frozenset({"pending", "incorporated", "rejected"})
_PLAN_LANES = frozenset({"express", "light", "full"})
_PLAN_REQUIRED = (
    "motivation",
    "goal",
    "scenarios",
    "architecture",
    "decisions",
    "acceptance_outcomes",
    "steps",
    "files",
    "context_paths",
    "verification",
    "e2e_recipe",
    "lane",
    "compatibility",
    "rollout",
    "risks",
)


class AttemptError(ValueError):
    """A planning artifact or state transition violates the gate contract."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def canonical_digest(value: object) -> str:
    """Return the SHA-256 digest of canonical UTF-8 JSON."""
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def envelope_json_schema() -> dict[str, Any]:
    """Return the strict provider-facing schema for one complete planner result."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Flow plan envelope",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "attempt_id",
            "version",
            "parent_digest",
            "base_sha",
            "route_digest",
            "author",
            "status",
            "plan",
            "questions",
            "incorporated_feedback_ids",
        ],
        "properties": {
            "attempt_id": {"type": "string", "minLength": 1},
            "version": {"type": "integer", "minimum": 1},
            "parent_digest": {
                "anyOf": [
                    {"type": "null"},
                    {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                ]
            },
            "base_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
            "route_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "author": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "harness", "model"],
                "properties": {
                    "id": {
                        "type": "string",
                        "minLength": 1,
                        "pattern": "^(codex|claude_code):.+$",
                        "description": "exact <harness>:<model> of the launched route",
                    },
                    "harness": {
                        "type": "string",
                        "minLength": 1,
                        "description": "the harness that executed the launched route",
                    },
                    "model": {
                        "type": "string",
                        "minLength": 1,
                        "description": "the exact model selector of the launched route",
                    },
                },
            },
            "status": {"enum": sorted(_STATUSES)},
            "plan": {
                "type": "object",
                "additionalProperties": False,
                "required": list(_PLAN_REQUIRED),
                "properties": {
                    "motivation": {"type": "string", "minLength": 1},
                    "goal": {"type": "string", "minLength": 1},
                    "scenarios": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["before", "after"],
                            "properties": {
                                "before": {"type": "string", "minLength": 1},
                                "after": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                    "architecture": {"type": "array", "items": {"type": "string"}},
                    "decisions": {"type": "array", "items": {"type": "string"}},
                    "acceptance_outcomes": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "files": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "context_paths": {"type": "array", "items": {"type": "string"}},
                    "verification": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "e2e_recipe": {"type": "string", "minLength": 1},
                    "lane": {"enum": sorted(_PLAN_LANES)},
                    "compatibility": {"type": "array", "items": {"type": "string"}},
                    "rollout": {"type": "string", "minLength": 1},
                    "risks": {"type": "array", "items": {"type": "string"}},
                },
            },
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "text", "anchors"],
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "text": {"type": "string", "minLength": 1},
                        "anchors": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "incorporated_feedback_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }


def _body_with_digest(body: dict[str, Any]) -> dict[str, Any]:
    return {**body, "digest": canonical_digest(body)}


def _verify_digest(value: Mapping[str, Any], schema: str) -> dict[str, Any]:
    body = {key: item for key, item in value.items() if key != "digest"}
    if value.get("schema") != schema:
        raise AttemptError(f"unsupported schema {value.get('schema')!r}; expected {schema!r}")
    if value.get("digest") != canonical_digest(body):
        raise AttemptError("artifact digest does not match its canonical content")
    return body


def _require_hex(value: object, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise AttemptError(f"{label} must be a lowercase hexadecimal digest")
    return value


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AttemptError(f"{label} must be a non-empty string")
    return value.strip()


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AttemptError(f"{label} must be a list of strings")
    if len(set(value)) != len(value):
        raise AttemptError(f"{label} must not contain duplicates")
    return tuple(item for item in value if isinstance(item, str))


def _plan_string_list(value: object, label: str, *, required: bool = False) -> tuple[str, ...]:
    items = _string_list(value, label)
    if required and not items:
        raise AttemptError(f"{label} must contain at least one item")
    if any(not item.strip() for item in items):
        raise AttemptError(f"{label} entries must be non-empty strings")
    return items


def _validate_complete_plan(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise AttemptError("planner response must contain a complete plan object")
    missing = [field for field in _PLAN_REQUIRED if field not in value]
    if missing:
        raise AttemptError(f"complete plan is missing required field {missing[0]!r}")
    extra = set(value) - set(_PLAN_REQUIRED)
    if extra:
        raise AttemptError(f"plan has unknown fields: {', '.join(sorted(extra))}")
    _nonempty(value.get("motivation"), "plan motivation")
    _nonempty(value.get("goal"), "plan goal")
    scenarios = value.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise AttemptError("plan scenarios must contain at least one before/after scenario")
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise AttemptError(f"plan scenario {index + 1} must be an object")
        extra = set(scenario) - {"before", "after"}
        if extra:
            raise AttemptError(
                f"plan scenario {index + 1} has unknown fields: {', '.join(sorted(extra))}"
            )
        _nonempty(scenario.get("before"), f"plan scenario {index + 1} before")
        _nonempty(scenario.get("after"), f"plan scenario {index + 1} after")
    _plan_string_list(value.get("architecture"), "plan architecture")
    _plan_string_list(value.get("decisions"), "plan decisions")
    _plan_string_list(value.get("acceptance_outcomes"), "plan acceptance outcomes", required=True)
    _plan_string_list(value.get("steps"), "plan steps", required=True)
    _plan_string_list(value.get("files"), "plan files", required=True)
    _plan_string_list(value.get("context_paths"), "plan context paths")
    _plan_string_list(value.get("verification"), "plan verification", required=True)
    _nonempty(value.get("e2e_recipe"), "plan e2e recipe")
    if value.get("lane") not in _PLAN_LANES:
        raise AttemptError(f"plan lane must be one of {sorted(_PLAN_LANES)!r}")
    _plan_string_list(value.get("compatibility"), "plan compatibility")
    _nonempty(value.get("rollout"), "plan rollout")
    _plan_string_list(value.get("risks"), "plan risks")
    return json.loads(json.dumps(value))


def _paths_overlap(left: str, right: str) -> bool:
    left_parts = tuple(part for part in left.replace("\\", "/").split("/") if part not in {"", "."})
    right_parts = tuple(
        part for part in right.replace("\\", "/").split("/") if part not in {"", "."}
    )
    if not left_parts or not right_parts:
        return False
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


def _verified_route_receipt(
    value: Mapping[str, Any],
    *,
    route_digest: str,
    profile: str,
) -> dict[str, Any]:
    try:
        receipt = agent_routes.verify_receipt(dict(value))
    except agent_routes.RouteError as exc:
        raise AttemptError(f"invalid {profile} launch receipt: {exc}") from exc
    if receipt.get("snapshot_digest") != route_digest or receipt.get("profile") != profile:
        raise AttemptError(f"{profile} launch receipt does not match the attempt route")
    return json.loads(json.dumps(receipt))


@dataclass(frozen=True)
class PlanEnvelope:
    """One complete, immutable planner result accepted by owner-side CAS."""

    attempt_id: str
    version: int
    parent_digest: str | None
    base_sha: str
    route_digest: str
    author: dict[str, str]
    status: str
    plan: dict[str, Any]
    questions: tuple[dict[str, Any], ...]
    incorporated_feedback_ids: tuple[str, ...]
    digest: str
    schema: str = field(default=ENVELOPE_SCHEMA, init=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PlanEnvelope:  # noqa: C901
        """Validate a worker result and derive its canonical content digest."""
        allowed = {*envelope_json_schema()["required"], "schema", "digest"}
        extra = set(value) - allowed
        if extra:
            raise AttemptError(f"plan envelope has unknown fields: {', '.join(sorted(extra))}")
        if value.get("schema") not in {None, ENVELOPE_SCHEMA}:
            raise AttemptError("unsupported plan envelope schema")
        attempt_id = _nonempty(value.get("attempt_id"), "attempt id")
        version = value.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise AttemptError("version must be a positive integer")
        parent = value.get("parent_digest")
        if parent is not None:
            parent = _require_hex(parent, "parent digest", _HEX_64)
        base_sha = _require_hex(value.get("base_sha"), "base SHA", _HEX_40)
        route_digest = _require_hex(value.get("route_digest"), "route digest", _HEX_64)
        raw_author = value.get("author")
        if not isinstance(raw_author, dict):
            raise AttemptError("author must be an identity object")
        author_extra = set(raw_author) - {"id", "harness", "model"}
        if author_extra:
            raise AttemptError(f"author has unknown fields: {', '.join(sorted(author_extra))}")
        author = {str(key): _nonempty(item, f"author.{key}") for key, item in raw_author.items()}
        if not author.get("id") or not author.get("harness") or not author.get("model"):
            raise AttemptError("author requires id, harness, and model")
        status = value.get("status")
        if status not in _STATUSES:
            raise AttemptError(f"status must be one of {sorted(_STATUSES)!r}")
        plan = _validate_complete_plan(value.get("plan"))
        raw_questions = value.get("questions")
        if not isinstance(raw_questions, list):
            raise AttemptError("questions must be a list")
        questions: list[dict[str, Any]] = []
        seen_questions: set[str] = set()
        for raw in raw_questions:
            if not isinstance(raw, dict):
                raise AttemptError("each question must be an object")
            question_extra = set(raw) - {"id", "text", "anchors"}
            if question_extra:
                raise AttemptError(
                    "question has unknown fields: " + ", ".join(sorted(question_extra))
                )
            question_id = _nonempty(raw.get("id"), "question id")
            text = _nonempty(raw.get("text"), "question text")
            anchors = _string_list(raw.get("anchors", []), "question anchors")
            if question_id in seen_questions:
                raise AttemptError(f"duplicate question id {question_id!r}")
            seen_questions.add(question_id)
            questions.append({"id": question_id, "text": text, "anchors": list(anchors)})
        if status == "NEEDS_INPUT" and not questions:
            raise AttemptError("NEEDS_INPUT requires at least one typed question")
        if status == "PLAN_READY" and questions:
            raise AttemptError("PLAN_READY cannot contain unresolved questions")
        incorporated = _string_list(
            value.get("incorporated_feedback_ids", []), "incorporated feedback ids"
        )
        body = {
            "schema": ENVELOPE_SCHEMA,
            "attempt_id": attempt_id,
            "version": version,
            "parent_digest": parent,
            "base_sha": base_sha,
            "route_digest": route_digest,
            "author": author,
            "status": status,
            "plan": plan,
            "questions": questions,
            "incorporated_feedback_ids": list(incorporated),
        }
        digest = canonical_digest(body)
        supplied_digest = value.get("digest")
        if supplied_digest is not None and supplied_digest != digest:
            raise AttemptError("plan envelope digest does not match its canonical content")
        return cls(
            attempt_id=attempt_id,
            version=version,
            parent_digest=parent,
            base_sha=base_sha,
            route_digest=route_digest,
            author=author,
            status=status,
            plan=plan,
            questions=tuple(questions),
            incorporated_feedback_ids=incorporated,
            digest=digest,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "attempt_id": self.attempt_id,
            "version": self.version,
            "parent_digest": self.parent_digest,
            "base_sha": self.base_sha,
            "route_digest": self.route_digest,
            "author": dict(self.author),
            "status": self.status,
            "plan": json.loads(json.dumps(self.plan)),
            "questions": [dict(item) for item in self.questions],
            "incorporated_feedback_ids": list(self.incorporated_feedback_ids),
            "digest": self.digest,
        }


def approval_plan_bytes(envelope: PlanEnvelope) -> bytes:
    """Render the only plan-file bytes that may cross the native approval gate."""
    plan = envelope.plan
    lines = [
        f"# Complete plan v{envelope.version}",
        "",
        f"Plan digest: `{envelope.digest}`",
        f"Base SHA: `{envelope.base_sha}`",
        f"Route digest: `{envelope.route_digest}`",
        f"Author: `{envelope.author['id']}`",
        f"Lane: `{plan['lane']}`",
        "",
        "## Motivation",
        "",
        str(plan["motivation"]),
        "",
        "## Goal",
        "",
        str(plan["goal"]),
    ]

    def add_list(title: str, values: Iterable[object]) -> None:
        lines.extend(["", f"## {title}", ""])
        rendered = list(values)
        lines.extend("- " + str(value).replace("\n", "\n  ") for value in rendered)
        if not rendered:
            lines.append("- None declared")

    scenarios = [f"Before: {item['before']} → After: {item['after']}" for item in plan["scenarios"]]
    add_list("Before and after", scenarios)
    add_list("Architecture", plan["architecture"])
    add_list("Decisions", plan["decisions"])
    add_list("Acceptance outcomes", plan["acceptance_outcomes"])
    add_list("Implementation steps", plan["steps"])
    add_list("Files", plan["files"])
    add_list("Context paths", plan["context_paths"])
    add_list("Verification", plan["verification"])
    lines.extend(["", "## E2E recipe", "", str(plan["e2e_recipe"])])
    add_list("Compatibility", plan["compatibility"])
    lines.extend(["", "## Rollout", "", str(plan["rollout"])])
    add_list("Risks", plan["risks"])
    return ("\n".join(lines) + "\n").encode()


@dataclass(frozen=True)
class FeedbackEntry:
    id: str
    verbatim: str
    anchors: tuple[str, ...]
    owner_synthesis: str
    disposition: str
    rejection_reason: str | None = None

    @classmethod
    def create(
        cls,
        *,
        feedback_id: str,
        verbatim: str,
        anchors: list[str] | tuple[str, ...],
        owner_synthesis: str,
        disposition: str = "pending",
        rejection_reason: str | None = None,
    ) -> FeedbackEntry:
        if disposition not in _DISPOSITIONS:
            raise AttemptError(f"feedback disposition must be one of {sorted(_DISPOSITIONS)!r}")
        reason = rejection_reason.strip() if isinstance(rejection_reason, str) else None
        if disposition == "rejected" and not reason:
            raise AttemptError("rejected feedback requires a visible reason")
        if disposition != "rejected" and reason:
            raise AttemptError("only rejected feedback may carry a rejection reason")
        _nonempty(verbatim, "verbatim feedback")
        return cls(
            id=_nonempty(feedback_id, "feedback id"),
            verbatim=verbatim,
            anchors=_string_list(list(anchors), "feedback anchors"),
            owner_synthesis=owner_synthesis.strip(),
            disposition=disposition,
            rejection_reason=reason,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "verbatim": self.verbatim,
            "anchors": list(self.anchors),
            "owner_synthesis": self.owner_synthesis,
            "disposition": self.disposition,
            "rejection_reason": self.rejection_reason,
        }


def feedback_watermark(entries: Iterable[FeedbackEntry]) -> str:
    """Digest the complete visible feedback disposition ledger."""
    ordered = [item.to_mapping() for item in sorted(entries, key=lambda entry: entry.id)]
    return canonical_digest(ordered)


@dataclass(frozen=True)
class AssessorVerdict:
    assessor_id: str
    author_id: str
    plan_digest: str
    outcome: str
    findings: tuple[str, ...]
    fresh: bool
    launch_receipt_digest: str | None
    digest: str
    schema: str = field(default=VERDICT_SCHEMA, init=False)

    @classmethod
    def create(
        cls,
        *,
        assessor_id: str,
        author_id: str,
        plan_digest: str,
        outcome: str,
        findings: list[str] | tuple[str, ...],
        fresh: bool = False,
        launch_receipt_digest: str | None = None,
    ) -> AssessorVerdict:
        assessor = _nonempty(assessor_id, "assessor id")
        author = _nonempty(author_id, "author id")
        assessed_plan = _require_hex(plan_digest, "assessed plan digest", _HEX_64)
        if assessor == author:
            raise AttemptError("plan author and assessor must differ")
        if outcome not in {"pass", "fail"}:
            raise AttemptError("assessment outcome must be pass or fail")
        checked_findings = _string_list(list(findings), "assessment findings")
        receipt_digest = None
        if launch_receipt_digest is not None:
            receipt_digest = _require_hex(
                launch_receipt_digest, "assessor launch receipt digest", _HEX_64
            )
        if bool(fresh) != (receipt_digest is not None):
            raise AttemptError(
                "fresh assessment requires exactly one assessor launch receipt digest"
            )
        body = {
            "schema": VERDICT_SCHEMA,
            "assessor_id": assessor,
            "author_id": author,
            "plan_digest": assessed_plan,
            "outcome": outcome,
            "findings": list(checked_findings),
            "fresh": bool(fresh),
            "launch_receipt_digest": receipt_digest,
        }
        return cls(
            assessor_id=assessor,
            author_id=author,
            plan_digest=assessed_plan,
            outcome=outcome,
            findings=checked_findings,
            fresh=bool(fresh),
            launch_receipt_digest=receipt_digest,
            digest=canonical_digest(body),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "assessor_id": self.assessor_id,
            "author_id": self.author_id,
            "plan_digest": self.plan_digest,
            "outcome": self.outcome,
            "findings": list(self.findings),
            "fresh": self.fresh,
            "launch_receipt_digest": self.launch_receipt_digest,
            "digest": self.digest,
        }


def requires_fresh_assessor(
    *,
    owner_authored: bool = False,
    unattended: bool = False,
    hot: bool = False,
    escalated: bool = False,
) -> bool:
    """Return whether policy requires a physically fresh assessor."""
    return owner_authored or unattended or hot or escalated


@dataclass(frozen=True)
class RevalidationReceipt:
    approved_base: str
    latest_base: str
    changed_paths: tuple[str, ...] | None
    planned_paths: tuple[str, ...]
    context_paths: tuple[str, ...]
    classification: str
    digest: str
    schema: str = field(default=REVALIDATION_SCHEMA, init=False)

    @classmethod
    def create(
        cls,
        *,
        approved_base: str,
        latest_base: str,
        changed_paths: list[str] | None,
        planned_paths: list[str],
        context_paths: list[str],
    ) -> RevalidationReceipt:
        approved = _require_hex(approved_base, "approved base SHA", _HEX_40)
        latest = _require_hex(latest_base, "latest base SHA", _HEX_40)
        planned = _string_list(planned_paths, "planned paths")
        context = _string_list(context_paths, "context paths")
        changed = None if changed_paths is None else _string_list(changed_paths, "changed paths")
        if approved == latest:
            classification = "unchanged"
        elif changed is None:
            classification = "ambiguous"
        elif any(
            _paths_overlap(changed_path, reviewed_path)
            for changed_path in changed
            for reviewed_path in (*planned, *context)
        ):
            classification = "relevant"
        else:
            classification = "unrelated"
        body = {
            "schema": REVALIDATION_SCHEMA,
            "approved_base": approved,
            "latest_base": latest,
            "changed_paths": list(changed) if changed is not None else None,
            "planned_paths": list(planned),
            "context_paths": list(context),
            "classification": classification,
        }
        return cls(
            approved_base=approved,
            latest_base=latest,
            changed_paths=changed,
            planned_paths=planned,
            context_paths=context,
            classification=classification,
            digest=canonical_digest(body),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "approved_base": self.approved_base,
            "latest_base": self.latest_base,
            "changed_paths": list(self.changed_paths) if self.changed_paths is not None else None,
            "planned_paths": list(self.planned_paths),
            "context_paths": list(self.context_paths),
            "classification": self.classification,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class GateTuple:
    attempt_id: str
    plan_version: int
    plan_digest: str
    approved_base_sha: str
    route_digest: str
    planner_launch_receipt_digest: str
    feedback_watermark: str
    assessor_verdict_digest: str
    revalidation_digest: str
    digest: str
    schema: str = field(default=GATE_SCHEMA, init=False)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "attempt_id": self.attempt_id,
            "plan_version": self.plan_version,
            "plan_digest": self.plan_digest,
            "approved_base_sha": self.approved_base_sha,
            "route_digest": self.route_digest,
            "planner_launch_receipt_digest": self.planner_launch_receipt_digest,
            "feedback_watermark": self.feedback_watermark,
            "assessor_verdict_digest": self.assessor_verdict_digest,
            "revalidation_digest": self.revalidation_digest,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class ApprovalReceipt:
    attempt_id: str
    native_gate_id: str
    approved_base_sha: str
    route_digest: str
    plan_file_sha256: str
    gate: GateTuple
    digest: str
    plan_lane: str | None = None
    schema: str = field(default=APPROVAL_SCHEMA, init=False)

    def to_mapping(self) -> dict[str, Any]:
        mapping = {
            "schema": self.schema,
            "attempt_id": self.attempt_id,
            "native_gate_id": self.native_gate_id,
            "approved_base_sha": self.approved_base_sha,
            "route_digest": self.route_digest,
            "plan_file_sha256": self.plan_file_sha256,
            "gate": self.gate.to_mapping(),
            "digest": self.digest,
        }
        if self.plan_lane is not None:
            mapping["plan_lane"] = self.plan_lane
        return mapping

    def verify_plan_bytes(self, content: bytes) -> None:
        if hashlib.sha256(content).hexdigest() != self.plan_file_sha256:
            raise AttemptError("approved plan file digest does not match the native-gate receipt")


@dataclass
class PlanningAttempt:
    attempt_id: str
    base_sha: str
    route_digest: str
    owner_identity: str
    history: list[PlanEnvelope] = field(default_factory=list)
    planner_launch_receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    feedback: dict[str, FeedbackEntry] = field(default_factory=dict)
    assessment: AssessorVerdict | None = None
    assessment_launch_receipt: dict[str, Any] | None = None
    revalidation: RevalidationReceipt | None = None
    frozen: bool = False
    requires_fresh_rehydration: bool = False

    @classmethod
    def create(
        cls, *, attempt_id: str, base_sha: str, route_digest: str, owner_identity: str
    ) -> PlanningAttempt:
        return cls(
            attempt_id=_nonempty(attempt_id, "attempt id"),
            base_sha=_require_hex(base_sha, "base SHA", _HEX_40),
            route_digest=_require_hex(route_digest, "route digest", _HEX_64),
            owner_identity=_nonempty(owner_identity, "owner identity"),
        )

    @property
    def current(self) -> PlanEnvelope | None:
        if self.requires_fresh_rehydration or not self.history:
            return None
        return self.history[-1]

    def _assert_mutable(self) -> None:
        if self.frozen:
            raise AttemptError("planning attempt is frozen at the native approval gate")

    def accept(
        self,
        value: Mapping[str, Any],
        *,
        launch_receipt: Mapping[str, Any],
    ) -> PlanEnvelope:
        """CAS a complete worker result into the logical attempt."""
        self._assert_mutable()
        envelope = PlanEnvelope.from_mapping(value)
        if envelope.attempt_id != self.attempt_id:
            raise AttemptError("plan envelope attempt id does not match the active attempt")
        if envelope.base_sha != self.base_sha:
            raise AttemptError("plan envelope base SHA does not match the active attempt")
        if envelope.route_digest != self.route_digest:
            raise AttemptError("plan envelope route digest does not match the active attempt")
        planner_receipt = _verified_route_receipt(
            launch_receipt,
            route_digest=self.route_digest,
            profile="planner",
        )
        desired = planner_receipt.get("desired")
        effective = planner_receipt.get("effective")
        if (
            planner_receipt.get("activation") != "active"
            or planner_receipt.get("transport") != "cli"
            or not isinstance(desired, dict)
            or not isinstance(effective, dict)
            or desired != effective
            or desired.get("harness") != envelope.author["harness"]
            or desired.get("model") != envelope.author["model"]
        ):
            raise AttemptError("planner launch receipt does not prove the envelope's exact route")
        if any(
            existing.get("digest") == planner_receipt.get("digest")
            for existing in self.planner_launch_receipts.values()
        ):
            raise AttemptError("planner launch receipt has already been bound to another plan")
        previous = self.history[-1] if self.history else None
        expected_version = 1 if previous is None else previous.version + 1
        expected_parent = None if previous is None else previous.digest
        if envelope.version != expected_version:
            raise AttemptError(
                f"plan envelope version {envelope.version} does not match "
                f"expected {expected_version}"
            )
        if envelope.parent_digest != expected_parent:
            raise AttemptError("plan envelope parent digest failed compare-and-swap")
        unknown = set(envelope.incorporated_feedback_ids) - set(self.feedback)
        if unknown:
            raise AttemptError(
                f"plan incorporated unknown feedback ids: {', '.join(sorted(unknown))}"
            )
        for feedback_id in envelope.incorporated_feedback_ids:
            entry = self.feedback[feedback_id]
            if entry.disposition == "rejected":
                raise AttemptError(f"rejected feedback {feedback_id!r} cannot also be incorporated")
        self.history.append(envelope)
        self.planner_launch_receipts[envelope.digest] = planner_receipt
        self.requires_fresh_rehydration = False
        for feedback_id in envelope.incorporated_feedback_ids:
            entry = self.feedback[feedback_id]
            self.feedback[feedback_id] = replace(entry, disposition="incorporated")
        self.assessment = None
        self.assessment_launch_receipt = None
        self.revalidation = None
        return envelope

    def add_feedback(
        self,
        *,
        feedback_id: str,
        verbatim: str,
        anchors: list[str] | tuple[str, ...],
        owner_synthesis: str,
    ) -> FeedbackEntry:
        self._assert_mutable()
        entry = FeedbackEntry.create(
            feedback_id=feedback_id,
            verbatim=verbatim,
            anchors=anchors,
            owner_synthesis=owner_synthesis,
        )
        if entry.id in self.feedback:
            raise AttemptError(f"duplicate feedback id {entry.id!r}")
        self.feedback[entry.id] = entry
        self.assessment = None
        self.assessment_launch_receipt = None
        self.revalidation = None
        return entry

    def reject_feedback(self, feedback_id: str, reason: str) -> FeedbackEntry:
        self._assert_mutable()
        try:
            entry = self.feedback[feedback_id]
        except KeyError as exc:
            raise AttemptError(f"unknown feedback id {feedback_id!r}") from exc
        if entry.disposition == "incorporated":
            raise AttemptError(f"incorporated feedback {feedback_id!r} cannot later be rejected")
        updated = FeedbackEntry.create(
            feedback_id=entry.id,
            verbatim=entry.verbatim,
            anchors=entry.anchors,
            owner_synthesis=entry.owner_synthesis,
            disposition="rejected",
            rejection_reason=reason,
        )
        self.feedback[feedback_id] = updated
        return updated

    def assess(
        self,
        verdict: AssessorVerdict,
        *,
        require_fresh: bool = False,
        launch_receipt: Mapping[str, Any] | None = None,
    ) -> None:
        self._assert_mutable()
        current = self.current
        if current is None:
            raise AttemptError("cannot assess without a current complete plan")
        if verdict.author_id != current.author["id"]:
            raise AttemptError("assessment plan author does not match the current plan author")
        if verdict.plan_digest != current.digest:
            raise AttemptError("assessment does not match the current plan digest")
        if require_fresh and not verdict.fresh:
            raise AttemptError("assessment policy requires a fresh physical assessor")
        assessor_receipt: dict[str, Any] | None = None
        if verdict.fresh:
            if launch_receipt is None:
                raise AttemptError("fresh assessment requires its structured launch receipt")
            assessor_receipt = _verified_route_receipt(
                launch_receipt,
                route_digest=self.route_digest,
                profile="plan_assessor",
            )
            if (
                assessor_receipt.get("digest") != verdict.launch_receipt_digest
                or assessor_receipt.get("worker_id") != verdict.assessor_id
                or assessor_receipt.get("activation") != "active"
                or assessor_receipt.get("transport") != "cli"
                or assessor_receipt.get("effective") != assessor_receipt.get("desired")
                or not isinstance(assessor_receipt.get("physical_attempt"), dict)
                or assessor_receipt["physical_attempt"].get("terminal_acknowledged") is not True
                or not isinstance(assessor_receipt.get("cleanup"), dict)
                or assessor_receipt["cleanup"].get("capsule_absent") is not True
                or assessor_receipt["cleanup"].get("quarantined") is not False
            ):
                raise AttemptError("fresh assessor launch receipt does not prove independence")
        elif launch_receipt is not None:
            raise AttemptError("non-fresh assessment cannot carry a launch receipt")
        self.assessment = verdict
        self.assessment_launch_receipt = assessor_receipt

    def revalidate(self, receipt: RevalidationReceipt) -> None:
        self._assert_mutable()
        if receipt.approved_base != self.base_sha:
            raise AttemptError("revalidation does not start from the attempt base SHA")
        self.revalidation = receipt
        if receipt.classification in {"relevant", "ambiguous"}:
            self.base_sha = receipt.latest_base
            self.requires_fresh_rehydration = True
            self.assessment = None
            self.assessment_launch_receipt = None

    def gate_tuple(self) -> GateTuple:
        current = self.current
        if current is None or current.status != "PLAN_READY":
            raise AttemptError("native gate requires a current PLAN_READY envelope")
        pending = [entry.id for entry in self.feedback.values() if entry.disposition == "pending"]
        if pending:
            raise AttemptError(
                f"native gate is blocked by pending feedback: {', '.join(sorted(pending))}"
            )
        if self.assessment is None or self.assessment.outcome != "pass":
            raise AttemptError("native gate is blocked because assessment has not passed")
        if self.assessment.author_id != current.author["id"]:
            raise AttemptError("native gate assessment does not match the current plan author")
        if self.assessment.plan_digest != current.digest:
            raise AttemptError("native gate assessment does not match the current plan digest")
        planner_receipt = self.planner_launch_receipts.get(current.digest)
        if planner_receipt is None:
            raise AttemptError("native gate is missing the current planner launch receipt")
        if self.assessment.fresh and self.assessment_launch_receipt is None:
            raise AttemptError("native gate is missing fresh assessor launch provenance")
        if self.revalidation is None:
            raise AttemptError("native gate requires selective base revalidation")
        if self.revalidation.classification in {"relevant", "ambiguous"}:
            raise AttemptError("native gate is blocked by relevant or ambiguous base drift")
        body = {
            "schema": GATE_SCHEMA,
            "attempt_id": self.attempt_id,
            "plan_version": current.version,
            "plan_digest": current.digest,
            "approved_base_sha": self.base_sha,
            "route_digest": self.route_digest,
            "planner_launch_receipt_digest": planner_receipt["digest"],
            "feedback_watermark": feedback_watermark(self.feedback.values()),
            "assessor_verdict_digest": self.assessment.digest,
            "revalidation_digest": self.revalidation.digest,
        }
        return GateTuple(
            attempt_id=self.attempt_id,
            plan_version=current.version,
            plan_digest=current.digest,
            approved_base_sha=self.base_sha,
            route_digest=self.route_digest,
            planner_launch_receipt_digest=str(body["planner_launch_receipt_digest"]),
            feedback_watermark=str(body["feedback_watermark"]),
            assessor_verdict_digest=self.assessment.digest,
            revalidation_digest=self.revalidation.digest,
            digest=canonical_digest(body),
        )

    def freeze(
        self,
        *,
        native_gate_id: str,
        expected_gate_digest: str,
        plan_bytes: bytes,
    ) -> ApprovalReceipt:
        self._assert_mutable()
        gate = self.gate_tuple()
        expected = _require_hex(expected_gate_digest, "expected pre-gate digest", _HEX_64)
        if gate.digest != expected:
            raise AttemptError(
                "native approval does not match the exact pre-gate digest; review again"
            )
        current = self.current
        if current is None:
            raise AttemptError("native gate lost its current plan")
        expected_plan_bytes = approval_plan_bytes(current)
        if plan_bytes != expected_plan_bytes:
            raise AttemptError(
                "approved plan file is not the canonical rendering of the reviewed envelope"
            )
        plan_lane = current.plan["lane"]
        body = {
            "schema": APPROVAL_SCHEMA,
            "attempt_id": self.attempt_id,
            "native_gate_id": _nonempty(native_gate_id, "native gate id"),
            "approved_base_sha": gate.approved_base_sha,
            "route_digest": gate.route_digest,
            "plan_file_sha256": hashlib.sha256(plan_bytes).hexdigest(),
            "gate": gate.to_mapping(),
            "plan_lane": plan_lane,
        }
        receipt = ApprovalReceipt(
            attempt_id=self.attempt_id,
            native_gate_id=body["native_gate_id"],
            approved_base_sha=gate.approved_base_sha,
            route_digest=gate.route_digest,
            plan_file_sha256=body["plan_file_sha256"],
            gate=gate,
            plan_lane=plan_lane,
            digest=canonical_digest(body),
        )
        self.frozen = True
        return receipt

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": ATTEMPT_SCHEMA,
            "attempt_id": self.attempt_id,
            "base_sha": self.base_sha,
            "route_digest": self.route_digest,
            "owner_identity": self.owner_identity,
            "history": [item.to_mapping() for item in self.history],
            "planner_launch_receipts": dict(sorted(self.planner_launch_receipts.items())),
            "feedback": [
                item.to_mapping()
                for item in sorted(self.feedback.values(), key=lambda entry: entry.id)
            ],
            "assessment": self.assessment.to_mapping() if self.assessment else None,
            "assessment_launch_receipt": self.assessment_launch_receipt,
            "revalidation": self.revalidation.to_mapping() if self.revalidation else None,
            "frozen": self.frozen,
            "requires_fresh_rehydration": self.requires_fresh_rehydration,
        }

    def save_bundle(self, directory: Path) -> None:
        """Persist review state without any physical worker-session receipt."""
        path = directory.expanduser().resolve() / "attempt.json"
        atomic_write_text(path, json.dumps(self.to_mapping(), indent=2, sort_keys=True) + "\n")

    @classmethod
    def load_bundle(cls, directory: Path) -> PlanningAttempt:  # noqa: C901
        path = directory.expanduser().resolve() / "attempt.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AttemptError(f"cannot read planning attempt bundle {path}: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema") != ATTEMPT_SCHEMA:
            raise AttemptError("unsupported planning attempt bundle")
        attempt = cls.create(
            attempt_id=raw.get("attempt_id"),
            base_sha=raw.get("base_sha"),
            route_digest=raw.get("route_digest"),
            owner_identity=raw.get("owner_identity"),
        )
        attempt.requires_fresh_rehydration = bool(raw.get("requires_fresh_rehydration"))
        raw_feedback = raw.get("feedback", [])
        if not isinstance(raw_feedback, list):
            raise AttemptError("planning bundle feedback must be a list")
        for item in raw_feedback:
            if not isinstance(item, dict):
                raise AttemptError("planning bundle feedback entries must be objects")
            entry = FeedbackEntry.create(
                feedback_id=item.get("id"),
                verbatim=item.get("verbatim"),
                anchors=item.get("anchors", []),
                owner_synthesis=item.get("owner_synthesis", ""),
                disposition=item.get("disposition", "pending"),
                rejection_reason=item.get("rejection_reason"),
            )
            attempt.feedback[entry.id] = entry
        previous: PlanEnvelope | None = None
        raw_history = raw.get("history", [])
        raw_planner_receipts = raw.get("planner_launch_receipts", {})
        if not isinstance(raw_history, list):
            raise AttemptError("planning bundle history must be a list")
        if not isinstance(raw_planner_receipts, dict):
            raise AttemptError("planning bundle planner receipts must be an object")
        for item in raw_history:
            if not isinstance(item, dict):
                raise AttemptError("planning bundle history entries must be objects")
            envelope = PlanEnvelope.from_mapping(item)
            expected_version = 1 if previous is None else previous.version + 1
            expected_parent = None if previous is None else previous.digest
            if envelope.attempt_id != attempt.attempt_id:
                raise AttemptError("bundle history has a mismatched attempt id")
            if envelope.route_digest != attempt.route_digest:
                raise AttemptError("bundle history has a mismatched route digest")
            if envelope.version != expected_version or envelope.parent_digest != expected_parent:
                raise AttemptError("bundle history failed version and parent CAS validation")
            unknown = set(envelope.incorporated_feedback_ids) - set(attempt.feedback)
            if unknown:
                raise AttemptError("bundle history incorporates unknown feedback ids")
            raw_receipt = raw_planner_receipts.get(envelope.digest)
            if not isinstance(raw_receipt, dict):
                raise AttemptError("bundle history is missing a planner launch receipt")
            planner_receipt = _verified_route_receipt(
                raw_receipt,
                route_digest=attempt.route_digest,
                profile="planner",
            )
            desired = planner_receipt.get("desired")
            if (
                planner_receipt.get("activation") != "active"
                or planner_receipt.get("transport") != "cli"
                or not isinstance(desired, dict)
                or planner_receipt.get("effective") != desired
                or desired.get("harness") != envelope.author["harness"]
                or desired.get("model") != envelope.author["model"]
            ):
                raise AttemptError("bundle planner receipt does not prove the envelope route")
            attempt.history.append(envelope)
            attempt.planner_launch_receipts[envelope.digest] = planner_receipt
            previous = envelope
        if set(raw_planner_receipts) != set(attempt.planner_launch_receipts):
            raise AttemptError("planning bundle contains an unbound planner launch receipt")
        if (
            previous is not None
            and not attempt.requires_fresh_rehydration
            and previous.base_sha != attempt.base_sha
        ):
            raise AttemptError("bundle current plan does not match the attempt base SHA")
        raw_assessment = raw.get("assessment")
        if isinstance(raw_assessment, dict):
            raw_assessor_receipt = raw.get("assessment_launch_receipt")
            attempt.assess(
                _verdict_from_mapping(raw_assessment),
                launch_receipt=(
                    raw_assessor_receipt if isinstance(raw_assessor_receipt, dict) else None
                ),
            )
        elif raw.get("assessment_launch_receipt") is not None:
            raise AttemptError("planning bundle has assessor provenance without an assessment")
        raw_revalidation = raw.get("revalidation")
        if isinstance(raw_revalidation, dict):
            attempt.revalidation = _revalidation_from_mapping(raw_revalidation)
        attempt.frozen = bool(raw.get("frozen"))
        return attempt


def _attempt_lock_path(directory: Path) -> Path:
    return directory.expanduser().resolve() / "attempt.lock"


def mutate_bundle(
    directory: Path,
    operation: Callable[[PlanningAttempt], Any],
) -> Any:
    """Serialize one complete load, compare-and-swap mutation, and save."""
    resolved = directory.expanduser().resolve()
    with _locking.flock_blocking(_attempt_lock_path(resolved)):
        attempt = PlanningAttempt.load_bundle(resolved)
        result = operation(attempt)
        attempt.save_bundle(resolved)
        return result


def write_approval_receipt(path: Path, receipt: ApprovalReceipt) -> None:
    atomic_write_text(
        path.expanduser().resolve(),
        json.dumps(receipt.to_mapping(), indent=2, sort_keys=True) + "\n",
    )


def _verdict_from_mapping(value: Mapping[str, Any]) -> AssessorVerdict:
    verdict = AssessorVerdict.create(
        assessor_id=str(value.get("assessor_id", "")),
        author_id=str(value.get("author_id", "")),
        plan_digest=str(value.get("plan_digest", "")),
        outcome=str(value.get("outcome", "")),
        findings=value.get("findings", []),
        fresh=bool(value.get("fresh")),
        launch_receipt_digest=value.get("launch_receipt_digest"),
    )
    if value.get("schema") not in {None, VERDICT_SCHEMA}:
        raise AttemptError("unsupported assessor verdict schema")
    if value.get("digest") not in {None, verdict.digest}:
        raise AttemptError("assessor verdict digest does not match its canonical content")
    return verdict


def _revalidation_from_mapping(value: Mapping[str, Any]) -> RevalidationReceipt:
    receipt = RevalidationReceipt.create(
        approved_base=str(value.get("approved_base", "")),
        latest_base=str(value.get("latest_base", "")),
        changed_paths=value.get("changed_paths"),
        planned_paths=value.get("planned_paths", []),
        context_paths=value.get("context_paths", []),
    )
    if value.get("schema") not in {None, REVALIDATION_SCHEMA}:
        raise AttemptError("unsupported revalidation receipt schema")
    if value.get("classification") not in {None, receipt.classification}:
        raise AttemptError("revalidation classification does not match its paths")
    if value.get("digest") not in {None, receipt.digest}:
        raise AttemptError("revalidation digest does not match its canonical content")
    return receipt


def _gate_from_mapping(value: Mapping[str, Any]) -> GateTuple:
    _verify_digest(value, GATE_SCHEMA)
    raw_version = value.get("plan_version")
    if not isinstance(raw_version, int) or isinstance(raw_version, bool) or raw_version < 1:
        raise AttemptError("gate plan version must be a positive integer")
    return GateTuple(
        attempt_id=_nonempty(value.get("attempt_id"), "attempt id"),
        plan_version=raw_version,
        plan_digest=_require_hex(value.get("plan_digest"), "plan digest", _HEX_64),
        approved_base_sha=_require_hex(
            value.get("approved_base_sha"), "approved base SHA", _HEX_40
        ),
        route_digest=_require_hex(value.get("route_digest"), "route digest", _HEX_64),
        planner_launch_receipt_digest=_require_hex(
            value.get("planner_launch_receipt_digest"),
            "planner launch receipt digest",
            _HEX_64,
        ),
        feedback_watermark=_require_hex(
            value.get("feedback_watermark"), "feedback watermark", _HEX_64
        ),
        assessor_verdict_digest=_require_hex(
            value.get("assessor_verdict_digest"), "assessor verdict digest", _HEX_64
        ),
        revalidation_digest=_require_hex(
            value.get("revalidation_digest"), "revalidation digest", _HEX_64
        ),
        digest=str(value["digest"]),
    )


def load_approval_receipt(path: Path) -> ApprovalReceipt:
    """Load and recursively digest-check an exact native-gate receipt."""
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AttemptError(f"cannot read approval receipt {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AttemptError("approval receipt must be a JSON object")
    _verify_digest(value, APPROVAL_SCHEMA)
    raw_gate = value.get("gate")
    if not isinstance(raw_gate, dict):
        raise AttemptError("approval receipt requires an exact gate tuple")
    gate = _gate_from_mapping(raw_gate)
    plan_lane: str | None = None
    if "plan_lane" in value:
        raw_plan_lane = value["plan_lane"]
        if raw_plan_lane is None:
            raise AttemptError(
                "approval receipt plan_lane must not be null; omit the field for a legacy receipt"
            )
        if not isinstance(raw_plan_lane, str) or raw_plan_lane not in _PLAN_LANES:
            raise AttemptError(f"approval receipt plan_lane must be one of {sorted(_PLAN_LANES)!r}")
        plan_lane = raw_plan_lane
    receipt = ApprovalReceipt(
        attempt_id=_nonempty(value.get("attempt_id"), "attempt id"),
        native_gate_id=_nonempty(value.get("native_gate_id"), "native gate id"),
        approved_base_sha=_require_hex(
            value.get("approved_base_sha"), "approved base SHA", _HEX_40
        ),
        route_digest=_require_hex(value.get("route_digest"), "route digest", _HEX_64),
        plan_file_sha256=_require_hex(value.get("plan_file_sha256"), "plan file digest", _HEX_64),
        gate=gate,
        plan_lane=plan_lane,
        digest=str(value["digest"]),
    )
    if receipt.attempt_id != gate.attempt_id:
        raise AttemptError("approval receipt attempt does not match its gate tuple")
    if receipt.approved_base_sha != gate.approved_base_sha:
        raise AttemptError("approval receipt base does not match its gate tuple")
    if receipt.route_digest != gate.route_digest:
        raise AttemptError("approval receipt route does not match its gate tuple")
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage typed pre-approval planning artifacts.")
    sub = parser.add_subparsers(dest="operation", required=True)

    schema = sub.add_parser("schema")
    schema.add_argument("--output")

    create = sub.add_parser("create")
    create.add_argument("--attempt-dir", required=True)
    create.add_argument("--attempt-id", required=True)
    create.add_argument("--base-sha", required=True)
    create.add_argument("--route-digest", required=True)
    create.add_argument("--owner-identity", required=True)

    accept = sub.add_parser("accept")
    accept.add_argument("--attempt-dir", required=True)
    accept.add_argument("--envelope-from", required=True)
    accept.add_argument("--route-receipt", required=True)

    feedback = sub.add_parser("feedback")
    feedback.add_argument("--attempt-dir", required=True)
    feedback.add_argument("--feedback-from", required=True)

    reject = sub.add_parser("reject-feedback")
    reject.add_argument("--attempt-dir", required=True)
    reject.add_argument("--feedback-id", required=True)
    reject.add_argument("--reason", required=True)

    assess = sub.add_parser("assess")
    assess.add_argument("--attempt-dir", required=True)
    assess.add_argument("--verdict-from", required=True)
    assess.add_argument("--launch-receipt")
    assess.add_argument("--require-fresh", action="store_true")

    revalidate = sub.add_parser("revalidate")
    revalidate.add_argument("--attempt-dir", required=True)
    revalidate.add_argument("--receipt-from", required=True)

    show = sub.add_parser("show")
    show.add_argument("--attempt-dir", required=True)

    gate = sub.add_parser("gate")
    gate.add_argument("--attempt-dir", required=True)

    render = sub.add_parser("render-plan")
    render.add_argument("--attempt-dir", required=True)
    render.add_argument("--output", required=True)

    approve = sub.add_parser("approve")
    approve.add_argument("--attempt-dir", required=True)
    approve.add_argument("--native-gate-id", required=True)
    approve.add_argument("--expected-gate-digest", required=True)
    approve.add_argument("--plan-from", required=True)
    approve.add_argument("--output", required=True)

    verify = sub.add_parser("verify-approval")
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--plan-from", required=True)
    return parser


def cli_main(argv: list[str]) -> int:  # noqa: C901
    args = _parser().parse_args(argv)
    try:
        if args.operation == "schema":
            result = envelope_json_schema()
            if args.output:
                atomic_write_text(
                    Path(args.output).expanduser().resolve(),
                    json.dumps(result, indent=2, sort_keys=True) + "\n",
                )
        elif args.operation == "create":
            directory = Path(args.attempt_dir).expanduser().resolve()
            with _locking.flock_blocking(_attempt_lock_path(directory)):
                attempt = PlanningAttempt.create(
                    attempt_id=args.attempt_id,
                    base_sha=args.base_sha,
                    route_digest=args.route_digest,
                    owner_identity=args.owner_identity,
                )
                attempt.save_bundle(directory)
                result: object = attempt.to_mapping()
        elif args.operation == "verify-approval":
            receipt = load_approval_receipt(Path(args.receipt))
            receipt.verify_plan_bytes(Path(args.plan_from).read_bytes())
            result = receipt.to_mapping()
        else:
            directory = Path(args.attempt_dir).expanduser().resolve()
            with _locking.flock_blocking(_attempt_lock_path(directory)):
                attempt = PlanningAttempt.load_bundle(directory)
                if args.operation == "accept":
                    envelope_value = json.loads(
                        Path(args.envelope_from).read_text(encoding="utf-8")
                    )
                    if not isinstance(envelope_value, dict):
                        raise AttemptError("envelope input must be a JSON object")
                    launch_receipt = json.loads(
                        Path(args.route_receipt).read_text(encoding="utf-8")
                    )
                    if not isinstance(launch_receipt, dict):
                        raise AttemptError("planner route receipt must be a JSON object")
                    envelope = attempt.accept(envelope_value, launch_receipt=launch_receipt)
                    attempt.save_bundle(directory)
                    result = envelope.to_mapping()
                elif args.operation == "feedback":
                    value = json.loads(Path(args.feedback_from).read_text(encoding="utf-8"))
                    if isinstance(value, dict):
                        entry = attempt.add_feedback(
                            feedback_id=value.get("id"),
                            verbatim=value.get("verbatim"),
                            anchors=value.get("anchors", []),
                            owner_synthesis=value.get("owner_synthesis", ""),
                        )
                        attempt.save_bundle(directory)
                        result = entry.to_mapping()
                    elif isinstance(value, list):
                        entries = []
                        for item in value:
                            if not isinstance(item, dict):
                                raise AttemptError("feedback array elements must be JSON objects")
                            entries.append(
                                attempt.add_feedback(
                                    feedback_id=item.get("id"),
                                    verbatim=item.get("verbatim"),
                                    anchors=item.get("anchors", []),
                                    owner_synthesis=item.get("owner_synthesis", ""),
                                )
                            )
                        attempt.save_bundle(directory)
                        result = [entry.to_mapping() for entry in entries]
                    else:
                        raise AttemptError(
                            "feedback input must be a JSON object or an array of JSON objects"
                        )
                elif args.operation == "reject-feedback":
                    entry = attempt.reject_feedback(args.feedback_id, args.reason)
                    attempt.save_bundle(directory)
                    result = entry.to_mapping()
                elif args.operation == "assess":
                    value = json.loads(Path(args.verdict_from).read_text(encoding="utf-8"))
                    if not isinstance(value, dict):
                        raise AttemptError("verdict input must be a JSON object")
                    verdict = _verdict_from_mapping(value)
                    launch_receipt = (
                        json.loads(Path(args.launch_receipt).read_text(encoding="utf-8"))
                        if args.launch_receipt
                        else None
                    )
                    if launch_receipt is not None and not isinstance(launch_receipt, dict):
                        raise AttemptError("assessor launch receipt must be a JSON object")
                    attempt.assess(
                        verdict,
                        require_fresh=args.require_fresh,
                        launch_receipt=launch_receipt,
                    )
                    attempt.save_bundle(directory)
                    result = verdict.to_mapping()
                elif args.operation == "revalidate":
                    value = json.loads(Path(args.receipt_from).read_text(encoding="utf-8"))
                    if not isinstance(value, dict):
                        raise AttemptError("revalidation input must be a JSON object")
                    revalidation = _revalidation_from_mapping(value)
                    attempt.revalidate(revalidation)
                    attempt.save_bundle(directory)
                    result = revalidation.to_mapping()
                elif args.operation == "gate":
                    result = attempt.gate_tuple().to_mapping()
                elif args.operation == "render-plan":
                    current = attempt.current
                    if current is None:
                        raise AttemptError("cannot render without a current complete plan")
                    atomic_write_bytes(
                        Path(args.output).expanduser().resolve(), approval_plan_bytes(current)
                    )
                    result = {"output": str(Path(args.output).expanduser().resolve())}
                elif args.operation == "approve":
                    plan_bytes = Path(args.plan_from).read_bytes()
                    receipt = attempt.freeze(
                        native_gate_id=args.native_gate_id,
                        expected_gate_digest=args.expected_gate_digest,
                        plan_bytes=plan_bytes,
                    )
                    attempt.save_bundle(directory)
                    write_approval_receipt(Path(args.output), receipt)
                    result = receipt.to_mapping()
                else:
                    result = attempt.to_mapping()
    except (AttemptError, OSError) as exc:
        sys.stderr.write(f"planning-attempt: {exc}\n")
        return 2
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"planning-attempt: malformed JSON input: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "APPROVAL_SCHEMA",
    "ApprovalReceipt",
    "AssessorVerdict",
    "AttemptError",
    "FeedbackEntry",
    "GateTuple",
    "PlanEnvelope",
    "PlanningAttempt",
    "RevalidationReceipt",
    "approval_plan_bytes",
    "canonical_digest",
    "envelope_json_schema",
    "feedback_watermark",
    "load_approval_receipt",
    "mutate_bundle",
    "requires_fresh_assessor",
    "write_approval_receipt",
]
