"""Regression tests for the write-confinement briefing in manager.md + delivery-loop.md.

These are string pins, not behavior coverage. They prove the sentences are present so a
later editor cannot silently drop them; they cannot prove an agent obeys them, and no
test can. The defect they guard is a host behavior with no flow-side mechanism: an
agent's Edit/Write is refused as confined to a worktree it was never given, so prose
carrying the known workarounds to the agents who hit it is the whole deliverable.
"""

import pathlib

REFERENCES = pathlib.Path(__file__).parent.parent.parent / "references"
MANAGER = REFERENCES / "manager.md"
DELIVERY_LOOP = REFERENCES / "delivery-loop.md"


def _spawn_template() -> str:
    """The fenced spawn template in manager.md, flattened, WITHOUT the surrounding prose.

    The bullet has to live inside the fence, not merely somewhere in the file: outside it
    `lint_comments` starts checking the prose, and more importantly the driver spawn prompt
    stops carrying the briefing, which is the entire delivery mechanism. Searching the whole
    document would pass on a bullet that had drifted out of the fence.
    """
    text = MANAGER.read_text()
    start = text.index("```text")
    end = text.index("```", start + len("```text"))
    return " ".join(text[start:end].split())


def _flat(path: pathlib.Path) -> str:
    """Document text with runs of whitespace collapsed to single spaces.

    These documents are hard-wrapped, so a pinned phrase can straddle a line break and a
    raw substring search would miss it. Worse, it would pass today and fail the next time
    someone rewraps a paragraph without changing a word, which is a false alarm about the
    one thing these tests exist to watch.
    """
    return " ".join(path.read_text().split())


def test_spawn_template_names_the_pinned_worktree_binding():
    text = _spawn_template()
    assert "binds to its PINNED" in text, (
        "manager.md's spawn template must tell the driver that write confinement binds to "
        "the session's PINNED worktree, not its working directory. Without that, a driver "
        "reads a refusal as its own path mistake and looks in the wrong place."
    )


def test_spawn_template_names_the_driver_repin():
    text = _spawn_template()
    assert "EnterWorktree" in text, (
        "manager.md's spawn template must name the driver's re-pin (EnterWorktree with an "
        "explicit path). It is the one remedy witnessed working for a driver session, and "
        "cd is not a substitute: cd moves the working directory, not the pin."
    )


def test_spawn_template_tells_a_blocked_subagent_to_return_blocked():
    text = _spawn_template()
    assert "return BLOCKED at once" in text, (
        "manager.md's spawn template must tell a blocked stage subagent to return BLOCKED "
        "immediately rather than fight the guard. A subagent's pin is fixed at spawn so it "
        "cannot recover, and the driver has a documented takeover that is cheaper."
    )


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
