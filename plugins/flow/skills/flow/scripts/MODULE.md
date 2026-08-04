# Script map (current)

The live "which script does what" map. One authored row per script: purpose plus the contract notes argparse cannot express (exit codes, artifact paths, who consumes it). The mechanical surface — every script's subcommand names and true importers — is the generated table in §Derived surfaces at the bottom: regenerate it with `python3 module_map.py write`; `seam_check.py` fails when it is stale. For API/contract tables (Jira REST mapping, beads CLI surface, `state.json` schema), see `inventory.md`.

`lib` = imported module, no standalone CLI. Everything else is a thin CLI subprocessed from SKILL.md prose, a reference doc, or another script.

## Reader entry-point map

The ordered file chain (prose → contract → code) for the questions a fresh reader asks most. Answers are assembled across files by design; this table is the index, not a fourth copy.

| Question | Chain |
|----------|-------|
| How does a stage get dispatched, end to end? | `references/delivery-loop.md` → `dispatch_stage.py` row below → `inventory.md` §Handler-descriptor JSON shape + §Stage lifecycle |
| Where does run state live; what is its schema? | `state.py` row below (state lives in the worktree's `.flow/runs/<key>/`) → `inventory.md` §state.json schema |
| How do memory commands work? | `references/command-memory.md` → `recall.py` / `memory_append.py` rows below; the store is `.flow/memory/<namespace>/knowledge.jsonl` |
| How does self-evolution edit the machinery mid-run? | `references/self-evolution.md` → `references/stage-reflect.md` §Lens B → `machinery_edit.py` |
| How do I add a tracker/forge backend? | §Adding a tracker/forge backend below |
| How do I repair a broken run? | `references/delivery-repair.md` → `recover.py` row below |
| Why is this safety code load-bearing? | `references/robustness.md` (threat → file → witnessed failure per mechanism) |

## State machine + run safety (hot path)

| Script | Role | Contract notes |
|--------|------|----------------|
| `dispatch_stage.py` | State-machine driver for the delivery loop. Does NOT run handlers; emits a handler-descriptor JSON for the prose layer. `revise-open` opens a revision sub-run under a terminal run (own lease/state/snapshot at `runs/<ticket>/revisions/<id>/`, original untouched); `--revision <id>` redirects `next`/`advance`/`release` to drive that sub-run. | `advance --skill-output-from` reads optional structured stage output from a file; reads+writes `state.json` |
| `state.py` (lib) | Atomic `state.json` read/write under flock, backup rotation, quarantine recovery. | — |
| `lifecycle.py` (lib) | Pure reducer over normalized tracker, run, lease, snapshot, revision, and forge evidence. Returns the closed `start\|answer\|resume\|running\|repair\|revise\|show\|conflict` action vocabulary; its multi-target coordinator returns direct, needs-choice, sequential, or together and permits together only when every action is `start`. | no I/O; consumed by the public target router |
| `cockpit.py` (lib) | Pure attention-first cockpit join and logical-FLOW renderer over normalized run, deferred, pending-write, and PR-feedback evidence. | — |
| `snapshot.py` (lib) | Canonical workspace snapshot at init; verify on each `next` (TOCTOU drift guard). The WORKSPACE drift snapshot — not state.json's `.bak` backups, which are state.py's. | — |
| `lease.py` (lib) | Per-ticket run lease: acquire / refresh / release / expiry + takeover detection. | — |
| `validate_workspace.py` | HARD GATE: schema-validate `workspace.toml` + `stage-registry.toml` on every run. | exit 1 = violations to stderr |

The fifth run-safety mechanism, the content-ownership commit gate, is `diff_extract.py`'s `check-ownership` (§Frontmatter / diff / commit) — AGENTS.md's "content-ownership commit gate" resolves there. The full threat → file → witnessed-failure map is `references/robustness.md`.

## Bootstrap

| Script | Role | Contract notes |
|--------|------|----------------|
| `init.py` | Transactional workspace bootstrap. Collects backend/bundle answers, writes `workspace.toml`, preserves optional `[models]` hints on reconfigure, checks postconditions, and atomically writes `.flow/.initialized`. | `--config <json>` (`--reconfigure` / `--resume`) |
| `runtime_layout.py` (lib) | Layout-v2 resolver: install and rebind the runtime pointers (relocatable local and absolute external memory bases), namespace collision refusal, rebind refusal when the recorded namespace already holds data. | — |
| `flow_launcher.py` | Install or repair `.flow/runtime/{skill-root,flow}`; stabilize both Claude Code and Codex versioned plugin-cache paths to a local marketplace source when available (`CODEX_HOME` respected). Imported at setup/worktree creation so both stamp the executing installation. | `--workspace-root`; exit 1 on missing workspace config, invalid metadata, or install error |
| `flowctl.py` | Allowlisted post-init facade: require an absolute workspace root, validate `FLOW_HARNESS` on every invocation, change cwd, export `FLOW_SKILL_DIR`, and exec the mapped implementation with unchanged args/signals/stdio/exit status. No raw-script escape hatch. | `--workspace-root <absolute-path> <command> [args...]`; unknown command or harness exits 2 |
| `public_commands_cli.py` | Operational registry seam; imports `public_commands.py` to validate public tokens before orchestration and to render identical logical help in every harness. | `route [--workspace-root <absolute-path>] -- <public tokens>` / `help [topic]`; compact sorted JSON for route, logical `FLOW` text for help, exit 2 invalid |
| `lifecycle_cli.py` | Read normalized host evidence from an absolute JSON file and invoke the pure target reducer without probing or mutation; coordinate already-reduced multi-target actions. | `reduce --evidence <absolute-json-file>` / `coordinate --action <action>... [--together --unattended --choice]`; compact JSON, structured errors |
| `cockpit_cli.py` | Construct the cockpit evidence model from an absolute JSON file and render the deterministic attention-first snapshot without probing or mutation. | `render --evidence <absolute-json-file> [--json]`; logical text or compact snapshot JSON, structured JSON errors |
| `flow_worktree.py` | Post-approval worktree seeding plus the exported `is_ticket_branch` ownership predicate shared by preview and reap. Resolves the approved base, seeds the approved Markdown plan and `state.json`, stamps frontmatter, and binds the worktree's v2 memory pointer to main. Reap guards base/revision state, verifies an optional expected tip, and checkpoints dirty work before removal. | flags per `--help`; `create --auto` gates the unattended path |
| `branch_ticket.py` | Resolve ticket key from current git branch (backend-aware regex); `--branch <name>` resolves from an explicit branch instead (the PR->ticket enabler for revise). | `--workspace-root [--branch]`; exit 0 match / 1 env / 3 no-match |
| `scrutinize_seat.py` | Deterministic half of the scrutinize seating (`references/scrutinize.md` §Seating): refuse outside the self-target, fetch origin, resolve default/integration refs, name configured tracker/forge without adapter construction, scan registered worktrees for non-terminal base/revision runs, safely fast-forward or re-park only without local run evidence, ensure the standing bench, and emit bounded local posture. Invocable from the bench itself; posture always describes the primary checkout. | `--workspace-root [--dry-run]`; exit 0 posture ok, 2 failure (posture printed for fetch/bench failures; a pre-posture probe error goes to stderr instead). Consumed by `references/scrutinize.md` |
| `scrutinize_trace.py` | Transcript trace miner for the scrutinize sweep (`references/scrutinize.md` §The sweep): one incremental pass per session file under a `~/.claude/projects/<slug>/` dir, emitting the dispatch spine (facade calls), user messages with an `is_skill` flag (nudge lens; plain-string and list content both read), agent spawns joined to `<session>/subagents/agent-*.jsonl` spans (per-stage wall clock lives there), and tool errors with their originating command. Read-only; replaces the per-seat ad-hoc miner rebuilt every sweep before 2026-08-03. | `--transcript-dir [--since <iso>] [--session <id>]... [--json]`; exit 0 mined / 2 missing dir. Consumed by `references/scrutinize.md` |
| `_harness.py` (lib) | The closed host-adapter vocabulary: `flow_harness()` reads `FLOW_HARNESS`, defaults an unset selector to `claude-code`, and raises `HarnessError` on anything but `codex`/`claude-code`. Deliberately import-light — the workspace shims read it on every facade call. | — |

## Tracker

| Script | Role | Contract notes |
|--------|------|----------------|
| `tracker.py` (lib) | Tracker Protocol base + `make_tracker()` factory. Adapters load lazily inside `make_tracker`; `flow_worktree` imports lazily in `_refuse_terminal_bead`. | — |
| `tracker_cli.py` | CLI wrapper around the Protocol (the only tracker surface the prose calls). `list-assigned` exposes compact cross-backend assigned-ticket reads with a default non-terminal filter. `is-shipped` consults the frozen ship-event file before the adapter (state=shipped / source=frozen_event_file when one exists), so the live backend query runs only for a never-observed ticket. | subcommand names in §Derived surfaces |
| `tracker_jira.py` (lib) | Jira Cloud REST v3 + Agile/1.0 adapter (Basic auth via `ATLASSIAN_EMAIL`/`ATLASSIAN_API_TOKEN`). | — |
| `tracker_beads.py` (lib) | Beads `bd` CLI adapter (local-only tracker). | — |

## Forge (PR host)

Pluggable PR-host seam, structural twin of the tracker seam. The `create_pr` and `review_loop` stages reach the host ONLY through `forge_cli.py`, so a GitHub and a Bitbucket workspace run the same prose. Selected by `[forge] backend = "github" | "bitbucket"` in `workspace.toml` (the block is OPTIONAL; absent = no forge).

| Script | Role | Contract notes |
|--------|------|----------------|
| `forge.py` (lib) | Forge Protocol base + `make_forge()` factory + `read_forge_config()` + normalized `PullRequest`/`CIStatus`/`ReviewThread`. `detect_pr` selects open or merged state; adapters include the optional head SHA and produce commit-pinned source URLs for reviewer evidence. Adapters load lazily inside `make_forge`. | — |
| `forge_cli.py` | CLI wrapper around the Protocol (the only forge surface the prose calls); `list-authored` returns compact open PR rows, and cap-gated subcommands degrade to `{"supported": false}` exit 0. `detect-pr` accepts `--state open\|merged`. | subcommand names in §Derived surfaces |
| `forge_github.py` (lib) | GitHub `gh` adapter: detect/open PR, CI rollup (`statusCheckRollup`), mark-ready/merge/delete-branch, and commit-pinned `blob/<sha>/<path>#Lx-Ly` source URLs. review_threads/post_reply/resolve_thread supported via gh api graphql. `set_default_reviewers` raises `NotSupported` (solo repo, CODEOWNERS covers reviewers). | — |
| `forge_bitbucket.py` (lib) | Bitbucket `bkt` adapter (absorbs ship-it): detect/open PR, CI rollup from `bkt pr checks`, commit-pinned `src/<sha>/<path>#lines-x:y` source URLs, CodeRabbit review-thread fetch + verified resolve (`.resolution != null`), `set_default_reviewers` (GET `2.0/user` author + GET `default-reviewers`, drop author by `account_id`, PUT `{reviewers:[{uuid}]}`). | — |
| `review_brief.py` | Deep, stdlib-only review-companion renderer. Strictly validates the motivation-first JSON model, binds local and PR heads to one full SHA, extracts source from that commit, builds responsive/CSP-protected self-contained HTML with exact Forge links, publishes atomically, and records/probes freshness. | writes `<ticket-dir>/stages/review_brief/<sha>/{brief.json,review-brief-*.html,receipt.json}` |

### Adding a tracker/forge backend

The seams are designed for extension but had no recipe; the ordered touch-points (forge shown, the tracker seam mirrors each step in `tracker.py` / a `tracker_<name>.py` adapter / `tracker_cli.py`):

1. `forge.py`: add the name to `KNOWN_BACKENDS` and a lazy-import branch in `make_forge()`. `read_forge_config()` validates against `KNOWN_BACKENDS` a SECOND time — miss it and every workspace read rejects the new backend even though `make_forge` knows it.
2. Write `forge_<name>.py` implementing the Forge Protocol (`forge_github.py` is the reference adapter; raise `NotSupported` for what the host lacks — that raise IS the capability gate, and `forge_cli.py` already degrades a cap-gated subcommand to `{"supported": false}`, no CLI change needed).
3. Extend the `[forge]` workspace schema (inventory.md §`[forge]` workspace schema) and the `init.py` wizard prompts.
4. Adapter tests mirroring `tests/test_forge_github.py`; run `seam_check.py` (prose flag references must still resolve).

### Adding a public command

Edit `public-commands.toml`, run `python3 public_commands_check.py write` to regenerate
SKILL.md's derived surfaces, then `python3 public_commands_check.py` to confirm it is clean.
Public paths describe intent; internal facade script names remain implementation
details. A command change touches the skill router and is therefore hot.

## Frontmatter / diff / commit

| Script | Role | Contract notes |
|--------|------|----------------|
| `ticket_frontmatter.py` | TOML frontmatter r/w under flock + atomic rename (delimiter `+++`). | `read <path>` / `update <path> --set k=v` |
| `lint_ticket.py` | HARD GATE: required frontmatter fields per stage. | `--stage --ticket-path --workspace-root` |
| `lint_comments.py` | Deterministic comment-quality floor under the stage-implement bar (Step 4): flags em-dash, banned filler/inflation vocabulary, narration markers, and over-limit or under-filled comment/docstring prose in the files a run touched. Python is exact (tokenize + ast); other languages get line-start markers only, so string literals cannot false-positive. Markdown (`.md`/`.markdown`) runs the em-dash check only, outside fenced code blocks (docs are prose, so the banned-word and width checks stay off). Line limit auto-discovered per file (ruff/black/.editorconfig, default 88); `--line-length` overrides. `--diff-base <ref>` keeps only findings on lines changed vs the ref (how the stages scope a legacy file to the run's own edits). | `<file> [...] [--line-length N] [--diff-base REF] [--json]`; exit 0 clean / 1 findings. Consumed by `references/stage-implement.md` Step 4 |
| `diff_extract.py` | Git diff capture for implement/code_review/commit/reflect; baseline + ownership. `capture-implement-diff` anchors on the moving `baseline.head_sha` and keeps `--binary --raw` so the commit stage applies only remaining work. `capture-review-diff` anchors on the stable `baseline.origin_sha` so committed work remains visible after a baseline re-record; a legacy baseline without that key falls back to `head_sha`, while an invalid present value is refused. It drops `--binary --raw` so the reviewer gets a text-only payload with binary elided. The captures are deliberately separate functions, not one parameterised helper, so the guard function cannot be changed by editing the review path, and each spells out its own refusal of an empty `planned_files`, which would otherwise diff the whole repository for want of a pathspec. `check-ownership` IS the content-ownership commit gate (AGENTS.md's term): refuses a branch delta outside the baseline `planned_files`, dirty tree AND committed, so a rogue mid-implement commit is seen too. Its committed half anchors on `baseline.origin_sha`, written once and preserved across re-records, so the post-implement reconcile widens the owned set without shrinking the scanned range to nothing, and the result reports `ownership_anchor`, `anchor_source`, and `committed_scan_empty`. `committed_scan_empty` is about THAT HALF ALONE and never means the gate checked nothing: the ordinary healthy run has every edit uncommitted, so its committed range is empty while the working-tree half checks the whole change. A scan that covered nothing is the conjunction of `ok`, an empty `changed`, and `committed_scan_empty`. | subcommand names in §Derived surfaces |
| `compose_commit.py` | Deterministic conventional-commit header skeleton (LLM fills body). | `--ticket --type --summary [--scope --files --covers]` |
| `scrub_ci_skip.py` | Neutralize bracketed GitHub CI-skip tokens (`[skip ci]` etc.) in a commit-message file, in place; strips the brackets, keeps the words. Exit 0 always. | `<message-path>` |

## PR lifecycle

Opening and versioning the PR (the host seam itself is §Forge above).

| Script | Role | Contract notes |
|--------|------|----------------|
| `create_pr.py` | `create_pr` stage handler: git push of the branch, then open/resolve the PR through the forge seam (`fg.detect_pr` / `fg.open_pr` via the injected `Forge` adapter), so the same handler serves GitHub and Bitbucket. Title from HEAD commit subject (not `--fill`); body from the required authored `--body-file` (de-AI `pr_body.scrub` floor + `closes_footer` appended), falling back to the subject when the authored prose is empty. On first open, attaches default reviewers via `fg.set_default_reviewers` (swallows `NotSupported` + any `ForgeError` so a reviewer hiccup never fails the PR). Draft by default, ready-for-review when `[create_pr] draft = false` (`--draft` forces a draft); idempotent (reuse existing PR on resume). Base branch from `--base`, else `[create_pr] base` in workspace.toml, else `main`. Prints `PR_URL=<url>`. | `--workspace-root --body-file FILE [--base BRANCH --ticket KEY --draft]`; exit 3 = protected branch. Wired `create_pr = "inline"` via `references/stage-create_pr.md` |
| `pr_body.py` (lib) | PR-body helpers for `create_pr` (no CLI). `closes_footer(raw_commit_body)` extracts the `Closes <KEY>` trailer lines the script appends under the authored `--body-file`. `scrub(body)` runs a deterministic, idempotent de-AI pass (em-dash → punctuation, sentence-case `# Heading`, flatten `- **Term:**` bullets; fenced code untouched) as the floor over the authored body. `enforce_cap(body, cap=32000)` is the deterministic body-size net under the stricter forge description limit: over cap it shrinks the largest fenced blocks (head+tail lines around a `… N lines trimmed …` marker), then drops `<details>` bodies keeping their `<summary>`, then hard-truncates; idempotent, ≤ cap on every non-exceptional path. `flatten_details(body)` rewrites each `<details>`/`<summary>` wrapper to a `###` heading + body (fenced content kept; no match = byte-identical passthrough); `create_pr` applies it when the forge backend is bitbucket, which renders no raw HTML in markdown. All TOTAL (never raise; passthrough on adversarial input). | — |
| `revise_config.py` | Reader for the `[revise]` block of workspace.toml (revision sub-runs, epic flow-kx17). `plain_comment_severity` (default `"minor"`, validated against `forge.THREAD_SEVERITY`) is the floor the revision review_loop applies to unresolved minor (plain human) threads; bad value / missing config → default, exit 0. The pure `apply_floor(threads, severity)` helper bumps every UNRESOLVED `minor` thread to the floor (returns new dicts, no-op when `minor`); the bump stays loop-side so `forge_github._severity_from_state` is pure of config. | `apply-floor --workspace-root .` reads a threads JSON array on stdin and prints it floored. Consumed by `references/stage-review_loop.md` (revision mode) |
| `version.py` | Version-derivation + merge-time stamp seam (epic flow-6gx): compute the next plugin version (read the current `plugin.json` version on a git ref; semantic bump: MINOR on a feat commit type, PATCH otherwise — type from `--commit-type`, else the HEAD commit-subject conventional prefix) and surgically write it into both version files (`plugin.json` top-level + the `marketplace.json` flow entry), preserving JSON formatting. The per-PR bump is gone; `stamp` runs server-side post-merge on `main` (the `version-stamp.yml` Action). | `stamp [--ref <ref>] [--cwd <path>] [--commit-type <type>]` writes both files then prints JSON `{"ref","current","next","bump","commit_type"}`; exit 0 = ok, 2 = tool error. Consumed by `.github/workflows/version-stamp.yml` (the server-side post-merge Action). |

## Memory / recall

| Script | Role | Contract notes |
|--------|------|----------------|
| `_memory_paths.py` (lib) | Namespace resolution + `.flow/memory/<namespace>/` path conventions + `load_semantic_config`. Layout v2 reads `.flow/runtime/memory-root`, treats `.flow/memory` as a relocatable workspace-local sentinel, and refuses missing/malformed pointers or an unstamped workspace. | — |
| `memory_append.py` | Single-writer `knowledge.jsonl` append with sha-keyed idempotency. `--supersedes` is a CLI-level single string; the library `append()` also accepts a list of target ids (a canonical entry consolidating a whole cluster, used by `sweep_knowledge.apply_cluster`, not exposed as a CLI flag). `--labels` is an optional CSV `facet:value` array, metadata only (not a hash input). | `--type --text --branch --ticket [--supersedes --labels]` |
| `preflight.py` | Credential preflight for the `[preflight]` block of workspace.toml (flow-g4iz, credentials-only by human ruling: no binary/daemon checks, no auto-install). `check` is the attended plan-time mode (inherits stdio so a check-then-login wrapper like `mise sso` can surface its SSO URL; 600s ceiling); `probe` is the silent stage-side mode (captured output, stdin closed, 60s ceiling, one clean JSON line for the agent to parse). Absent block/key → `unconfigured`, exit 0, zero cost. | `check|probe --workspace-root . [--dry-run]`; exit 0 ok/unconfigured/would_run, 2 failed/timeout, 3 config error. Consumed by `references/delivery-plan.md` §1 (check) and `references/stage-e2e.md` step 3 (probe) |
| `recall.py` | BM25 ranker over `knowledge.jsonl` with an OPTIONAL semantic-fusion overlay (cosine over the `memory_embed` sidecar index, rank-based top-K cosine selection, RRF over BM25; any failure → BM25 fallback + stderr backend status; `[memory.semantic]` absent/off → byte-identical BM25). `--threshold` is a low floor (drop non-positive cosines), NOT the candidate gate. `--label` is a HARD pre-filter over `labels[]` (exhaustive, bypasses `--top-n`, query optional). `--digest` (requires `--label`) renders that cluster as a markdown card (sections by type, newest-first) instead of JSON. `--reindex` dispatches to `memory_embed.py`. Library-only `similar_entries` is the separate silent seam: pure cosine, an ABSOLUTE floor, `[]` on any unavailability, used by `flow_friction` to answer an append. | `<query> [--branch --tickets --ticket --label --digest --top-n --include-superseded --semantic --threshold --query-file --record-pending --reindex --full]` |
| `memory_embed.py` | Embedder seam (shells a configured command — `[memory.semantic].embedder` or the default `uvx --with fastembed python embedder_fastembed.py`; pure stdlib, never imports the model) + derived sidecar index under `.flow/memory/<namespace>/knowledge.embed` (supersede-filtered, incremental, model-mismatch → full rebuild). | `reindex --workspace-root [--full --model --embedder]` / `embed [--workspace-root --model --embedder]` (stdin texts → JSON vectors); exit 1 = workspace invalid, 2 = embedder unavailable |
| `embedder_fastembed.py` | DEFAULT reference embedder, run BY `uvx` (`--with fastembed`, ONNX, no torch); stdin texts → JSON vectors via `fastembed.TextEmbedding(<model>).embed`. Default model `BAAI/bge-small-en-v1.5` (384-dim). Standalone subprocess entrypoint. | `[--model <id>]` (stdin → JSON); exit 0 ok, 1 load/encode failure |
| `sweep_knowledge.py` | Retro-curation sweep over `knowledge.jsonl`; propose-only until a confirmed manifest applies append-only supersession tombstones. Usage ranking and semantic clustering support `FLOW memory prune`. | subcommand names in §Derived surfaces |
| `recall_pending.py` (lib) | Promote recall-pending entries into the per-ticket recall log; the promoting rewrite also moves >24h entries to `.stale`. The producer is the plan-phase `recall.py --record-pending` (post-gate write); the dispatcher promotes at init. | — |
| `recall_usage.py` | Recall observability: append deduplicated usage and near-duplicate miss records beneath the v2 namespace; feeds recall-quality and pruning rank. | `record-usage --ticket --ticket-dir [--used-ids]` / `detect-misses --ticket --ticket-dir [--threshold]` |
| `reflect_inputs.py` | Bundle the reflect-stage inputs (state + frontmatter + diff + subagent reports + friction + reflect_config). | `--ticket --ticket-dir --ticket-frontmatter --cwd` |
| `observe_ship_event.py` | Sole writer of `ship-events/<ticket>.json` (atomic, dupe-safe). | `--ticket --evidence-json --run-id --tier --acceptance-invariant --lane --workspace-root` |
| `observe_at_close.py` (lib) | Freeze the ship event from a doomed run's `state.json` before the post-merge reap (janitor sweep / finalize) destroys it. `is_shipped`-gated (only `not_yet_observed` observes; PR#277's property never loosened) → run_id capture → tier/acceptance-invariant/lane gather → `observe_ship_event.observe` against the MAIN store, attribution stamped from the worktree's state.json. Never raises; returns an `{action}` dict (observed / skipped / failed). | imported by `finalize.py` + `worktree_janitor.py` |

## Self-evolution

The reflect stage's self-repair path — see `../references/self-evolution.md` for the model.

| Script | Role | Contract notes |
|--------|------|----------------|
| `machinery_edit.py` | Flock-serialized applier for reflect lens-B self-edits to flow's OWN source. Refuses out-of-tree + snapshot-pinned paths + skill-root on a protected branch (main/master/dev/develop → propose+record instead). See `../references/self-evolution.md`. | `apply --skill-root --payload` |
| `flow_beads_create.py` | File a self-work (machinery) bead into flow's OWN beads, gated on a route back to flow's repo; always targets flow's beads, never the run's tracker. | `--workspace-root --summary --description [--type --labels --parent --dedup-key --acceptance-invariant]`; exit 4 = not maintainer |

## Close-out and worker seams

| Script | Role | Contract notes |
|--------|------|----------------|
| `worker_pool.py` | Deterministic seams around host-native collaboration: reserves one driver slot, captures/compares strong read-only git receipts, and reduces durable driver-loss evidence. | `limit --configured --capacity` / `snapshot --workspace-root` / `guard --workspace-root --before` (exit 3 mutation) / `recover --evidence`. Consumed by `references/background-pipeline.md` §Worker contract |
| `worktree_janitor.py` | Workspace-local stale-worktree janitor. Derives the primary checkout from Git, recognizes only its managed worktree directories, resolves Jira or Beads keys, reads normalized tracker and forge state, and classifies each candidate conservatively. Open PRs are preserved; merged PRs require local-tip/head equality; terminal no-PR branches require a verified remote default and zero unique commits. A real sweep requires the preview's target and candidate confirmation IDs, then sends each removal through revision-aware `flow_worktree.reap_worktree`. | `sweep --workspace-root [--dry-run --confirmed-target --confirmed-candidate]`; exit 2 = repository-level git error. Consumed by `references/command-workspace.md` |
| `finalize.py` | Post-merge close-out for one delivered ticket (`FLOW ticket finalize`). Probe (no writes): locate the ticket's managed worktree or unique local branch, require merged-PR proof through the forge seam (open/no PR exits 3 so a host-owned watch re-invokes until 0), refuse on a live/corrupt lease, a merged-head/tip mismatch, or invocation from the doomed worktree itself. On proof, each step best-effort and idempotent in the canonical close-out order: tracker transition to done, `observe_at_close` ship-event freeze, remote branch delete, `reap_worktree` teardown. | `--workspace-root --key [--dry-run]`; exit 0 finalized / 2 probe error / 3 not merged / 4 refused. Consumed by `references/command-ticket.md` |

## Work-mode quality gate

| Script | Role | Contract notes |
|--------|------|----------------|
| `metric.py` | Metrics calculator: shipped tickets/week, time-to-PR, friction events/run, and revert-rate — from ship-event and friction-jsonl evidence (revert-rate joins ship-events to `bd history` AND scans the default-branch git log for reverts, emitting durable revert events via observe_ship_event), and a `trend` roll-up of all five window measures (table + `--json`), plus a `corpus-health` measure over `knowledge.jsonl` (live-vs-superseded entry counts, supersession rate, and oldest live DECISION age; the dead-set now folds list-valued tombstones via `recall.superseded_ids`) and a `recall-hit-rate` measure (precision = used/surfaced + `RECALL_MISS` count, from `recall-usage.jsonl`; auto-resolves `--namespace` from workspace.toml when omitted; the fifth trend measure), plus a `fix-efficacy` measure (per closed MACHINERY-fix bead, reusing `friction_recurrence`'s distinctive-anchor selection then a structural `(stage, type, anchor)` tuple join grounded in pre-fix friction, with a `recurred`/`clean` verdict + evidence, `claimed_tuples`, and a per-bead unmeasurable reason; lifetime metric, `--since`/`--until` ignored). | Consumed by `references/command-measure.md` |
| `pending_mutations.py` (lib) | Transient tracker-mutation queue (create/transition/comment/link; entries with any other op are parked by sync). | — |
| `sync.py` | Drain `pending-mutations.jsonl` + reconcile against live tracker. | `--workspace-root` |

## Status / recovery / friction

| Script | Role | Contract notes |
|--------|------|----------------|
| `status.py` | Read-only run/stage/lease table (no network). | `[--ticket] --workspace-root [--json]` |
| `group_candidates.py` | `FLOW ticket group` core: fetch + normalize grouping candidates (explicit keys, or the `--mine` assigned selector) through the tracker seam, then surface empty-body title-twin duplicate hints. Read-only; the lead+covers clustering judgment lives in `references/command-ticket.md`. | `[<key> ...] --mine --filter --workspace-root`; exit 1 tracker / 2 config / 3 no input. Consumed by `references/command-ticket.md` |
| `group_persist.py` | `FLOW ticket group` defer-path persistence: record a cover set as a `flow-group covers:` marker comment on the lead (`persist`, idempotent), read it back (`derive`), and dissolve it (`clear`, persists an empty set) so a grouping survives propose→act across sessions and can be undone. Cross-backend (only `comment`/`get`); the `references/delivery-plan.md` §6 bootstrap auto-derives `--covers` from it. | `persist --lead --covers --workspace-root` / `derive --lead --workspace-root` / `clear --lead --workspace-root`; exit 1 tracker / 2 config / 3 args. Consumed by `references/command-ticket.md` + `references/delivery-plan.md` |
| `triage.py` | `list`: read-only `deferred` + decided-mode `blocked` queue with each one's defer comment (beads only); `--ready` opt-in adds the ready queue. `decided`: probe a bead's recorded triage decision; returns `{decided,answer,is_hot,hitl}` JSON. `lane`: resolve a bead's verification lane (express\|light\|full) from its tier labels (delegates policy to `tier_policy.lane_for`; spec-time twin of `flow_worktree._lane_for_bead`). Houses `_GUARD_FILES` + `is_hot_change`. | — |
| `tier_policy.py` (lib) | Pure tier→verification-lane decider: `lane_for(labels)` maps tier labels to a lane (`tier:trivial`→express, `tier:light`→light, hot/untiered→full). Scales gate depth to the cost of being wrong (the xqt verdict operationalized). The per-lane gate policy (what each lane skips) lives in the stage prose that branches on the lane string (`references/delivery-plan.md`, `stage-implement.md`, `stage-reflect.md`), not here. No I/O — callers supply the labels. `triage` uses it in `lane`; `flow_worktree` in `_lane_for_bead`. | — |
| `model_resolve.py` | Resolve an optional native-agent model hint from `[models].<stage>`. Missing, disabled, or unsupported hints inherit the driver session model. | `--workspace-root --stage`; consumed by native agent dispatch |
| `recover.py` | Inspect + remediate a broken run. | recipes in `references/delivery-repair.md` |
| `flow_friction.py` | Append-only `friction.jsonl` log (the reflect/self-evolution feedstock). The CLI also echoes the live knowledge entries describing the same snag (`recall.similar_entries` above an absolute floor), after the durable write and guarded so no failure of it can change the exit code, the record, or the first stdout line. The library `append()` stays a pure writer. | `--ticket --run-id --stage --type --body [--detail --severity]` |
| `friction_recurrence.py` | Read-only forward-join of `friction.jsonl` to MACHINERY-prefixed `knowledge.jsonl` entries: surfaces friction classes that recurred after a claimed fix, clustered two ways (`signature_classes`, a single distinctive anchor token, cross-cutting stage/type; `structural_classes`, `(stage, type, anchor)`), carrying evidence (entry ids, run ids, fix sha) for a downstream judge. Reads friction/knowledge/ship-events, never writes. | `--workspace-root` |
| `friction_escalate.py` | Propose-only recurrence escalation: consumes `friction_recurrence.analyze` and files ONE deduped `recurrent`-labelled bead per signature class that recurred `>=K` times since its LATEST claimed MACHINERY fix (not the detector's earliest-anchored `post_fix_count`, which over-counts a multi-fix class). `K` + an exempt-anchor set are `[reflect]` workspace.toml knobs (`recurrence_escalation_k` default 3, `recurrence_exempt_anchors` default `[planned_files]`). Dedup key is the bare anchor (no `::`), so at most one bead per anchor ever and only the exact `evid:` dedup net fires. Labels are `recurrent` only, never `evolve`, so nothing auto-gates them. Auto-dormant without a route back to flow's repo (`flow_beads_create.resolve_maintainer_repo`). | `escalate --workspace-root` |

## Shared helpers (lib)

The highest-fan-in modules in the flat dir: a signature change here ripples through the import graph (the flat-dir invariant in AGENTS.md), so each helper's Imported-by column in §Derived surfaces is the blast radius — generated from the AST, so it cannot drift.

| Script | Role |
|--------|------|
| `_timeutil.py` | UTC ISO8601 parse + format; `utcnow_iso`/`utcnow_iso_ms`/`iso_z` emitters + the colon-free `ts_token` quarantine-filename stamp. |
| `_runner.py` | Subprocess-runner factories: positional-cwd `Runner`/`default_runner`, keyword-only `KwRunner`/`kw_default_runner`, cwd-bound `CwdRunner`/`cwd_default_runner`; each consumer picks the one flavor its call style needs. |
| `_workspace.py` | workspace.toml load + `plugin_version` manifest self-read. |
| `_locking.py` | Flock retry is the substrate under the lease, state, and memory writers. |
| `_atomicio.py` | Atomic temp-write + fsync + `os.replace` + parent-dir fsync (the rename itself is crash-durable). |
| `_jsonl.py` | JSONL quarantine parse + the read-only `read_jsonl_lenient` twin. |
| `maintainer.py` | Self-target routing via the `[maintainer]` marker. The public `--require-current` gate refuses configured redirects and names the external target. |
| `_registry.py` | Stage-registry parse + the single-source handler-string grammar `parse_handler`/`HANDLER_RE`. |
| `public_commands.py` (lib) | Load and validate `public-commands.toml`; classify static namespaces, configured tickets, PR targets, and removed-token errors; validate options; render deterministic help/router/trigger blocks without host syntax. `replace_generated_block`/`check_generated_block` are the managed-marker primitives `module_map.py` reuses. |

## Dev tooling

| Script | Role | Contract notes |
|--------|------|----------------|
| `public_commands_check.py` | Render + check SKILL.md's generated public-command surfaces (frontmatter description, public router block, public grammar block) and every command reference, plus the hand-authored namespace enumerations (the plugin/marketplace description strings and command-target.md's static-roots sentence must name only live static namespaces). `check` (default) reports drift without writing; `write` regenerates the three surfaces in place and is only ever run by a human or agent (hooks stay check-only). | `check` (default; exit 1 stale) / `write` |
| `module_map.py` | Render + check the generated derived surfaces: MODULE.md's §Derived surfaces table (subcommand names from AST `add_parser` constants, importers from the AST import graph) and stage-reflect.md's guard-file enumeration (from `triage._GUARD_FILES`). `check` is folded into `seam_check.py`, so CI and the prek hook catch staleness; `write` regenerates in place and is only ever run by a human or agent (hooks stay check-only). | `check` (default; exit 1 stale) / `write` |
| `seam_check.py` | Structurally validate documented absolute, call-local-harness facade commands against flowctl's allowlist and argparse surfaces. Reject cwd-dependent facades, host-specific public recipes, and stale direct scripts. Enforce descriptor, role, registry, and module-map contracts. Flag a recipe span that binds a zsh-reserved or otherwise unsafe shell name (`zsh_unsafe_binding_problems`), and a citation of a references/ doc that no longer exists (`dangling_doc_citation_problems`). | `[--verbose]`. Exit 1 on drift |

## Reference docs (generated)

One line per prose doc under `references/`, rendered from each doc's opening purpose
sentence by `module_map.py`. Regenerate with `python3 module_map.py write`; hand edits
inside the markers are overwritten. The machine indexes remain canonical for their maps:
`public-commands.toml` (command → reference doc) and `stage-registry.toml` (stage →
reference doc); this table is for finding a doc, not for wiring.

<!-- flow:docs-index:begin -->
| Doc | Covers |
|-----|--------|
| `references/background-pipeline.md` | Backgrounding is a host operation applied to the driver conversation. |
| `references/command-measure.md` | FLOW measure reads immutable delivery evidence, tracker history where required, and memory telemetry. |
| `references/command-memory.md` | Flow memory is append-only source data plus derived indexes. |
| `references/command-target.md` | This reference owns bare FLOW, FLOW <target>..., and FLOW help. |
| `references/command-ticket.md` | This reference owns ticket authoring, grouping, and splitting. |
| `references/command-workspace.md` | Workspace commands manage Flow's local installation, health, repairs, queued tracker writes, and runtime layout. |
| `references/delivery-loop.md` | The dispatcher owns state, lease refresh, snapshot validation, stage transitions, and the canonical descriptor. |
| `references/delivery-plan.md` | Planning is an attended conversation owned by the driver. |
| `references/delivery-repair.md` | Repairs are evidence-specific, target-specific, and confirmation-gated. |
| `references/delivery-revision.md` | A lifecycle revise action updates a delivered run's open PR. |
| `references/e2e-recipes.md` | Read this at plan time — delivery-plan.md's recipe-settling step — the moment you settle a ticket's e2e_recipe. |
| `references/harness.md` | Claude Code and Codex are the two hosts for the same Flow engine and public grammar. |
| `references/revision-triage-board.md` | The revision board is the disposition step of an attended same-PR revision sub-run: the human decides which unresolved comments to fix now… |
| `references/robustness.md` | The threat → file → witnessed-failure index that AGENTS.md's "Robustness (do not erode)" paragraph points at. |
| `references/scrutinize.md` | Scrutiny is flow's post-hoc maintenance verb. |
| `references/self-evolution.md` | Flow improves itself through the same ticket-to-PR lifecycle it applies to delivery work. |
| `references/stage-code_review.md` | Have one fresh reviewer challenge the implementation before commit. |
| `references/stage-commit.md` | Compose a conventional commit, apply the recorded implement-stage diff, and transition the tracker ticket. |
| `references/stage-create_pr.md` | Opens a PR for the run's feature branch — a draft by default, or ready for review when [create_pr] draft = false in workspace.toml… |
| `references/stage-e2e.md` | Execute the **e2e recipe the plan declared** and surface any failure. |
| `references/stage-implement.md` | Implement the ticket against its approved plan using strict TDD, and report only when the tests are green. |
| `references/stage-plan.md` | The inline plan stage records the one human-approved Markdown plan authored by the driver. |
| `references/stage-reflect.md` | Extract durable knowledge from this ticket's run, append entries to the compounding memory layer, and (if the ticket shipped) record an… |
| `references/stage-review_brief.md` | Generate a beautiful, read-only HTML companion for the human reviewing the PR. |
| `references/stage-review_loop.md` | Wait for the existing pull request's CI result, address actionable review findings, and stop. |
| `references/stage-ticket.md` | Resolve the ticket key, fetch ticket context from the tracker, write a local cache, and stamp the ticket's frontmatter status to… |
| `references/troubleshooting.md` | Machine/tool sharp edges that repeatedly burn fresh sessions. |
<!-- flow:docs-index:end -->

## Derived surfaces (generated)

Every script's argparse subcommand names and true importers, rendered from the AST by
`module_map.py`. Regenerate with `python3 module_map.py write`; hand edits inside the
markers are overwritten. `—` = none.

<!-- flow:module-map:begin -->
| Script | Subcommands | Imported by |
|--------|-------------|-------------|
| `_atomicio.py` | — | `diff_extract`, `dispatch_stage`, `flow_launcher`, `flow_worktree`, `init`, `lease`, `machinery_edit`, `memory_embed`, `pending_mutations`, `recall_pending`, `review_brief`, `runtime_layout`, `snapshot`, `state`, `ticket_frontmatter` |
| `_harness.py` | — | `dispatch_stage`, `flow_launcher`, `flowctl`, `init`, `model_resolve` |
| `_jsonl.py` | — | `friction_recurrence`, `memory_append`, `memory_embed`, `metric`, `pending_mutations`, `recall`, `recall_pending`, `recall_usage`, `reflect_inputs`, `sweep_knowledge` |
| `_locking.py` | — | `dispatch_stage`, `flow_friction`, `flow_worktree`, `lease`, `machinery_edit`, `memory_append`, `memory_embed`, `observe_ship_event`, `pending_mutations`, `recall_pending`, `recall_usage`, `state`, `ticket_frontmatter` |
| `_memory_paths.py` | — | `flow_friction`, `flow_worktree`, `friction_escalate`, `friction_recurrence`, `memory_append`, `memory_embed`, `metric`, `observe_at_close`, `observe_ship_event`, `recall`, `recall_usage`, `reflect_inputs`, `sweep_knowledge`, `tracker_cli` |
| `_registry.py` | — | `dispatch_stage`, `init`, `lint_ticket`, `model_resolve`, `seam_check`, `validate_workspace` |
| `_runner.py` | — | `branch_ticket`, `create_pr`, `diff_extract`, `finalize`, `flow_beads_create`, `flow_worktree`, `forge_bitbucket`, `forge_github`, `friction_escalate`, `init`, `recall_pending`, `review_brief`, `scrutinize_seat`, `tracker_beads`, `version`, `worktree_janitor` |
| `_timeutil.py` | — | `dispatch_stage`, `flow_friction`, `flow_worktree`, `init`, `lease`, `memory_append`, `memory_embed`, `metric`, `observe_at_close`, `observe_ship_event`, `recall`, `recall_pending`, `recall_usage`, `recover`, `scrutinize_seat`, `state`, `status`, `sweep_knowledge`, `ticket_frontmatter`, `tracker_cli`, `worktree_janitor` |
| `_workspace.py` | — | `branch_ticket`, `create_pr`, `flow_friction`, `flow_worktree`, `forge`, `friction_escalate`, `maintainer`, `metric`, `model_resolve`, `observe_ship_event`, `preflight`, `recover`, `reflect_inputs`, `revise_config`, `scrutinize_seat`, `snapshot`, `status`, `tracker_cli`, `triage` |
| `branch_ticket.py` | — | `finalize`, `worktree_janitor` |
| `cockpit.py` | — | `cockpit_cli` |
| `cockpit_cli.py` | `render` | — |
| `compose_commit.py` | — | — |
| `create_pr.py` | — | — |
| `diff_extract.py` | `capture-implement-diff` `capture-review-diff` `check-ownership` `record-baseline` `since-stage` | `reflect_inputs` |
| `dispatch_stage.py` | `advance` `init` `next` `release` `revise-open` | — |
| `embedder_fastembed.py` | — | — |
| `finalize.py` | — | — |
| `flow_beads_create.py` | — | `friction_escalate` |
| `flow_friction.py` | — | `recover` |
| `flow_launcher.py` | — | `flow_worktree`, `init` |
| `flow_worktree.py` | `create` `locate-or-reseed` `reap` | `finalize`, `worktree_janitor` |
| `flowctl.py` | — | `seam_check` |
| `forge.py` | — | `create_pr`, `finalize`, `forge_bitbucket`, `forge_cli`, `forge_github`, `review_brief`, `revise_config`, `worktree_janitor` |
| `forge_bitbucket.py` | — | `forge` |
| `forge_cli.py` | `ci-rollup` `delete-branch` `detect-pr` `list-authored` `mark-ready` `merge` `post-reply` `resolve-thread` `review-status` `review-threads` `update-body` | — |
| `forge_github.py` | — | `forge` |
| `friction_escalate.py` | `escalate` | — |
| `friction_recurrence.py` | — | `friction_escalate`, `metric`, `reflect_inputs` |
| `group_candidates.py` | — | — |
| `group_persist.py` | `clear` `derive` `persist` | — |
| `init.py` | — | — |
| `lease.py` | `acquire` `classify` `release` | `dispatch_stage`, `flow_worktree`, `recover`, `scrutinize_seat`, `status`, `worktree_janitor` |
| `lifecycle.py` | — | `lifecycle_cli` |
| `lifecycle_cli.py` | `coordinate` `reduce` | — |
| `lint_comments.py` | — | — |
| `lint_ticket.py` | — | — |
| `machinery_edit.py` | `apply` | — |
| `maintainer.py` | — | `flow_beads_create` |
| `memory_append.py` | — | `sweep_knowledge` |
| `memory_embed.py` | `embed` `reindex` | `recall`, `recall_usage`, `sweep_knowledge` |
| `metric.py` | `corpus-health` `fix-efficacy` `friction-per-run` `recall-hit-rate` `revert-rate` `tickets-per-week` `time-to-pr` `trend` | — |
| `model_resolve.py` | — | `validate_workspace` |
| `module_map.py` | — | `seam_check` |
| `observe_at_close.py` | — | `finalize`, `worktree_janitor` |
| `observe_ship_event.py` | — | `metric`, `observe_at_close` |
| `pending_mutations.py` | — | `sync`, `tracker_cli` |
| `pr_body.py` | — | `create_pr` |
| `preflight.py` | `check` `probe` | — |
| `public_commands.py` | — | `module_map`, `public_commands_check`, `public_commands_cli` |
| `public_commands_check.py` | — | — |
| `public_commands_cli.py` | `help` `route` | — |
| `recall.py` | — | `flow_friction`, `memory_embed`, `metric`, `recall_usage`, `reflect_inputs`, `sweep_knowledge` |
| `recall_pending.py` | — | `dispatch_stage`, `recall` |
| `recall_usage.py` | `detect-misses` `record-usage` | `metric`, `sweep_knowledge` |
| `recover.py` | `abort` `detect` `reload-snapshot` `retry` `skip` `takeover` | — |
| `reflect_inputs.py` | — | — |
| `review_brief.py` | `freshness` `render` | — |
| `revise_config.py` | `apply-floor` | — |
| `runtime_layout.py` | — | `flow_launcher` |
| `scrub_ci_skip.py` | — | — |
| `scrutinize_seat.py` | — | — |
| `scrutinize_trace.py` | — | — |
| `seam_check.py` | — | — |
| `snapshot.py` | — | `dispatch_stage`, `recover` |
| `state.py` | — | `diff_extract`, `dispatch_stage`, `flow_worktree`, `recall_usage`, `recover`, `reflect_inputs`, `status` |
| `status.py` | — | — |
| `sweep_knowledge.py` | `apply` `apply-cluster` `cluster` `propose` | — |
| `sync.py` | — | — |
| `ticket_frontmatter.py` | `read` `update` | `diff_extract`, `flow_worktree`, `lint_ticket`, `observe_at_close`, `reflect_inputs`, `review_brief` |
| `tier_policy.py` | — | `flow_worktree`, `triage` |
| `tracker.py` | — | `finalize`, `flow_worktree`, `group_candidates`, `group_persist`, `observe_at_close`, `sync`, `tracker_beads`, `tracker_cli`, `tracker_jira`, `worktree_janitor` |
| `tracker_beads.py` | — | `tracker`, `triage` |
| `tracker_cli.py` | `comment` `create` `download-attachments` `get` `is-shipped` `link` `list-assigned` `list-epics` `list-sprints` `list-types` `set-sprint` `state` `transition` | `finalize`, `group_candidates`, `group_persist`, `observe_at_close`, `sync`, `triage`, `worktree_janitor` |
| `tracker_jira.py` | — | `tracker` |
| `triage.py` | `decided` `lane` `list` | `flow_worktree` |
| `validate_workspace.py` | — | `dispatch_stage`, `review_brief` |
| `version.py` | `stamp` | — |
| `worker_pool.py` | `guard` `limit` `recover` `snapshot` | — |
| `worktree_janitor.py` | `sweep` | `finalize`, `scrutinize_seat` |
<!-- flow:module-map:end -->
