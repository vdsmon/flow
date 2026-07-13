"""Pure state-aware target lifecycle reducer.

The reducer deliberately consumes normalized evidence rather than reading tracker,
run, lease, snapshot, or forge state itself.  Callers own probing and normalization;
this module owns the one deterministic precedence table shared by every harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

TicketState = Literal["open", "deferred", "blocked", "done", "cancelled"]
RunState = Literal["none", "healthy", "failed", "stale", "drifted", "corrupt", "completed"]
LeaseState = Literal["free", "owned", "live_foreign", "stale_foreign"]
PrState = Literal["none", "open", "merged", "closed"]


class LifecycleAction(StrEnum):
    """Closed vocabulary returned for every known, valid target."""

    START = "start"
    ANSWER = "answer"
    RESUME = "resume"
    RUNNING = "running"
    REPAIR = "repair"
    REVISE = "revise"
    SHOW = "show"
    CONFLICT = "conflict"


class MultiTargetChoice(StrEnum):
    """Explicit answer to the attended multi-target coordination question."""

    SEQUENTIAL = "sequential"
    TOGETHER = "together"


class MultiTargetDisposition(StrEnum):
    """Closed coordination result layered above per-target lifecycle actions."""

    DIRECT = "direct"
    NEEDS_CHOICE = "needs_choice"
    SEQUENTIAL = "sequential"
    TOGETHER = "together"


class LifecycleError(ValueError):
    """Base class for invalid requests that are not lifecycle actions."""


class UnknownTargetError(LifecycleError):
    """The tracker/forge probe could not resolve the requested target."""


class InvalidLifecycleRequest(LifecycleError):
    """The requested option cannot be applied to the normalized state."""


@dataclass(frozen=True)
class LifecycleEvidence:
    """Normalized read-only evidence for one logical target.

    ``contradictions`` carries conflicts found while joining sources.  A corrupt
    ship event is separate because it has a specific stop rule: Flow must never
    guess past it or offer a generic repair that might destroy evidence.
    """

    target_exists: bool
    ticket_state: TicketState
    run_state: RunState
    lease_state: LeaseState
    pr_state: PrState
    request: bool = False
    scope_approved: bool = False
    stored_question: bool = False
    actionable_feedback: bool = False
    ship_event_corrupt: bool = False
    contradictions: tuple[str, ...] = ()


@dataclass(frozen=True)
class GroupTargetEvidence:
    """Groupability facts for one target, gathered without mutation."""

    key: str
    live: bool
    epic: bool


@dataclass(frozen=True)
class GroupabilityEvidence:
    """Evidence required before multiple fresh targets may share one run."""

    targets: tuple[GroupTargetEvidence, ...]
    coupling_verified: bool


_BROKEN_RUNS = frozenset({"failed", "stale", "drifted", "corrupt"})
_TERMINAL_TICKETS = frozenset({"done", "cancelled"})


def _is_contradictory(evidence: LifecycleEvidence) -> bool:
    if evidence.contradictions or evidence.ship_event_corrupt:
        return True
    if evidence.actionable_feedback and evidence.pr_state != "open":
        return True
    if evidence.ticket_state in {"deferred", "blocked"} and not evidence.stored_question:
        return True
    return evidence.pr_state in {"merged", "closed"} and evidence.run_state == "healthy"


def _request_targets_terminal_delivery(evidence: LifecycleEvidence) -> bool:
    if not evidence.request or evidence.pr_state == "open":
        return False
    return (
        evidence.pr_state in {"merged", "closed"}
        or evidence.run_state == "completed"
        or evidence.ticket_state in _TERMINAL_TICKETS
    )


def reduce_lifecycle(evidence: LifecycleEvidence) -> LifecycleAction:
    """Reduce normalized evidence to the single safe next lifecycle action.

    Unknown targets and invalid option/state combinations are input errors, not
    lifecycle actions. Contradictory evidence is different: it is a known target
    whose sources cannot be reconciled, so ``conflict`` preserves those sources.
    """

    if not evidence.target_exists:
        raise UnknownTargetError("target does not exist")

    if _is_contradictory(evidence):
        return LifecycleAction.CONFLICT

    if _request_targets_terminal_delivery(evidence):
        raise InvalidLifecycleRequest("--request cannot change a terminal delivery")

    # A deferred decision is intentionally checked before run recovery: the
    # stored human question, not pipeline machinery, is the current blocker.
    if evidence.ticket_state in {"deferred", "blocked"}:
        return LifecycleAction.ANSWER

    if evidence.run_state == "healthy" and evidence.request and evidence.scope_approved:
        raise InvalidLifecycleRequest("--request cannot change an approved scope")

    # A live foreign owner always wins over recovery. Observers may report the
    # holder, but they must not race it or offer a takeover as though it were stale.
    if evidence.lease_state == "live_foreign":
        return LifecycleAction.RUNNING

    if evidence.run_state == "healthy":
        if evidence.lease_state == "stale_foreign":
            return LifecycleAction.REPAIR
        return LifecycleAction.RESUME

    if evidence.run_state in _BROKEN_RUNS or evidence.lease_state == "stale_foreign":
        return LifecycleAction.REPAIR

    if evidence.pr_state == "open":
        if evidence.actionable_feedback or evidence.request:
            return LifecycleAction.REVISE
        return LifecycleAction.SHOW

    if evidence.pr_state in {"merged", "closed"}:
        return LifecycleAction.SHOW

    if evidence.run_state == "completed" or evidence.ticket_state in _TERMINAL_TICKETS:
        return LifecycleAction.SHOW

    if (
        evidence.ticket_state == "open"
        and evidence.run_state == "none"
        and evidence.lease_state == "free"
        and evidence.pr_state == "none"
    ):
        return LifecycleAction.START

    # No safe interpretation matched. Returning conflict is deliberately more
    # conservative than inventing a repair or starting duplicate work.
    return LifecycleAction.CONFLICT


def _validate_groupability(
    actions: list[LifecycleAction] | tuple[LifecycleAction, ...],
    groupability: GroupabilityEvidence | None,
) -> None:
    if groupability is None:
        raise InvalidLifecycleRequest("running together requires groupability evidence")
    if len(groupability.targets) != len(actions):
        raise InvalidLifecycleRequest("groupability target count must match lifecycle action count")
    keys = [target.key for target in groupability.targets]
    if not all(keys) or len(keys) != len(set(keys)):
        raise InvalidLifecycleRequest("running together requires distinct target keys")
    if any(not target.live for target in groupability.targets):
        raise InvalidLifecycleRequest("running together requires every target to be live")
    if any(target.epic for target in groupability.targets):
        raise InvalidLifecycleRequest("epic targets cannot run together")
    if not groupability.coupling_verified:
        raise InvalidLifecycleRequest("running together requires verified coupling")


def coordinate_targets(
    actions: list[LifecycleAction] | tuple[LifecycleAction, ...],
    *,
    together: bool = False,
    unattended: bool = False,
    choice: MultiTargetChoice | None = None,
    groupability: GroupabilityEvidence | None = None,
) -> MultiTargetDisposition:
    """Choose how already-reduced target actions may be coordinated safely."""

    if not actions:
        raise InvalidLifecycleRequest("target coordination requires at least one target")
    if len(actions) == 1:
        if together or choice is not None or groupability is not None:
            raise InvalidLifecycleRequest("one target runs directly; coordination mode is invalid")
        return MultiTargetDisposition.DIRECT
    if together and choice is not None:
        raise InvalidLifecycleRequest("--together conflicts with an attended coordination choice")
    if unattended and choice is not None:
        raise InvalidLifecycleRequest("unattended mode cannot consume an attended choice")
    if groupability is not None and not (together or choice is MultiTargetChoice.TOGETHER):
        raise InvalidLifecycleRequest("groupability evidence is valid only for together delivery")

    run_together = together or choice is MultiTargetChoice.TOGETHER
    if run_together:
        if any(action is not LifecycleAction.START for action in actions):
            raise InvalidLifecycleRequest(
                "running together requires every target action to be start"
            )
        _validate_groupability(actions, groupability)
        return MultiTargetDisposition.TOGETHER

    if unattended:
        raise InvalidLifecycleRequest(
            "unattended multiple-target delivery requires explicit --together"
        )
    if choice is MultiTargetChoice.SEQUENTIAL:
        return MultiTargetDisposition.SEQUENTIAL
    return MultiTargetDisposition.NEEDS_CHOICE


__all__ = [
    "GroupTargetEvidence",
    "GroupabilityEvidence",
    "InvalidLifecycleRequest",
    "LeaseState",
    "LifecycleAction",
    "LifecycleError",
    "LifecycleEvidence",
    "MultiTargetChoice",
    "MultiTargetDisposition",
    "PrState",
    "RunState",
    "TicketState",
    "UnknownTargetError",
    "coordinate_targets",
    "reduce_lifecycle",
]
