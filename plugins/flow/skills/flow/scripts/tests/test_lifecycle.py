from __future__ import annotations

import pytest

from lifecycle import (
    GroupabilityEvidence,
    GroupTargetEvidence,
    InvalidLifecycleRequest,
    LeaseState,
    LifecycleAction,
    LifecycleEvidence,
    MultiTargetChoice,
    MultiTargetDisposition,
    PrState,
    RunState,
    TicketState,
    UnknownTargetError,
    coordinate_targets,
    reduce_lifecycle,
)


def groupability(
    *keys: str,
    coupling_verified: bool = True,
    live: bool = True,
    epic: bool = False,
) -> GroupabilityEvidence:
    return GroupabilityEvidence(
        targets=tuple(GroupTargetEvidence(key, live=live, epic=epic) for key in keys),
        coupling_verified=coupling_verified,
    )


def evidence(
    *,
    target_exists: bool = True,
    ticket_state: TicketState = "open",
    run_state: RunState = "none",
    lease_state: LeaseState = "free",
    pr_state: PrState = "none",
    request: bool = False,
    scope_approved: bool = False,
    stored_question: bool = False,
    actionable_feedback: bool = False,
    ship_event_corrupt: bool = False,
    contradictions: tuple[str, ...] = (),
) -> LifecycleEvidence:
    return LifecycleEvidence(
        target_exists=target_exists,
        ticket_state=ticket_state,
        run_state=run_state,
        lease_state=lease_state,
        pr_state=pr_state,
        request=request,
        scope_approved=scope_approved,
        stored_question=stored_question,
        actionable_feedback=actionable_feedback,
        ship_event_corrupt=ship_event_corrupt,
        contradictions=contradictions,
    )


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (evidence(), LifecycleAction.START),
        (evidence(request=True), LifecycleAction.START),
        (evidence(ticket_state="deferred", stored_question=True), LifecycleAction.ANSWER),
        (evidence(ticket_state="blocked", stored_question=True), LifecycleAction.ANSWER),
        (evidence(run_state="healthy"), LifecycleAction.RESUME),
        (
            evidence(run_state="healthy", lease_state="live_foreign"),
            LifecycleAction.RUNNING,
        ),
        (evidence(run_state="none", lease_state="live_foreign"), LifecycleAction.RUNNING),
        (evidence(run_state="failed"), LifecycleAction.REPAIR),
        (evidence(run_state="stale"), LifecycleAction.REPAIR),
        (evidence(run_state="drifted"), LifecycleAction.REPAIR),
        (evidence(run_state="corrupt"), LifecycleAction.REPAIR),
        (evidence(run_state="healthy", lease_state="stale_foreign"), LifecycleAction.REPAIR),
        (
            evidence(run_state="completed", pr_state="open", actionable_feedback=True),
            LifecycleAction.REVISE,
        ),
        (
            evidence(run_state="completed", pr_state="open", request=True),
            LifecycleAction.REVISE,
        ),
        (evidence(run_state="completed", pr_state="open"), LifecycleAction.SHOW),
        (
            evidence(ticket_state="done", run_state="completed", pr_state="merged"),
            LifecycleAction.SHOW,
        ),
        (evidence(ticket_state="cancelled", pr_state="closed"), LifecycleAction.SHOW),
        (evidence(run_state="completed"), LifecycleAction.SHOW),
    ],
)
def test_reducer_decision_table(case: LifecycleEvidence, expected: LifecycleAction) -> None:
    assert reduce_lifecycle(case) is expected


def test_unknown_target_is_an_error_not_a_lifecycle_action() -> None:
    with pytest.raises(UnknownTargetError):
        reduce_lifecycle(evidence(target_exists=False))


def test_request_after_scope_approval_is_rejected_for_incomplete_run() -> None:
    with pytest.raises(InvalidLifecycleRequest, match="approved scope"):
        reduce_lifecycle(evidence(run_state="healthy", scope_approved=True, request=True))

    with pytest.raises(InvalidLifecycleRequest, match="approved scope"):
        reduce_lifecycle(
            evidence(
                run_state="healthy",
                lease_state="live_foreign",
                scope_approved=True,
                request=True,
            )
        )


@pytest.mark.parametrize(
    "case",
    [
        evidence(ticket_state="done", run_state="completed", pr_state="merged", request=True),
        evidence(ticket_state="cancelled", pr_state="closed", request=True),
        evidence(run_state="completed", request=True),
    ],
)
def test_request_on_terminal_delivery_is_rejected(case: LifecycleEvidence) -> None:
    with pytest.raises(InvalidLifecycleRequest, match="terminal delivery"):
        reduce_lifecycle(case)


@pytest.mark.parametrize(
    "case",
    [
        evidence(pr_state="none", actionable_feedback=True),
        evidence(pr_state="merged", actionable_feedback=True, run_state="completed"),
        evidence(pr_state="merged", run_state="healthy", ticket_state="done"),
        evidence(ticket_state="blocked", stored_question=False),
        evidence(contradictions=("tracker says both open and done",)),
        evidence(ship_event_corrupt=True),
    ],
)
def test_contradictory_or_unsafe_evidence_preserves_state(case: LifecycleEvidence) -> None:
    assert reduce_lifecycle(case) is LifecycleAction.CONFLICT


def test_live_foreign_lease_wins_over_broken_run_until_holder_releases() -> None:
    case = evidence(run_state="failed", lease_state="live_foreign")
    assert reduce_lifecycle(case) is LifecycleAction.RUNNING


def test_deferred_question_wins_before_run_recovery() -> None:
    case = evidence(
        ticket_state="deferred",
        stored_question=True,
        run_state="failed",
        lease_state="free",
    )
    assert reduce_lifecycle(case) is LifecycleAction.ANSWER


def test_action_vocabulary_is_closed() -> None:
    assert {action.value for action in LifecycleAction} == {
        "start",
        "answer",
        "resume",
        "running",
        "repair",
        "revise",
        "show",
        "conflict",
    }


def test_one_target_runs_directly() -> None:
    assert coordinate_targets([LifecycleAction.RESUME]) is MultiTargetDisposition.DIRECT


def test_together_requires_every_target_to_be_fresh() -> None:
    assert (
        coordinate_targets(
            [LifecycleAction.START, LifecycleAction.START],
            together=True,
            groupability=groupability("FT-1", "FT-2"),
        )
        is MultiTargetDisposition.TOGETHER
    )
    with pytest.raises(InvalidLifecycleRequest, match=r"every target.*start"):
        coordinate_targets(
            [LifecycleAction.START, LifecycleAction.RESUME],
            together=True,
        )


def test_unattended_multiple_targets_requires_an_explicit_together_mode() -> None:
    with pytest.raises(InvalidLifecycleRequest, match=r"unattended.*--together"):
        coordinate_targets(
            [LifecycleAction.START, LifecycleAction.START],
            unattended=True,
        )


def test_attended_multiple_targets_asks_then_honors_explicit_sequential_choice() -> None:
    actions = [LifecycleAction.START, LifecycleAction.RESUME]
    assert coordinate_targets(actions) is MultiTargetDisposition.NEEDS_CHOICE
    assert (
        coordinate_targets(actions, choice=MultiTargetChoice.SEQUENTIAL)
        is MultiTargetDisposition.SEQUENTIAL
    )


def test_attended_together_choice_uses_the_same_freshness_gate() -> None:
    actions = [LifecycleAction.START, LifecycleAction.START]
    assert (
        coordinate_targets(
            actions,
            choice=MultiTargetChoice.TOGETHER,
            groupability=groupability("FT-1", "FT-2"),
        )
        is MultiTargetDisposition.TOGETHER
    )


@pytest.mark.parametrize(
    ("evidence_value", "message"),
    [
        (None, "groupability evidence"),
        (groupability("FT-1", "FT-1"), "distinct"),
        (groupability("FT-1", "FT-2", live=False), "live"),
        (groupability("FT-1", "FT-2", epic=True), "epic"),
        (groupability("FT-1", "FT-2", coupling_verified=False), "verified coupling"),
        (groupability("FT-1"), "count"),
    ],
)
def test_together_requires_complete_groupability_evidence(
    evidence_value: GroupabilityEvidence | None, message: str
) -> None:
    with pytest.raises(InvalidLifecycleRequest, match=message):
        coordinate_targets(
            [LifecycleAction.START, LifecycleAction.START],
            together=True,
            groupability=evidence_value,
        )


@pytest.mark.parametrize(
    ("together", "unattended", "choice"),
    [
        (True, False, MultiTargetChoice.SEQUENTIAL),
        (False, True, MultiTargetChoice.SEQUENTIAL),
    ],
)
def test_coordination_rejects_conflicting_control_inputs(
    together: bool,
    unattended: bool,
    choice: MultiTargetChoice,
) -> None:
    with pytest.raises(InvalidLifecycleRequest):
        coordinate_targets(
            [LifecycleAction.START, LifecycleAction.START],
            together=together,
            unattended=unattended,
            choice=choice,
        )


def test_coordination_rejects_empty_or_overconfigured_single_target() -> None:
    with pytest.raises(InvalidLifecycleRequest, match="at least one"):
        coordinate_targets([])
    with pytest.raises(InvalidLifecycleRequest, match="one target"):
        coordinate_targets([LifecycleAction.START], together=True)
