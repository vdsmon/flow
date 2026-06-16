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


def _is_trailer_line(line: str) -> bool:
    return bool(
        _TICKET_RE.match(line)
        or _CLOSES_RE.match(line)
        or _FILES_HEAD_RE.match(line)
        or _FILES_CHILD_RE.match(line)
    )


def build_body(raw_commit_body: str) -> str:
    """Build a PR body from `git log -1 --format=%b`.

    Parses ONLY the contiguous LEADING trailer block (lines that match a trailer
    shape: `ticket:`, `Closes <KEY>`, `files:`, indented `  - `/`  * ` children),
    stopping at the first non-trailer or blank line. `Closes` lines become a footer;
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
    closes: list[str] = []
    i = 0
    # consume the contiguous leading trailer block ONLY while lines match a trailer
    # shape; the first blank/non-trailer line ends it. No leading trailer => i stays
    # 0 and the whole body is prose.
    while i < len(lines) and _is_trailer_line(lines[i]):
        if _CLOSES_RE.match(lines[i]):
            closes.append(lines[i].strip())
        i += 1
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
        line = f"{indent}{term}: {rest}".rstrip() if rest else f"{indent}{term}".rstrip()
        return line
    # `# Title Case Heading` -> sentence case (keep the marker, lowercase non-initial
    # words). Idempotent: re-applying to already sentence-cased text is a no-op.
    hm = re.match(r"^(#+\s+)(.*)$", line)
    if hm:
        marker, text = hm.group(1), hm.group(2)
        line = marker + _sentence_case(text)
    return line


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
            # capitalize the first letter but leave an ALL-CAPS / mixed-case
            # acronym tail alone (same guard as the non-initial branch).
            result.append(w[:1].upper() + (w[1:] if not w[1:].islower() else w[1:].lower()))
            seen_word = True
        else:
            # leave ALL-CAPS acronyms and mixed-case identifiers alone; lowercase a
            # plain Title-cased word.
            result.append(w[:1].lower() + w[1:] if w[1:].islower() else w)
    return " ".join(result)


__all__ = ["build_body", "scrub"]
