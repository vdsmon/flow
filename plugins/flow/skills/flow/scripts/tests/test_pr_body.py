from __future__ import annotations

import compose_commit
import pr_body


def _realistic_raw_b(prose: str, *, covers=("flow-nr8c", "flow-pms6")) -> str:
    """The `%b` an author leaves at PR time: the compose_commit skeleton body (trailer + surviving
    fill-in-below marker) plus appended prose. Grounds the fixture in the real producer, not a
    hand-clean string."""
    full = compose_commit.compose(
        "flow-x1yq",
        "chore",
        "build a real PR body",
        files=["create_pr.py", "pr_body.py"],
        covers=list(covers),
    )
    body_b = full.split("\n", 1)[1].lstrip("\n")
    return body_b + prose


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


_ADVERSARIAL = [
    "",
    "\x00binary\x00",
    "```\nunclosed fence",
    "a" * 100_000,
    "ticket: x\nCloses y\n\n" + "wrap " * 5000,
    "<details><summary>x</summary>",
]


def test_scrub_never_raises_on_adversarial():
    for c in _ADVERSARIAL:
        assert isinstance(pr_body.scrub(c), str)


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
            f"<details>\n<summary>run {i}: 5 passed (2s)</summary>\n\n"
            f"```\n{transcript}\n```\n\n</details>"
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
    body = (
        "## Evidence\n\n<details>\n<summary>run: 3 passed (1s)</summary>\n\n"
        f"{transcript}\n\n</details>\n"
    )
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
