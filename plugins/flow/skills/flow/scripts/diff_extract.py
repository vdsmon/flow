"""Git diff capture for the dispatcher's implement / commit / reflect stages.

Library + thin CLI. Stdlib-only.

Subcommands:

  since --ref <git-ref>
      git diff --numstat <ref>..HEAD; emits {files_touched, insertions,
      deletions, binary} JSON.

  since-stage --stage <name> --ticket <key> --ticket-dir <dir>
      Reads <ticket-dir>/state.json for stages.<name>.started_at_sha; if absent
      exits 1. Then runs `since` mode with that sha.

  record-baseline --stage <name> --ticket <key> --ticket-dir <dir>
                  [--files <comma-sep>] [--capture-blobs]
      Writes <ticket-dir>/baseline.json: head_sha + planned_files + (when
      --capture-blobs set) per-file index entries via `git ls-files -s`.

  capture-implement-diff --ticket <key> --ticket-dir <dir>
      Reads baseline.json for {head_sha, planned_files}, runs `git diff
      --binary --raw <head_sha> -- <files>`, writes to
      <ticket-dir>/implement.diff.

Exit codes:
  0 = ok
  1 = missing baseline / state.json
  2 = git error (stderr propagated)
  3 = check-ownership only: ownership violation (unowned paths in the diff)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TypedDict

import state
import ticket_frontmatter
from _atomicio import atomic_write_text
from _runner import Runner
from _runner import default_runner as _default_runner


class OwnershipResult(TypedDict):
    ok: bool
    planned_files: list[str]
    changed: list[str]
    unowned_changes: list[str]


class _GitError(Exception):
    """Raised on git command failure. Exit code 2."""


class _BaselineMissing(Exception):
    """Raised when baseline.json or state.json absent. Exit code 1."""


class _IgnoredPlannedFile(_BaselineMissing):
    """A planned file is gitignored, so it cannot be committed. Exit code 1.

    Subclasses _BaselineMissing so the existing CLI handler maps it to exit 1; a
    gitignored planned file is a fix-your-inputs problem, not a git failure.
    """


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _git(args: list[str], cwd: Path, runner: Runner) -> str:
    # core.quotePath=false so non-ASCII paths come back literal (UTF-8) instead of
    # C-quoted/octal-escaped. The porcelain/ls-files/numstat parsers below compare
    # raw output against planned paths; an escaped "caf\303\251.py" never matches
    # "café.py" and the ownership gate false-flags a legit file as unowned.
    result = runner(["git", "-c", "core.quotePath=false", *args], cwd)
    if result.returncode != 0:
        raise _GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


_PORCELAIN_ESCAPES = {
    "n": 0x0A,
    "t": 0x09,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
    "a": 0x07,
    "b": 0x08,
    "f": 0x0C,
    "v": 0x0B,
}


def _unquote_porcelain_path(token: str) -> str:
    """C-decode a `git status --porcelain` path token.

    Porcelain v1 wraps a path in double-quotes and C-escapes it whenever it holds
    a space, double-quote, backslash, tab, newline or control char (column
    disambiguation, independent of core.quotePath). An unquoted token is returned
    unchanged. Octal escapes (`\\303\\251`) are collected as raw bytes so multibyte
    UTF-8 round-trips through the single final decode. Malformed input fails safe to
    the raw token.
    """
    if not token.startswith('"'):
        return token
    if len(token) < 2 or not token.endswith('"'):
        return token
    interior = token[1:-1]
    buf = bytearray()
    i = 0
    n = len(interior)
    while i < n:
        ch = interior[i]
        if ch != "\\":
            buf.extend(ch.encode("utf-8"))
            i += 1
            continue
        if i + 1 >= n:  # trailing backslash, malformed
            return token
        nxt = interior[i + 1]
        if nxt in "01234567":
            j = i + 1
            octal = ""
            while j < n and len(octal) < 3 and interior[j] in "01234567":
                octal += interior[j]
                j += 1
            buf.append(int(octal, 8) & 0xFF)
            i = j
            continue
        mapped = _PORCELAIN_ESCAPES.get(nxt)
        if mapped is None:  # unknown escape, malformed
            return token
        buf.append(mapped)
        i += 2
    try:
        return bytes(buf).decode("utf-8")
    except UnicodeDecodeError:
        return token


def _head_sha(cwd: Path, runner: Runner) -> str:
    return _git(["rev-parse", "HEAD"], cwd, runner).strip()


def _baseline_path(ticket_dir: Path) -> Path:
    return ticket_dir / "baseline.json"


def _implement_diff_path(ticket_dir: Path) -> Path:
    return ticket_dir / "implement.diff"


def _untracked_files(files: list[str], cwd: Path, runner: Runner) -> list[str]:
    """Return the subset of `files` that git does not currently track.

    `git ls-files -- <paths>` lists only tracked or staged paths, so anything in
    `files` missing from its output is untracked in the working tree.
    """
    if not files:
        return []
    raw = _git(["ls-files", "--", *files], cwd, runner)
    tracked = {line for line in raw.splitlines() if line}
    return [f for f in files if f not in tracked]


def _staged_deletions(files: list[str], cwd: Path, runner: Runner) -> list[str]:
    """Return the subset of `files` staged as a deletion relative to HEAD.

    `git rm --cached <p>` untracks a path while keeping its working copy, so it is
    absent from `git ls-files` (reads as untracked-new) yet `git diff HEAD` already
    emits the deletion and needs no intent-to-add. `git diff --cached --diff-filter=D`
    is the exact query; `git ls-files --deleted` is NOT (it lists working-tree-deleted
    paths, the opposite case).
    """
    if not files:
        return []
    raw = _git(["diff", "--cached", "--diff-filter=D", "--name-only", "--", *files], cwd, runner)
    return [line for line in raw.splitlines() if line]


def _gitignored(files: list[str], cwd: Path, runner: Runner) -> list[str]:
    """Return the subset of `files` git ignores. check-ignore exits 0 when a path
    is ignored, 1 when none are, so it bypasses `_git` (which raises on non-zero)."""
    if not files:
        return []
    result = runner(["git", "check-ignore", "--", *files], cwd)
    if result.returncode not in (0, 1):
        raise _GitError(f"git check-ignore failed: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# ─── since / since-stage ─────────────────────────────────────────────────────


def diff_since(ref: str, cwd: Path, runner: Runner | None = None) -> dict[str, Any]:
    r = runner or _default_runner()
    raw = _git(["diff", "--numstat", f"{ref}..HEAD"], cwd, r)
    files_touched: list[str] = []
    insertions = 0
    deletions = 0
    binary = False
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        ins_s, del_s, path = parts[0], parts[1], parts[2]
        if ins_s == "-" or del_s == "-":
            binary = True
        else:
            insertions += int(ins_s)
            deletions += int(del_s)
        files_touched.append(path)
    return {
        "files_touched": files_touched,
        "insertions": insertions,
        "deletions": deletions,
        "binary": binary,
    }


def diff_since_stage(
    stage: str,
    ticket_dir: Path,
    cwd: Path,
    runner: Runner | None = None,
) -> dict[str, Any]:
    ts, exit_code = state.read(ticket_dir)
    if ts is None or exit_code == 2:
        raise _BaselineMissing(f"no usable state.json at {ticket_dir}")
    record = ts.stages.get(stage)
    if record is None:
        raise _BaselineMissing(f"stage {stage!r} not in state.json")
    if not record.started_at_sha:
        raise _BaselineMissing(f"stage {stage!r} has no started_at_sha")
    return diff_since(record.started_at_sha, cwd, runner)


# ─── record-baseline ─────────────────────────────────────────────────────────


def _ls_files_blobs(files: list[str], cwd: Path, runner: Runner) -> dict[str, dict[str, str]]:
    """Run `git ls-files -s -- <files>` and return mode/type/sha map per path.

    Format: `<mode> <sha> <stage>\t<path>` for each file.
    """
    if not files:
        return {}
    raw = _git(["ls-files", "-s", "--", *files], cwd, runner)
    blobs: dict[str, dict[str, str]] = {}
    for line in raw.splitlines():
        head, _, path = line.partition("\t")
        parts = head.split()
        if len(parts) < 3:
            continue
        mode, sha, _stage_num = parts[0], parts[1], parts[2]
        blobs[path] = {"mode": mode, "type": "blob", "sha": sha}
    return blobs


def _parse_files_arg(raw: str) -> list[str]:
    stripped = raw.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--files: malformed JSON array literal: {raw!r}") from exc
        if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
            raise ValueError(f"--files: malformed JSON array literal: {raw!r}")
        return [x.strip() for x in parsed if x.strip()]
    return [f.strip() for f in stripped.split(",") if f.strip()]


def _union_frontmatter_planned(files: list[str], ticket: str | None, cwd: Path) -> list[str]:
    """Union passed `--files` with the ticket frontmatter `planned_files`.

    The frontmatter `planned_files` can hold entries a `--files`-only baseline omits,
    so a `--files`-only capture would drop them from the implement.diff. Reads them
    back here so they survive. (The version files are no longer auto-added to
    `planned_files`; they are stamped at merge time, not in the implement diff.)

    `--files` come first (input order preserved), then frontmatter-only entries in
    frontmatter order; exact-string dedup. Returns `files` unchanged when `ticket`
    is falsy (every existing positional caller). `ticket_frontmatter.read` returns
    {} on missing/malformed, so degradation to `--files` is free.
    """
    if not ticket:
        return files
    fm = ticket_frontmatter.read(cwd / ".flow" / "tickets" / f"{ticket}.md")
    planned = fm.get("planned_files", [])
    if not isinstance(planned, list):
        return files
    merged = list(files)
    seen = set(merged)
    for entry in planned:
        coerced = str(entry)
        if coerced not in seen:
            merged.append(coerced)
            seen.add(coerced)
    return merged


def record_baseline(
    stage: str,
    ticket_dir: Path,
    cwd: Path,
    files: list[str] | None = None,
    capture_blobs: bool = False,
    runner: Runner | None = None,
    ticket: str | None = None,
) -> dict[str, Any]:
    r = runner or _default_runner()
    head = _head_sha(cwd, r)
    blobs: dict[str, dict[str, str]] = {}
    files = files or []
    files = _union_frontmatter_planned(files, ticket, cwd)
    if capture_blobs and files:
        blobs = _ls_files_blobs(files, cwd, r)
    payload: dict[str, Any] = {
        "stage": stage,
        "head_sha": head,
        "planned_files": files,
        "blobs": blobs,
    }
    atomic_write_text(
        _baseline_path(ticket_dir), json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
    return payload


# ─── capture-implement-diff ──────────────────────────────────────────────────


def capture_implement_diff(
    ticket_dir: Path,
    cwd: Path,
    runner: Runner | None = None,
) -> Path:
    r = runner or _default_runner()
    bpath = _baseline_path(ticket_dir)
    if not bpath.exists():
        raise _BaselineMissing(f"no baseline.json at {bpath}")
    try:
        baseline = json.loads(bpath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _BaselineMissing(f"baseline.json malformed: {exc}") from exc
    head_sha = baseline.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        raise _BaselineMissing("baseline.json missing head_sha")
    planned = baseline.get("planned_files", [])
    if not isinstance(planned, list):
        raise _BaselineMissing("baseline.json planned_files is not a list")
    paths = [str(p) for p in planned]
    existing = [p for p in paths if (cwd / p).exists()]
    # stage intent-to-add for any planned file that exists but is untracked, so
    # newly created files show up in the diff against head_sha; without this
    # `git diff` emits nothing for them and they vanish from the patch.
    untracked = _untracked_files(existing, cwd, r) if existing else []
    # a `git rm --cached` path is absent from `git ls-files` (reads as untracked) but
    # `git diff HEAD` already emits its deletion; carve it out so it skips the gitignore
    # guard, the intent-to-add, and the finally reset.
    if untracked:
        staged_deleted = set(_staged_deletions(untracked, cwd, r))
        untracked = [p for p in untracked if p not in staged_deleted]
    # `git add --intent-to-add` hard-fails on a gitignored path, which would abort
    # the commit stage with an opaque git error. Surface it as a diagnosable one
    # instead (the bootstrap gate normally catches this earlier; this is the
    # defense for a file gitignored after bootstrap).
    if untracked:
        ignored = _gitignored(untracked, cwd, r)
        if ignored:
            raise _IgnoredPlannedFile(
                "planned file(s) gitignored, cannot be committed: " + ", ".join(ignored)
            )
        _git(["add", "--intent-to-add", "--", *untracked], cwd, r)
    try:
        # --no-ext-diff so a configured diff.external (e.g. difftastic) cannot
        # replace the patch body with display output that `git apply` later rejects.
        args = ["diff", "--no-ext-diff", "--binary", "--raw", head_sha]
        if paths:
            args.append("--")
            args.extend(paths)
        raw = _git(args, cwd, r)
    finally:
        # capture is an observation; undo the intent-to-add so the index is left
        # exactly as it was found (these paths were untracked, so reset restores that).
        if untracked:
            _git(["reset", "--quiet", "--", *untracked], cwd, r)
    out_path = _implement_diff_path(ticket_dir)
    atomic_write_text(out_path, raw)
    return out_path


def check_ownership(
    ticket_dir: Path,
    cwd: Path,
    runner: Runner | None = None,
) -> OwnershipResult:
    """Refuse if the working tree has changes outside the baseline planned_files.

    Filename-level gate (the commit stage stages by patch from implement.diff, so
    this guards against unrelated edits sneaking into the commit). Hunk-level
    ownership against implement.diff is a deeper check deferred to a later phase.
    """
    r = runner or _default_runner()
    bpath = _baseline_path(ticket_dir)
    if not bpath.exists():
        raise _BaselineMissing(f"no baseline.json at {bpath}")
    try:
        baseline = json.loads(bpath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _BaselineMissing(f"baseline.json malformed: {exc}") from exc
    planned = baseline.get("planned_files", [])
    owned = {str(p) for p in planned} if isinstance(planned, list) else set()
    # --untracked-files=all lists each untracked file individually; without it
    # git collapses a fully-untracked directory to "foo/", which never matches a
    # per-file planned_files entry and false-positives the whole dir as unowned.
    raw = _git(["status", "--porcelain", "--untracked-files=all"], cwd, r)
    changed: list[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        token = line[3:].strip()
        # a rename reports both endpoints as `old -> new` (each side quoted
        # apart); route BOTH through the same unquote + exclusion logic so an
        # out-of-scope rename source can't slip past the ownership gate.
        tokens = [side.strip() for side in token.split(" -> ", 1)] if " -> " in token else [token]
        for tok in tokens:
            path = _unquote_porcelain_path(tok)
            # flow's own run state lives under .flow/; its writes are never an
            # unrelated user edit, so they never count against ownership. the
            # bootstrap (flow_worktree._copy_config) likewise copies the whole
            # .claude/ scaffolding (hooks/skills/settings) into each worktree; it is
            # dev config, never the ticket's own edit, so it is excluded too.
            if path == ".flow" or path.startswith(".flow/"):
                continue
            if path == ".claude" or path.startswith(".claude/"):
                continue
            changed.append(path)
    unowned = sorted(p for p in changed if p not in owned)
    return {
        "ok": not unowned,
        "planned_files": sorted(owned),
        "changed": sorted(changed),
        "unowned_changes": unowned,
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Git diff capture for /flow stages.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_since = sub.add_parser("since", help="git diff <ref>..HEAD numstat.")
    p_since.add_argument("--ref", required=True)
    p_since.add_argument("--cwd", default=".")

    p_stage = sub.add_parser("since-stage", help="diff since stage started_at_sha.")
    p_stage.add_argument("--stage", required=True)
    p_stage.add_argument("--ticket", required=True)
    p_stage.add_argument("--ticket-dir", required=True)
    p_stage.add_argument("--cwd", default=".")

    p_record = sub.add_parser("record-baseline", help="write baseline.json for the stage.")
    p_record.add_argument("--stage", required=True)
    p_record.add_argument("--ticket", required=True)
    p_record.add_argument("--ticket-dir", required=True)
    p_record.add_argument("--files", default=None, help="comma-separated planned files.")
    p_record.add_argument("--capture-blobs", action="store_true")
    p_record.add_argument("--cwd", default=".")

    p_capture = sub.add_parser("capture-implement-diff", help="dump implement.diff.")
    p_capture.add_argument("--ticket", required=True)
    p_capture.add_argument("--ticket-dir", required=True)
    p_capture.add_argument("--cwd", default=".")

    p_own = sub.add_parser("check-ownership", help="refuse changes outside planned_files.")
    p_own.add_argument("--ticket", required=True)
    p_own.add_argument("--ticket-dir", required=True)
    p_own.add_argument("--cwd", default=".")

    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    cwd = Path(args.cwd).resolve()

    try:
        if args.cmd == "since":
            payload = diff_since(args.ref, cwd)
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            return 0

        if args.cmd == "since-stage":
            ticket_dir = Path(args.ticket_dir).resolve()
            payload = diff_since_stage(args.stage, ticket_dir, cwd)
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            return 0

        if args.cmd == "record-baseline":
            ticket_dir = Path(args.ticket_dir).resolve()
            files: list[str] = []
            if args.files:
                files = _parse_files_arg(args.files)
            payload = record_baseline(
                args.stage,
                ticket_dir,
                cwd,
                files=files,
                capture_blobs=args.capture_blobs,
                ticket=args.ticket,
            )
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            return 0

        if args.cmd == "capture-implement-diff":
            ticket_dir = Path(args.ticket_dir).resolve()
            out = capture_implement_diff(ticket_dir, cwd)
            sys.stdout.write(json.dumps({"diff_path": str(out)}) + "\n")
            return 0

        if args.cmd == "check-ownership":
            ticket_dir = Path(args.ticket_dir).resolve()
            payload = check_ownership(ticket_dir, cwd)
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            return 0 if payload["ok"] else 3

    except ValueError as exc:
        sys.stderr.write(f"diff-extract: {exc}\n")
        return 2
    except _BaselineMissing as exc:
        sys.stderr.write(f"diff-extract: {exc}\n")
        return 1
    except _GitError as exc:
        sys.stderr.write(f"diff-extract: {exc}\n")
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "capture_implement_diff",
    "check_ownership",
    "cli_main",
    "diff_since",
    "diff_since_stage",
    "record_baseline",
]
