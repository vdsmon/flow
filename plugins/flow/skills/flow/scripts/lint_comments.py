"""Deterministic comment-quality floor under the stage-implement code-comment bar.

Library + thin CLI. Stdlib-only.

Lints comments and docstrings only, never code. Python files get exact extraction (tokenize for
comments, ast for module/class/function docstrings); other languages get full-line comments
recognized by a line-start marker (`#`, `//`, `--` by extension), so a trailing marker inside a
string literal cannot false-positive (a marker-shaped line inside a multi-line string or heredoc
still can; accepted, those languages have no stdlib tokenizer here).
Markdown (`.md`/`.markdown`) is documentation prose rather than code, so only the em-dash check runs
on it, outside fenced code blocks; the banned-word and width checks are the code-comment bar and
stay off documentation prose. Other unknown extensions are skipped.
`--diff-base <ref>` keeps only findings on lines changed vs that ref, the mode the stages use so a
legacy file's pre-existing comments do not flood a run's gate.

Categories:
  - em-dash     : the em-dash character anywhere in a comment or docstring.
  - banned-word : filler and inflation vocabulary (the `_BANNED_WORD_RES` list, plus the
                  standalone filler adverb `_JUST_RE` hunts; hyphen compounds and temporal uses
                  pass).
  - narration   : reviewer-directed narration markers (`_NARRATION_RES`).
  - long-line   : a full-line comment or docstring line over the configured limit (formatters
                  wrap code, never comment prose, so these survive them).
  - under-fill  : a hand-wrapped block narrower than the configured limit. Flagged when a line
                  breaks mid-sentence even though the next line's first word would fit within the
                  limit; sentence-final breaks, list/table lines, and code fences never count. At
                  most one finding per block.

The limit comes from `--line-length`, else per-file discovery: the nearest pyproject.toml
`[tool.ruff]`/`[tool.black]` line-length, ruff.toml/.ruff.toml, or .editorconfig max_line_length,
walking up through the repo root; ruff's default 88 when nothing declares one.

Exit codes:
  0 = clean (unreadable/missing files are skipped with a stderr note)
  1 = findings printed (human lines on stdout, or a JSON array with --json)
  2 = internal error
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tokenize
import tomllib
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path

_EM_DASH = "—"

_BANNED_WORD_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\belegantly\b",
        r"\bsimply\b",
        r"\bnote that\b",
        r"\bworth mentioning\b",
        r"\bleverag(?:e|es|ed|ing)\b",
        r"\brobustly\b",
        r"\bseamlessly\b",
        r"\bcomprehensive(?:ly)?\b",
        r"\bpowerful(?:ly)?\b",
    )
)
_JUST_RE = re.compile(r"\bjust\b", re.IGNORECASE)
# Temporal "just before X" and hyphen compounds like "just-merged" pass the filler check.
_JUST_OK_NEXT = frozenset(
    {"before", "after", "above", "below", "past", "once", "behind", "ahead", "prior"}
)
_NARRATION_RES: tuple[re.Pattern[str], ...] = (re.compile(r"\bhere we\b", re.IGNORECASE),)

# Tooling directives; every check skips them. Prefixes carry their delimiter so prose that merely
# starts with the same letters ("pragmatic", "coding style") stays linted; the bare words get an
# explicit non-alphanumeric boundary check in _is_pragma.
_PRAGMA_PREFIXES = (
    "!",
    "-*-",
    "type:",
    "pragma:",
    "ruff:",
    "mypy:",
    "pylint:",
    "isort:",
    "fmt:",
    "coding:",
    "coding=",
    "eslint-",
    "prettier-",
    "biome-",
    "@ts-",
)
_PRAGMA_WORDS = ("noqa", "nolint", "shellcheck")

# Line-start markers by extension for non-Python files. Trailing comments are skipped there on
# purpose: without a real tokenizer a marker mid-line may sit inside a string.
_EXT_MARKER: dict[str, str] = (
    dict.fromkeys((".sh", ".bash", ".zsh", ".rb", ".pl", ".yaml", ".yml", ".toml", ".tf"), "#")
    | dict.fromkeys(
        (
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".go",
            ".rs",
            ".java",
            ".c",
            ".h",
            ".cc",
            ".cpp",
            ".hpp",
            ".cs",
            ".swift",
            ".kt",
            ".kts",
            ".scala",
            ".dart",
            ".proto",
        ),
        "//",
    )
    | dict.fromkeys((".sql", ".lua", ".hs"), "--")
)

_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

_DEFAULT_LIMIT = 88
# Prose shorter than this reads as a label or fragment; fill checks skip it.
_MIN_PROSE_LEN = 20


@dataclass
class Finding:
    path: str
    line: int
    category: str
    message: str


@dataclass
class _ProseLine:
    """One comment/docstring line: prose text plus the raw line's filled width."""

    line: int
    text: str
    end_col: int


def _is_pragma(text: str) -> bool:
    t = text.lstrip().lower()
    if t.startswith(_PRAGMA_PREFIXES):
        return True
    return any(
        t.startswith(w) and (len(t) == len(w) or not t[len(w)].isalnum()) for w in _PRAGMA_WORDS
    )


def _first_word(text: str) -> str:
    parts = text.split()
    return parts[0] if parts else ""


def _check_wording(path: str, line: int, text: str) -> list[Finding]:
    out: list[Finding] = []
    if _EM_DASH in text:
        out.append(Finding(path, line, "em-dash", "em-dash in comment/docstring prose"))
    for pat in _BANNED_WORD_RES:
        m = pat.search(text)
        if m:
            out.append(Finding(path, line, "banned-word", f'"{m.group(0)}"'))
    for m in _JUST_RE.finditer(text):
        before = text[: m.start()]
        after = text[m.end() :]
        if before.endswith("-") or after.startswith("-"):
            continue
        if _first_word(after).lower().rstrip(".,;:") in _JUST_OK_NEXT:
            continue
        out.append(Finding(path, line, "banned-word", '"just" as filler'))
        break
    for pat in _NARRATION_RES:
        m = pat.search(text)
        if m:
            out.append(Finding(path, line, "narration", f'"{m.group(0)}"'))
    return out


def _is_structured(t: str) -> bool:
    """List/table/quote rows, headers, field lists ("Args:", "ticket: ..."), and code samples."""
    if t[0] in "-*|>#=:":
        return True
    first = _first_word(t)
    if len(first) > 1 and first.endswith(":"):
        return True
    return first.rstrip(".)").isdigit() or t.startswith(">>>")


def _skip_for_fill(text: str) -> bool:
    """Lines that legitimately break early. Short prose reads as a label or fragment."""
    t = text.strip()
    return not t or len(t) < _MIN_PROSE_LEN or _is_structured(t)


def _skip_as_continuation(text: str) -> bool:
    """A continuation only contributes its first word, so short lines stay eligible here."""
    t = text.strip()
    return not t or _is_structured(t)


def _fence_mask(block: list[_ProseLine]) -> list[bool]:
    """True per line when it is a code-fence delimiter or sits inside an open fence."""
    mask: list[bool] = []
    open_fence = False
    for pl in block:
        delim = pl.text.strip().startswith("```")
        mask.append(open_fence or delim)
        if delim:
            open_fence = not open_fence
    return mask


def _underfill_findings(path: str, block: list[_ProseLine], limit: int) -> list[Finding]:
    fenced = _fence_mask(block)
    for i in range(len(block) - 1):
        cur, nxt = block[i], block[i + 1]
        if fenced[i] or fenced[i + 1]:
            continue
        if _skip_for_fill(cur.text) or _skip_as_continuation(nxt.text):
            continue
        stripped = cur.text.rstrip()
        if stripped and stripped[-1] in ".!?:;":
            continue
        word = _first_word(nxt.text)
        if word and cur.end_col + 1 + len(word) <= limit:
            msg = (
                f"block hand-wrapped at col {cur.end_col}; the next word fits within {limit} "
                f"(fill comment prose to the configured line length)"
            )
            return [Finding(path, cur.line, "under-fill", msg)]
    return []


def _lint_block(path: str, block: list[_ProseLine], limit: int) -> list[Finding]:
    out: list[Finding] = []
    for pl in block:
        out.extend(_check_wording(path, pl.line, pl.text))
        if pl.end_col > limit:
            out.append(Finding(path, pl.line, "long-line", f"col {pl.end_col} > {limit}"))
    out.extend(_underfill_findings(path, block, limit))
    return out


def _python_comment_blocks(
    text: str,
) -> tuple[list[list[_ProseLine]], list[tuple[int, str]]]:
    """Full-line comment blocks, plus trailing comments as bare (line, text) pairs."""
    lines = text.splitlines()
    blocks: list[list[_ProseLine]] = []
    trailing: list[tuple[int, str]] = []
    cur: list[_ProseLine] = []
    prev_line, prev_col = -2, -1
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return [], []
    for tok in tokens:
        if tok.type != tokenize.COMMENT or _is_pragma(tok.string.lstrip("#")):
            continue
        lineno, col = tok.start
        body = tok.string.lstrip("#").lstrip()
        full_line = lines[lineno - 1][:col].strip() == ""
        if not full_line:
            trailing.append((lineno, body))
            continue
        pl = _ProseLine(lineno, body, tok.end[1])
        if lineno == prev_line + 1 and col == prev_col:
            cur.append(pl)
        else:
            if cur:
                blocks.append(cur)
            cur = [pl]
        prev_line, prev_col = lineno, col
    if cur:
        blocks.append(cur)
    return blocks, trailing


def _python_docstring_blocks(text: str) -> list[list[_ProseLine]]:
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    blocks: list[list[_ProseLine]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if not (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            continue
        const = body[0].value
        end_lineno = const.end_lineno or const.lineno
        block: list[_ProseLine] = []
        for n in range(const.lineno, end_lineno + 1):
            if n - 1 >= len(lines):
                break
            raw = lines[n - 1]
            # Slice to the constant's own columns so code sharing the line (a one-line def, a
            # trailing comment after the closing quotes) never enters the scan.
            start = const.col_offset if n == const.lineno else 0
            stop = const.end_col_offset if n == end_lineno and const.end_col_offset else len(raw)
            span = raw[start:stop]
            block.append(_ProseLine(n, span.strip(" \t\"'"), start + len(span.rstrip())))
        if block:
            blocks.append(block)
    return blocks


def _lint_python(path: str, text: str, limit: int) -> list[Finding]:
    out: list[Finding] = []
    blocks, trailing = _python_comment_blocks(text)
    for block in blocks:
        out.extend(_lint_block(path, block, limit))
    for lineno, body in trailing:
        out.extend(_check_wording(path, lineno, body))
    for block in _python_docstring_blocks(text):
        out.extend(_lint_block(path, block, limit))
    return out


def _lint_markdown(path: str, text: str) -> list[Finding]:
    """Em-dash check only, outside fenced code blocks.

    Markdown is documentation prose, so the banned-word / narration list and the width checks (the
    code-comment bar) stay off it; the em-dash is the one signal that transfers. Fenced blocks are
    skipped so an em-dash inside an emitted template string (a DECISION / defer / commit heredoc) is
    not flagged.
    """
    out: list[Finding] = []
    open_fence = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if raw.strip().startswith("```"):
            open_fence = not open_fence
            continue
        if open_fence:
            continue
        if _EM_DASH in raw:
            out.append(Finding(path, lineno, "em-dash", "em-dash in markdown prose"))
    return out


def _lint_generic(path: str, text: str, limit: int, marker: str) -> list[Finding]:
    out: list[Finding] = []
    cur: list[_ProseLine] = []
    prev_line, prev_indent = -2, -1
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.lstrip()
        if not stripped.startswith(marker) or _is_pragma(stripped[len(marker) :]):
            if cur:
                out.extend(_lint_block(path, cur, limit))
                cur = []
            continue
        indent = len(raw) - len(stripped)
        pl = _ProseLine(lineno, stripped[len(marker) :].lstrip(), len(raw.rstrip()))
        if lineno == prev_line + 1 and indent == prev_indent:
            cur.append(pl)
        else:
            if cur:
                out.extend(_lint_block(path, cur, limit))
            cur = [pl]
        prev_line, prev_indent = lineno, indent
    if cur:
        out.extend(_lint_block(path, cur, limit))
    return out


@cache
def _limit_for_dir(directory: Path) -> int | None:
    pyproject = directory / "pyproject.toml"
    if pyproject.is_file():
        try:
            tool = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {})
        except (ValueError, OSError):
            tool = {}
        for section in ("ruff", "black"):
            if section in tool:
                value = tool[section].get("line-length", _DEFAULT_LIMIT)
                return value if isinstance(value, int) else _DEFAULT_LIMIT
    for name in ("ruff.toml", ".ruff.toml"):
        cfg = directory / name
        if cfg.is_file():
            try:
                value = tomllib.loads(cfg.read_text(encoding="utf-8")).get("line-length")
            except (ValueError, OSError):
                value = None
            return value if isinstance(value, int) else _DEFAULT_LIMIT
    editorconfig = directory / ".editorconfig"
    if editorconfig.is_file():
        try:
            m = re.search(
                r"^\s*max_line_length\s*=\s*(\d+)",
                editorconfig.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
        except (ValueError, OSError):
            m = None
        if m:
            return int(m.group(1))
    return None


def discover_line_length(path: Path) -> int:
    """Nearest declared line length above `path`, walking up through the repo root."""
    resolved = path.resolve()
    for directory in (resolved.parent, *resolved.parent.parents):
        limit = _limit_for_dir(directory)
        if limit is not None:
            return limit
        if (directory / ".git").exists():
            break
    return _DEFAULT_LIMIT


def _changed_lines(path: Path, base: str) -> set[int] | None:
    """New-side line numbers changed vs `base` (working tree included); None = no usable diff."""
    try:
        cp = subprocess.run(
            # --no-ext-diff pins the parseable unified format even when the user's gitconfig
            # routes diffs through an external tool (delta, difftastic).
            ["git", "diff", "--no-ext-diff", "-U0", base, "--", path.name],
            cwd=str(path.parent),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    changed: set[int] = set()
    for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", cp.stdout, re.MULTILINE):
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        changed.update(range(start, start + count))
    return changed


def lint_file(path: Path, line_length: int = 0) -> list[Finding]:
    """Findings for one file; [] for unknown extensions.

    Raises OSError or UnicodeDecodeError when the file itself is unreadable; an unreadable config
    file up the tree only degrades discovery to the default limit.
    """
    text = path.read_text(encoding="utf-8")
    limit = line_length if line_length > 0 else discover_line_length(path)
    name = str(path)
    if path.suffix == ".py":
        return _lint_python(name, text, limit)
    if path.suffix in _MARKDOWN_SUFFIXES:
        return _lint_markdown(name, text)
    marker = _EXT_MARKER.get(path.suffix)
    if marker is None:
        return []
    return _lint_generic(name, text, limit, marker)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deterministic comment/docstring quality floor (the stage-implement bar)."
    )
    parser.add_argument("files", nargs="+", help="files to lint; unknown extensions are skipped.")
    parser.add_argument(
        "--line-length",
        type=int,
        default=0,
        help="max line length; 0 (default) auto-discovers from ruff/black/.editorconfig.",
    )
    parser.add_argument("--json", action="store_true", help="emit findings as a JSON array.")
    parser.add_argument(
        "--diff-base",
        default=None,
        help="git ref; keep only findings on lines changed vs it (working tree included), so "
        "pre-existing comments in a legacy file do not flood the gate.",
    )
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    findings: list[Finding] = []
    for name in args.files:
        path = Path(name)
        try:
            file_findings = lint_file(path, args.line_length)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            sys.stderr.write(f"lint-comments: skipped {name}: {exc}\n")
            continue
        if args.diff_base and file_findings:
            changed = _changed_lines(path, args.diff_base)
            if changed is None:
                sys.stderr.write(
                    f"lint-comments: no usable diff vs {args.diff_base} for {name}; "
                    f"keeping all findings\n"
                )
            else:
                file_findings = [f for f in file_findings if f.line in changed]
        findings.extend(file_findings)
    findings.sort(key=lambda f: (f.path, f.line, f.category))
    if args.json:
        sys.stdout.write(json.dumps([asdict(f) for f in findings], indent=1) + "\n")
    else:
        for f in findings:
            sys.stdout.write(f"{f.path}:{f.line}: {f.category}: {f.message}\n")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["Finding", "cli_main", "discover_line_length", "lint_file"]
