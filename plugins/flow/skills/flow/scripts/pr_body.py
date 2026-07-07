"""Build + scrub a PR body from the run's HEAD commit body.

Library module (no shebang, no PEP 723 inline deps, no CLI). The only caller is the
inline `create_pr` handler via `import pr_body`.

The PR body is derived from `git log -1 --format=%b`, which at PR time is the
`compose_commit.py` skeleton's body section: a contiguous leading trailer block
(`ticket: KEY`, zero+ `Closes <KEY>`, an optional `files:` list with indented
`  - ...` children), then a blank line, then the prose the commit stage authored
(which may still carry the `# body — fill in below this line` skeleton marker if the
author appended below it rather than overwriting).

`build_body` strips the trailer noise, keeps the `Closes` lines as a footer, and
unwraps commit-style hard wraps within prose paragraphs. `scrub` runs a
deterministic de-AI pass over the result. Both are TOTAL: they never raise on
adversarial input; they degrade to passthrough.
"""

from __future__ import annotations

import re

_CLOSES_RE = re.compile(r"^Closes\s+\S+\s*$")
_TICKET_RE = re.compile(r"^ticket:\s")
_FILES_HEAD_RE = re.compile(r"^files:\s*$")
_FILES_CHILD_RE = re.compile(r"^\s+[-*]\s")
_LIST_ITEM_RE = re.compile(r"^\s*([-*]|\d+\.)\s")
_SKELETON_MARKER_RE = re.compile(r"^#\s*body\s*[—-]\s*fill in below this line\s*$")
_FENCE_RE = re.compile(r"^\s*```")


def _scan_trailer(lines: list[str]) -> tuple[int, list[str]]:
    """Walk the contiguous leading trailer block; return (end index, Closes lines).

    An indented bullet counts as a trailer line only directly under a `files:`
    head (or another files child); with no such context it is prose and ends the
    block, honoring build_body's no-deletion commitment.
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


def build_body(raw_commit_body: str) -> str:
    """Build a PR body from `git log -1 --format=%b`.

    Parses ONLY the contiguous LEADING trailer block (lines that match a trailer
    shape: `ticket:`, `Closes <KEY>`, `files:`, and indented `  - `/`  * ` children
    directly under a `files:` head), stopping at the first non-trailer or blank
    line. `Closes` lines become a footer;
    `ticket:`/`files:` are dropped. The remaining prose is unwrapped (hard wraps
    within a paragraph joined) but never reflowed across blank-line paragraph breaks,
    never across list items, never inside a fenced code block. A stray skeleton
    marker line is dropped. TOTAL: never raises; passthrough on adversarial input.

    Crucially, a body with NO leading trailer is treated as all-prose: the first
    line not matching a trailer shape ends the (possibly empty) trailer block, so
    prose is never mistaken for trailer and deleted.
    """
    try:
        return _build_body(raw_commit_body)
    except Exception:
        return raw_commit_body


def _build_body(raw: str) -> str:
    lines = raw.splitlines()
    # consume the contiguous leading trailer block ONLY while lines match a trailer
    # shape; the first blank/non-trailer line ends it. No leading trailer => i stays
    # 0 and the whole body is prose.
    i, closes = _scan_trailer(lines)
    # skip the single blank line separating the trailer from the prose.
    if i < len(lines) and not lines[i].strip():
        i += 1

    prose_lines = lines[i:]
    prose = _unwrap_prose(prose_lines)

    parts: list[str] = []
    if prose.strip():
        parts.append(prose.rstrip())
    if closes:
        parts.append("\n".join(closes))
    return "\n\n".join(parts)


def _unwrap_prose(lines: list[str]) -> str:
    """Join hard-wrapped lines within prose paragraphs; preserve structure.

    Consecutive non-blank prose lines join into one. A blank line, a list item, or
    a fenced code block boundary breaks the join. Lines inside a fenced ``` block
    pass through verbatim. A stray skeleton marker is dropped.
    """
    out: list[str] = []
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        if buf:
            out.append(" ".join(s.strip() for s in buf))
            buf.clear()

    for line in lines:
        if _FENCE_RE.match(line):
            flush()
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        if _SKELETON_MARKER_RE.match(line):
            continue
        if not line.strip():
            flush()
            out.append("")
            continue
        if _LIST_ITEM_RE.match(line):
            flush()
            out.append(line.rstrip())
            continue
        buf.append(line)
    flush()
    return "\n".join(out)


def closes_footer(raw_commit_body: str) -> str:
    """Extract the `Closes <KEY>` lines from the leading trailer block.

    Same trailer scan as build_body: walk the contiguous leading trailer lines,
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
# a collapsed <details> whose body is dropped in tier 2: keep the opening tag +
# <summary>, replace the body with a one-line note, keep the closing tag.
_DETAILS_RE = re.compile(
    r"(<details\b[^>]*>\s*<summary\b[^>]*>.*?</summary>)(.*?)(</details>)", re.DOTALL
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
    body = _DETAILS_RE.sub(lambda m: f"{m.group(1)}\n{_TIER2_NOTE}\n{m.group(3)}", body)
    if len(body) <= cap:
        return body
    return _hard_truncate(body, cap)


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


__all__ = ["build_body", "closes_footer", "enforce_cap", "scrub"]
