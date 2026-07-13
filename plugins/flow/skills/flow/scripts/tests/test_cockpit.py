from __future__ import annotations

from cockpit import (
    CockpitInput,
    DeferredItem,
    FeedbackItem,
    MaintenanceNotice,
    PendingMutation,
    build_cockpit,
    render_cockpit,
)


def test_cockpit_groups_attention_before_active_work() -> None:
    snapshot = build_cockpit(
        CockpitInput(
            runs=(
                {
                    "ticket": "FT-2",
                    "next_or_blocked": "plan:pending",
                    "lease": "free",
                    "completed": 1,
                    "total_stages": 7,
                },
                {
                    "ticket": "FT-1",
                    "next_or_blocked": "verify:failed",
                    "lease": "stale",
                    "completed": 4,
                    "total_stages": 7,
                },
            ),
            deferred=(DeferredItem("FT-3", "Choose the public name", "deferred"),),
            pending=(PendingMutation("FT-4", "transition"),),
            feedback=(FeedbackItem("FT-5", "17", 2),),
        )
    )

    assert [item.target for item in snapshot.attention] == ["FT-1", "FT-3", "FT-5"]
    assert [item.target for item in snapshot.active] == ["FT-2"]
    assert snapshot.pending_mutations == 1
    assert snapshot.next_commands == (
        "FLOW workspace repair FT-1",
        'FLOW FT-3 --request "<answer>"',
        "FLOW pr:17",
        "FLOW workspace sync",
        "FLOW FT-2",
    )


def test_cockpit_deduplicates_pending_mutation_targets_and_commands() -> None:
    snapshot = build_cockpit(
        CockpitInput(
            pending=(
                PendingMutation("FT-1", "comment"),
                PendingMutation("FT-1", "transition"),
            )
        )
    )

    assert snapshot.pending_mutations == 2
    assert snapshot.pending_targets == ("FT-1",)
    assert snapshot.next_commands == ("FLOW workspace sync",)


def test_render_cockpit_is_compact_and_uses_logical_flow() -> None:
    snapshot = build_cockpit(
        CockpitInput(deferred=(DeferredItem("FT-7", "Need an API choice", "blocked"),))
    )

    rendered = render_cockpit(snapshot)
    assert "Needs attention" in rendered
    assert "FT-7" in rendered
    assert 'FLOW FT-7 --request "<answer>"' in rendered
    assert "/flow" not in rendered
    assert "$flow:flow" not in rendered


def test_empty_cockpit_has_one_useful_next_command() -> None:
    snapshot = build_cockpit(CockpitInput())

    assert snapshot.next_commands == ("FLOW help",)
    assert "No active Flow work" in render_cockpit(snapshot)


def test_maintainer_preflight_issues_are_visible_and_actionable() -> None:
    snapshot = build_cockpit(
        CockpitInput(
            maintenance=(
                MaintenanceNotice(
                    "nightly schedule",
                    "last fire failed",
                    "FLOW maintain evolution audit",
                ),
            )
        )
    )

    assert snapshot.maintenance[0].label == "nightly schedule"
    assert snapshot.next_commands == ("FLOW maintain evolution audit",)
    rendered = render_cockpit(snapshot)
    assert "Maintainer health" in rendered
    assert "last fire failed" in rendered
