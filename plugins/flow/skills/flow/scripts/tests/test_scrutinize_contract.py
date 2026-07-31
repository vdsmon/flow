"""Regression tests for the seat's bounded seating contract."""

from __future__ import annotations

from pathlib import Path

SCRUTINIZE_DOC = Path(__file__).resolve().parents[2] / "references" / "scrutinize.md"


def _seating() -> str:
    text = SCRUTINIZE_DOC.read_text(encoding="utf-8")
    return text.split("## Seating", 1)[1].split("## Pickup", 1)[0]


def test_seating_has_an_open_direction_boundary_without_a_queue_scan() -> None:
    seating = _seating()
    assert "What would you like to do?" in seating
    assert "Read `bd ready`" not in seating
    assert "ranked shortlist" not in seating
    assert "parked PR whose gate is met" not in seating
    assert "live run is resumed" not in seating


def test_remote_reads_are_deferred_until_the_user_selects_work() -> None:
    seating = _seating()
    boundary = seating.index("After the human chooses a direction")
    before_choice = seating[:boundary]
    after_choice = seating[boundary:]
    for forbidden in (
        "tracker list-assigned",
        "tracker get",
        "forge list-authored",
        "fetch CI",
    ):
        assert forbidden not in before_choice
    assert "tracker list-assigned --filter open" in after_choice
    assert "forge list-authored --state open" in after_choice
    assert "a general queue scan still waits" in " ".join(after_choice.split())
