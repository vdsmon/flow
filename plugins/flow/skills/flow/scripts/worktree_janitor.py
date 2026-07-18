"""List or reap stale Flow worktrees owned by the invoking repository.

The janitor recognizes registered worktrees only under the primary checkout's `.claude/worktrees`
and legacy `.flow/worktrees` directories. It resolves ticket, tracker, and forge evidence through
their normalized seams and preserves a worktree whenever a probe is unavailable or inconclusive.

Every removal goes through `flow_worktree.reap_worktree`, which repeats the branch ownership check,
holds the exact ticket lease across teardown, and checkpoints dirty work to a rescue ref before
removal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import branch_ticket
import cognitive_workers
import lease
import observe_at_close
from _locking import flock_blocking
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from _timeutil import utcnow_iso
from flow_worktree import is_ticket_branch, reap_worktree
from forge import ForgeConfigError, make_forge, read_forge_config
from tracker import make_tracker
from tracker_cli import _read_tracker_config

_QUARANTINE_AGED_THRESHOLD_S = 7 * 24 * 60 * 60

_TERMINAL_STATES = frozenset({"done", "cancelled"})
_BUCKETS = (
    "reapable",
    "reaped",
    "reap_failed",
    "skipped_invoking_checkout",
    "skipped_unconfirmed",
    "skipped_unmanaged",
    "skipped_unrecognized",
    "skipped_live_lease",
    "skipped_corrupt_lease",
    "skipped_open_pr",
    "skipped_non_terminal",
    "skipped_merged_head_mismatch",
    "skipped_remote_default",
    "skipped_unique_commits",
    "probe_failed",
)


class _JanitorError(Exception):
    """A repository-level probe failed before candidates could be isolated."""


def _run(runner: Runner, args: list[str], what: str) -> str:
    result = runner(args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise _JanitorError(f"{what} failed: {detail}")
    return result.stdout.strip()


def _enumerate_worktrees(porcelain: str) -> list[dict[str, str | None]]:
    entries: list[dict[str, str | None]] = []
    current: dict[str, str | None] = {}
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            if current:
                entries.append(current)
            current = {"worktree": line.removeprefix("worktree ").strip(), "branch": None}
        elif line.startswith("HEAD "):
            current["tip"] = line.removeprefix("HEAD ").strip()
        elif line.startswith("branch "):
            current["branch"] = line.removeprefix("branch ").strip().removeprefix("refs/heads/")
        elif not line.strip() and current:
            entries.append(current)
            current = {}
    if current:
        entries.append(current)
    return entries


def _load_tracker(workspace_root: Path):
    return make_tracker(_read_tracker_config(workspace_root))


def _load_forge(workspace_root: Path):
    config = read_forge_config(workspace_root)
    if config is None:
        raise ForgeConfigError("workspace.toml has no [forge] block")
    return make_forge(config)


def _candidate_row(
    entry: dict[str, str | None], key: str | None = None, **extra: Any
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "key": key,
        "branch": entry.get("branch"),
        "worktree": entry.get("worktree"),
        "tip": entry.get("tip"),
    }
    row.update(extra)
    return row


def _confirmation_id(path: Path, branch: str, tip: str) -> str:
    payload = "\0".join((str(path.resolve()), branch, tip)).encode()
    return hashlib.sha256(payload).hexdigest()


def _managed(path: Path, main_root: Path) -> bool:
    return any(
        path.is_relative_to(base)
        for base in (main_root / ".claude" / "worktrees", main_root / ".flow" / "worktrees")
    )


def _candidate_lease_blocker(path: Path, key: str) -> tuple[str, Path] | None:
    base = path / ".flow" / "runs" / key
    owners = [base]
    revisions = base / "revisions"
    if revisions.is_dir():
        owners.extend(sorted(child for child in revisions.iterdir() if child.is_dir()))
    now, boot, host = utcnow_iso(), lease.boot_id(), lease.hostname()
    for owner in owners:
        info = lease.classify(owner, now, current_boot=boot, hostname=host)
        state = str(info.get("state"))
        if state in ("live", "corrupt"):
            return state, owner
    return None


def _verified_remote_default(runner: Runner) -> tuple[str | None, str | None]:
    local_ref = _run(
        runner,
        ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
        "resolve local remote default",
    )
    if not local_ref.startswith("refs/remotes/origin/"):
        raise _JanitorError(f"unexpected origin HEAD ref {local_ref!r}")
    local_sha = _run(runner, ["git", "rev-parse", local_ref], "read local remote default")
    branch = local_ref.removeprefix("refs/remotes/origin/")
    remote = _run(
        runner,
        ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
        "read remote default",
    )
    remote_sha = remote.split()[0] if remote.split() else ""
    if not remote_sha:
        raise _JanitorError(f"origin default branch {branch!r} returned no SHA")
    if local_sha != remote_sha:
        return None, f"local {local_sha} does not match remote {remote_sha}"
    return local_sha, None


def _empty_result(main_root: Path, dry_run: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "target_root": str(main_root.resolve()),
        "dry_run": dry_run,
    }
    result.update({bucket: [] for bucket in _BUCKETS})
    return result


def sweep(  # noqa: C901
    workspace_root: Path,
    *,
    dry_run: bool,
    confirmed_target: Path | None = None,
    confirmed_candidates: frozenset[str] | None = None,
) -> dict[str, Any]:
    invoking = workspace_root.expanduser().resolve()
    runner = _default_runner(invoking)
    entries = _enumerate_worktrees(
        _run(runner, ["git", "worktree", "list", "--porcelain"], "git worktree list")
    )
    if not entries or not entries[0].get("worktree"):
        raise _JanitorError("git worktree list returned no primary checkout")
    main_root = Path(str(entries[0]["worktree"])).expanduser().resolve()
    result = _empty_result(main_root, dry_run)
    if not dry_run:
        if confirmed_target is None or confirmed_candidates is None:
            raise _JanitorError("real sweep requires a confirmed target and candidate set")
        target = confirmed_target.expanduser().resolve()
        if target != main_root:
            raise _JanitorError(
                f"confirmed target {target} does not match current target {main_root}"
            )
    confirmed_ids = confirmed_candidates or frozenset()

    try:
        tracker: Any = _load_tracker(main_root)
    except Exception as exc:
        tracker = exc
    try:
        forge: Any = _load_forge(main_root)
    except Exception as exc:
        forge = exc

    for entry in entries[1:]:
        raw_path = entry.get("worktree")
        branch = entry.get("branch")
        tip = entry.get("tip")
        if not raw_path or not branch or not tip:
            result["skipped_unrecognized"].append(_candidate_row(entry))
            continue
        path = Path(raw_path).expanduser().resolve()
        if path == invoking:
            result["skipped_invoking_checkout"].append(_candidate_row(entry))
            continue
        if not _managed(path, main_root):
            result["skipped_unmanaged"].append(_candidate_row(entry))
            continue

        try:
            key = branch_ticket.resolve(main_root, path, branch=branch)
        except Exception as exc:
            result["probe_failed"].append(
                _candidate_row(entry, probe="branch_ticket", error=str(exc))
            )
            continue
        if key is None or not is_ticket_branch(branch, key):
            result["skipped_unrecognized"].append(_candidate_row(entry, key))
            continue
        row = _candidate_row(
            entry,
            key,
            confirmation_id=_confirmation_id(path, branch, tip),
        )

        try:
            lease_blocker = _candidate_lease_blocker(path, key)
        except Exception as exc:
            result["probe_failed"].append({**row, "probe": "lease", "error": str(exc)})
            continue
        if lease_blocker is not None and lease_blocker[0] == "live":
            result["skipped_live_lease"].append({**row, "lease_owner": str(lease_blocker[1])})
            continue
        if lease_blocker is not None and lease_blocker[0] == "corrupt":
            result["skipped_corrupt_lease"].append({**row, "lease_owner": str(lease_blocker[1])})
            continue

        if isinstance(forge, Exception):
            result["probe_failed"].append({**row, "probe": "forge_config", "error": str(forge)})
            continue
        try:
            open_pr = forge.detect_pr(branch, state="open")
        except Exception as exc:
            result["probe_failed"].append({**row, "probe": "forge_open_pr", "error": str(exc)})
            continue
        if open_pr is not None:
            result["skipped_open_pr"].append({**row, "pr": open_pr})
            continue
        try:
            merged_pr = forge.detect_pr(branch, state="merged")
        except Exception as exc:
            result["probe_failed"].append({**row, "probe": "forge_merged_pr", "error": str(exc)})
            continue

        if isinstance(tracker, Exception):
            result["probe_failed"].append({**row, "probe": "tracker_config", "error": str(tracker)})
            continue
        try:
            normalized = tracker.state(key).get("normalized")
        except Exception as exc:
            result["probe_failed"].append({**row, "probe": "tracker_state", "error": str(exc)})
            continue
        if normalized not in _TERMINAL_STATES:
            result["skipped_non_terminal"].append({**row, "tracker_state": normalized})
            continue

        if merged_pr is not None:
            head_sha = merged_pr.get("head_sha")
            if not head_sha or tip != head_sha:
                result["skipped_merged_head_mismatch"].append(
                    {**row, "pr": merged_pr, "head_sha": head_sha}
                )
                continue
            row = {**row, "reason": "merged_pr_head_match", "pr": merged_pr}
        else:
            try:
                default_sha, mismatch = _verified_remote_default(runner)
            except Exception as exc:
                result["probe_failed"].append({**row, "probe": "remote_default", "error": str(exc)})
                continue
            if default_sha is None:
                result["skipped_remote_default"].append({**row, "detail": mismatch})
                continue
            try:
                count_raw = _run(
                    runner,
                    ["git", "rev-list", "--count", f"{default_sha}..{tip}"],
                    f"count unique commits for {branch}",
                )
                unique_commits = int(count_raw)
            except (ValueError, _JanitorError) as exc:
                result["probe_failed"].append({**row, "probe": "unique_commits", "error": str(exc)})
                continue
            if unique_commits:
                result["skipped_unique_commits"].append({**row, "unique_commits": unique_commits})
                continue
            row = {**row, "reason": "terminal_no_pr_zero_unique_commits"}

        if not dry_run and row["confirmation_id"] not in confirmed_ids:
            result["skipped_unconfirmed"].append(row)
            continue
        result["reapable"].append(row)
        if dry_run:
            continue
        try:
            receipt = reap_worktree(
                ticket=key,
                main_root=main_root,
                branch=branch,
                expected_tip=tip,
                before_remove=lambda doomed, ticket=key: observe_at_close.observe_at_close(
                    main_root, ticket, doomed
                ),
            )
        except Exception as exc:
            result["reap_failed"].append({**row, "reap_error": str(exc)})
            continue
        if receipt.get("before_remove_error"):
            row["ship_event"] = {
                "action": "failed",
                "reason": receipt["before_remove_error"],
            }
        elif "before_remove_result" in receipt:
            row["ship_event"] = receipt["before_remove_result"]
        completed = {**row, "receipt": receipt}
        if receipt.get("skipped"):
            result["reap_failed"].append(completed)
        else:
            result["reaped"].append(completed)
    return result


def _discover_quarantine_journals(workspace_root: Path) -> list[Path]:
    """Journal paths under every workspace-owned run/revision cognitive root."""
    runs_root = workspace_root / ".flow" / "runs"
    if not runs_root.is_dir():
        return []
    found: set[Path] = set()
    for pattern in (
        "*/cognitive/*/invocations/*/journal.json",
        "*/revisions/*/cognitive/*/invocations/*/journal.json",
    ):
        found.update(runs_root.glob(pattern))
    return sorted(found)


def _quarantine_confirmation_id(target: Path, quarantine_path: Path, journal_digest: str) -> str:
    payload = "\0".join((str(target.resolve()), str(quarantine_path), journal_digest)).encode()
    return hashlib.sha256(payload).hexdigest()


def _quarantine_row(
    target: Path, journal_path: Path, value: dict[str, Any], *, now: float
) -> dict[str, Any] | None:
    """One reportable row for a quarantined journal, or None when there is nothing to sweep.

    A journal reaches "quarantined" only through `_dispose_failed_capsule(..., quarantine=True)`,
    which always records a `quarantine_path` once the source capsule existed, even when the move
    itself failed, since that rename is wrapped in a suppressed `except OSError`. A missing
    `quarantine_path` means the capsule never existed at dispose time, so there is nothing here.
    """
    if value.get("state") != "quarantined":
        return None
    disposal = value.get("disposal")
    disposal = disposal if isinstance(disposal, dict) else {}
    if disposal.get("quarantined") is not True or "quarantine_path" not in disposal:
        return None
    quarantine_path = Path(str(disposal["quarantine_path"]))
    failure = value.get("failure")
    failure_code = failure.get("code") if isinstance(failure, dict) else None
    age_seconds = max(0.0, now - float(value.get("updated_at", now)))
    exists = quarantine_path.is_dir()
    # A prior real pass renames the capsule out from under `quarantine_path` and annotates the
    # journal with where it went (see quarantine_clean below); re-reading that same journal on a
    # later preview must recognize the annotation rather than reporting completed work as a
    # suppressed move failure.
    archive = value.get("archive")
    archive_path_raw = archive.get("archive_path") if isinstance(archive, dict) else None
    already_archived = (
        not exists and isinstance(archive_path_raw, str) and Path(archive_path_raw).is_dir()
    )
    row: dict[str, Any] = {
        "journal_path": str(journal_path),
        "quarantine_path": str(quarantine_path),
        "state": "quarantined",
        "failure_code": failure_code,
        "age_seconds": age_seconds,
        "aged": age_seconds >= _QUARANTINE_AGED_THRESHOLD_S,
        "exists": exists,
        "confirmation_id": _quarantine_confirmation_id(
            target, quarantine_path, str(value.get("digest", ""))
        ),
    }
    if already_archived:
        row["already_archived"] = True
        row["archive_path"] = archive_path_raw
        return row
    if not exists:
        original = disposal.get("capsule")
        row["original_capsule_exists"] = bool(original) and Path(str(original)).exists()
    return row


def quarantine_clean(  # noqa: C901
    workspace_root: Path,
    *,
    dry_run: bool,
    confirmed_target: Path | None = None,
    confirmed_candidates: frozenset[str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Preview or archive workspace-owned quarantined cognitive capsules.

    Discovery reads every quarantined journal under a run or revision cognitive root and reports
    the capsule path that journal recorded moving to. A capsule is never deleted, only renamed
    into a sibling archive area next to `quarantine/`, and only after the real pass re-reads the
    journal and re-checks containment and the digest-bound confirmation ID under the same
    per-invocation lock CognitiveWorkers itself uses (the journal's sibling `.lock` file), so a
    concurrent recovery of the same invocation cannot race the archive.
    """
    target = workspace_root.expanduser().resolve()
    moment = time.time() if now is None else now
    if not dry_run:
        if confirmed_target is None or confirmed_candidates is None:
            raise _JanitorError("real quarantine clean requires a confirmed target and candidates")
        resolved_confirmed = confirmed_target.expanduser().resolve()
        if resolved_confirmed != target:
            raise _JanitorError(
                f"confirmed target {resolved_confirmed} does not match current target {target}"
            )
    confirmed_ids = confirmed_candidates or frozenset()

    result: dict[str, Any] = {
        "target_root": str(target),
        "dry_run": dry_run,
        "reapable": [],
        "younger": [],
        "recorded_missing": [],
        "archived": [],
        "archive_failed": [],
        "skipped_unconfirmed": [],
        "skipped_uncontained": [],
    }
    contained_root = (target / ".flow" / "runs").resolve()

    for journal_path in _discover_quarantine_journals(target):
        try:
            value = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        row = _quarantine_row(target, journal_path, value, now=moment)
        if row is None:
            continue
        if row.get("already_archived"):
            result["archived"].append(row)
            continue
        if not row["exists"]:
            result["recorded_missing"].append(row)
            continue
        quarantine_path = Path(row["quarantine_path"]).resolve()
        if not quarantine_path.is_relative_to(contained_root):
            result["skipped_uncontained"].append(row)
            continue
        if dry_run:
            result["reapable" if row["aged"] else "younger"].append(row)
            continue
        if row["confirmation_id"] not in confirmed_ids:
            result["skipped_unconfirmed"].append({**row, "reason": "not_confirmed"})
            continue
        with flock_blocking(journal_path.parent.with_suffix(".lock")):
            try:
                fresh_value = json.loads(journal_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                result["archive_failed"].append({**row, "error": str(exc)})
                continue
            fresh_row = _quarantine_row(target, journal_path, fresh_value, now=moment)
            if fresh_row is None or fresh_row["confirmation_id"] != row["confirmation_id"]:
                result["skipped_unconfirmed"].append({**row, "reason": "drifted_since_preview"})
                continue
            archive_root = quarantine_path.parent.parent / "archive"
            archive_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            archive_path = archive_root / quarantine_path.name
            if archive_path.exists():
                result["archive_failed"].append(
                    {**row, "error": f"archive destination {archive_path} already exists"}
                )
                continue
            try:
                os.replace(quarantine_path, archive_path)
            except OSError as exc:
                result["archive_failed"].append({**row, "error": str(exc)})
                continue
            archived_row = {**row, "archive_path": str(archive_path)}
            logical_id = fresh_value.get("logical_invocation_id")
            if isinstance(logical_id, str):
                try:
                    journal = cognitive_workers.InvocationJournal(journal_path, logical_id)
                    journal.transition(
                        "quarantined",
                        archive={"archive_path": str(archive_path), "archived_at": moment},
                    )
                except cognitive_workers.WorkerFailure as exc:
                    archived_row["journal_annotation_error"] = str(exc)
            result["archived"].append(archived_row)
    return result


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="List or reap stale workspace worktrees and quarantined cognitive capsules."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sweep_parser = sub.add_parser("sweep")
    sweep_parser.add_argument("--workspace-root", required=True)
    sweep_parser.add_argument("--dry-run", action="store_true")
    sweep_parser.add_argument("--confirmed-target")
    sweep_parser.add_argument("--confirmed-candidate", action="append", default=[])
    quarantine_parser = sub.add_parser("quarantine-clean")
    quarantine_parser.add_argument("--workspace-root", required=True)
    quarantine_parser.add_argument("--dry-run", action="store_true")
    quarantine_parser.add_argument("--confirmed-target")
    quarantine_parser.add_argument("--confirmed-candidate", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.cmd == "quarantine-clean":
            result = quarantine_clean(
                Path(args.workspace_root),
                dry_run=bool(args.dry_run),
                confirmed_target=(Path(args.confirmed_target) if args.confirmed_target else None),
                confirmed_candidates=(
                    None if args.dry_run else frozenset(args.confirmed_candidate)
                ),
            )
        else:
            result = sweep(
                Path(args.workspace_root),
                dry_run=bool(args.dry_run),
                confirmed_target=(Path(args.confirmed_target) if args.confirmed_target else None),
                confirmed_candidates=(
                    None if args.dry_run else frozenset(args.confirmed_candidate)
                ),
            )
    except _JanitorError as exc:
        print(f"worktree-janitor: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main", "quarantine_clean", "sweep"]
