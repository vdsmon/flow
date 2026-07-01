from __future__ import annotations

import compose_commit
import pr_body


def _realistic_raw_b(prose: str, *, covers=("flow-nr8c", "flow-pms6")) -> str:
    """The `%b` an author leaves at PR time: the compose_commit skeleton body
    (trailer + surviving `# body — fill in below this line` marker) plus appended
    prose. Grounds the fixture in the real producer, not a hand-clean string."""
    full = compose_commit.compose(
        "flow-x1yq",
        "chore",
        "build a real PR body",
        files=["create_pr.py", "pr_body.py"],
        covers=list(covers),
    )
    body_b = full.split("\n", 1)[1].lstrip("\n")
    return body_b + prose


# ─── build_body: trailer parsing ─────────────────────────────────────────────


def test_trailer_stripped_and_closes_footer():
    out = pr_body.build_body(_realistic_raw_b("Real prose body.\n"))
    assert "ticket:" not in out
    assert "files:" not in out
    assert "create_pr.py" not in out  # files children dropped
    assert "Real prose body." in out
    assert out.rstrip().endswith("Closes flow-nr8c\nCloses flow-pms6")


def test_skeleton_marker_dropped():
    # the marker survives in %b when the author appends below it; build_body drops it.
    raw = _realistic_raw_b("Some prose here.\n")
    assert "fill in below" in raw
    assert "fill in below" not in pr_body.build_body(raw)


def test_no_trailer_prose_preserved():
    # commitment #1: a body with no leading trailer is all prose; nothing deleted.
    raw = "Just prose here\nwrapped across lines.\n\nSecond paragraph.\n"
    out = pr_body.build_body(raw)
    assert out == "Just prose here wrapped across lines.\n\nSecond paragraph."


def test_contiguous_trailer_stops_at_blank_prose_closes_survives():
    # a `Closes` in PROSE (after the blank) is NOT collected as a trailer footer.
    raw = "ticket: flow-x\nCloses flow-real\n\nThis Closes the gap nicely.\n"
    out = pr_body.build_body(raw)
    assert "This Closes the gap nicely." in out
    # exactly one Closes footer (the trailer one), prose Closes left in place
    assert out.count("Closes flow-real") == 1
    assert out.endswith("Closes flow-real")


def test_no_closes_no_footer():
    raw = "ticket: flow-x\nfiles:\n  - a.py\n\nBody text.\n"
    out = pr_body.build_body(raw)
    assert out == "Body text."


# ─── build_body: prose unwrap ────────────────────────────────────────────────


def test_hard_wraps_unwrapped_within_paragraph():
    raw = "ticket: flow-x\n\nLine one wraps\ninto line two\ninto line three.\n"
    assert pr_body.build_body(raw) == "Line one wraps into line two into line three."


def test_blank_line_paragraph_break_not_reflowed():
    raw = "ticket: flow-x\n\nPara one.\n\nPara two.\n"
    assert pr_body.build_body(raw) == "Para one.\n\nPara two."


def test_list_items_not_reflowed():
    raw = "ticket: flow-x\n\n- first\n- second\n* third\n1. fourth\n"
    out = pr_body.build_body(raw)
    assert out == "- first\n- second\n* third\n1. fourth"


def test_fenced_code_not_reflowed():
    raw = "ticket: flow-x\n\nIntro.\n\n```\nline a\nline b\n```\n\nOutro.\n"
    out = pr_body.build_body(raw)
    assert "```\nline a\nline b\n```" in out
    assert "line a line b" not in out


# ─── scrub ───────────────────────────────────────────────────────────────────


def test_scrub_em_dash_to_punctuation():
    out = pr_body.scrub("Text with an em — dash here.")
    assert "—" not in out
    assert ";" not in out
    assert out == "Text with an em, dash here."


def test_scrub_title_case_heading_to_sentence_case():
    assert pr_body.scrub("# Title Case Heading") == "# Title case heading"


def test_scrub_heading_preserves_acronyms():
    # an ALL-CAPS acronym (first word or later) survives sentence-casing.
    assert pr_body.scrub("# API Reference") == "# API reference"
    assert pr_body.scrub("# CLI Usage") == "# CLI usage"
    assert pr_body.scrub("# The HTTP Layer") == "# The HTTP layer"


def test_scrub_flattens_bold_term_bullet():
    assert pr_body.scrub("- **Term:** body text") == "Term: body text"
    assert pr_body.scrub("- **Term** body text") == "Term: body text"


def test_scrub_idempotent():
    s = "# Title Case Heading\nText — with em.\n- **Term:** body\n"
    once = pr_body.scrub(s)
    assert pr_body.scrub(once) == once


def test_scrub_leaves_fenced_code_untouched():
    s = "Intro — here.\n\n```\ncode — with — dashes\n```\n"
    out = pr_body.scrub(s)
    assert "code — with — dashes" in out
    assert "Intro, here." in out


# ─── closes_footer ───────────────────────────────────────────────────────────


def test_closes_footer_collects_trailer_closes():
    raw = _realistic_raw_b("Prose body.\n", covers=("flow-nr8c", "flow-pms6"))
    assert pr_body.closes_footer(raw) == "Closes flow-nr8c\nCloses flow-pms6"


def test_closes_footer_none_when_no_covers():
    raw = _realistic_raw_b("Prose body.\n", covers=())
    assert pr_body.closes_footer(raw) == ""


def test_closes_footer_empty_on_no_trailer():
    assert pr_body.closes_footer("Just prose, no trailer.\n") == ""


def test_closes_footer_ignores_prose_closes():
    # a Closes AFTER the blank (in prose) is not a trailer footer.
    raw = "ticket: flow-x\nCloses flow-real\n\nThis Closes the gap.\n"
    assert pr_body.closes_footer(raw) == "Closes flow-real"


# ─── totality: never raise on adversarial input ──────────────────────────────


def test_build_and_scrub_never_raise_on_adversarial():
    cases = [
        "",
        "   \n  ",
        "ticket: flow-x",  # trailer only, no prose
        "Just prose, no trailer at all.",
        "```\nunterminated fence to end of body",
        "Closes mentioned in prose\n\n# heading",
        "​� stray unicode \U0001f600",
        "files:\n  - a\n  - b",  # trailer-only files block
    ]
    for c in cases:
        assert isinstance(pr_body.build_body(c), str)
        assert isinstance(pr_body.scrub(c), str)
        assert isinstance(pr_body.closes_footer(c), str)
        # build then scrub composes without raising
        assert isinstance(pr_body.scrub(pr_body.build_body(c)), str)


def test_build_body_empty_is_empty():
    assert pr_body.build_body("") == ""
