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
to recover them. A green non-hot CLEAN/DRAFT PR whose branch plugin version equals
main's CURRENT version (duplicate stamp: a sibling merged first stamping the same
next version, both sides changed the version line identically so git reports CLEAN)
also lands in `version_recoverable` — merging it would mint two releases sharing one
version number. A duplicate-stamp hot is never promoted (and never counts toward
one-hot isolation); it stays in skipped_hot. A green PR whose run lease still
reads live/corrupt lands in skipped_live (held, not merged) — the run
self-merges in its own merge stage, so the reap stays an orphan-only safety-net.
Before any promotion, reap() probes main's OWN CI health for the turn
(main_ci_health.py): when main is genuinely red (failed), every would-be-merge (the
promoted hot AND the non-hot leaves) routes into held_main_red instead, no hot promotes,
and reap() files ONE deduped P0 naming the failing sha + check(s). Green / pending / a
transient probe error all resume normally. Anything else lands in
not_green / skipped_hot / skipped_live / version_recoverable / blocked / held_main_red / ignored.

CLI:
  evolve_reap.py --workspace-root <dir>

Exit codes:
  0 = ok (prints the classification JSON)
  2 = tool error (gh/bd failed; stderr propagated)
  4 = not a maintainer setup (dormant; nothing reaped)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

import lease
import main_ci_health
import version
from _evolve_common import NotMaintainer, ToolError, bead_labels
from _evolve_common import key_from_ref as _key_from_ref
from _evolve_common import loads as _loads
from _evolve_common import ok as _ok
from _evolve_common import run_dir_for as _run_dir_for
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from _timeutil import utcnow_iso
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
    prs: list[dict],
    labels_index: dict[str, list[str]],
    *,
    auto_merge_hot: bool = False,
    main_version: str | None = None,
    branch_versions: dict[str, str] | None = None,
    liveness: dict[str, str] | None = None,
    main_ci_status: str | None = None,
) -> dict:
    """Pure core: bucket open PRs into merge / not_green / skipped_hot /
    skipped_live / version_recoverable / blocked.

    version_recoverable: a green NON-hot PR whose mergeStateStatus is DIRTY, or a
    green non-hot CLEAN/DRAFT PR with a DUPLICATE version stamp (branch plugin
    version == main's current; a sibling merged first stamping the same next
    version, so the version-line changes are identical and git reports CLEAN). In a
    multi-bead drain every PR bumps the two version files, so main walks forward and
    later PRs go DIRTY on the version line ONLY. This bucket is a CANDIDATE set; the
    caller runs version_remerge.py, which authoritatively gates whether the conflict
    is truly version-only (it aborts on any other conflict) and restamps a
    duplicate. A hot DIRTY PR is NOT routed here (hot never auto-recovers) — it
    stays blocked; a duplicate-stamp hot is never promoted and never counts toward
    one-hot isolation (it stays skipped_hot). Hotness is the `hot` label OR a
    guard-file diff (`_effective_hot`), so a guard-file PR with no label is held
    back here too, not auto-recovered.

    prs: parsed `gh pr list` items (number, headRefName, isDraft, mergeStateStatus,
    statusCheckRollup, files). labels_index: key -> labels, for every evolve bead.

    auto_merge_hot: when True AND exactly one hot PR is auto-merge-eligible this
    pass, that one hot PR is promoted into `merge`; all other hot PRs stay in
    skipped_hot (serialize). When False (the default), every hot PR is skipped.

    main_version / branch_versions (keyed by headRefName): fresh plugin versions
    for the duplicate-stamp check; unknown on either side (None / missing branch)
    means not-a-duplicate, so a failed gather degrades to legacy routing.

    liveness (key -> lease state): when provided, a green PR whose run lease reads
    "live" or "corrupt" is held in skipped_live (the live run owns its own merge),
    regardless of hot/dirty. None (the default) preserves legacy routing
    byte-for-byte. Built in reap(); classify stays pure.

    main_ci_status (the per-drain-turn main-CI verdict from main_ci_health.py): when
    "failed" (main's own CI is genuinely red), every PR that would route into `merge`
    (the promoted hot AND the non-hot leaves) routes into `held_main_red` instead, and
    no hot promotes this turn. None or any non-"failed" value (green / pending / a
    transient probe "error") is a no-op, preserving legacy routing byte-for-byte.
    `held_main_red` is always present (empty when main is not red). reap() probes the
    verdict and files the deduped P0; classify stays pure.
    """
    merge: list[dict] = []
    not_green: list[dict] = []
    skipped_hot: list[dict] = []
    skipped_live: list[dict] = []
    version_recoverable: list[dict] = []
    blocked: list[dict] = []
    held_main_red: list[dict] = []

    def _duplicate_stamp(ref: str) -> bool:
        # explicit None guards: unknown-vs-unknown must never read as a duplicate.
        bv = (branch_versions or {}).get(ref)
        return main_version is not None and bv is not None and bv == main_version

    hot_eligible = [
        pr
        for pr in prs
        if (key := _key_from_ref(str(pr.get("headRefName", "")))) is not None
        and key in labels_index
        and _hot_eligible(pr, labels_index[key])
        and not _duplicate_stamp(str(pr.get("headRefName", "")))
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
        if liveness is not None and liveness.get(key) in ("live", "corrupt"):
            # a live (or unconfirmable-corrupt) run's green PR is hands-off: its own
            # merge stage owns the merge. The reap is an orphan-only safety-net, so a
            # parallel drain must not merge it out from under the live session
            # (flow-ztfv). Holds regardless of hot/dirty.
            skipped_live.append(entry)
            continue
        state = str(pr.get("mergeStateStatus", "")).upper()
        if _effective_hot(pr, labels):
            # hot never auto-recovers (conservative): a hot DIRTY PR stays blocked,
            # not version_recoverable. Only the isolation-eligible hot promotes.
            if promote is not None and number == promote:
                target = held_main_red if main_ci_status == "failed" else merge
                target.append({**entry, "is_draft": bool(pr.get("isDraft")), "is_hot": True})
            elif state in _MERGEABLE_STATES:
                skipped_hot.append(entry)
            else:
                blocked.append({**entry, "reason": state or "UNKNOWN"})
            continue
        if state == "DIRTY":
            # green non-hot DIRTY: candidate for merge-time version-conflict recovery.
            # version_remerge.py authoritatively gates whether it is truly version-only.
            # recovery ends in its own merge this turn, so a red main holds it too.
            if main_ci_status == "failed":
                held_main_red.append(
                    {**entry, "is_draft": bool(pr.get("isDraft")), "is_hot": False}
                )
            else:
                version_recoverable.append(entry)
            continue
        if state in _MERGEABLE_STATES and _duplicate_stamp(ref):
            # duplicate stamp: merging as-is would mint two releases sharing one
            # version number; the recover recipe restamps it instead. same red-main
            # hold as DIRTY: the restamp path also merges within the turn.
            if main_ci_status == "failed":
                held_main_red.append(
                    {**entry, "is_draft": bool(pr.get("isDraft")), "is_hot": False}
                )
            else:
                version_recoverable.append(entry)
            continue
        if state not in _MERGEABLE_STATES:
            blocked.append({**entry, "reason": state or "UNKNOWN"})
            continue
        target = held_main_red if main_ci_status == "failed" else merge
        target.append({**entry, "is_draft": bool(pr.get("isDraft")), "is_hot": False})

    return {
        "merge": merge,
        "not_green": not_green,
        "skipped_hot": skipped_hot,
        "skipped_live": skipped_live,
        "version_recoverable": version_recoverable,
        "blocked": blocked,
        "held_main_red": held_main_red,
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
            runner(
                ["bd", "list", "-l", label, "--status", _EVOLVE_STATUSES, "--limit", "0", "--json"]
            ),
            "bd list",
        )
        for b in _loads(raw):
            if isinstance(b, dict) and b.get("id"):
                index[str(b["id"])] = list(b.get("labels") or [])
    return index


def _gather_versions(
    run: Runner, repo: Path, branches: list[str]
) -> tuple[str | None, dict[str, str]]:
    """Fresh plugin versions for the duplicate-stamp check: (main's, {branch: its}).

    STRICTLY FAIL-OPEN: any git/parse failure degrades to version-unknown (None
    main / omitted branch), which keeps classify on its legacy routing — a
    transient git hiccup must never freeze orphan reaping. No candidates means
    zero git calls. Branch versions are read at the post-fetch remote-tracking
    ref `origin/<branch>`, never the bare branch name (a stale local branch
    would silently make the guard inert).
    """
    if not branches:
        return None, {}
    head = run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])
    default_ref = (head.stdout or "").strip() if head.returncode == 0 else ""
    if not default_ref:
        default_ref = "origin/main"
    if run(["git", "fetch", "origin", "--prune"]).returncode != 0:
        return None, {}
    try:
        main_version = version.read_version(cwd=repo, ref=default_ref, runner=run)
    except (version.ToolError, ValueError):
        return None, {}
    branch_versions: dict[str, str] = {}
    for branch in branches:
        try:
            branch_versions[branch] = version.read_version(
                cwd=repo, ref=f"origin/{branch}", runner=run
            )
        except (version.ToolError, ValueError):
            continue
    return main_version, branch_versions


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


_MAIN_RED_STEM = "main-ci-red"


def _file_main_red_p0(run: Runner, sha: str | None, failing_checks: list[str]) -> None:
    """Best-effort: file ONE deduped P0 naming the failing main sha + check(s).

    At-most-one-open: scan `bd list --status open --json` titles for the
    `main-ci-red` stem and file via `bd create -p P0` only when none is open. Filing
    directly (not flow_beads_create.py: its dedup is closed-inclusive, so it would
    never refile after a human closes the P0, and it passes no priority). Every bd
    call is guarded so a tracker hiccup never crashes the reap (the gate already
    held the merges; the bead is the alert, not the safety property).
    """
    try:
        listed = run(["bd", "list", "--status", "open", "--json"])
        if listed.returncode == 0:
            for b in _loads(listed.stdout or "[]"):
                if isinstance(b, dict) and _MAIN_RED_STEM in str(b.get("title", "")):
                    return  # an open P0 already covers this red main
    except Exception:
        return  # a list failure: skip filing rather than risk a duplicate
    checks = ", ".join(failing_checks) if failing_checks else "unknown check"
    title = f"{_MAIN_RED_STEM}: {sha or 'unknown-sha'} {checks}"
    with contextlib.suppress(Exception):
        # best-effort; the held_main_red report still names the held PRs
        run(["bd", "create", "-p", "P0", "--title", title])


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
    branches = [
        ref
        for pr in prs
        if isinstance(pr, dict) and _key_from_ref(ref := str(pr.get("headRefName", ""))) in index
    ]
    main_version, branch_versions = _gather_versions(run, repo, branches)
    now = utcnow_iso()
    current_boot = lease.boot_id()
    host = lease.hostname()
    liveness: dict[str, str] = {}
    for key in index:
        run_dir = _run_dir_for(repo, key)
        liveness[key] = (
            "absent"
            if run_dir is None
            else str(
                lease.classify(run_dir, now, current_boot=current_boot, hostname=host).get("state")
            )
        )
    # pass the param (None in production) so the probe builds its own token-aware
    # _gh_runner; reap always resolves `run`, which would route the probe into the
    # injected-runner lane and skip the GH_TOKEN export (headless gh 401 flake).
    health = main_ci_health.probe(repo, runner=runner)
    main_ci_status = str(health.get("status"))
    if main_ci_status == "failed":
        _file_main_red_p0(run, health.get("sha"), health.get("failing_checks") or [])
    return classify(
        prs,
        index,
        auto_merge_hot=auto_merge_hot,
        main_version=main_version,
        branch_versions=branch_versions,
        liveness=liveness,
        main_ci_status=main_ci_status,
    )


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
