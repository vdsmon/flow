"""Regression tests for stage-code_review.md phrasing."""

import pathlib

DOC = (
    pathlib.Path(__file__).parent.parent.parent  # skills/flow/
    / "references"
    / "stage-code_review.md"
)


def test_no_phantom_do_stage_in_code_review_doc():
    text = DOC.read_text()
    assert "/flow do --stage" not in text, (
        "stage-code_review.md still contains phantom verb modifier '/flow do --stage'; "
        "replace with '/flow recover <KEY>' -> 'retry --stage ...'"
    )


def test_canonical_recovery_phrasing_in_code_review_doc():
    text = DOC.read_text()
    assert "/flow recover" in text, (
        "stage-code_review.md is missing canonical recovery phrasing '/flow recover'; "
        "the error path should say '/flow recover <KEY>' -> 'retry --stage implement'"
    )
