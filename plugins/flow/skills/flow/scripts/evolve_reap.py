"""Classify open evolve PRs for auto-merge (the drain loop's reap-step core, pure).

User opted in: green LEAF evolve PRs auto-merge to the default branch unattended,
immediate on green. Hot PRs auto-merge too, but only under the `auto_merge_hot`
config AND isolation (exactly one hot-eligible PR this pass — serialize hot
merges, at most one per pass); otherwise they land in skipped_hot for the human.
Non-green and conflicted PRs always wait. With the flag off (the default, every
user project), hot PRs stay in skipped_hot — the human gate survives where risk
lives.

Repo reality (this build): GitHub-native auto-merge is off and there is no branch
protection, so the drain reap step owns the merge in code and enforces "green" by reading
the actual check rollup rather than trusting GitHub. CI runs on `push` + every
`pull_request`, so a PR's checks go green while it is still a draft — this classify
can confirm green here, and the verb marks the PR ready just before merging.

This module is pure classification (no side effects). The `/flow evolve drain`
reap step performs the merge: `gh pr ready` (if draft) then `gh pr merge --squash`
over the `merge` set. The remote branch is deleted separately via
`git push origin --delete` — `--delete-branch` is dropped because the still-
registered worktree holds the local branch checked out, which makes gh's
branch-delete step fail and an otherwise-clean merge exit 1.

Eligibility (all required): branch is `feature/<key>-*`; the bead carries `evolve`;
the check rollup is non-empty and all SUCCESS (green); mergeable (CLEAN, or DRAFT
which just needs `gh pr ready`). A hot PR additionally needs `auto_merge_hot`
plus isolation (it is the only hot-eligible PR this pass). Hotness is the `hot`
label OR a diff touching a `triage._GUARD_FILES` guard file — a substantively-hot
PR counts as hot even with no label, so it can't slip into the non-hot lane. A
green non-hot PR that is DIRTY (conflicted) lands in `version_recoverable`: in a
multi-bead drain every PR bumps the two version files, so main walks forward and
later PRs conflict on the version line ONLY — the caller runs `version_remerge.py`
to recover them. Anything else lands in
not_green / skipped_hot / version_recoverable / blocked / ignored.

CLI:
  evolve_reap.py --workspace-root <dir>

Exit codes:
  0 = ok (prints the classification JSON)
  2 = tool error (gh/bd failed; stderr propagated)
  4 = not a maintainer setup (dormant; nothing reaped)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _evolve_common import NotMaintainer, ToolError, bead_labels
from _evolve_common import key_from_ref as _key_from_ref
from _evolve_common import loads as _loads
from _evolve_common import ok as _ok
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from _workspace import WorkspaceConfigError, load_workspace_toml
from maintainer import resolve_maintainer_repo
from triage import is_hot_change

_EVOLVE_STATUSES = "open,in_progress,blocked,deferred,closed"
_MERGEABLE_STATES = {"CLEAN", "DRAFT"}  # DRAFT becomes CLEAN after `gh pr ready`


def rollup_is_green(rollup: list) -> bool:
    """True iff the check rollup is non-empty and every entry is a completed SUCCESS.

    Handles both shapes gh emits: CheckRun ({status, conclusion}) and StatusContext
    ({state}). A still-running check (status != COMPLETED) or any non-SUCCESS makes
    it not green.
    """
    if not rollup:
        return False
    for e in rollup:
        if not isinstance(e, dict):
            return False
        status = e.get("status")
        if status and status != "COMPLETED":
            return False
        verdict = (e.get("conclusion") or e.get("state") or "").upper()
        if verdict != "SUCCESS":
            return False
    return True


def _effective_hot(pr: dict, labels: list[str]) -> bool:
    """Hotness for reap routing: the `hot` label OR a diff touching a guard file.

    A substantively-hot PR (one whose changed paths hit `triage._GUARD_FILES`)
    counts as hot even with no `hot` label, so it can't slip into the non-hot
    auto-recover lane. Total: a malformed/absent `files` key defaults to [].
    """
    if "hot" in labels:
        return True
    files = pr.get("files")
    if not isinstance(files, list):
        return False
    return is_hot_change([f.get("path", "") for f in files if isinstance(f, dict)])


def _hot_eligible(pr: dict, labels: list[str]) -> bool:
    """A hot PR is auto-merge-eligible when it is green AND mergeable (CLEAN/DRAFT).

    Hotness is the `hot` label OR a guard-file diff (see `_effective_hot`).
    """
    if not _effective_hot(pr, labels):
        return False
    if not rollup_is_green(pr.get("statusCheckRollup") or []):
        return False
    return str(pr.get("mergeStateStatus", "")).upper() in _MERGEABLE_STATES


def classify(
    prs: list[dict], labels_index: dict[str, list[str]], *, auto_merge_hot: bool = False
) -> dict:
    """Pure core: bucket open PRs into merge / not_green / skipped_hot /
    version_recoverable / blocked.

    version_recoverable: a green NON-hot PR whose mergeStateStatus is DIRTY. In a
    multi-bead drain every PR bumps the two version files, so main walks forward and
    later PRs go DIRTY on the version line ONLY. This bucket is a CANDIDATE set; the
    caller runs version_remerge.py, which authoritatively gates whether the conflict
    is truly version-only (it aborts on any other conflict). A hot DIRTY PR is NOT
    routed here (hot never auto-recovers) — it stays blocked. Hotness is the `hot`
    label OR a guard-file diff (`_effective_hot`), so a guard-file PR with no label
    is held back here too, not auto-recovered.

    prs: parsed `gh pr list` items (number, headRefName, isDraft, mergeStateStatus,
    statusCheckRollup, files). labels_index: key -> labels, for every evolve bead.

    auto_merge_hot: when True AND exactly one hot PR is auto-merge-eligible this
    pass, that one hot PR is promoted into `merge`; all other hot PRs stay in
    skipped_hot (serialize). When False (the default), every hot PR is skipped.
    """
    merge: list[dict] = []
    not_green: list[dict] = []
    skipped_hot: list[dict] = []
    version_recoverable: list[dict] = []
    blocked: list[dict] = []

    hot_eligible = [
        pr
        for pr in prs
        if (key := _key_from_ref(str(pr.get("headRefName", "")))) is not None
        and key in labels_index
        and _hot_eligible(pr, labels_index[key])
    ]
    promote = hot_eligible[0]["number"] if (auto_merge_hot and len(hot_eligible) == 1) else None

    for pr in prs:
        ref = str(pr.get("headRefName", ""))
        key = _key_from_ref(ref)
        if key is None or key not in labels_index:
            continue  # not one of our evolve PRs
        labels = labels_index[key]
        number = pr.get("number")
        entry = {"pr": number, "key": key, "branch": ref}

        if not rollup_is_green(pr.get("statusCheckRollup") or []):
            not_green.append(entry)
            continue
        state = str(pr.get("mergeStateStatus", "")).upper()
        if _effective_hot(pr, labels):
            # hot never auto-recovers (conservative): a hot DIRTY PR stays blocked,
            # not version_recoverable. Only the isolation-eligible hot promotes.
            if promote is not None and number == promote:
                merge.append({**entry, "is_draft": bool(pr.get("isDraft")), "is_hot": True})
            elif state in _MERGEABLE_STATES:
                skipped_hot.append(entry)
            else:
                blocked.append({**entry, "reason": state or "UNKNOWN"})
            continue
        if state == "DIRTY":
            # green non-hot DIRTY: candidate for merge-time version-conflict recovery.
            # version_remerge.py authoritatively gates whether it is truly version-only.
            version_recoverable.append(entry)
            continue
        if state not in _MERGEABLE_STATES:
            blocked.append({**entry, "reason": state or "UNKNOWN"})
            continue
        merge.append({**entry, "is_draft": bool(pr.get("isDraft")), "is_hot": False})

    return {
        "merge": merge,
        "not_green": not_green,
        "skipped_hot": skipped_hot,
        "version_recoverable": version_recoverable,
        "blocked": blocked,
    }


def _labels_index(runner: Runner, *, include_proposals: bool = False) -> dict[str, list[str]]:
    """key -> labels for every evolve bead (plus `proposal` beads when opted in).

    `classify` skips any PR whose key is absent here, so the proposal backlog MUST
    join the index under `include_proposals` or proposal orphans (runs that died
    before self-merging) would never reap and pile up unmerged.
    """
    labels = bead_labels(include_proposals)
    index: dict[str, list[str]] = {}
    for label in labels:
        raw = _ok(
            runner(["bd", "list", "-l", label, "--status", _EVOLVE_STATUSES, "--json"]),
            "bd list",
        )
        for b in _loads(raw):
            if isinstance(b, dict) and b.get("id"):
                index[str(b["id"])] = list(b.get("labels") or [])
    return index


def _auto_merge_hot(workspace_root: Path) -> bool:
    try:
        config = load_workspace_toml(workspace_root)
    except WorkspaceConfigError:
        return False
    section = config.get("evolve")
    if not isinstance(section, dict):
        return False
    value = section.get("auto_merge_hot")
    return value if isinstance(value, bool) else False


def reap(
    workspace_root: Path, *, runner: Runner | None = None, include_proposals: bool = False
) -> dict:
    repo = resolve_maintainer_repo(workspace_root)
    if repo is None:
        raise NotMaintainer("not a flow maintainer setup; nothing to reap")
    run = runner or _default_runner(repo)
    auto_merge_hot = _auto_merge_hot(workspace_root)
    pr_raw = _ok(
        run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--json",
                "number,headRefName,isDraft,mergeStateStatus,statusCheckRollup,files",
                "--limit",
                "200",
            ]
        ),
        "gh pr list",
    )
    prs = _loads(pr_raw)
    index = _labels_index(run, include_proposals=include_proposals)
    return classify(prs, index, auto_merge_hot=auto_merge_hot)


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Classify open evolve PRs for auto-merge.")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument(
        "--include-proposals",
        action="store_true",
        help="DANGEROUS: also reap orphan `proposal` PRs (pairs with the same flag "
        "on evolve_drain.py). Default off; evolve/audit PRs only.",
    )
    args = parser.parse_args(argv)
    try:
        result = reap(Path(args.workspace_root), include_proposals=args.include_proposals)
    except NotMaintainer as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
