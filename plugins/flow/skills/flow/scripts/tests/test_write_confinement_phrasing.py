"""Regression tests for the write-confinement briefing in delivery-loop.md.

These are string pins, not behavior coverage. They prove the sentences are present so a
later editor cannot silently drop them; they cannot prove an agent obeys them, and no
test can. The defect they guard is a host behavior with no flow-side mechanism: an
agent's Edit/Write is refused as confined to a worktree it was never given, so prose
carrying the known workarounds to the agents who hit it is the whole deliverable.

The foreman.md spawn-template pins that used to live here were deleted with the
template itself: the post-hoc recharter removed the managed driver topology, and
delivery-loop.md's Independent agent section is now the sole carrier of the briefing.
"""

import pathlib

REFERENCES = pathlib.Path(__file__).parent.parent.parent / "references"
DELIVERY_LOOP = REFERENCES / "delivery-loop.md"


def _flat(path: pathlib.Path) -> str:
    """Document text with runs of whitespace collapsed to single spaces.

    These documents are hard-wrapped, so a pinned phrase can straddle a line break and a
    raw substring search would miss it. Worse, it would pass today and fail the next time
    someone rewraps a paragraph without changing a word, which is a false alarm about the
    one thing these tests exist to watch.
    """
    return " ".join(path.read_text().split())


def test_independent_agent_prompt_states_the_confinement_rule():
    text = _flat(DELIVERY_LOOP)
    assert "State the write-confinement rule in the same prompt" in text, (
        "delivery-loop.md 'Independent agent' must instruct the driver to state the "
        "write-confinement rule in every stage-agent prompt. This section composes the "
        "prompt for every stage, so it is the only place the briefing reaches all of them."
    )


def test_blocked_agent_routes_to_the_inline_downgrade():
    text = _flat(DELIVERY_LOOP)
    assert "answers an agent that returns BLOCKED on write confinement" in text, (
        "delivery-loop.md must route an agent that returns BLOCKED on write confinement to "
        "the existing inline downgrade. That path is contract-blessed and keeps atomic "
        "replacement and the read-before-edit guard; without it the contract has no answer."
    )


def test_bash_route_is_kept_with_the_cost_that_orders_it():
    text = _flat(DELIVERY_LOOP)
    assert "trades a refused write for a wrong-write risk" in text, (
        "delivery-loop.md must keep the Bash exact-match route together with its cost. "
        "Three witnesses completed on that route, so it is not dead and must stay "
        "documented; it is ordered second only because it gives up read-before-edit and "
        "atomic replacement. Dropping the cost leaves the ordering looking arbitrary; "
        "dropping the route leaves an agent that cannot hand its stage back with nothing."
    )
