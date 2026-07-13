"""Deterministic model and renderer for bare ``FLOW``.

The harness gathers evidence through the existing run, tracker, mutation, and
forge seams.  This module owns the stable join and presentation vocabulary, so
Claude Code and Codex produce the same priorities and logical next commands.
It is intentionally side-effect free.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class DeferredItem:
    target: str
    question: str
    state: str


@dataclass(frozen=True)
class FeedbackItem:
    target: str
    pr: str
    actionable_count: int


@dataclass(frozen=True)
class PendingMutation:
    target: str
    operation: str


@dataclass(frozen=True)
class MaintenanceNotice:
    label: str
    detail: str
    next_command: str


@dataclass(frozen=True)
class CockpitInput:
    runs: tuple[Mapping[str, object], ...] = ()
    deferred: tuple[DeferredItem, ...] = ()
    pending: tuple[PendingMutation, ...] = ()
    feedback: tuple[FeedbackItem, ...] = ()
    maintenance: tuple[MaintenanceNotice, ...] = ()


@dataclass(frozen=True)
class CockpitItem:
    target: str
    state: str
    detail: str
    next_command: str


@dataclass(frozen=True)
class CockpitSnapshot:
    attention: tuple[CockpitItem, ...]
    active: tuple[CockpitItem, ...]
    pending_mutations: int
    pending_targets: tuple[str, ...]
    maintenance: tuple[MaintenanceNotice, ...]
    next_commands: tuple[str, ...]


_UNHEALTHY_LEASES = frozenset({"corrupt", "expired", "stale", "stale_foreign"})


def _run_item(run: Mapping[str, object]) -> tuple[CockpitItem, bool]:
    target = str(run.get("ticket", ""))
    state = str(run.get("next_or_blocked", "unknown"))
    lease = str(run.get("lease", "unknown"))
    completed = run.get("completed", "?")
    total = run.get("total_stages", "?")
    unhealthy = state.endswith(":failed") or lease in _UNHEALTHY_LEASES
    command = f"FLOW workspace repair {target}" if unhealthy else f"FLOW {target}"
    detail = f"{completed}/{total} stages; next {state}; lease {lease}"
    kind = "stuck" if unhealthy else "active"
    return CockpitItem(target, kind, detail, command), unhealthy


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def build_cockpit(evidence: CockpitInput) -> CockpitSnapshot:
    """Join normalized evidence into a stable attention-first snapshot."""

    attention: list[CockpitItem] = []
    active: list[CockpitItem] = []
    for run in sorted(evidence.runs, key=lambda item: str(item.get("ticket", ""))):
        item, unhealthy = _run_item(run)
        (attention if unhealthy else active).append(item)

    attention.extend(
        CockpitItem(
            deferred.target,
            deferred.state,
            deferred.question,
            f'FLOW {deferred.target} --request "<answer>"',
        )
        for deferred in sorted(evidence.deferred, key=lambda item: item.target)
    )

    for feedback in sorted(evidence.feedback, key=lambda item: item.target):
        count = feedback.actionable_count
        noun = "thread" if count == 1 else "threads"
        attention.append(
            CockpitItem(
                feedback.target,
                "feedback",
                f"PR {feedback.pr}: {count} actionable {noun}",
                f"FLOW pr:{feedback.pr}",
            )
        )

    pending_targets = tuple(sorted({item.target for item in evidence.pending}))
    commands = [item.next_command for item in attention]
    if evidence.pending:
        commands.append("FLOW workspace sync")
    commands.extend(notice.next_command for notice in evidence.maintenance)
    commands.extend(item.next_command for item in active)
    if not commands:
        commands.append("FLOW help")

    return CockpitSnapshot(
        attention=tuple(attention),
        active=tuple(active),
        pending_mutations=len(evidence.pending),
        pending_targets=pending_targets,
        maintenance=tuple(evidence.maintenance),
        next_commands=_unique(commands),
    )


def _render_section(title: str, items: tuple[CockpitItem, ...]) -> list[str]:
    if not items:
        return []
    lines = [title]
    lines.extend(f"- {item.target} [{item.state}] — {item.detail}" for item in items)
    return lines


def render_cockpit(snapshot: CockpitSnapshot) -> str:
    """Render a compact, harness-neutral cockpit using logical ``FLOW``."""

    lines: list[str] = []
    lines.extend(_render_section("Needs attention", snapshot.attention))
    lines.extend(_render_section("Active", snapshot.active))
    if snapshot.pending_mutations:
        targets = ", ".join(snapshot.pending_targets)
        lines.extend(
            [
                "Pending tracker writes",
                f"- {snapshot.pending_mutations} queued mutation(s) across {targets}",
            ]
        )
    if snapshot.maintenance:
        lines.append("Maintainer health")
        lines.extend(f"- {notice.label} — {notice.detail}" for notice in snapshot.maintenance)
    if not lines:
        lines.append("No active Flow work.")
    lines.append("Next")
    lines.extend(f"- {command}" for command in snapshot.next_commands)
    return "\n".join(lines)


__all__ = [
    "CockpitInput",
    "CockpitItem",
    "CockpitSnapshot",
    "DeferredItem",
    "FeedbackItem",
    "MaintenanceNotice",
    "PendingMutation",
    "build_cockpit",
    "render_cockpit",
]
