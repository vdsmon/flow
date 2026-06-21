"""Tier -> verification-lane decider (pure): scale gate depth to the cost of being wrong.

flow runs the same verification on a comment typo as on a hot-concurrency change.
The xqt counterfactual (docs/research/xqt-counterfactual-verdict-2026-06.md) measured
that as 6-25 min/run of overhead with zero quality gain below the complexity ceiling,
and recommended bounding the machinery to high-complexity/hot work. flow already
classifies beads (tier:trivial / tier:light, see verb-evolve.md) but the label only
picks the worker MODEL (evolve_select.py model_per_key). This maps the SAME labels to
a verification LANE that the spec/implement/reflect stages branch on (they read the
lane string directly — verb-spec.md `--auto`, stage-implement.md, stage-reflect.md;
the per-lane gate policy lives in that prose, not here).

Safety: a tier label is a vetted judgment from the Opus producer's audit step, so an
express/light lane is "don't re-run judgment that already happened," NOT "skip
judgment." The independent review (CI + the review bot) and the deterministic safety
machinery (lease / snapshot / content-ownership / push-state) run on every lane. A
hot or untiered bead always gets the full lane. tier:light keeps TDD (it can be
behavior-changing); tier:trivial is behavior-preserving by the producer's definition,
so it earns the plan/test skips.

Imported by `triage.lane` (spec-time, via raw bd read) and `flow_worktree._lane_for_bead`
(bootstrap-time, via the tracker). No I/O here — callers supply the labels.
"""

from __future__ import annotations

LANES = ("full", "light", "express")


def lane_for(labels: object) -> str:
    """Map a bead's labels to its verification lane.

    Trusts the producer's tier stamp: tier:trivial is already vetted behavior-preserving
    (a behavior-changing finding with a checkable invariant gets NO tier label upstream),
    so re-deriving behavior-preservation here would be the redundant judgment we are
    removing. `hot` always wins -> full lane.
    """
    label_set = {str(x) for x in labels} if isinstance(labels, (list, tuple, set)) else set()
    if "hot" in label_set:
        return "full"
    if "tier:trivial" in label_set:
        return "express"
    if "tier:light" in label_set:
        return "light"
    return "full"
