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


def test_leading_indented_bullets_are_prose_not_trailer():
    # commitment #1 adversarial case: an indented bullet with NO files: head above
    # it is prose, not a files child; it must survive, not be eaten by the scan.
    raw = "  - fixed the lease race\n  - added regression tests\n\nSecond paragraph.\n"
    out = pr_body.build_body(raw)
    assert "fixed the lease race" in out
    assert "added regression tests" in out
    assert "Second paragraph." in out


def test_indented_bullet_after_non_files_trailer_ends_block():
    # a ticket: line does not open files context; the bullet after it is prose.
    raw = "ticket: flow-x\n  - looks like a files child but is not\n\nBody.\n"
    out = pr_body.build_body(raw)
    assert "looks like a files child but is not" in out
    assert "ticket:" not in out


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


def test_closes_footer_leading_bullet_ends_trailer_scan():
    # same files-context guard as build_body: a leading indented bullet is prose,
    # so the Closes after it is prose too, not a trailer footer.
    raw = "  - a prose bullet\nCloses flow-x\n\nBody.\n"
    assert pr_body.closes_footer(raw) == ""


# ─── totality: never raise on adversarial input ──────────────────────────────


def test_build_and_scrub_never_raise_on_adversarial():
    cases = [
        "",
        "   \n  ",
        "ticket: flow-x",  # trailer only, no prose
        "Just prose, no trailer at all.",
        "```\nunterminated fence to end of body",
        "Closes mentioned in prose\n\n# heading",
        "\u200b� stray unicode \U0001f600",
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


# ─── enforce_cap: forge body-size net ────────────────────────────────────────


def _fenced(label: str, n: int) -> str:
    body = "\n".join(f"{label}{i}" for i in range(n))
    return f"```\n{body}\n```"


def test_enforce_cap_under_cap_passthrough():
    body = "short body\n\n```\nline a\nline b\n```\n"
    assert pr_body.enforce_cap(body, cap=10_000) == body


def test_enforce_cap_default_cap_signature():
    # the default-cap call (no cap arg) is passthrough on a tiny body.
    body = "## Evidence\n\n<details>\n<summary>run: 3 passed (1s)</summary>\n\nok\n\n</details>\n"
    assert pr_body.enforce_cap(body) == body


def test_enforce_cap_exact_boundary_untouched():
    body = "x" * 500
    assert pr_body.enforce_cap(body, cap=500) == body  # len == cap is under (<=)


def test_enforce_cap_trims_largest_fenced_block_first():
    small = _fenced("s", 3)
    large = _fenced("L", 200)
    body = f"intro\n\n{small}\n\n{large}\n"
    out = pr_body.enforce_cap(body, cap=400)
    assert len(out) <= 400
    assert "lines trimmed" in out  # a fenced-block trim happened
    assert "L0" in out
    assert "L199" in out
    assert "s0" in out
    assert "s2" in out


def test_enforce_cap_summary_lines_survive_all_tiers():
    blocks = []
    for i in range(10):
        transcript = "\n".join(f"t{i}-{j}" for j in range(100))
        blocks.append(
            f"<details>\n<summary>run {i}: 5 passed (2s)</summary>\n\n```\n{transcript}\n```\n\n</details>"
        )
    body = "## Evidence\n\n" + "\n\n".join(blocks) + "\n"
    out = pr_body.enforce_cap(body, cap=1200)
    assert len(out) <= 1200
    for i in range(10):
        assert f"run {i}:" in out  # every <summary> survived the structured trim tiers


def test_enforce_cap_idempotent():
    block = f"intro\n\n<details>\n<summary>run</summary>\n\n{_fenced('L', 300)}\n\n</details>\n"
    body = block * 5
    once = pr_body.enforce_cap(body, cap=500)
    assert len(once) <= 500
    assert pr_body.enforce_cap(once, cap=500) == once


def test_enforce_cap_hard_truncate_pure_prose():
    # no fences, no <details>: only the hard-truncate backstop can enforce the cap.
    body = "prose line\n" * 1000
    out = pr_body.enforce_cap(body, cap=300)
    assert len(out) <= 300
    assert "truncated" in out


def test_enforce_cap_never_raises_and_always_caps_on_adversarial():
    cases = [
        "",
        "x" * 5000,  # pure prose, no structure
        "```\nunclosed fence " + "y" * 5000,  # unbalanced fence
        "<details>\n<summary>s</summary>\n" + "z" * 5000,  # unclosed <details>
        "<details>" * 200,  # malformed markup
        "```\n" + "a\n" * 2000 + "```",  # one big fenced block, no <details>
    ]
    for c in cases:
        out = pr_body.enforce_cap(c, cap=200)
        assert isinstance(out, str)
        assert len(out) <= 200


def test_enforce_cap_scrub_fence_byte_identical_under_cap():
    # fence-preservation fixture: under cap enforce_cap is passthrough, so a fenced
    # transcript survives scrub(enforce_cap(...)) verbatim (scrub's fence passthrough).
    transcript = "```\ncmd — with an em dash\nline b\n```"
    body = f"## Evidence\n\n<details>\n<summary>run: 3 passed (1s)</summary>\n\n{transcript}\n\n</details>\n"
    assert pr_body.enforce_cap(body) == body  # default cap, under -> identical
    assert transcript in pr_body.scrub(pr_body.enforce_cap(body))  # fenced content untouched


# ─── flatten_details: bitbucket no-raw-HTML flatten ──────────────────────────


def test_flatten_details_basic_wrapper_to_heading():
    body = (
        "## Evidence\n\n<details>\n<summary>run: 3 passed (1s)</summary>\n\n"
        "```\nline a\nline b\n```\n\n</details>\n"
    )
    out = pr_body.flatten_details(body)
    assert "<details>" not in out
    assert "</details>" not in out
    assert "<summary>" not in out
    assert "### run: 3 passed (1s)" in out
    assert "```\nline a\nline b\n```" in out  # fenced body preserved


def test_flatten_details_multiple_blocks():
    blocks = "\n\n".join(
        f"<details>\n<summary>run {i}: ok</summary>\n\nbody {i}\n\n</details>" for i in range(3)
    )
    out = pr_body.flatten_details(f"## Evidence\n\n{blocks}\n")
    assert "<details>" not in out
    assert "<summary>" not in out
    for i in range(3):
        assert f"### run {i}: ok" in out
        assert f"body {i}" in out


def test_flatten_details_no_match_byte_identical():
    body = "plain prose\n\n## Changes\n- `x.py`: a thing\n\n```\nfenced\n```\n"
    assert pr_body.flatten_details(body) == body


def test_flatten_details_unclosed_and_malformed_passthrough():
    cases = [
        "<details>\n<summary>s</summary>\n\nnever closed",
        "<details>\nno summary at all\n</details>",
        "<details>" * 5,
        "",
    ]
    for c in cases:
        assert pr_body.flatten_details(c) == c


def test_flatten_details_idempotent_and_still_capped():
    transcript = "\n".join(f"t{j}" for j in range(200))
    body = (
        "## Evidence\n\n<details>\n<summary>run: ok</summary>\n\n"
        f"```\n{transcript}\n```\n\n</details>\n"
    )
    flat = pr_body.flatten_details(body)
    assert pr_body.flatten_details(flat) == flat
    # a flattened body skips the tier-2 <details> drop; tier-1 fence trim still caps it.
    capped = pr_body.enforce_cap(flat, cap=400)
    assert len(capped) <= 400
    assert "### run: ok" in capped
