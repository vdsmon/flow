"""Prose-to-runtime seam checker for Flow.

Parses workspace-facade recipes and the narrow direct-bootstrap calls from SKILL.md plus
references/*.md. It validates them against flowctl's allowlist and each script's real argparse
surface, checks the generated managed AGENTS block, and retains the existing
module/registry/descriptor drift gates. Facade command shape is strict: missing or misspelled
subcommands and misplaced flags fail.

Run from anywhere:
    python3 seam_check.py            # check the live SKILL.md + references/
    python3 seam_check.py --verbose  # also print every invocation it resolved

Exit 0 = every contract resolves. Exit 1 = at least one ERROR. WARN lines are
reserved for intentionally partial direct-bootstrap prose and do not fail.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import agent_routes
import flowctl

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_DIR.parent
_COGNITIVE_WORKER_DESIGN_DIGEST = "36b2007e88e43cd99b6c1b3a99b7a4102ff6f099a9525f6a58854b01407f3a85"

# A direct script reference inside prose, using a legacy child environment alias or the
# harness-neutral loaded-root placeholder.
# Char class is [a-z0-9_]+ (NOT [a-z_]+): omitting the digit silently skips a
# digit-bearing basename like embedder_model2vec.py, same lesson _MODULE_NAME_RE
# and _STAGE_DOC_RE carry.
_SCRIPT_RE = re.compile(
    r"(?P<direct_quote>[\"'])?"
    r"(?:\$\{(?:CLAUDE_SKILL_DIR|FLOW_SKILL_DIR)\}|<skill-root>)/scripts/"
    r"(?P<script>[a-z0-9_]+\.py)(?P=direct_quote)?"
)
# The canonical post-init command form is the bound ``<facade>`` placeholder or an absolute
# ``.../.flow/runtime/flow`` path. Relative paths remain parseable so the rooted-context gate can
# reject them explicitly instead of silently omitting their CLI surface from validation.
_FACADE_RE = re.compile(
    r"""
    (?<![A-Za-z0-9_])
    (?:
        (?P<quote>["'])(?P<quoted_path><facade>|[^"'\n]*\.flow/runtime/flow)(?P=quote)
        |
        (?P<bare_path><facade>|(?:[^\s"'`|;&()]+/)?(?:\./)?\.flow/runtime/flow)
    )
    (?:\s+(?P<command>[^\s`|;&()]+))?
    """,
    re.VERBOSE,
)
_CALL_LOCAL_HARNESS_RE = re.compile(
    r"FLOW_HARNESS\s*=\s*(?:[\"']?(?:<harness>|codex|claude-code|generic)[\"']?)\s*$"
)
# A command-like bare script citation becomes an invocation only when the next token is a real
# subcommand of that script or a leading long option. This avoids treating narrative ``foo.py owns
# ...`` prose as executable while catching post-init escapes such as ``recover.py retry --stage
# implement``.
# A slash immediately before the basename excludes intentional ``scripts/foo.py`` source-path
# citations and the direct-bootstrap parser's rooted paths.
_BARE_SCRIPT_CANDIDATE_RE = re.compile(
    r"(?<![/A-Za-z0-9_])(?P<script>[a-z0-9_]+\.py)\s+"
    r"(?P<token>--[A-Za-z][A-Za-z0-9-]*|[a-z][a-z0-9-]*)"
)
_DIRECT_BOOTSTRAP_ALLOWLIST = frozenset({"init.py", "flow_launcher.py", "public_commands_cli.py"})
# A long-option token. Matches the flag name and stops before `=`.
_FLAG_RE = re.compile(r"--[a-zA-Z][a-zA-Z0-9-]*")
# Quoted strings and command substitutions hold VALUES, not flags of this
# command (e.g. `--base "$(git rev-parse --abbrev-ref HEAD)"`). Strip them
# before pulling flag names so a nested command's flag is not misattributed.
_VALUE_SPAN_RE = re.compile(r"\"[^\"]*\"|'[^']*'|`[^`]*`|\$\([^)]*\)")
# argparse renders the subparser positional as `{a,b,c} ...` (trailing `...`),
# which distinguishes it from a flag's value choices like `--status {a,b}`.
# The subparser group may sit after top-level optionals, so anchor on the `...`.
_USAGE_SUBCMD_RE = re.compile(r"\{([^}]+)\}\s*\.\.\.")
# Tokens that are obviously argument VALUES / placeholders, never subcommands.
_PLACEHOLDER_RE = re.compile(r"""^[<"'$.]|^-""")
# An argparse option declaration whose long flag consumes a value. Restrict the metavar to
# uppercase/braced shapes so the first word of help prose is not mistaken for a metavar (``--help
# show ...``).
_VALUE_OPTION_RE = re.compile(
    r"^\s*(?:-[A-Za-z0-9],\s*)?(--[A-Za-z][A-Za-z0-9-]*)"
    r"(?:[ =]+(\{[^}]+\}|[A-Z][A-Z0-9_=-]*))?"
)
_MASKED_VALUE = "__FLOW_ARGUMENT_VALUE__"

_AGENTS_STANZA_NAME = "_AGENTS_STANZA"
_AGENTS_BEGIN_MARKER = "<!-- flow:begin -->"
_AGENTS_END_MARKER = "<!-- flow:end -->"

# Sentinel flags that one script detects in raw argv and forwards (minus the
# sentinel) to another script's CLI. recall.py --metric <...> dispatches to
# metric.cli_main, so the trailing flags are metric.py's surface, not recall's.
_FORWARDERS = {("recall.py", "--metric"): "metric.py"}

# A bare script name as it appears in MODULE.md backticks/prose (no path prefix).
# Char class is [a-z0-9_]+ (NOT [a-z_]+): omitting the digit silently misses a
# digit-bearing basename like embedder_model2vec.py (the model2vec `2`), same
# lesson _STAGE_DOC_RE already carries for stage-e2e.md.
_MODULE_NAME_RE = re.compile(r"[a-z0-9_]+\.py")

# A script basename inside a stage-registry.toml [[stage]].description. Allows
# hyphens and uppercase so a stale hyphenated reference (compose-commit.py for
# the real compose_commit.py) is matched literally and flagged, not normalized
# away. Do NOT reuse `[a-z_]+\.py` here. It cannot match a hyphenated drift.
_REGISTRY_SCRIPT_RE = re.compile(r"[A-Za-z0-9_-]+\.py")

# A stage-doc basename, e.g. references/stage-e2e.md. The char class is
# [a-z0-9_]+ (NOT [a-z_]+): omitting the digit silently misses stage-e2e.md and
# makes the registry parse return 9 docs not 10. ONE regex serves both the
# registry-side parse and the doc-side citation scan so they cannot diverge.
_STAGE_DOC_RE = re.compile(r"stage-[a-z0-9_]+\.md")

# Live-corpus max distinct stage-doc citations per doc = 2 (SKILL.md,
# delivery-plan.md, command-maintain.md); 3 = max+1 tripwire. A doc citing 3+ distinct
# registry stage-docs is a static re-enumeration of the registry mapping (the
# flow-0n8 regression class). Fire condition is count >= limit.
STAGE_DOC_CITATION_LIMIT = 3

# A dotted descriptor field read in prose, e.g. `descriptor.roles`. The do-loop
# names the dispatch payload `descriptor`, so `descriptor.<field>` is an
# unambiguous read of one emitted key.
_DESCRIPTOR_FIELD_RE = re.compile(r"descriptor\.([a-z_][a-z0-9_]*)")
# A JSON object key in prose, `"key":` (the trailing colon distinguishes a key
# from a quoted value like `"<stage>"`). Scoped to descriptor branch spans only.
_JSON_KEY_RE = re.compile(r"\"([a-z_][a-z0-9_]*)\"\s*:")
# The do-loop enumerates the handler descriptor inline: "handler descriptor with
# `stage`, `handler_type`, ...". Backticked tokens after this phrase are keys.
_DESCRIPTOR_ENUM_ANCHOR = "descriptor with"
# A backticked lower_snake token (an enumerated descriptor key in prose).
_BACKTICK_IDENT_RE = re.compile(r"`([a-z_][a-z0-9_]*)`")
# The roles-membership idiom: `descriptor.roles` includes `"records_diff_baseline"`.
# The quoted token after the anchor is a role literal whose only validator is the
# registry roles array (SKILL.md prose, no argparse surface).
_ROLE_MEMBERSHIP_RE = re.compile(r"roles[`\s]*(?:includes?|contains?|has)\b")
# quotes-only: a later backticked prose token must not over-capture as a role
_ROLE_LITERAL_RE = re.compile(r"[\"']+([a-z][a-z0-9_]+)[\"']+")
# An inline-code span: text between a pair of backticks on one line.
_INLINE_SPAN_RE = re.compile(r"`([^`]*)`")
# A whitespace-delimited token inside an executable recipe span (see _executable_recipe_spans). Only
# a glued-suffix corruption of the runtime-directory substring (extra text pasted directly after
# `.flow/runtime` with no separator, e.g. `.flow/runtimeFLOW`) is flagged; legitimate sibling
# runtime files (`skill-root`, `memory-root`, `layout-version`) and bare directory mentions keep a
# separator right after the substring and are never flagged.
_RECIPE_TOKEN_RE = re.compile(r"\S+")
_RUNTIME_SUBSTRING = ".flow/runtime"
# Characters that may legitimately follow `_RUNTIME_SUBSTRING` inside a token: a path separator (a
# sibling runtime file or a subdirectory), a closing quote/paren, or nothing (end of token).
# Anything else means text was glued directly onto the runtime-dir substring with no separator, the
# flow-lhhn corruption shape (`.flow/runtimeFLOW`).
_RUNTIME_SUBSTRING_SEPARATORS = frozenset({"/", '"', "'", ")"})
# A fenced-code block delimiter (``` or ~~~), ignoring leading whitespace.
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# Reusable docs use logical FLOW. Host-specific invocations belong only at the
# conversation boundary, never in stage/command recipes.
_HOST_PUBLIC_RE = re.compile(r"^(?:/flow|\$flow:flow)\s+\S")


@dataclass(frozen=True)
class Surface:
    """The real CLI surface of one script, read from its `--help` output."""

    subcommands: frozenset[str]
    global_flags: frozenset[str]
    sub_flags: dict[str, frozenset[str]]
    # Long flags rendered before the `{subcommands} ...` positional in the top-level USAGE block
    # only (never the full help text, which over-captures flags merely mentioned in a docstring).
    # This is the ownership evidence for the post-subcommand parent-flag position check; do not use
    # `global_flags` for that purpose.
    parent_usage_flags: frozenset[str] = frozenset()

    def all_sub_flags(self) -> frozenset[str]:
        out: set[str] = set()
        for fl in self.sub_flags.values():
            out |= fl
        return frozenset(out)


@dataclass
class Invocation:
    doc: str
    line: int
    script: str
    subcommand: str | None
    flags: list[str]
    raw: str
    facade_command: str | None = None
    # Arguments after the facade command, with quoted / command-substitution values masked. Only
    # facade invocations populate this structural argv.
    argv: tuple[str, ...] | None = None
    facade_path: str | None = None
    call_local_harness: bool = False


@dataclass
class Problem:
    doc: str
    line: int
    level: str  # "ERROR" | "WARN"
    msg: str
    raw: str


# --- prose parsing -----------------------------------------------------------


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash-continued lines. Returns (1-based start line, joined text)."""
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0
    for i, line in enumerate(text.splitlines(), start=1):
        if not buf:
            start = i
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf.append(stripped[:-1])
            continue
        buf.append(line)
        out.append((start, " ".join(p.strip() for p in buf)))
        buf = []
    if buf:
        out.append((start, " ".join(p.strip() for p in buf)))
    return out


def _clean(token: str) -> str:
    return token.strip().strip("[](){}\"'`,")


def find_invocations(doc_name: str, text: str) -> list[Invocation]:
    invs: list[Invocation] = []
    for lineno, logical in _logical_lines(text):
        matches = list(_SCRIPT_RE.finditer(logical))
        # Every match on the logical line yields an Invocation (a `&&`-joined recipe names two
        # commands, both must lint). Each invocation's args span runs to the next match's start,
        # then is truncated at a shell sequencing operator so a second command's flags are not
        # attributed to this script.
        for i, m in enumerate(matches):
            script = m.group("script")
            end = matches[i + 1].start() if i + 1 < len(matches) else len(logical)
            args = logical[m.end() : end]
            # Strip quoted value-spans FIRST so a sequencing char inside a quoted
            # value (e.g. `--text "a; b"`) does not truncate the span mid-way and
            # leak inner --flags. Unquoted operators survive the sub.
            args = _VALUE_SPAN_RE.sub(" ", args)
            for sep in ("&&", "||", "|", ";"):
                idx = args.find(sep)
                if idx != -1:
                    args = args[:idx]
            flags = _FLAG_RE.findall(args)
            invs.append(
                Invocation(
                    doc=doc_name,
                    line=lineno,
                    script=script,
                    subcommand=None,  # resolved later, once the surface is known
                    flags=flags,
                    raw=logical[m.start() : end].strip(),
                )
            )
    return invs


def find_facade_invocations(doc_name: str, text: str) -> list[Invocation]:
    """Parse workspace facade calls through flowctl's allowlist.

    Keep a structural argv rather than searching the whole prose span for a known subcommand token.
    Otherwise ``dispatch typo --stage next`` falsely resolves as ``next`` because the option value
    happens to be a valid token.
    """
    invs: list[Invocation] = []
    for lineno, logical in _logical_lines(text):
        matches = list(_FACADE_RE.finditer(logical))
        for index, match in enumerate(matches):
            command = match.group("command") or ""
            if not command:
                # A prose citation such as "the `.flow/runtime/flow` executable" is not a recipe. A
                # line whose entire executable content is the facade is, and must fail as a
                # missing-command shape.
                prefix = logical[: match.start()].strip()
                suffix = logical[match.end() :].strip()
                if prefix or suffix:
                    continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(logical)
            if logical[: match.start()].count("`") % 2 == 1:
                closing_tick = logical.find("`", match.end())
                if closing_tick != -1:
                    end = min(end, closing_tick)
            command_end = match.end("command") if command else match.end()
            raw_args = logical[command_end:end]
            args = _VALUE_SPAN_RE.sub(f" {_MASKED_VALUE} ", raw_args)
            for separator in ("&&", "||", "|", ";"):
                operator = args.find(separator)
                if operator != -1:
                    args = args[:operator]
            try:
                argv = tuple(shlex.split(args, posix=True))
            except ValueError:
                # Malformed illustrative shell still gets linted. Validation will report its
                # structural command problem where applicable.
                argv = tuple(args.split())
            script = flowctl.COMMANDS.get(command, "")
            facade_path = match.group("quoted_path") or match.group("bare_path")
            prefix = logical[: match.start()]
            invs.append(
                Invocation(
                    doc=doc_name,
                    line=lineno,
                    script=script,
                    subcommand=None,
                    flags=_FLAG_RE.findall(args),
                    raw=logical[match.start() : end].strip(),
                    facade_command=command,
                    argv=argv,
                    facade_path=facade_path,
                    call_local_harness=_CALL_LOCAL_HARNESS_RE.search(prefix) is not None,
                )
            )
    return invs


def facade_context_problems(invocations: list[Invocation]) -> list[Problem]:
    """Reject cwd-dependent or adapter-ambiguous post-init recipes."""

    problems: list[Problem] = []
    for invocation in invocations:
        path = invocation.facade_path or ""
        rooted = path == "<facade>" or path.startswith(("/", "<run_root>/"))
        if not rooted:
            problems.append(
                Problem(
                    doc=invocation.doc,
                    line=invocation.line,
                    level="ERROR",
                    msg=(
                        "facade invocation is workspace-relative; use the absolute <facade> binding"
                    ),
                    raw=invocation.raw,
                )
            )
        if not invocation.call_local_harness:
            problems.append(
                Problem(
                    doc=invocation.doc,
                    line=invocation.line,
                    level="ERROR",
                    msg="facade invocation is missing call-local FLOW_HARNESS=<harness>",
                    raw=invocation.raw,
                )
            )
    return problems


def find_bare_script_invocations(doc_name: str, text: str) -> list[Invocation]:
    """Parse executable bare ``*.py`` recipes that bypass the facade.

    Fenced lines are commands by construction. In ordinary prose, scope the check to an inline-code
    span that carries at least one long option; a short citation such as ``flow_worktree.py create``
    names an implementation surface but is not an executable recipe. Keeping the span boundary also
    prevents flags from a later code span on the same line leaking into this invocation.
    """
    invocations: list[Invocation] = []
    spans: list[tuple[int, str, bool]] = []
    in_fence = False
    shell_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            if not in_fence:
                marker = _FENCE_RE.match(line)
                assert marker is not None
                language = line[marker.end() :].strip().lower()
                shell_fence = language in {"bash", "sh", "shell", "zsh"}
                in_fence = True
            else:
                in_fence = False
                shell_fence = False
            continue
        if in_fence and shell_fence:
            spans.append((lineno, line.strip(), True))
        elif not in_fence:
            spans.extend(
                (lineno, match.group(1).strip(), False) for match in _INLINE_SPAN_RE.finditer(line)
            )

    for lineno, span, fenced in spans:
        matches = list(_BARE_SCRIPT_CANDIDATE_RE.finditer(span))
        for index, match in enumerate(matches):
            script = match.group("script")
            token = match.group("token")
            end = matches[index + 1].start() if index + 1 < len(matches) else len(span)
            raw = span[match.start() : end].strip()
            if not fenced and _FLAG_RE.search(raw) is None:
                continue
            surface = surface_of(script)
            if not token.startswith("--") and (surface is None or token not in surface.subcommands):
                continue
            args = span[match.end("script") : end]
            args = _VALUE_SPAN_RE.sub(" ", args)
            for separator in ("&&", "||", "|", ";"):
                operator = args.find(separator)
                if operator != -1:
                    args = args[:operator]
            invocations.append(
                Invocation(
                    doc=doc_name,
                    line=lineno,
                    script=script,
                    subcommand=token
                    if surface is not None and token in surface.subcommands
                    else None,
                    flags=_FLAG_RE.findall(args),
                    raw=raw,
                )
            )
    return invocations


def _executable_recipe_spans(text: str) -> list[tuple[int, str]]:
    """Lines this checker treats as executable shell recipes.

    A fenced ``bash``/``sh``/``shell``/``zsh`` line is a recipe by construction, the same convention
    `find_bare_script_invocations` uses. An inline-code span outside any fence counts only when it
    carries a long option, which keeps a bare citation like ``<run_root>/.flow/runtime/flow`` and a
    non-shell text-layout fence out of the executable set.
    """
    spans: list[tuple[int, str]] = []
    in_fence = False
    shell_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            if not in_fence:
                marker = _FENCE_RE.match(line)
                assert marker is not None
                language = line[marker.end() :].strip().lower()
                shell_fence = language in {"bash", "sh", "shell", "zsh"}
                in_fence = True
            else:
                in_fence = False
                shell_fence = False
            continue
        if in_fence and shell_fence:
            spans.append((lineno, line))
        elif not in_fence:
            for match in _INLINE_SPAN_RE.finditer(line):
                span = match.group(1)
                if _FLAG_RE.search(span) is not None:
                    spans.append((lineno, span))
    return spans


def _is_glued_runtime_corruption(token: str) -> bool:
    """Whether `token` glues text directly onto the runtime-dir substring with no separator."""
    start = token.find(_RUNTIME_SUBSTRING)
    if start == -1:
        return False
    end = start + len(_RUNTIME_SUBSTRING)
    if end == len(token):
        return False
    return token[end] not in _RUNTIME_SUBSTRING_SEPARATORS


def malformed_runtime_token_problems(doc_name: str, text: str) -> list[Problem]:
    """ERROR on a glued-suffix runtime-facade corruption in an executable recipe.

    Scoped to `_executable_recipe_spans` so narrative prose and non-shell layout examples never
    enter the scan. A candidate token must both glue text directly onto `.flow/runtime` with no
    separator (`_is_glued_runtime_corruption`, the flow-lhhn shape: `.flow/runtimeFLOW`) and fall
    outside every `_FACADE_RE` match span on the same line. Coverage is per match span, which
    extends past the path to include an optional trailing `command` token; a runtime token separated
    from a valid facade call by a shell operator (`&&`, `||`, `|`, `;`) or by more than one token of
    whitespace still falls outside that span and is checked independently. Legitimate sibling
    runtime files (`skill-root`, `memory-root`, `layout-version`) and bare directory mentions keep a
    separator right after the substring, so `_is_glued_runtime_corruption` never flags them, even
    when no facade match covers them.
    """
    problems: list[Problem] = []
    for lineno, span in _executable_recipe_spans(text):
        covered = [match.span() for match in _FACADE_RE.finditer(span)]
        for token_match in _RECIPE_TOKEN_RE.finditer(span):
            token = token_match.group(0)
            if not _is_glued_runtime_corruption(token):
                continue
            start, end = token_match.span()
            if any(cov_start <= start and end <= cov_end for cov_start, cov_end in covered):
                continue
            problems.append(
                Problem(
                    doc=doc_name,
                    line=lineno,
                    level="ERROR",
                    msg=f"malformed runtime facade token (text glued onto .flow/runtime): {token}",
                    raw=span.strip(),
                )
            )
    return problems


def stale_direct_invocation_problems(invocations: list[Invocation]) -> list[Problem]:
    """Reject direct skill-script calls outside bootstrap and public routing."""
    return [
        Problem(
            doc=inv.doc,
            line=inv.line,
            level="ERROR",
            msg=(
                f"stale direct script invocation: {inv.script}; use the absolute <facade> binding "
                "outside setup, launcher repair, and public routing"
            ),
            raw=inv.raw,
        )
        for inv in invocations
        if inv.script not in _DIRECT_BOOTSTRAP_ALLOWLIST
    ]


def _slash_spans(text: str) -> list[tuple[int, str]]:
    """Yield (1-based line, span-content) for each inline-code span and each
    fenced-code line. Spans are extracted independently so two adjacent backtick
    spans on one line never merge (e.g. `FLOW workspace repair <KEY>` then `retry --stage
    ticket` stay separate)."""
    spans: list[tuple[int, str]] = []
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            spans.append((lineno, line.strip()))
        else:
            spans.extend((lineno, m.group(1).strip()) for m in _INLINE_SPAN_RE.finditer(line))
    return spans


def host_specific_invocation_problems(doc_name: str, text: str) -> list[Problem]:
    """Reject host-rendered public recipes from reusable skill references."""

    return [
        Problem(
            doc=doc_name,
            line=lineno,
            level="ERROR",
            msg="host-specific public invocation in reusable prose; use logical FLOW",
            raw=span,
        )
        for lineno, span in _slash_spans(text)
        if _HOST_PUBLIC_RE.match(span)
    ]


# --- script introspection ----------------------------------------------------


def _run_help(script: Path, sub: str | None) -> str | None:
    argv = [sys.executable, str(script)]
    if sub:
        argv.append(sub)
    argv.append("--help")
    # argparse colorizes --help on 3.14+ (PYTHON_COLORS/FORCE_COLOR win over a piped
    # stdout) and ANSI defeats _VALUE_OPTION_RE, so probes force a plain-text child
    # (flow-nmnb).
    env = os.environ.copy()
    for var in ("FORCE_COLOR", "PYTHON_COLORS", "CLICOLOR_FORCE", "CLICOLOR"):
        env.pop(var, None)
    env["NO_COLOR"] = "1"
    try:
        cp = subprocess.run(
            argv,
            cwd=str(SCRIPTS_DIR),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return cp.stdout or None


def _usage_block(help_text: str) -> str:
    """The argparse `usage:` line plus its indented wrap-continuation lines, joined.

    Bounded at the first blank line, which always precedes the description/positional-arguments
    sections. Callers additionally slice at the `{subcommands} ...` marker to keep the subcommand
    list and anything past it (including a script's docstring prose) out of parent-flag ownership
    derivation; this function only trims the usage block itself, capturing wrap-continuation lines
    that a plain first-N-lines join would miss.
    """
    lines = help_text.splitlines()
    if not lines or not lines[0].startswith("usage:"):
        return ""
    block = [lines[0]]
    for line in lines[1:]:
        if not line.strip() or not line[:1].isspace():
            break
        block.append(line)
    return " ".join(block)


@cache
def surface_of(script_name: str) -> Surface | None:
    script = SCRIPTS_DIR / script_name
    if not script.is_file():
        return None
    top = _run_help(script, None)
    if top is None:
        return None
    subs: frozenset[str] = frozenset()
    parent_usage_flags: frozenset[str] = frozenset()
    usage_block = _usage_block(top)
    um = _USAGE_SUBCMD_RE.search(usage_block)
    if um:
        subs = frozenset(s.strip() for s in um.group(1).split(",") if s.strip())
        parent_usage_flags = frozenset(_FLAG_RE.findall(usage_block[: um.start()]))
    global_flags = frozenset(_FLAG_RE.findall(top))
    sub_flags: dict[str, frozenset[str]] = {}
    for s in subs:
        sh = _run_help(script, s)
        sub_flags[s] = frozenset(_FLAG_RE.findall(sh)) if sh else frozenset()
    return Surface(
        subcommands=subs,
        global_flags=global_flags,
        sub_flags=sub_flags,
        parent_usage_flags=parent_usage_flags,
    )


@cache
def value_flags_of(script_name: str, subcommand: str | None = None) -> frozenset[str]:
    """Long flags that consume the following token on one argparse surface."""
    help_text = _run_help(SCRIPTS_DIR / script_name, subcommand)
    if help_text is None:
        return frozenset()
    flags: set[str] = set()
    for line in help_text.splitlines():
        match = _VALUE_OPTION_RE.match(line)
        if match is not None and match.group(2) is not None:
            flags.add(match.group(1))
    return frozenset(flags)


def _facade_shape(inv: Invocation, surface: Surface) -> tuple[str | None, list[str], list[str]]:
    """Return the first positional subcommand, flags before it, and flags after it.

    Global options may legally precede a subcommand. Their values are skipped using the real
    argparse help surface. A subcommand-only option in that slot is returned for a placement error
    instead of being allowed to disguise its value as the command token.

    Scanning continues past the subcommand so a parent-parser flag placed after it can be caught by
    the position check. A `--` end-of-options sentinel only takes effect in the phase it appears in:
    in the pre-subcommand phase it consumes the very next token as the candidate and scanning then
    resumes in the post-subcommand phase; in the post-subcommand phase it ends scanning outright, so
    a token after it there is a positional, never a misplaced flag.
    """
    tokens = list(inv.argv or ())
    all_value_flags = set(value_flags_of(inv.script))
    for subcommand in surface.subcommands:
        all_value_flags.update(value_flags_of(inv.script, subcommand))

    before: list[str] = []
    after: list[str] = []
    index = 0
    candidate: str | None = None
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            candidate = _clean(tokens[index]) if index < len(tokens) else None
            index += 1
            break
        if token.startswith("-"):
            flag = token.split("=", 1)[0]
            before.append(flag)
            consumes_next = "=" not in token and flag in all_value_flags
            index += 2 if consumes_next else 1
            continue
        candidate = _clean(token)
        index += 1
        break

    resolved_value_flags = set(value_flags_of(inv.script))
    if candidate is not None:
        resolved_value_flags.update(value_flags_of(inv.script, candidate))

    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            break
        if token.startswith("-"):
            flag = token.split("=", 1)[0]
            after.append(flag)
            consumes_next = "=" not in token and flag in resolved_value_flags
            index += 2 if consumes_next else 1
            continue
        index += 1

    return candidate, before, after


def _resolve_subcommand(inv: Invocation, surface: Surface) -> str | None:
    if not surface.subcommands:
        return None
    if inv.facade_command is not None:
        candidate, _, _ = _facade_shape(inv, surface)
        return candidate if candidate in surface.subcommands else None
    args = inv.raw.split(inv.script, 1)[-1]
    for tok in (_clean(t) for t in args.split()):
        if tok in surface.subcommands:
            return tok
    return None


# --- validation --------------------------------------------------------------


def _validate_facade_shape(inv: Invocation, surface: Surface) -> tuple[str | None, list[Problem]]:
    """Validate the structural subcommand slot of one facade invocation."""
    candidate, before_flags, after_flags = _facade_shape(inv, surface)
    subcommand = candidate if candidate in surface.subcommands else None
    problems: list[Problem] = []
    if candidate is None:
        if "--help" not in before_flags:
            problems.append(
                Problem(
                    inv.doc,
                    inv.line,
                    "ERROR",
                    f"{inv.script}: facade invocation is missing a subcommand",
                    inv.raw,
                )
            )
    elif subcommand is None:
        problems.append(
            Problem(
                inv.doc,
                inv.line,
                "ERROR",
                f"{inv.script}: unknown subcommand {candidate} "
                f"(known: {sorted(surface.subcommands)})",
                inv.raw,
            )
        )
    problems.extend(
        Problem(
            inv.doc,
            inv.line,
            "ERROR",
            f"{inv.script}: flag {flag} is not valid before subcommand",
            inv.raw,
        )
        for flag in before_flags
        if flag not in surface.global_flags and flag != "--help"
    )
    if subcommand is not None:
        # Real argparse rejects a parent-parser flag placed after the subcommand (`unrecognized
        # arguments`). Ownership evidence is the usage-derived parent set, never `global_flags`
        # (which over-captures docstring mentions). Skip when the subcommand's own probe came back
        # empty: an unknown/degenerate sub-surface would otherwise make every parent option look
        # parent-only.
        sub_flags = surface.sub_flags.get(subcommand, frozenset())
        if sub_flags:
            parent_only = surface.parent_usage_flags - sub_flags
            problems.extend(
                Problem(
                    inv.doc,
                    inv.line,
                    "ERROR",
                    f"{inv.script}: flag {flag} is a parent-parser option and must precede "
                    f"{subcommand}, not follow it",
                    inv.raw,
                )
                for flag in after_flags
                if flag in parent_only
            )
    return subcommand, problems


def validate(inv: Invocation) -> list[Problem]:
    if inv.facade_command == "":
        return [
            Problem(
                inv.doc,
                inv.line,
                "ERROR",
                "facade invocation is missing a command",
                inv.raw,
            )
        ]
    if inv.facade_command is not None and not inv.script:
        return [
            Problem(
                inv.doc,
                inv.line,
                "ERROR",
                f"facade command is not allowlisted: {inv.facade_command}",
                inv.raw,
            )
        ]
    surface = surface_of(inv.script)
    if surface is None:
        return [
            Problem(
                inv.doc,
                inv.line,
                "ERROR",
                f"script not found / not runnable: {inv.script}",
                inv.raw,
            )
        ]
    problems: list[Problem] = []
    if inv.facade_command is not None and surface.subcommands:
        sub, shape_problems = _validate_facade_shape(inv, surface)
        problems.extend(shape_problems)
    else:
        sub = _resolve_subcommand(inv, surface)
    inv.subcommand = sub

    # A script with subcommands invoked without one we recognize: WARN only
    # (prose sometimes shows a partial / illustrative call).
    if surface.subcommands and sub is None and inv.facade_command is None:
        barewords = [
            _clean(t)
            for t in inv.raw.split(inv.script, 1)[-1].split()
            if _clean(t) and not _PLACEHOLDER_RE.match(_clean(t))
        ]
        unknown = [b for b in barewords if b not in surface.subcommands]
        if unknown:
            problems.append(
                Problem(
                    inv.doc,
                    inv.line,
                    "WARN",
                    f"{inv.script}: no recognized subcommand "
                    f"(saw {unknown[:3]}; known: {sorted(surface.subcommands)})",
                    inv.raw,
                )
            )

    known_any = surface.global_flags | surface.all_sub_flags() | {"--help"}
    known_strict = surface.global_flags | {"--help"}
    if sub is not None:
        known_strict |= surface.sub_flags.get(sub, frozenset())

    # Fold in a forwarded script's surface when its sentinel flag is present.
    for (fscript, sentinel), target in _FORWARDERS.items():
        if inv.script == fscript and sentinel in inv.flags:
            tsurface = surface_of(target)
            extra = {sentinel}
            if tsurface is not None:
                extra |= tsurface.global_flags | tsurface.all_sub_flags()
            known_any |= extra
            known_strict |= extra

    for flag in inv.flags:
        if flag not in known_any:
            problems.append(
                Problem(
                    inv.doc,
                    inv.line,
                    "ERROR",
                    f"{inv.script}{' ' + sub if sub else ''}: unknown flag {flag}",
                    inv.raw,
                )
            )
        elif sub is not None and flag not in known_strict:
            problems.append(
                Problem(
                    inv.doc,
                    inv.line,
                    "ERROR" if inv.facade_command is not None else "WARN",
                    f"{inv.script} {sub}: flag {flag} valid elsewhere but not for this subcommand",
                    inv.raw,
                )
            )
    return problems


def _literal_assignment(source: str, name: str) -> str | None:
    """Read one top-level literal string assignment without importing its module."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id != name:
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    return None


def managed_agents_guidance_drift(
    init_path: Path = SCRIPTS_DIR / "init.py",
) -> list[str]:
    """Validate the generated managed AGENTS block by stable semantic contracts.

    Incidental comments or prose that merely mention AGENTS.md do not count. The source of truth is
    the literal ``_AGENTS_STANZA`` assignment bracketed by the stable managed markers. Wording may
    evolve while these load/facade/gate/isolation contracts remain true.
    """
    try:
        source = init_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read generated guidance source: {exc}"]
    stanza = _literal_assignment(source, _AGENTS_STANZA_NAME)
    if stanza is None:
        return [f"missing literal {_AGENTS_STANZA_NAME} assignment"]

    if stanza.count(_AGENTS_BEGIN_MARKER) != 1 or stanza.count(_AGENTS_END_MARKER) != 1:
        return ["generated guidance must contain exactly one pair of managed markers"]
    begin = stanza.index(_AGENTS_BEGIN_MARKER) + len(_AGENTS_BEGIN_MARKER)
    end = stanza.index(_AGENTS_END_MARKER)
    if begin > end:
        return ["generated guidance managed markers are out of order"]
    guidance = stanza[begin:end]

    contracts: tuple[tuple[str, bool], ...] = (
        (
            "adapter-supplied absolute installation guidance",
            "FLOW_SKILL_DIR" in guidance
            and "absolute" in guidance.lower()
            and re.search(r"do not search", guidance, re.IGNORECASE) is not None,
        ),
        (
            "router and harness guidance",
            "SKILL.md" in guidance and "references/harness.md" in guidance,
        ),
        (
            "public registry routing",
            "public-commands.toml" in guidance
            and "Static namespaces" in guidance
            and "removed forms stop" in guidance,
        ),
        (
            "call-local harness selector guidance",
            "FLOW_HARNESS" in guidance
            and all(value in guidance for value in ("codex", "claude-code", "generic"))
            and "same" in guidance.lower()
            and "export" in guidance.lower(),
        ),
        (
            "approval-before-coding guidance",
            re.search(r"approv", guidance, re.IGNORECASE) is not None
            and "read-only" in guidance.lower()
            and re.search(r"\bstop\b", guidance, re.IGNORECASE) is not None,
        ),
        (
            "absolute rooted facade guidance",
            ".flow/runtime/flow" in guidance
            and "absolute" in guidance.lower()
            and "run root" in guidance.lower(),
        ),
        (
            "non-persistent call rooting guidance",
            "explicit workdir" in guidance.lower()
            and re.search(r"prior(?: standalone)? `cd`", guidance, re.IGNORECASE) is not None
            and "never persistent" in guidance.lower(),
        ),
        (
            "dirty main-checkout protection",
            "never relocate dirty" in guidance.lower()
            and "provenance" in guidance.lower()
            and "recovery" in guidance.lower(),
        ),
    )
    drift: list[str] = [f"missing {label}" for label, satisfied in contracts if not satisfied]

    direct = find_invocations("generated AGENTS guidance", guidance) + find_bare_script_invocations(
        "generated AGENTS guidance", guidance
    )
    drift.extend(problem.msg for problem in stale_direct_invocation_problems(direct))
    for invocation in find_facade_invocations("generated AGENTS guidance", guidance):
        drift.extend(problem.msg for problem in validate(invocation) if problem.level == "ERROR")
    return drift


# --- driver ------------------------------------------------------------------


def scripts_missing_from_module_md(
    scripts_dir: Path = SCRIPTS_DIR, module_text: str | None = None
) -> set[str]:
    """Non-test *.py files on disk not named anywhere in MODULE.md."""
    if module_text is None:
        module_text = (scripts_dir / "MODULE.md").read_text(encoding="utf-8")
    on_disk = {
        p.name
        for p in scripts_dir.glob("*.py")
        if not p.name.startswith("test") and p.name != "conftest.py"
    }
    named = set(_MODULE_NAME_RE.findall(module_text))
    return on_disk - named


def scripts_missing_from_registry_descriptions(
    registry_path: Path = SKILL_ROOT / "stage-registry.toml",
    scripts_dir: Path = SCRIPTS_DIR,
) -> set[str]:
    """Script basenames named in a [[stage]].description but not on disk.

    Matches the LITERAL basename (hyphens preserved, no normalization) so a
    stale compose-commit.py reference for the real compose_commit.py is caught,
    not masked.
    """
    data = tomllib.loads(registry_path.read_text(encoding="utf-8"))
    named: set[str] = set()
    for stage in data.get("stage", []):
        named |= set(_REGISTRY_SCRIPT_RE.findall(stage.get("description", "")))
    return {name for name in named if not (scripts_dir / name).is_file()}


# A MODULE.md table row's first cell: the script the row documents. Backtick
# optional so a bare-name row is still owned by the check.
_MODULE_ROW_RE = re.compile(r"^\|\s*`?([a-z0-9_]+\.py)`?")
# The forward-direction import claim inside a row ("imports x, y"). \b keeps
# "imported by" rows out (a different word, gated by module_md_importer_drift).
_FORWARD_IMPORTS_RE = re.compile(r"\bimports\s")


def phantom_module_md_rows(
    scripts_dir: Path = SCRIPTS_DIR, module_text: str | None = None
) -> set[str]:
    """MODULE.md rows documenting a script that no longer exists on disk.

    Reverse direction of scripts_missing_from_module_md, which only computes
    on_disk - named: without this, `git rm scripts/<x>.py` leaves a dangling
    live-map row that CI accepts (witnessed twice: validate_postmortem.py,
    queue_reviews.py). Scoped to the row-defining FIRST CELL: a historical
    mention inside a Role cell ("absorbed from queue_reviews.py") is
    deliberate prose, not a row, and stays legal.
    """
    if module_text is None:
        module_text = (scripts_dir / "MODULE.md").read_text(encoding="utf-8")
    phantoms: set[str] = set()
    for line in module_text.splitlines():
        m = _MODULE_ROW_RE.match(line)
        if m is None:
            continue
        name = m.group(1)
        if not (scripts_dir / name).is_file() and not (scripts_dir / "tests" / name).is_file():
            phantoms.add(name)
    return phantoms


def module_md_forward_import_drift(
    scripts_dir: Path = SCRIPTS_DIR, module_text: str | None = None
) -> list[tuple[str, str]]:
    """MODULE.md forward "imports x, y" claims that are not true imports.

    module_md_importer_drift skips these rows by design (its anchor is
    "imported by"), so a stale forward claim had no gate. Same enumerability
    rule: every token after the anchor must resolve to a local stem or the
    claim is treated as prose and skipped. Phantom-only direction: a listed
    module the row's script does not actually import is the drift; an
    undocumented import is normal.
    """
    if module_text is None:
        module_text = (scripts_dir / "MODULE.md").read_text(encoding="utf-8")
    stems = _local_stems(scripts_dir)
    truth = true_importers(scripts_dir)
    drifts: list[tuple[str, str]] = []
    for line in module_text.splitlines():
        row = _MODULE_ROW_RE.match(line)
        if row is None:
            continue
        module_stem = row.group(1)[: -len(".py")]
        m = _FORWARD_IMPORTS_RE.search(line)
        if m is None:
            continue
        cell = line[m.end() :].split("|", 1)[0].split(";", 1)[0]
        tokens: list[str] = []
        for raw_tok in re.split(r"[,+]", cell):
            tok = raw_tok.split("(", 1)[0]
            tok = tok.strip().strip("`").strip()
            tok = tok.removeprefix("the ")
            tok = tok.strip().strip(".").strip()
            tok = tok.removesuffix(".py")
            tok = tok.strip()
            if tok:
                tokens.append(tok)
        if not tokens or any(tok not in stems for tok in tokens):
            continue
        drifts.extend(
            (module_stem, tok) for tok in tokens if module_stem not in truth.get(tok, set())
        )
    return drifts


def triage_guard_files(scripts_dir: Path = SCRIPTS_DIR) -> frozenset[str]:
    """triage._GUARD_FILES parsed from source (AST, no import side effects)."""
    tree = ast.parse((scripts_dir / "triage.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id == "_GUARD_FILES":
                return frozenset(
                    c.value
                    for c in ast.walk(node.value)
                    if isinstance(c, ast.Constant) and isinstance(c.value, str)
                )
    return frozenset()


_GUARD_LIST_ANCHOR = "safety-machinery guard file"
_GUARD_LIST_EXPECTED_DOCS = 1  # stage-reflect.md is the canonical prose enumeration


def guard_file_list_drift(
    docs: list[Path] | None = None, guard_files: frozenset[str] | None = None
) -> list[tuple[str, int, str]]:
    """Prose hot-guard enumerations diverging from triage._GUARD_FILES.

    The canonical prose list stays readable while the runtime set remains code. The
    enumeration is anchored on the literal
    "safety-machinery guard file" phrase followed by a parenthesized list of backticked *.py names;
    that set must equal the *.py members of _GUARD_FILES. Finding fewer than the expected anchored
    enumerations is itself a drift (the phrase moved and the gate would silently check nothing).
    """
    if guard_files is None:
        guard_files = triage_guard_files(SCRIPTS_DIR)
    expected = {name for name in guard_files if name.endswith(".py")}
    if not expected:
        return [("triage.py", 0, "could not parse _GUARD_FILES from triage.py source")]
    if docs is None:
        docs = docs_to_check()
    drifts: list[tuple[str, int, str]] = []
    anchored = 0
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        for lineno, logical in _logical_lines(text):
            idx = logical.find(_GUARD_LIST_ANCHOR)
            if idx == -1:
                continue
            tail = logical[idx + len(_GUARD_LIST_ANCHOR) :]
            paren = tail.find("(")
            close = tail.find(")", paren)
            if paren == -1 or close == -1:
                continue
            listed = set(_MODULE_NAME_RE.findall(tail[paren:close]))
            if not listed:
                continue
            anchored += 1
            missing = expected - listed
            extra = listed - expected
            if missing or extra:
                drifts.append(
                    (
                        doc.name,
                        lineno,
                        f"missing {sorted(missing)}, extra {sorted(extra)}",
                    )
                )
    if anchored < _GUARD_LIST_EXPECTED_DOCS:
        drifts.append(
            (
                "guard-file lists",
                0,
                f"found {anchored} anchored enumeration(s), "
                f"expected >= {_GUARD_LIST_EXPECTED_DOCS}",
            )
        )
    return drifts


def _local_stems(scripts_dir: Path) -> set[str]:
    """Stems of every non-test *.py basename in scripts_dir (the resolvable modules)."""
    return {
        p.stem
        for p in scripts_dir.glob("*.py")
        if not p.name.startswith("test") and p.name != "conftest.py"
    }


def true_importers(scripts_dir: Path = SCRIPTS_DIR) -> dict[str, set[str]]:
    """AST-walk every non-test scripts/*.py and build {imported_stem: {importer_stem}}.

    Walks the whole module body so lazy/in-function imports are credited (e.g.
    tracker imports its adapters inside make_tracker). Only resolvable local
    stems are kept; an import of a module's own stem is dropped.
    """
    stems = _local_stems(scripts_dir)
    out: dict[str, set[str]] = {}
    for path in scripts_dir.glob("*.py"):
        if path.name.startswith("test") or path.name == "conftest.py":
            continue
        importer = path.stem
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            for name in names:
                stem = name.split(".")[0]
                if stem in stems and stem != importer:
                    out.setdefault(stem, set()).add(importer)
    return out


@dataclass(frozen=True)
class ImporterDrift:
    module: str
    missing: frozenset[str]  # in true importers, absent from the row
    phantom: frozenset[str]  # in the row, not a true importer


def module_md_importer_drift(
    scripts_dir: Path = SCRIPTS_DIR, module_text: str | None = None
) -> list[ImporterDrift]:
    """Drift between MODULE.md 'imported by' rows and the AST-computed truth.

    For each line: take the module from the first backticked *.py token, require
    the literal `imported by` anchor (skips reverse-direction `imports x, y`
    rows), parse the importer list after it (split on `,`, `+`, and the
    natural-language word `and`; strip parentheticals/backticks/the/.py). A row
    is enumerable iff every token resolves to a local stem; unresolvable rows
    (prose like `the adapters`) are skipped. Enumerable rows whose parsed set !=
    true set yield one descriptor.
    """
    if module_text is None:
        module_text = (scripts_dir / "MODULE.md").read_text(encoding="utf-8")
    stems = _local_stems(scripts_dir)
    truth = true_importers(scripts_dir)
    drifts: list[ImporterDrift] = []
    for line in module_text.splitlines():
        module_stem: str | None = None
        for span in _INLINE_SPAN_RE.finditer(line):
            mm = _MODULE_NAME_RE.search(span.group(1))
            if mm:
                module_stem = mm.group(0)[: -len(".py")]
                break
        if module_stem is None:
            continue
        anchor = "imported by"
        idx = line.find(anchor)
        if idx == -1:
            continue
        cell = line[idx + len(anchor) :]
        cell = cell.split("|", 1)[0]
        tokens: list[str] = []
        for raw_tok in re.split(r"[,+]|\band\b", cell):
            tok = raw_tok.split("(", 1)[0]
            tok = tok.strip().strip("`").strip()
            tok = tok.removeprefix("the ")
            tok = tok.strip().strip(".").strip()
            tok = tok.removesuffix(".py")
            tok = tok.strip()
            if tok:
                tokens.append(tok)
        if not tokens or any(tok not in stems for tok in tokens):
            continue
        parsed = set(tokens)
        true_set = truth.get(module_stem, set())
        if parsed != true_set:
            drifts.append(
                ImporterDrift(
                    module=module_stem,
                    missing=frozenset(true_set - parsed),
                    phantom=frozenset(parsed - true_set),
                )
            )
    return drifts


@dataclass(frozen=True)
class SurfaceCellDrift:
    module: str
    missing: frozenset[str]  # real subcommands absent from the surface cell


def module_md_surface_cell_drift(
    scripts_dir: Path = SCRIPTS_DIR,
    module_text: str | None = None,
    surface_lookup=surface_of,
) -> list[SurfaceCellDrift]:
    """Drift between a MODULE.md surface cell and a script's real argparse subcommands.

    Per table row (a line containing `|`): the module stem is the first backticked
    *.py token in the first non-empty `|`-cell. `(lib)` rows are skipped (libs are
    documented by their importer list, not a CLI surface). The surface cell is the
    LAST non-empty `|`-cell; a real subcommand counts as enumerated only as a
    standalone token (boundary regex, so `list` does not match inside
    `list-assigned`). A row enumerating ZERO real subcommands is not claiming to be
    a surface listing (e.g. metric.py's forwarded `(via recall.py --metric)`) and is
    skipped. A row enumerating >=1 but not all yields one descriptor.
    """
    if module_text is None:
        module_text = (scripts_dir / "MODULE.md").read_text(encoding="utf-8")
    drifts: list[SurfaceCellDrift] = []
    for line in module_text.splitlines():
        if "|" not in line:
            continue
        cells = [c for c in (c.strip() for c in line.split("|")) if c]
        if not cells:
            continue
        first, last = cells[0], cells[-1]
        if "(lib)" in first:
            continue
        module_stem: str | None = None
        for span in _INLINE_SPAN_RE.finditer(first):
            mm = _MODULE_NAME_RE.search(span.group(1))
            if mm:
                module_stem = mm.group(0)[: -len(".py")]
                break
        if module_stem is None:
            continue
        surface = surface_lookup(module_stem + ".py")
        if surface is None or not surface.subcommands:
            continue
        present = {
            sub
            for sub in surface.subcommands
            if re.search(rf"(?<![A-Za-z0-9-]){re.escape(sub)}(?![A-Za-z0-9-])", last)
        }
        if not present:
            continue
        if present != surface.subcommands:
            drifts.append(
                SurfaceCellDrift(
                    module=module_stem,
                    missing=frozenset(surface.subcommands - present),
                )
            )
    return drifts


def docs_over_stage_doc_citation_limit(
    registry_path: Path = SKILL_ROOT / "stage-registry.toml",
    docs: list[Path] | None = None,
    limit: int = STAGE_DOC_CITATION_LIMIT,
) -> dict[str, int]:
    """Docs that statically re-enumerate the stage->reference_doc map (flow-0n8).

    Parse the registry's reference_doc fields into the set of stage-doc
    basenames it owns, then for each doc count how many DISTINCT registry
    stage-docs it cites (intersection, so a non-registry stage-*.md token can
    not inflate). Returns {doc.name: count} for docs citing >= limit.
    """
    data = tomllib.loads(registry_path.read_text(encoding="utf-8"))
    registry_basenames: set[str] = set()
    for stage in data.get("stage", []):
        registry_basenames |= set(_STAGE_DOC_RE.findall(stage.get("reference_doc", "")))
    if docs is None:
        docs = docs_to_check()
    out: dict[str, int] = {}
    for doc in docs:
        cited = set(_STAGE_DOC_RE.findall(doc.read_text(encoding="utf-8"))) & registry_basenames
        if len(cited) >= limit:
            out[doc.name] = len(cited)
    return out


# --- descriptor-key + role-literal gates (dispatch_stage stdout contract) -----

# The script whose stdout-JSON the do-loop parses by key. seam_check otherwise
# only validates argparse flags/subcommands; the emitted descriptor keys
# (done, blocked_by, stage, head_sha, roles, reference_doc, ...) have no surface
# and so no gate. A key rename there passes argparse + unit tests and breaks
# every run at the SKILL.md descriptor parse.
_DESCRIPTOR_SCRIPT = "dispatch_stage.py"


def emitted_descriptor_keys(script_path: Path | None = None) -> set[str] | None:
    """The set of string keys dispatch_stage.py can emit in its stdout JSON.

    Extracted statically: every string key of every dict literal, PLUS every
    string-constant subscript assignment target (`payload["reference_doc"] = ...`,
    which is not a dict literal). Over-capture is safe and intended. The gate is
    one-directional (a prose-cited key absent from THIS set is the drift), so a
    superset only suppresses false errors, never causes one. Returns None if the
    script is missing or unparseable (the gate then no-ops rather than mass-fail).
    """
    if script_path is None:
        script_path = SCRIPTS_DIR / _DESCRIPTOR_SCRIPT
    try:
        tree = ast.parse(script_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.slice, ast.Constant)
                    and isinstance(tgt.slice.value, str)
                ):
                    keys.add(tgt.slice.value)
    return keys


def prose_descriptor_key_citations(text: str) -> list[tuple[int, str]]:
    """Descriptor keys the prose reads off the dispatch payload, by structural anchor.

    Three anchors, each unambiguous so a foreign JSON key (another script's
    stdout, of which the corpus has many) cannot leak in:
      1. `descriptor.<field>` dotted access.
      2. backticked tokens after the "handler descriptor with" enumeration phrase.
      3. JSON `"key":` pairs inside a `{...}` span whose discriminator key is
         `"done"` (the descriptor's done/blocked branches; no other prose JSON
         object carries a `"done"` key).
    Returns (line, key). Bare-backtick mentions (run_id, holder, finished,
    reconciled_drift) carry no structural anchor and are intentionally not gated.
    """
    out: list[tuple[int, str]] = []
    for lineno, logical in _logical_lines(text):
        out.extend((lineno, m.group(1)) for m in _DESCRIPTOR_FIELD_RE.finditer(logical))
        idx = logical.find(_DESCRIPTOR_ENUM_ANCHOR)
        if idx != -1:
            tail = logical[idx + len(_DESCRIPTOR_ENUM_ANCHOR) :]
            out.extend((lineno, m.group(1)) for m in _BACKTICK_IDENT_RE.finditer(tail))
        for span in re.findall(r"\{[^{}]*\}", logical):
            keys = _JSON_KEY_RE.findall(span)
            if "done" in keys:
                out.extend((lineno, k) for k in keys)
    return out


def descriptor_key_drift(
    docs: list[Path] | None = None, emitted: set[str] | None = None
) -> list[tuple[str, int, str]]:
    """Prose-cited descriptor keys absent from dispatch_stage's emitted set.

    Same missing-only direction as the surface-cell gate: the script's emitted
    keys are truth; a citation that is not among them is stale (a rename). The
    reverse (emitted keys the prose never parses) is normal and not flagged.
    """
    if emitted is None:
        emitted = emitted_descriptor_keys()
    if not emitted:
        return []
    if docs is None:
        docs = docs_to_check()
    drifts: list[tuple[str, int, str]] = []
    for doc in docs:
        for lineno, key in prose_descriptor_key_citations(doc.read_text(encoding="utf-8")):
            if key not in emitted:
                drifts.append((doc.name, lineno, key))
    return drifts


def registry_roles(registry_path: Path = SKILL_ROOT / "stage-registry.toml") -> set[str]:
    """Union of every `roles` array across the registry's stages."""
    data = tomllib.loads(registry_path.read_text(encoding="utf-8"))
    roles: set[str] = set()
    for stage in data.get("stage", []):
        for r in stage.get("roles", []) or []:
            roles.add(str(r))
    return roles


def prose_role_citations(text: str) -> list[tuple[int, str]]:
    """Role literals prose uses to branch on registry metadata.

    Accept only the explicit ``roles ... includes/contains/has`` idiom. Stage names and role
    literals are different descriptor fields, so prose saying ``the stage is `<role>` `` must not
    satisfy this gate. Returns (line, role).
    """
    out: set[tuple[int, str]] = set()
    for lineno, logical in _logical_lines(text):
        found: set[str] = set()
        m = _ROLE_MEMBERSHIP_RE.search(logical)
        if m is not None:
            found.update(lm.group(1) for lm in _ROLE_LITERAL_RE.finditer(logical[m.end() :]))
        out.update((lineno, role) for role in found)
    return sorted(out)


def role_literal_drift(
    docs: list[Path] | None = None, roles: set[str] | None = None
) -> list[tuple[str, int, str]]:
    """Prose role literals absent from the registry roles arrays (a renamed role).

    The role string is round-tripped through the dispatch descriptor untyped, so
    a registry rename leaves the SKILL.md literal stale with no other validator.
    Missing-only: a cited role not in any registry array is the drift.
    """
    if roles is None:
        roles = registry_roles()
    if docs is None:
        docs = docs_to_check()
    drifts: list[tuple[str, int, str]] = []
    for doc in docs:
        for lineno, role in prose_role_citations(doc.read_text(encoding="utf-8")):
            if role not in roles:
                drifts.append((doc.name, lineno, role))
    return drifts


def docs_to_check() -> list[Path]:
    docs = [SKILL_ROOT / "SKILL.md"]
    refs = SKILL_ROOT / "references"
    if refs.is_dir():
        docs.extend(sorted(refs.glob("*.md")))
    return [d for d in docs if d.is_file()]


def _workspace_agents_drift(workspace_toml: str) -> list[str]:
    try:
        data = tomllib.loads(workspace_toml)
    except tomllib.TOMLDecodeError as exc:
        return [f"self-workspace is not valid TOML: {exc}"]
    # Schema validation first: it rejects unknown profiles and incomplete or malformed route tables,
    # so the render below is total (render_route_config would KeyError on a missing field and
    # silently drop an unknown profile).
    schema_problems = agent_routes.configuration_errors(data)
    if schema_problems:
        return [f"self-workspace {problem}" for problem in schema_problems]
    agents = data.get("agents")
    if not isinstance(agents, dict):
        return ["self-workspace has no [agents] configuration"]
    expected = set(agent_routes.PROFILES)
    configured = {str(profile) for profile in agents}
    if configured != expected:
        missing = expected - configured
        extra = configured - expected
        return [f"self-workspace route catalog mismatch; missing {missing}, extra {extra}"]
    if agent_routes.render_route_config(agents) != agent_routes.render_default_routes_toml():
        return ["self-workspace routes do not canonicalize to the rendered defaults"]
    return []


def _inventory_profiles_drift(inventory_text: str) -> list[str]:
    begin = agent_routes.INVENTORY_PROFILES_BEGIN
    end = agent_routes.INVENTORY_PROFILES_END
    if inventory_text.count(begin) != 1 or inventory_text.count(end) != 1:
        return ["inventory route-profile markers must appear exactly once each"]
    start = inventory_text.index(begin)
    stop = inventory_text.index(end)
    if stop < start:
        return ["inventory route-profile end marker precedes its begin marker"]
    block = inventory_text[start : stop + len(end)] + "\n"
    if block != agent_routes.render_inventory_profiles_block():
        return ["inventory route-profile block is stale relative to agent_routes.PROFILES"]
    return []


def route_contract_drift(
    *,
    workspace_toml: str | None = None,
    inventory_text: str | None = None,
) -> list[str]:
    """Check the committed route surfaces against their agent_routes renderers.

    Setup and migration TOML are rendered by agent_routes at runtime, and stage/substep composition
    is pinned by tests/test_agent_routes.py, so the only surfaces that can go stale are the
    committed ones: the inventory.md managed block and the deliberately pinned self-workspace
    [agents] configuration. Check-only, never writes.
    """
    if workspace_toml is None:
        workspace_path = SKILL_ROOT.parents[3] / ".flow" / "workspace.toml"
        if workspace_path.is_file():
            workspace_toml = workspace_path.read_text(encoding="utf-8")
    drift = [] if workspace_toml is None else _workspace_agents_drift(workspace_toml)

    if inventory_text is None:
        inventory_text = (SCRIPTS_DIR / "inventory.md").read_text(encoding="utf-8")
    drift.extend(_inventory_profiles_drift(inventory_text))
    return drift


def cognitive_worker_design_drift(path: Path | None = None) -> list[str]:
    """Require the landed capsule design to match its approved source bytes."""
    design = path or (
        SKILL_ROOT.parents[3]
        / "docs"
        / "specs"
        / "2026-07-14-universal-cognitive-worker-routing-design.md"
    )
    try:
        digest = hashlib.sha256(design.read_bytes()).hexdigest()
    except OSError as exc:
        return [f"cannot read cognitive-worker design: {exc}"]
    if digest != _COGNITIVE_WORKER_DESIGN_DIGEST:
        return [
            "cognitive-worker design digest is "
            f"{digest}, expected {_COGNITIVE_WORKER_DESIGN_DIGEST}"
        ]
    return []


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Validate Flow prose invocations against the public registry and real CLIs."
    )
    ap.add_argument("--verbose", action="store_true", help="print every resolved invocation")
    args = ap.parse_args(argv)

    docs = docs_to_check()
    all_invs: list[Invocation] = []
    problems: list[Problem] = []
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        direct = find_invocations(doc.name, text) + find_bare_script_invocations(doc.name, text)
        facade = find_facade_invocations(doc.name, text)
        all_invs.extend(direct)
        all_invs.extend(facade)
        problems.extend(host_specific_invocation_problems(doc.name, text))
        problems.extend(stale_direct_invocation_problems(direct))
        problems.extend(facade_context_problems(facade))
        problems.extend(malformed_runtime_token_problems(doc.name, text))

    for inv in all_invs:
        problems.extend(validate(inv))

    problems.extend(
        Problem(
            doc="MODULE.md",
            line=0,
            level="ERROR",
            msg=f"script not named in MODULE.md: {name}",
            raw="",
        )
        for name in sorted(scripts_missing_from_module_md())
    )

    problems.extend(
        Problem(
            doc="stage-registry.toml",
            line=0,
            level="ERROR",
            msg=f"description names a script not on disk: {name}",
            raw="",
        )
        for name in sorted(scripts_missing_from_registry_descriptions())
    )

    problems.extend(
        Problem(
            doc="MODULE.md",
            line=0,
            level="ERROR",
            msg=f"row documents a script not on disk: {name}",
            raw="",
        )
        for name in sorted(phantom_module_md_rows())
    )

    problems.extend(
        Problem(
            doc="MODULE.md",
            line=0,
            level="ERROR",
            msg=f"row for {module} claims it imports {imported}, but it does not",
            raw="",
        )
        for module, imported in sorted(module_md_forward_import_drift())
    )

    problems.extend(
        Problem(
            doc=doc_name,
            line=lineno,
            level="ERROR",
            msg=f"guard-file list diverges from triage._GUARD_FILES: {detail}",
            raw="",
        )
        for doc_name, lineno, detail in guard_file_list_drift()
    )

    problems.extend(
        Problem(
            doc="MODULE.md",
            line=0,
            level="ERROR",
            msg=(
                f"MODULE.md 'imported by' row for {drift.module}: "
                f"missing {sorted(drift.missing)}, phantom {sorted(drift.phantom)}"
            ),
            raw="",
        )
        for drift in module_md_importer_drift()
    )

    problems.extend(
        Problem(
            doc="MODULE.md",
            line=0,
            level="ERROR",
            msg=(
                f"MODULE.md surface cell for {drift.module}: "
                f"missing subcommand(s) {sorted(drift.missing)}"
            ),
            raw="",
        )
        for drift in module_md_surface_cell_drift()
    )

    for doc_name, count in sorted(docs_over_stage_doc_citation_limit().items()):
        problems.append(
            Problem(
                doc=doc_name,
                line=0,
                level="ERROR",
                msg=(
                    f"cites {count} distinct stage-docs (limit {STAGE_DOC_CITATION_LIMIT}): "
                    f"re-enumerates the stage->reference_doc map canonical in stage-registry.toml"
                ),
                raw="",
            )
        )

    for doc_name, lineno, key in descriptor_key_drift():
        problems.append(
            Problem(
                doc=doc_name,
                line=lineno,
                level="ERROR",
                msg=(
                    f"prose cites descriptor key {key!r} not emitted by "
                    f"{_DESCRIPTOR_SCRIPT} (renamed or stale)"
                ),
                raw="",
            )
        )

    for doc_name, lineno, role in role_literal_drift():
        problems.append(
            Problem(
                doc=doc_name,
                line=lineno,
                level="ERROR",
                msg=f"prose cites role {role!r} absent from stage-registry roles arrays",
                raw="",
            )
        )

    problems.extend(
        Problem(
            doc="init.py",
            line=0,
            level="ERROR",
            msg=f"generated managed AGENTS guidance: {detail}",
            raw="",
        )
        for detail in managed_agents_guidance_drift()
    )

    problems.extend(
        Problem(
            doc="agent route contract",
            line=0,
            level="ERROR",
            msg=detail,
            raw="",
        )
        for detail in route_contract_drift()
    )

    problems.extend(
        Problem(
            doc="cognitive-worker design",
            line=0,
            level="ERROR",
            msg=detail,
            raw="",
        )
        for detail in cognitive_worker_design_drift()
    )

    if args.verbose:
        for inv in all_invs:
            sub = f" {inv.subcommand}" if inv.subcommand else ""
            print(f"  {inv.doc}:{inv.line}  {inv.script}{sub}  {inv.flags}")

    errors = [p for p in problems if p.level == "ERROR"]
    warns = [p for p in problems if p.level == "WARN"]
    for p in errors + warns:
        print(f"{p.doc}:{p.line}: {p.level}: {p.msg}")
        if p.level == "ERROR":
            print(f"    {p.raw}")

    n_scripts = len({inv.script for inv in all_invs})
    print(
        f"\nseam_check: {len(all_invs)} invocations across {len(docs)} docs, "
        f"{n_scripts} scripts -> {len(errors)} error(s), {len(warns)} warning(s)"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
