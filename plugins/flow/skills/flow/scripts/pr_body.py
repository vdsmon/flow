"""Scrub, cap, and footer helpers for the authored PR body.

Library module (no shebang, no PEP 723 inline deps, no CLI). The only caller is the
inline `create_pr` handler via `import pr_body`.

The body itself is authored (stage-create_pr.md) and passed via `--body-file`;
this module supplies `closes_footer` (extracted from the HEAD commit's trailer
block), the deterministic de-AI `scrub` floor, `flatten_details` for forges that
render no raw HTML, and the `enforce_cap` size net. All are TOTAL: they never
raise on adversarial input; they degrade to passthrough.
"""

from __future__ import annotations

import re

_CLOSES_RE = re.compile(r"^Closes\s+\S+\s*$")
_TICKET_RE = re.compile(r"^ticket:\s")
_FILES_HEAD_RE = re.compile(r"^files:\s*$")
_FILES_CHILD_RE = re.compile(r"^\s+[-*]\s")
_FENCE_RE = re.compile(r"^\s*```")


def _scan_trailer(lines: list[str]) -> tuple[int, list[str]]:
    """Walk the contiguous leading trailer block; return (end index, Closes lines).

    An indented bullet counts as a trailer line only directly under a `files:`
    head (or another files child); with no such context it is prose and ends the
    block.
    """
    closes: list[str] = []
    i = 0
    in_files = False
    while i < len(lines):
        line = lines[i]
        if _TICKET_RE.match(line) or _CLOSES_RE.match(line) or _FILES_HEAD_RE.match(line):
            if _CLOSES_RE.match(line):
                closes.append(line.strip())
            in_files = bool(_FILES_HEAD_RE.match(line))
            i += 1
            continue
        if in_files and _FILES_CHILD_RE.match(line):
            i += 1
            continue
        break
    return i, closes


def closes_footer(raw_commit_body: str) -> str:
    """Extract the `Closes <KEY>` lines from the leading trailer block.

    Trailer scan: walk the contiguous leading trailer lines,
    collect the `Closes` ones, stop at the first blank or non-trailer line. Returns
    the newline-joined footer, or "". A `Closes` in the prose (after the blank) is
    NOT a trailer footer. TOTAL: never raises.
    """
    try:
        _, closes = _scan_trailer(raw_commit_body.splitlines())
        return "\n".join(closes)
    except Exception:
        return ""


def scrub(body: str) -> str:
    """Deterministic de-AI pass: fix, not detect; idempotent; passthrough on error.

    Outside fenced code blocks: replace em-dashes with commas, sentence-case
    `# Title Case Heading` lines, flatten `- **Term:** body` bullets to plain prose.
    Lines inside a fenced ``` block are untouched. TOTAL: never raises.
    """
    try:
        return _scrub(body)
    except Exception:
        return body


_BOLD_BULLET_RE = re.compile(r"^(\s*)[-*]\s+\*\*(.+?):?\*\*:?\s*(.*)$")


def _scrub(body: str) -> str:
    out: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        out.append(_scrub_line(line))
    return "\n".join(out)


def _scrub_line(line: str) -> str:
    # em-dash -> comma, normalizing surrounding whitespace to a single trailing space.
    line = re.sub(r"\s*—\s*", ", ", line)
    # `- **Term:** body` / `- **Term** body` bullet -> plain prose "Term: body".
    m = _BOLD_BULLET_RE.match(line)
    if m:
        indent, term, rest = m.group(1), m.group(2).strip(), m.group(3).strip()
        return f"{indent}{term}: {rest}".rstrip() if rest else f"{indent}{term}".rstrip()
    # `# Title Case Heading` -> sentence case (keep the marker, lowercase non-initial
    # words). Idempotent: re-applying to already sentence-cased text is a no-op.
    hm = re.match(r"^(#+\s+)(.*)$", line)
    if hm:
        marker, text = hm.group(1), hm.group(2)
        line = marker + _sentence_case(text)
    return line


# Inferred forge description cap with margin, not a verified API contract. The
# stricter forge (Bitbucket) caps a PR description near 32768 chars; GitHub allows
# 65536. 32000 is a conservative floor under the stricter one, the margin absorbing
# the guess. enforce_cap is the deterministic net so an oversized evidence body can
# never fail open_pr.
_FORGE_BODY_CAP = 32_000

_TRIM_HEAD = 8
_TRIM_TAIL = 8
_TIER2_NOTE = "… body trimmed to fit …"
_TRUNCATE_MARKER = "\n\n… body truncated to fit …"
# a <details>/<summary> wrapper: group 1 = opening tag through </summary>, group 2 =
# the summary text, group 3 = the body, group 4 = the closing tag. enforce_cap tier 2
# keeps groups 1 and 4 around a one-line note; flatten_details rewrites from 2 and 3.
_DETAILS_RE = re.compile(
    r"(<details\b[^>]*>\s*<summary\b[^>]*>(.*?)</summary>)(.*?)(</details>)", re.DOTALL
)


def enforce_cap(body: str, cap: int = _FORGE_BODY_CAP) -> str:
    """Shrink an over-cap PR body deterministically so it can never fail open_pr.

    Under cap: returned untouched (idempotent, byte-identical). Over cap, in order:
    shrink the largest fenced blocks (head+tail lines around a `… N lines trimmed …`
    marker) until it fits; still over, drop `<details>` bodies keeping their
    `<summary>` lines; final fallback, hard-truncate with a marker. The tiers
    guarantee a body no longer than `cap` on every non-exceptional path. TOTAL:
    never raises; on an unexpected internal error the outer guard degrades to
    passthrough like its siblings, trading the bound for totality.
    """
    try:
        return _enforce_cap(body, cap)
    except Exception:
        return body


def _enforce_cap(body: str, cap: int) -> str:
    if len(body) <= cap:
        return body
    body = _trim_fenced_blocks(body, cap)
    if len(body) <= cap:
        return body
    body = _DETAILS_RE.sub(lambda m: f"{m.group(1)}\n{_TIER2_NOTE}\n{m.group(4)}", body)
    if len(body) <= cap:
        return body
    return _hard_truncate(body, cap)


def flatten_details(body: str) -> str:
    """Rewrite each `<details>`/`<summary>` wrapper to a `###` heading plus body.

    Bitbucket Cloud renders no raw HTML in markdown, so a collapsible wrapper shows
    as literal tags there; create_pr applies this on a bitbucket forge. The summary
    text becomes the heading and the wrapper body (fenced blocks included) follows
    verbatim. No match is a byte-identical passthrough. TOTAL: never raises;
    passthrough on adversarial input.
    """
    try:
        return _DETAILS_RE.sub(lambda m: f"### {m.group(2).strip()}\n\n{m.group(3).strip()}", body)
    except Exception:
        return body


def compose(authored: str, raw_commit_body: str, *, flatten: bool = False) -> str:
    """The one compose path for a PR description: scrub, flatten, footer, cap.

    Shared by `create_pr` (first open) and `forge update-body` (the e2e early-tail description push)
    so the two surfaces cannot drift: both apply the de-AI `scrub` floor, flatten `<details>` on a
    forge that renders no raw HTML, re-append the deterministic `Closes` footer from the HEAD
    commit's trailer block, and pass through `enforce_cap`. Returns "" when the authored prose
    scrubs to nothing; the caller owns any fallback. TOTAL: every helper here degrades to
    passthrough rather than raising.
    """
    body = scrub(authored).strip()
    if flatten:
        body = flatten_details(body)
    if not body:
        return ""
    footer = closes_footer(raw_commit_body)
    return enforce_cap(f"{body}\n\n{footer}" if footer else body)


def _fenced_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """(open_fence_index, close_fence_index) for each closed fenced block."""
    blocks: list[tuple[int, int]] = []
    i, n = 0, len(lines)
    while i < n:
        if _FENCE_RE.match(lines[i]):
            j = i + 1
            while j < n and not _FENCE_RE.match(lines[j]):
                j += 1
            if j >= n:  # unclosed fence: no more blocks to trim
                break
            blocks.append((i, j))
            i = j + 1
        else:
            i += 1
    return blocks


def _trim_fenced_blocks(body: str, cap: int) -> str:
    lines = body.splitlines()
    threshold = _TRIM_HEAD + _TRIM_TAIL + 1
    while len("\n".join(lines)) > cap:
        # a block is worth trimming only when doing so strictly shrinks it; else the
        # trimmed head+marker+tail form would loop at a fixed size.
        candidates = [(s, e) for (s, e) in _fenced_blocks(lines) if (e - s - 1) > threshold]
        if not candidates:
            break
        # largest by content-line count, lowest start index breaks a tie.
        s, e = max(candidates, key=lambda be: (be[1] - be[0] - 1, -be[0]))
        content = lines[s + 1 : e]
        removed = len(content) - _TRIM_HEAD - _TRIM_TAIL
        marker = f"… {removed} lines trimmed …"
        trimmed = [*content[:_TRIM_HEAD], marker, *content[-_TRIM_TAIL:]]
        lines = lines[: s + 1] + trimmed + lines[e:]
    return "\n".join(lines)


def _hard_truncate(body: str, cap: int) -> str:
    if cap <= len(_TRUNCATE_MARKER):
        return body[:cap]
    return body[: cap - len(_TRUNCATE_MARKER)] + _TRUNCATE_MARKER


def _sentence_case(text: str) -> str:
    if not text.strip():
        return text
    words = text.split(" ")
    result: list[str] = []
    seen_word = False
    for w in words:
        if not w:
            result.append(w)
            continue
        if not seen_word:
            # uppercase only the first character; the untouched tail keeps
            # ALL-CAPS acronyms and mixed-case identifiers intact.
            result.append(w[:1].upper() + w[1:])
            seen_word = True
        else:
            # leave ALL-CAPS acronyms and mixed-case identifiers alone; lowercase a
            # plain Title-cased word.
            result.append(w[:1].lower() + w[1:] if w[1:].islower() else w)
    return " ".join(result)


__all__ = ["closes_footer", "compose", "enforce_cap", "flatten_details", "scrub"]
