from __future__ import annotations

import pytest

import tier_policy as tp


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["evolve", "tier:trivial"], "express"),
        (["tier:light"], "light"),
        (["evolve"], "full"),  # untiered
        ([], "full"),
        (["hot", "tier:trivial"], "full"),  # hot always wins over a tier stamp
        (["hot", "tier:light"], "full"),
    ],
)
def test_lane_for(labels, expected):
    assert tp.lane_for(labels) == expected


def test_lane_for_tolerates_non_list():
    assert tp.lane_for(None) == "full"
    assert tp.lane_for("tier:trivial") == "full"  # a bare string is not a label list
