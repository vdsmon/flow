# Script map (current)

The live "which script does what" map. One line per script: purpose + CLI surface (subcommands / key flags) + the state it touches. For the phase-by-phase build history and the deferred-work log, see `dev-history.md`. For API/contract tables (Jira REST mapping, beads CLI surface, `.flow-bundle.toml` schema, `state.json` schema), see `inventory.md`.

`lib` = imported module, no standalone CLI. Everything else is a thin CLI subprocessed from SKILL.md prose, a reference doc, or another script.

## State machine + run safety (hot path)

| Script | Role | Surface / touches |
|--------|------|-------------------|
| `dispatch_stage.py` | State-machine driver for `/flow do`. Does NOT run handlers; emits a handler-descriptor JSON for the prose layer. | `init` / `next` / `advance` / `finish` / `release` / `status`; reads+writes `state.json` |
| `state.py` (lib) | Atomic `state.json` read/write under flock, backup rotation, quarantine recovery. | imported by dispatch_stage, flow_worktree, diff_extract, recover, status, reflect_inputs |
| `snapshot.py` (lib) | Canonical workspace snapshot at init; verify on each `next` (TOCTOU drift guard). | imported by dispatch_stage, validate_workspace, recover |
| `lease.py` (lib) | Per-ticket run lease: acquire / refresh / release / expiry + takeover detection. | imported by dispatch_stage, recover, status, flow_worktree, evolve_drain, _evolve_common, evolve_session_cleanup, launch_ledger |
| `heartbeat.py` | Post-hoc hung-detection inspection library: reads a `<ticket_dir>/<stage>.progress` file if one exists and classifies a stalled stage. No producer and no live poller; nothing writes the file today, so detection is inert. | `read`; reads progress under `<ticket_dir>` |
| `validate_workspace.py` | HARD GATE: schema-validate `workspace.toml` + `stage-registry.toml` on every run. | exit 1 = violations to stderr |

## Bootstrap

| Script | Role | Surface / touches |
|--------|------|-------------------|
| `init.py` | Transactional workspace bootstrap. Collects backend/bundle answers, writes `workspace.toml`, postcondition checks, atomic `.flow/.initialized`. | `--config <json>` (`--reconfigure` / `--resume`) |
| `flow_worktree.py` | Post-approval worktree seeding: create worktree, seed `state.json` (plan completed), stamp frontmatter, redirect memory to main `.flow` via the gitignored `.flow/memory-root` sibling (tracked workspace.toml left byte-identical), `mise trust`. Autonomous bootstrap (`--auto`/`@default` base, beads) code-enforces the hot floor: refuses a hot `--planned-files` set with no recorded decision. | `create --ticket --plan-from --base --branch --main-root --planned-files --commit-type --commit-summary --e2e-recipe --worktree-path --copy --no-mise-trust --auto` / `reap --ticket --branch --main-root` |
| `branch_ticket.py` | Resolve ticket key from current git branch (backend-aware regex). | `--workspace-root`; exit 0 match / 1 env / 3 no-match |
| `bundle_discover.py` (lib) | Walk `~/.claude/plugins/*/` + `<repo>/.claude/plugins/*/` for `.flow-bundle.toml` manifests. | imported by init, resolve_handler |

## Tracker

| Script | Role | Surface / touches |
|--------|------|-------------------|
| `tracker.py` (lib) | Tracker Protocol base + `make_tracker()` factory + `CAPABILITY_ENUM`. | imported by `tracker_jira`, `tracker_beads`, `tracker_cli`, `sync` |
| `tracker_cli.py` | CLI wrapper around the Protocol (the only tracker surface the prose calls). | `get` / `state` / `transition` / `comment` / `create` / `is-shipped` / `list-assigned` / `download-attachments` |
| `tracker_jira.py` (lib) | Jira Cloud REST v3 + Agile/1.0 adapter (Basic auth via `ATLASSIAN_EMAIL`/`ATLASSIAN_API_TOKEN`). | imported by tracker.py (lazy in make_tracker) |
| `tracker_beads.py` (lib) | Beads `bd` CLI adapter (local-only tracker). | imported by triage, tracker (make_tracker factory) |
| `resolve_handler.py` | Resolve a `skill:<name>` handler: confirm bundle installed + manifest valid, return concrete `skill_name`/`skill_args`. | `--handler <string> --search-roots`; exit 1 not-installed / 2 invalid |

## Forge (PR host)

Pluggable PR-host seam, structural twin of the tracker seam. The `create_pr` and `review_loop` stages reach the host ONLY through `forge_cli.py`, so a GitHub and a Bitbucket workspace run the same prose. Selected by `[forge] backend = "github" | "bitbucket"` in `workspace.toml` (the block is OPTIONAL; absent = no forge).

| Script | Role | Surface / touches |
|--------|------|-------------------|
| `forge.py` (lib) | Forge Protocol base + `make_forge()` factory + `read_forge_config()` + `FORGE_CAPABILITY_ENUM` + normalized `PullRequest`/`CIStatus`/`ReviewThread`. | imported by `forge_github`, `forge_bitbucket`, `forge_cli`, `create_pr` |
| `forge_cli.py` | CLI wrapper around the Protocol (the only forge surface the prose calls); cap-gated subcommands degrade to `{"supported": false}` exit 0. | `detect-pr` / `open-pr` / `ci-rollup` / `review-threads` / `post-reply` / `resolve-thread` / `mark-ready` / `merge` / `delete-branch` |
| `forge_github.py` (lib) | GitHub `gh` adapter: detect/open PR, CI rollup (`statusCheckRollup`), mark-ready/merge/delete-branch. Review-threads capability OFF for now (no live review-bot-on-GitHub yet). | imported by forge.py (lazy in make_forge) |
| `forge_bitbucket.py` (lib) | Bitbucket `bkt` adapter (absorbs ship-it): detect/open PR, CI rollup from `bkt pr checks`, CodeRabbit review-thread fetch + verified resolve (`.resolution != null`). | imported by forge.py (lazy in make_forge) |

## Frontmatter / diff / commit

| Script | Role | Surface / touches |
|--------|------|-------------------|
| `ticket_frontmatter.py` | TOML frontmatter r/w under flock + atomic rename (delimiter `+++`). | `read <path>` / `update <path> --set k=v` |
| `lint_ticket.py` | HARD GATE: required frontmatter fields per stage. | `--stage --ticket-path --workspace-root` |
| `diff_extract.py` | Git diff capture for implement/commit/reflect; baseline + ownership. | `since` / `since-stage` / `record-baseline` / `capture-implement-diff` / `check-ownership` |
| `compose_commit.py` | Deterministic conventional-commit header skeleton (LLM fills body). | `--ticket --type --summary [--scope --files]` |

## Memory / reflect / self-evolution

| Script | Role | Surface / touches |
|--------|------|-------------------|
| `_memory_paths.py` (lib) | Namespace resolution + `.flow/<ns>/` path conventions. `resolve_memory_base` reads the gitignored `.flow/memory-root` sibling first, then `workspace.toml [memory].root`, then local `.flow`; every redirected worktree resolves the same store AND the same lock. | imported widely |
| `memory_append.py` | Single-writer `knowledge.jsonl` append with sha-keyed idempotency. | `--type --text --branch --ticket [--id]` |
| `recall.py` | BM25 ranker over `knowledge.jsonl`; `--metric` forwards to `metric.py`. | `<query> [--branch --tickets --top-n]` ; `--metric ...` |
| `recall_pending.py` (lib) | Promote SessionStart recall-pending entries into the per-ticket recall log. | imported by dispatch_stage |
| `reflect_inputs.py` | Bundle the reflect-stage inputs (state + frontmatter + diff + subagent reports + friction + reflect_config + a best-effort `harness_eval` availability block advertising the corpus regression eval). | `--ticket --ticket-dir --ticket-frontmatter --cwd` |
| `observe_ship_event.py` | Sole writer of `ship-events/<ticket>.json` (atomic, dupe-safe). | `--ticket --evidence-json --run-id --workspace-root` |
| `machinery_edit.py` | Flock-serialized applier for reflect lens-B self-edits to flow's OWN source. Refuses out-of-tree + snapshot-pinned paths + skill-root on a protected branch (main/master/dev/develop → propose+record instead). See `../references/self-evolution.md`. | `apply --skill-root --payload` |
| `flow_beads_create.py` | File a self-work (machinery) bead into flow's OWN beads, gated on maintainer mode; always targets flow's beads, never the run's tracker. | `--workspace-root --summary --description [--type --labels --parent]`; exit 4 = not maintainer |
| `_evolve_common.py` (lib) | Shared evolve/queue-drain helpers: `ToolError`/`NotMaintainer`, tool-call ok-wrapper, tolerant JSON-list parse, `feature/<key>` branch→key regex, bead label sets, worktree-pool run-dir resolution, selector primitives (in-flight join, branch/PR ref gather, pre-PR live-lease scan, BLAST-RADIUS anchor). | imported by evolve_reap, evolve_select, evolve_drain, evolve_session_cleanup, queue_select, queue_drain |
| `evolve_select.py` | Drain select core: select + partition the next batch of evolve beads to launch (`bd ready -l evolve`, drop in-flight, backpressure, coarse hot/anchor serialization). Pure, no side effects. | `--workspace-root [--cap --concurrency]`; exit 4 = not maintainer. Consumed by `evolve_drain.py` (the `/flow evolve drain` loop) |
| `queue_select.py` | Day-job sibling of `evolve_select.py`: select + partition the next batch of non-evolve beads to launch (`bd ready` unlabelled minus epic/evolve/proposal/hot, drop in-flight, queue-scoped backpressure counting only PRs outside the active-evolve set, anchor dedup, `model_per_key` sonnet for `tier:trivial`). No hot-serialization layer. Pure, no side effects. | `--workspace-root [--cap --concurrency]`; exit 4 = not maintainer, 2 = bd/git/gh error. Consumed by `queue_drain.py` (the `/flow queue drain` loop) |
| `queue_status.py` | Read-only day-job queue status (the `/flow queue` verb's core): wraps `queue_select.select()` with the full day-job ready backlog (a second `bd ready` re-filtered through the day-job rules), per-key lease liveness (`evolve_drain.liveness_map`), and the ADVISORY next action a drain would take (`evolve_drain.decide`). Touches no file ever — `launched_pending`-minus-registered is computed in memory, never via launch-ledger marker removal. | `--workspace-root [--cap --concurrency]`; exit 4 = not maintainer, 2 = bd/git/gh error. Consumed by `references/verb-queue.md` (the `/flow queue` verb) |
| `launch_ledger.py` | TTL launch ledger: a per-key marker written at `claude --bg "/flow <key> --auto"` time so the drain selector treats the key as in-flight during the launch→init blind window (no branch/PR ref, no pre-PR lease yet), closing the re-launch + 2nd-hot-isolation gap until a real lease/branch registers or the marker self-expires (`LAUNCH_TTL_SECONDS=1800`). Markers in the MAIN checkout `.flow/launch-ledger/`. | `add --key <K> --workspace-root <dir>` / `prune --workspace-root <dir>` / `list --workspace-root <dir> [--json]`; exit 4 = not maintainer. Imported by `evolve_select`, `evolve_drain`, `queue_select`, `queue_drain`; written by `references/verb-evolve.md` + `references/verb-queue.md` (drain step C) |
| `evolve_reap.py` | Drain reap-step core: classify open evolve PRs for auto-merge (green + leaf + mergeable → `merge`; a hot leaf also merges under `[evolve] auto_merge_hot` + isolation, one hot per pass; a green non-hot DIRTY → `version_recoverable` for merge-time version-conflict recovery; else not_green/skipped_hot/blocked). Pure; the loop does the `gh pr merge`. Role: orphan safety-net (a run that died before self-merging) + worktree teardown. | `--workspace-root`; exit 4 = not maintainer. Consumed by `/flow evolve drain` (reap step) |
| `evolve_drain.py` | Drain loop's next-action decider: `decide(select_result, liveness) → {action: launch\|wait\|done, launch, parked}`. CLI runs `evolve_select.select()` + annotates each in-flight bead with its run's lease liveness (`lease.classify`), so the loop terminates on "nothing startable + nothing live" and never spins on a withheld (parked) hot bead. Pure `decide()`; CLI removes launch markers for registered runs, else read-only. The pure `decide()`/`liveness_map()` core is also reused by `queue_drain.py` (import, no edit). | `--workspace-root [--cap --concurrency]`; exit 4 = not maintainer, 2 = bd/git/gh error. Consumed by the `/flow evolve drain` loop |
| `queue_drain.py` | Day-job sibling of `evolve_drain.py`: the `/flow queue drain` loop's next-action decider. CLI runs `queue_select.select()`, queue-scopes the wait gate (subtracts active-evolve keys from `live_runs`/`launched_pending` — the shared worktree pool + launch ledger must never make the day-job loop wait on a live evolve run), annotates in-flight day-job runs with lease liveness, and classifies merged flow PRs with a registered worktree or pending launch for reaping (pure `classify_reap`; a reaped launch key is dropped — merged-but-unclosed beads divert to the close path, never relaunch). Removes launch markers for registered runs; NEVER merges PRs (day-job PRs park for the maintainer). | `--workspace-root [--cap --concurrency]`; exit 4 = not maintainer, 2 = bd/git/gh error. Consumed by the `/flow queue drain` loop (`references/verb-queue.md` §drain) |
| `evolve_session_cleanup.py` | Drain session-cleanup core (step A2): classify finished `claude --bg /flow <key> --auto` jobs that are safe to stop + tombstone (filesystem scan of `~/.claude/jobs/*/state.json`, NEVER `claude agents --json`). Maps job→bead via the `intent` field; stoppable only when cwd is this repo's root + bead terminal + done/idle + lease non-live (the mid-reflect guard; absent worktree = non-live → proceeds) + transcript mtime idle; any busy/unprovable signal skips. Pure `classify()`; CLI read-only (the loop prose runs `claude stop` + `rm -rf <job_dir>`). | `--workspace-root [--self-job --idle-threshold-secs]`; exit 4 = not maintainer, 2 = bd error. Consumed by the `/flow evolve drain` loop (A2) |
| `evolve_self_merge.py` | Self-merge gate (the `merge` stage core): pure `decide(labels, is_maintainer, auto_merge_hot, ci_status, planned_files, eval_status) → {action, is_hot, reason}`, where `is_hot` is the `hot` label OR a guard-file hit in `planned_files` (reuses `triage.is_hot_change`), and a non-"pass" `eval_status` (the `harness_eval` verdict, fed by the stage when the PR touches scripts) blocks the merge (Self-Harness no-degradation rule). The stage acts on it: a hot bead gets an independent reviewer subagent (§6A) before `forge_cli merge`. | `--workspace-root --key --ci-status [--eval-status]`; consumed by `references/stage-merge.md` |
| `version_remerge.py` | Merge-time version-conflict recovery (Option B): on a green non-hot DIRTY evolve PR, merge the default branch into the feature branch and auto-resolve the conflict ONLY when it is EXACTLY the two version files (`plugin.json` + `marketplace.json`); take main's content, re-stamp the semantic NEXT from MAIN (MINOR on feat, PATCH otherwise; type from `--commit-type`, else a branch-only commit-subject scan), re-PUSH. STRICT detector: any other conflicting file → `git merge --abort`, recover nothing. Re-pushes but NEVER merges the PR — the caller re-waits CI on the new SHA, then merges. | `recover --branch <feature/...> --workspace-root . [--cwd <path>] [--commit-type <type>]`; exit 0 = remerged/remerged_clean, 3 = non-version-conflict (leave for human), 2 = tool error. Consumed by `references/stage-merge.md` (§3) + `references/verb-evolve.md` (drain reap) |
| `version.py` | Version-derivation + merge-time stamp seam (epic flow-6gx): compute the next plugin version (read the current `plugin.json` version on a git ref; semantic bump: MINOR on a feat commit type, PATCH otherwise — type from `--commit-type`, else the HEAD commit-subject conventional prefix) and surgically write it into both version files (`plugin.json` top-level + the `marketplace.json` flow entry), preserving JSON formatting. The per-PR bump is gone; `stamp` runs at merge time. | `next [--ref <ref>] [--cwd <path>] [--commit-type <type>]` prints JSON `{"ref","current","next","bump","commit_type"}`; `stamp [--ref <ref>] [--cwd <path>] [--commit-type <type>]` writes both files then prints the same JSON; exit 0 = ok, 2 = tool error. `stamp` consumed by `references/stage-merge.md` (§3). |
| `create_pr.py` | `create_pr` stage handler: git push of the branch, then open/resolve the PR through the forge seam (`fg.detect_pr` / `fg.open_pr` via the injected `Forge` adapter), so the same handler serves GitHub and Bitbucket. Title from HEAD commit subject (not `--fill`); ready-for-review by default, `--draft` only when `[create_pr] draft = true` or `--draft` passed; idempotent (reuse existing PR on resume). Base branch from `--base`, else `[create_pr] base` in workspace.toml, else `main`. Prints `PR_URL=<url>`. | `--workspace-root [--base BRANCH --ticket KEY --draft]`; exit 3 = protected branch. Wired `create_pr = "inline"` via `references/stage-create_pr.md` |

## Work-mode quality gate

| Script | Role | Surface / touches |
|--------|------|-------------------|
| `metric.py` | Metrics calculator: shipped tickets/week, time-to-PR, friction events/run, and revert-rate — from ship-event and friction-jsonl evidence (revert-rate joins ship-events to `bd history`). | (via `recall.py --metric`) |
| `baseline_collect.py` | Pre-migration time-to-PR baseline file + stats. | `build --samples-json` / `show` |
| `harness_corpus.py` (lib) | Frozen decider-fixture corpus loader/validator + replayer (regression-eval, epic flow-63q): replays held_in/held_out cases from the sibling `harness_corpus.json` data file against the four pure deciders (`evolve_select.partition`, `evolve_drain.decide`, `evolve_self_merge.decide`, `triage.is_hot_change`); the injectable `resolve=` is the candidate-checkout seam. | Frozen by `tests/test_harness_corpus.py` (full replay each CI run); imported by harness_eval, reflect_inputs |
| `harness_eval.py` | Regression-eval scorer (epic flow-63q): replays the frozen corpus against a candidate skill-checkout AND a baseline via an isolated subprocess driver per checkout; emits per-split pass/regress delta + `non_regression`; raw data only, gating policy lives in the merge stage (`stage-merge.md` §1 maps the exit code to `evolve_self_merge.py --eval-status`). | `score --candidate <scripts-dir> [--baseline <scripts-dir> --corpus <file> --timeout-secs N]` prints delta JSON; exit 0 = clean, 1 = bad dirs, 2 = corpus/driver error, 3 = regression. `drive` is the internal stdin-JSON replay driver. |
| `pending_mutations.py` (lib) | Transient tracker-mutation queue (create/edit/transition/comment/link). | imported by sync, tracker_cli |
| `sync.py` | Drain `pending-mutations.jsonl` + reconcile against live tracker. | `--workspace-root` |

## Status / recovery / friction

| Script | Role | Surface / touches |
|--------|------|-------------------|
| `status.py` | Read-only run/stage/lease table (no network). | `[--ticket] --workspace-root [--json]` |
| `triage.py` | `list`: read-only `deferred` + decided-mode `blocked` queue with each one's defer comment (beads only), every row tagged `queue=evolve\|day-job` (evolve label); `--ready` opt-in adds the ready queues. `decided`: probe a bead's recorded triage decision; returns `{decided,answer,is_hot}` JSON. Houses `_GUARD_FILES` + `is_hot_change`. | `list [--workspace-root --json --ready]` / `decided --key [--workspace-root --files]` / `adjudicate-enabled [--workspace-root]` / `adjudicate-hot-enabled [--workspace-root]` |
| `recover.py` | Inspect + remediate a broken run. | `detect` / `takeover` / `retry` / `skip` / `abort` / `reload-snapshot` |
| `flow_friction.py` | Append-only `friction.jsonl` log (the reflect/self-evolution feedstock). | `--ticket --run-id --stage --type --body [--detail --severity]` |

## Shared helpers (lib)

`_atomicio.py` (atomic temp-write + fsync), `_timeutil.py` (UTC ISO8601 parse + format; `require_z` for the strict contract, `utcnow_iso`/`iso_z` emitters), `_workspace.py` (workspace.toml load), `_registry.py` (stage-registry parse), `_locking.py` (flock retry), `_jsonl.py` (JSONL sidecar parse), `_runner.py` (subprocess-runner factories: positional-cwd `Runner`/`default_runner` for diff_extract/branch_ticket/recall_pending/flow_worktree/flow_beads_create, keyword-only `KwRunner`/`kw_default_runner` for init/tracker_beads, cwd-bound `CwdRunner`/`cwd_default_runner` for forge_github/forge_bitbucket/evolve_reap/evolve_select/queue_select/queue_status/queue_drain/create_pr/version_remerge), `maintainer.py` (maintainer-mode detection via the `[maintainer]` marker; gates the self-evolution loop).

## Dev tooling

| Script | Role | Surface / touches |
|--------|------|-------------------|
| `seam_check.py` | Validate every `${CLAUDE_SKILL_DIR}/scripts/*.py` invocation in SKILL.md + references against each script's real argparse surface. CI gate + `tests/test_seam_check.py` live-docs check. | `[--verbose]`; exit 1 on any unknown flag/subcommand |
