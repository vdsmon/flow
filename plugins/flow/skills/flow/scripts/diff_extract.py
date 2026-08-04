"""Git diff capture for the dispatcher's implement / code_review / commit / reflect stages.

Library + thin CLI. Stdlib-only.

Subcommands:

  since --ref <git-ref>
      git diff --numstat <ref>..HEAD; emits {files_touched, insertions,
      deletions, binary} JSON.

      Reads <ticket-dir>/state.json for stages.<name>.started_at_sha; if absent
      exits 1. Then runs `since` mode with that sha.

  record-baseline --stage <name> --ticket <key> --ticket-dir <dir>
                  [--files <comma-sep>] [--capture-blobs]
      Writes <ticket-dir>/baseline.json: head_sha + origin_sha + planned_files +
      (when --capture-blobs set) per-file index entries via `git ls-files -s`.
      head_sha is the moving implement-diff anchor and is recomputed from live
      HEAD on every record. origin_sha is the stable review and ownership
      anchor: it is written once, on the first record, and every later record
      preserves it. The post-implement reconcile re-records to widen
      planned_files without hiding committed work from either consumer.

  capture-implement-diff --ticket <key> --ticket-dir <dir>
      Reads baseline.json for {head_sha, planned_files}, runs `git diff
      --binary --raw <head_sha> -- <files>`, writes to
      <ticket-dir>/implement.diff. Refuses an empty planned_files (exit 1): with
      no pathspec git diffs the whole repository, and an empty owned set can
      never legitimately produce a scoped capture.

  capture-review-diff --ticket <key> --ticket-dir <dir>
      Same scope as capture-implement-diff, including the empty planned_files
      refusal, but runs `git diff <origin_sha> -- <files>` (no --binary/--raw)
      and writes to <ticket-dir>/review.diff. origin_sha is stable across
      baseline re-records, so the payload retains committed work. A legacy
      baseline without origin_sha falls back to head_sha. Binary content is
      elided (`Binary files ... differ`) rather than inlined, since this payload
      is read by code_review's reviewer and never applied.

Exit codes:
  0 = ok
  1 = missing baseline / state.json / empty planned_files
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
    # the sha the committed-delta half scanned from, and which baseline field it came from:
    # "origin_sha", or "head_sha_fallback" for a baseline written before origin_sha existed.
    # committed_scan_empty says that half listed no path between the anchor and HEAD. It describes
    # that half alone: the ordinary healthy run has every edit uncommitted, so the range is empty
    # while the working-tree half checks the whole change. What signals a scan that covered nothing
    # is the conjunction of ok, an empty changed, and committed_scan_empty.
    ownership_anchor: str
    anchor_source: str
    committed_scan_empty: bool


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


def _review_diff_path(ticket_dir: Path) -> Path:
    return ticket_dir / "review.diff"


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


def _recorded_origin_sha(ticket_dir: Path) -> str:
    """Read origin_sha out of an existing baseline.json, or "" if there is none.

    A malformed or unreadable baseline yields "" so the caller records a fresh anchor:
    record_baseline's job is to write the baseline down, never to refuse over one. The fresh anchor
    is live HEAD, and unlike check_ownership's head_sha_fallback that re-anchor leaves no mark on
    the result: a later scan reports anchor_source "origin_sha" for an anchor that moved.
    """
    bpath = _baseline_path(ticket_dir)
    if not bpath.exists():
        return ""
    try:
        prior = json.loads(bpath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    if not isinstance(prior, dict):
        return ""
    origin = prior.get("origin_sha")
    return origin if isinstance(origin, str) else ""


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
    # the ownership anchor is written once and preserved by every later record, and the module
    # docstring says why re-recording must not move it.
    origin = _recorded_origin_sha(ticket_dir) or head
    payload: dict[str, Any] = {
        "stage": stage,
        "head_sha": head,
        "origin_sha": origin,
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
    if not paths:
        # with no pathspec `git diff` covers the whole repository, so an empty owned set would widen
        # the capture instead of narrowing it. There is no scoped patch to produce, so refuse.
        raise _BaselineMissing(
            "baseline.json planned_files is empty; refusing a repo-wide implement diff"
        )
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


# ─── capture-review-diff ─────────────────────────────────────────────────────


def capture_review_diff(
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
    if "origin_sha" in baseline:
        origin_sha = baseline["origin_sha"]
        object_format = _git(["rev-parse", "--show-object-format=storage"], cwd, r).strip()
        object_id_length = {"sha1": 40, "sha256": 64}.get(object_format)
        if (
            not isinstance(origin_sha, str)
            or object_id_length is None
            or len(origin_sha) != object_id_length
            or any(char not in "0123456789abcdefABCDEF" for char in origin_sha)
        ):
            raise _BaselineMissing("baseline.json has invalid origin_sha")
        anchor = origin_sha
    else:
        anchor = head_sha
    planned = baseline.get("planned_files", [])
    if not isinstance(planned, list):
        raise _BaselineMissing("baseline.json planned_files is not a list")
    paths = [str(p) for p in planned]
    if not paths:
        # spelled out again rather than shared with capture_implement_diff: the two captures are
        # kept apart so the commit-side guard cannot be changed by an edit to the review path.
        # Nothing downstream of this payload checks ownership, so a repo-wide review diff would
        # reach a reviewer unfiltered.
        raise _BaselineMissing(
            "baseline.json planned_files is empty; refusing a repo-wide review diff"
        )
    existing = [p for p in paths if (cwd / p).exists()]
    # Stage intent-to-add for a planned file that exists but is untracked, so new files show up in
    # the diff against the anchor. Without this step, `git diff` emits nothing for them and they
    # vanish from the payload.
    untracked = _untracked_files(existing, cwd, r) if existing else []
    # a `git rm --cached` path is absent from `git ls-files` (reads as untracked) but `git diff
    # HEAD` already emits its deletion; carve it out so it skips the gitignore guard, the
    # intent-to-add, and the finally reset.
    if untracked:
        staged_deleted = set(_staged_deletions(untracked, cwd, r))
        untracked = [p for p in untracked if p not in staged_deleted]
    # `git add --intent-to-add` hard-fails on a gitignored path, which would abort the commit stage
    # with an opaque git error. Surface it as a diagnosable one instead (the bootstrap gate normally
    # catches this earlier; this is the defense for a file gitignored after bootstrap).
    if untracked:
        ignored = _gitignored(untracked, cwd, r)
        if ignored:
            raise _IgnoredPlannedFile(
                "planned file(s) gitignored, cannot be committed: " + ", ".join(ignored)
            )
        _git(["add", "--intent-to-add", "--", *untracked], cwd, r)
    try:
        # --no-ext-diff so a configured diff.external (e.g. difftastic) cannot replace the patch
        # body with display output. Unlike capture_implement_diff, no --binary/--raw: this payload
        # is read by a reviewer and never applied, so binary content is elided to a `Binary files
        # ... differ` line rather than inlined.
        args = ["diff", "--no-ext-diff", anchor]
        if paths:
            args.append("--")
            args.extend(paths)
        raw = _git(args, cwd, r)
    finally:
        # capture is an observation; undo the intent-to-add so the index is left exactly as it was
        # found (these paths were untracked, so reset restores that).
        if untracked:
            _git(["reset", "--quiet", "--", *untracked], cwd, r)
    out_path = _review_diff_path(ticket_dir)
    atomic_write_text(out_path, raw)
    return out_path


def _ownership_excluded(path: str) -> bool:
    # flow's own run state lives under .flow/; its writes are never an
    # unrelated user edit, so they never count against ownership. the
    # bootstrap (flow_worktree._copy_config) likewise copies the whole
    # .claude/ scaffolding (hooks/skills/settings) into each worktree; it is
    # dev config, never the ticket's own edit, so it is excluded too.
    if path == ".flow" or path.startswith(".flow/"):
        return True
    return path == ".claude" or path.startswith(".claude/")


def check_ownership(
    ticket_dir: Path,
    cwd: Path,
    runner: Runner | None = None,
) -> OwnershipResult:
    """Refuse if the branch delta has changes outside the baseline planned_files.

    Filename-level gate (the commit stage stages by patch from implement.diff, so
    this guards against unrelated edits sneaking into the commit). The scan covers
    the full delta against the recorded baseline: commits made since
    baseline.origin_sha AND the dirty working tree, so a change smuggled in via a
    rogue `git commit` mid-implement is seen too, not only uncommitted edits.
    Hunk-level ownership against implement.diff is a deeper check deferred to a
    later phase.

    The committed half anchors on origin_sha, not head_sha: head_sha follows HEAD on
    every re-record, so after the post-implement reconcile it would leave an empty
    range that checks nothing. The result names the anchor it used and whether that
    half found anything, so a green answer states what it covered.
    """
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
    origin_sha = baseline.get("origin_sha")
    if isinstance(origin_sha, str) and origin_sha:
        anchor, anchor_source = origin_sha, "origin_sha"
    else:
        anchor, anchor_source = head_sha, "head_sha_fallback"
    planned = baseline.get("planned_files", [])
    owned = {str(p) for p in planned} if isinstance(planned, list) else set()
    # --untracked-files=all lists each untracked file individually; without it
    # git collapses a fully-untracked directory to "foo/", which never matches a
    # per-file planned_files entry and false-positives the whole dir as unowned.
    raw = _git(["status", "--porcelain", "--untracked-files=all"], cwd, r)
    changed: set[str] = set()
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
            if _ownership_excluded(path):
                continue
            changed.add(path)
    # `git status` is blind to changes already committed on the branch, so a
    # rogue `git commit` of an unplanned file mid-implement would slip past a
    # working-tree-only scan and ride into the PR. Diff the run's origin against
    # HEAD to cover the committed delta too (empty on the normal path where HEAD
    # still equals the anchor). --no-renames lists both rename endpoints so an
    # out-of-scope rename source is seen here as well.
    raw = _git(["diff", "--name-only", "--no-renames", f"{anchor}..HEAD"], cwd, r)
    committed_lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for tok in committed_lines:
        path = _unquote_porcelain_path(tok)
        if _ownership_excluded(path):
            continue
        changed.add(path)
    unowned = sorted(p for p in changed if p not in owned)
    return {
        "ok": not unowned,
        "planned_files": sorted(owned),
        "changed": sorted(changed),
        "unowned_changes": unowned,
        "ownership_anchor": anchor,
        "anchor_source": anchor_source,
        "committed_scan_empty": not committed_lines,
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Git diff capture for /flow stages.")
    sub = parser.add_subparsers(dest="cmd", required=True)

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

    p_review = sub.add_parser("capture-review-diff", help="dump review.diff.")
    p_review.add_argument("--ticket", required=True)
    p_review.add_argument("--ticket-dir", required=True)
    p_review.add_argument("--cwd", default=".")

    p_own = sub.add_parser("check-ownership", help="refuse changes outside planned_files.")
    p_own.add_argument("--ticket", required=True)
    p_own.add_argument("--ticket-dir", required=True)
    p_own.add_argument("--cwd", default=".")

    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    cwd = Path(args.cwd).resolve()

    try:
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

        if args.cmd == "capture-review-diff":
            ticket_dir = Path(args.ticket_dir).resolve()
            out = capture_review_diff(ticket_dir, cwd)
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
    "capture_review_diff",
    "check_ownership",
    "cli_main",
    "diff_since",
    "diff_since_stage",
    "record_baseline",
]
