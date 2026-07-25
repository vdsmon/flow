# Script map (current)

The live "which script does what" map. One authored row per script: purpose plus the contract notes argparse cannot express (exit codes, artifact paths, who consumes it). The mechanical surface — every script's subcommand names and true importers — is the generated table in §Derived surfaces at the bottom: regenerate it with `python3 module_map.py write`; `seam_check.py` fails when it is stale. For the phase-by-phase build history and the deferred-work log, see `dev-history.md`. For API/contract tables (Jira REST mapping, beads CLI surface, `.flow-bundle.toml` schema, `state.json` schema), see `inventory.md`.

`lib` = imported module, no standalone CLI. Everything else is a thin CLI subprocessed from SKILL.md prose, a reference doc, or another script.

## Reader entry-point map

The ordered file chain (prose → contract → code) for the questions a fresh reader asks most. Answers are assembled across files by design; this table is the index, not a fourth copy.

| Question | Chain |
|----------|-------|
| How does a stage get dispatched, end to end? | `references/delivery-loop.md` → `dispatch_stage.py` row below → `inventory.md` §Handler-descriptor JSON shape + §Stage lifecycle |
| Where does run state live; what is its schema? | `state.py` row below (state lives in the worktree's `.flow/runs/<key>/`) → `inventory.md` §state.json schema |
| What happens at the merge stage, incl. the hot auto-merge gate? | `references/stage-merge.md` (judgment) → `stage_merge.py` (plumbing) → `evolve_self_merge.py` `decide()` (the gate) |
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
| `cockpit.py` (lib) | Pure attention-first cockpit join and logical-FLOW renderer over normalized run, deferred, pending-write, PR-feedback, and maintainer-health evidence. | — |
| `snapshot.py` (lib) | Canonical workspace snapshot at init; verify on each `next` (TOCTOU drift guard). The WORKSPACE drift snapshot — not state.json's `.bak` backups, which are state.py's. | — |
| `lease.py` (lib) | Per-ticket run lease: acquire / refresh / release / expiry + takeover detection. | — |
| `validate_workspace.py` | HARD GATE: schema-validate `workspace.toml` + `stage-registry.toml` on every run. | exit 1 = violations to stderr |

The fifth run-safety mechanism, the content-ownership commit gate, is `diff_extract.py`'s `check-ownership` (§Frontmatter / diff / commit) — AGENTS.md's "content-ownership commit gate" resolves there. The full threat → file → witnessed-failure map is `references/robustness.md`.

## Bootstrap

| Script | Role | Contract notes |
|--------|------|----------------|
| `init.py` | Transactional workspace bootstrap. Collects backend/bundle answers, writes `workspace.toml`, preserves optional `[models]` hints on reconfigure, checks postconditions, and atomically writes `.flow/.initialized`; guidance-only mode updates managed `AGENTS.md` without rerunning configuration. | `--config <json>` (`--reconfigure` / `--resume`) / `--guidance-only --workspace-root` |
| `runtime_layout.py` (lib) | Layout-v2 resolver and journaled v1 migration: relocatable local and absolute external memory roots, linked-worktree lease refusal, closed journal/path validation, namespace collision refusal, conflict preservation, backup plus size/SHA-256 verification, and forward resume. | — |
| `flow_launcher.py` | Install or repair `.flow/runtime/{skill-root,flow}` after converging layout v2; stabilize both Claude Code and Codex versioned plugin-cache paths to a local marketplace source when available (`CODEX_HOME` respected). Imported by init and flow_worktree so setup/worktree creation stamp the executing installation. | `--workspace-root`; exit 1 on missing workspace config, unsafe migration, invalid metadata, or install error |
| `flowctl.py` | Allowlisted post-init facade: require an absolute workspace root, validate `FLOW_HARNESS` on every invocation, change cwd, export `FLOW_SKILL_DIR` plus the legacy `CLAUDE_SKILL_DIR`, and exec the mapped implementation with unchanged args/signals/stdio/exit status. No raw-script escape hatch. | `--workspace-root <absolute-path> <command> [args...]`; unknown command or harness exits 2 |
| `public_commands_cli.py` | Operational registry seam; imports `public_commands.py` to validate public tokens before orchestration and to render identical logical help in every harness. | `route [--workspace-root <absolute-path>] -- <public tokens>` / `help [topic]`; compact sorted JSON for route, logical `FLOW` text for help, exit 2 invalid |
| `lifecycle_cli.py` | Read normalized host evidence from an absolute JSON file and invoke the pure target reducer without probing or mutation; coordinate already-reduced multi-target actions. | `reduce --evidence <absolute-json-file>` / `coordinate --action <action>... [--together --unattended --choice]`; compact JSON, structured errors |
| `cockpit_cli.py` | Construct the cockpit evidence model from an absolute JSON file and render the deterministic attention-first snapshot without probing or mutation. | `render --evidence <absolute-json-file> [--json]`; logical text or compact snapshot JSON, structured JSON errors |
| `flow_worktree.py` | Post-approval worktree seeding plus the exported `is_ticket_branch` ownership predicate shared by preview and reap. Resolves the approved base, seeds the approved Markdown plan and `state.json`, stamps frontmatter, and binds the worktree's v2 memory pointer to main. Reap guards base/revision state, verifies an optional expected tip, and checkpoints dirty work before removal. | flags per `--help`; `create --auto` gates the unattended path |
| `branch_ticket.py` | Resolve ticket key from current git branch (backend-aware regex); `--branch <name>` resolves from an explicit branch instead (the PR->ticket enabler for revise). | `--workspace-root [--branch]`; exit 0 match / 1 env / 3 no-match |
| `bundle_discover.py` (lib) | Harness-aware `.flow-bundle.toml` discovery. Unset/`claude-code` searches `${CLAUDE_CONFIG_DIR:-~/.claude}/plugins` + `<repo>/.claude/plugins`; `codex` searches installed plugins under `${CODEX_HOME:-~/.codex}/plugins`; `generic` requires explicit roots. Unknown adapters fail instead of mixing host installations. | — |

## Tracker

| Script | Role | Contract notes |
|--------|------|----------------|
| `tracker.py` (lib) | Tracker Protocol base + `make_tracker()` factory + `CAPABILITY_ENUM`. Adapters load lazily inside `make_tracker`; `flow_worktree` imports lazily in `_refuse_terminal_bead`. | — |
| `tracker_cli.py` | CLI wrapper around the Protocol (the only tracker surface the prose calls). | subcommand names in §Derived surfaces |
| `tracker_jira.py` (lib) | Jira Cloud REST v3 + Agile/1.0 adapter (Basic auth via `ATLASSIAN_EMAIL`/`ATLASSIAN_API_TOKEN`). | — |
| `tracker_beads.py` (lib) | Beads `bd` CLI adapter (local-only tracker). | — |
| `resolve_handler.py` | Resolve a `skill:<name>` handler through the selected harness's installed-plugin roots (including the owning workspace's Claude repo-local root): confirm the bundle is installed and valid, then return concrete `skill_name`/`skill_args`. Unknown harnesses fail clearly. | `--handler <string> --search-roots`; exit 1 not-installed / 2 invalid / 3 discovery or harness error |

## Forge (PR host)

Pluggable PR-host seam, structural twin of the tracker seam. The `create_pr` and `review_loop` stages reach the host ONLY through `forge_cli.py`, so a GitHub and a Bitbucket workspace run the same prose. Selected by `[forge] backend = "github" | "bitbucket"` in `workspace.toml` (the block is OPTIONAL; absent = no forge).

| Script | Role | Contract notes |
|--------|------|----------------|
| `forge.py` (lib) | Forge Protocol base + `make_forge()` factory + `read_forge_config()` + `FORGE_CAPABILITY_ENUM` (incl. `default_reviewers`) + normalized `PullRequest`/`CIStatus`/`ReviewThread`. `detect_pr` selects open or merged state; adapters include the optional head SHA and produce commit-pinned source URLs for reviewer evidence. Adapters load lazily inside `make_forge`. | — |
| `forge_cli.py` | CLI wrapper around the Protocol (the only forge surface the prose calls); cap-gated subcommands degrade to `{"supported": false}` exit 0. `detect-pr` accepts `--state open\|merged`. | subcommand names in §Derived surfaces |
| `forge_github.py` (lib) | GitHub `gh` adapter: detect/open PR, CI rollup (`statusCheckRollup`), mark-ready/merge/delete-branch, and commit-pinned `blob/<sha>/<path>#Lx-Ly` source URLs. review_threads/post_reply/resolve_thread supported via gh api graphql. `default_reviewers` capability OFF (`set_default_reviewers` raises `NotSupported`; solo repo, CODEOWNERS covers reviewers) — the first `supported=false` capability in a live adapter. `main_ci_health` reuses its `_classify_rollup`. | — |
| `forge_bitbucket.py` (lib) | Bitbucket `bkt` adapter (absorbs ship-it): detect/open PR, CI rollup from `bkt pr checks`, commit-pinned `src/<sha>/<path>#lines-x:y` source URLs, CodeRabbit review-thread fetch + verified resolve (`.resolution != null`), `set_default_reviewers` (GET `2.0/user` author + GET `default-reviewers`, drop author by `account_id`, PUT `{reviewers:[{uuid}]}`). | — |
| `review_brief.py` | Deep, stdlib-only review-companion renderer. Strictly validates the motivation-first JSON model, binds local and PR heads to one full SHA, extracts source from that commit, builds responsive/CSP-protected self-contained HTML with exact Forge links, publishes atomically, and records/probes freshness. | writes `<ticket-dir>/stages/review_brief/<sha>/{brief.json,review-brief-*.html,receipt.json}` |
| `main_ci_health.py` | Per-drain-turn main-CI health probe ("main" = the default BRANCH's CI, not the program's entrypoint): `gh api .../commits/<sha>/check-runs` (sha-keyed, owner/repo auto-resolved), uppercases each REST `status` then reuses `forge_github._classify_rollup` (inheriting the CANCELLED/STALE/NEUTRAL/SKIPPED to pending fold). Asymmetric: only `failed` pauses; green/pending/probe-`error` resume. Pure `classify_main_ci(check_runs)` for unit-testing. | `probe --workspace-root [--sha]`; consumed by `stage_merge.py` + `evolve_reap.py` |

### Adding a tracker/forge backend

The seams are designed for extension but had no recipe; the ordered touch-points (forge shown, the tracker seam mirrors each step in `tracker.py` / a `tracker_<name>.py` adapter / `tracker_cli.py`):

1. `forge.py`: add the name to `KNOWN_BACKENDS` and a lazy-import branch in `make_forge()`. `read_forge_config()` validates against `KNOWN_BACKENDS` a SECOND time — miss it and every workspace read rejects the new backend even though `make_forge` knows it.
2. Write `forge_<name>.py` implementing the Forge Protocol and declaring capabilities per `FORGE_CAPABILITY_ENUM` (`forge_github.py` is the reference adapter; raise `NotSupported` for what the host lacks — `forge_cli.py` already degrades a cap-gated subcommand to `{"supported": false}`, no CLI change needed).
3. Extend the `[forge]` workspace schema (inventory.md §`[forge]` workspace schema) and the `init.py` wizard prompts.
4. Adapter tests mirroring `tests/test_forge_github.py`; run `seam_check.py` (prose flag references must still resolve).

### Adding a public command

Edit `public-commands.toml`, regenerate its managed router/help/trigger surfaces, and
run `public_commands_check.py`. Public paths describe intent; internal facade script
names remain implementation details. A command change touches the skill router and is
therefore hot.

## Frontmatter / diff / commit

| Script | Role | Contract notes |
|--------|------|----------------|
| `ticket_frontmatter.py` | TOML frontmatter r/w under flock + atomic rename (delimiter `+++`). | `read <path>` / `update <path> --set k=v` |
| `lint_ticket.py` | HARD GATE: required frontmatter fields per stage. | `--stage --ticket-path --workspace-root` |
| `lint_comments.py` | Deterministic comment-quality floor under the stage-implement bar (Step 4): flags em-dash, banned filler/inflation vocabulary, narration markers, and over-limit or under-filled comment/docstring prose in the files a run touched. Python is exact (tokenize + ast); other languages get line-start markers only, so string literals cannot false-positive. Markdown (`.md`/`.markdown`) runs the em-dash check only, outside fenced code blocks (docs are prose, so the banned-word and width checks stay off). Line limit auto-discovered per file (ruff/black/.editorconfig, default 88); `--line-length` overrides. `--diff-base <ref>` keeps only findings on lines changed vs the ref (how the stages scope a legacy file to the run's own edits). | `<file> [...] [--line-length N] [--diff-base REF] [--json]`; exit 0 clean / 1 findings. Consumed by `references/stage-implement.md` Step 4 |
| `diff_extract.py` | Git diff capture for implement/commit/reflect; baseline + ownership. `check-ownership` IS the content-ownership commit gate (AGENTS.md's term): refuses a branch delta outside the baseline `planned_files`, dirty tree AND committed, so a rogue mid-implement commit is seen too. | subcommand names in §Derived surfaces |
| `compose_commit.py` | Deterministic conventional-commit header skeleton (LLM fills body). | `--ticket --type --summary [--scope --files --covers]` |
| `scrub_ci_skip.py` | Neutralize bracketed GitHub CI-skip tokens (`[skip ci]` etc.) in a commit-message file, in place; strips the brackets, keeps the words. Exit 0 always. | `<message-path>` |

## PR lifecycle

Opening and versioning the PR (the host seam itself is §Forge above).

| Script | Role | Contract notes |
|--------|------|----------------|
| `create_pr.py` | `create_pr` stage handler: git push of the branch, then open/resolve the PR through the forge seam (`fg.detect_pr` / `fg.open_pr` via the injected `Forge` adapter), so the same handler serves GitHub and Bitbucket. Title from HEAD commit subject (not `--fill`); body built from the HEAD commit body via `pr_body.build_body` + `pr_body.scrub` (trailer strip, Closes footer, prose unwrap, de-AI scrub), falling back to the subject when prose is empty. On first open, attaches default reviewers via `fg.set_default_reviewers` (swallows `NotSupported` + any `ForgeError` so a reviewer hiccup never fails the PR). Draft by default, ready-for-review when `[create_pr] draft = false` (`--draft` forces a draft); idempotent (reuse existing PR on resume). Base branch from `--base`, else `[create_pr] base` in workspace.toml, else `main`. Prints `PR_URL=<url>`. | `--workspace-root [--base BRANCH --ticket KEY --draft]`; exit 3 = protected branch. Wired `create_pr = "inline"` via `references/stage-create_pr.md` |
| `pr_body.py` (lib) | PR-body helpers for `create_pr` (no CLI). `closes_footer(raw_commit_body)` extracts the `Closes <KEY>` trailer lines the script appends under an authored `--body-file`. `build_body(raw_commit_body)` is the no-`--body-file` fallback: strips the contiguous leading `ticket:`/`files:` trailer, keeps `Closes <KEY>` lines as a footer, unwraps prose hard-wraps (never across blank-line breaks, list items, or fenced code). `scrub(body)` runs a deterministic, idempotent de-AI pass (em-dash → punctuation, sentence-case `# Heading`, flatten `- **Term:**` bullets; fenced code untouched) as the floor over the authored body. `enforce_cap(body, cap=32000)` is the deterministic body-size net under the stricter forge description limit: over cap it shrinks the largest fenced blocks (head+tail lines around a `… N lines trimmed …` marker), then drops `<details>` bodies keeping their `<summary>`, then hard-truncates; idempotent, ≤ cap on every non-exceptional path. `flatten_details(body)` rewrites each `<details>`/`<summary>` wrapper to a `###` heading + body (fenced content kept; no match = byte-identical passthrough); `create_pr` applies it when the forge backend is bitbucket, which renders no raw HTML in markdown. All TOTAL (never raise; passthrough on adversarial input). | — |
| `revise_config.py` | Reader for the `[revise]` block of workspace.toml (revision sub-runs, epic flow-kx17). `plain_comment_severity` (default `"minor"`, validated against `forge.THREAD_SEVERITY`) is the floor the revision review_loop applies to unresolved minor (plain human) threads; bad value / missing config → default, exit 0. The pure `apply_floor(threads, severity)` helper bumps every UNRESOLVED `minor` thread to the floor (returns new dicts, no-op when `minor`); the bump stays loop-side so `forge_github._severity_from_state` is pure of config. | `apply-floor --workspace-root .` reads a threads JSON array on stdin and prints it floored. Consumed by `references/stage-review_loop.md` (revision mode) |
| `version.py` | Version-derivation + merge-time stamp seam (epic flow-6gx): compute the next plugin version (read the current `plugin.json` version on a git ref; semantic bump: MINOR on a feat commit type, PATCH otherwise — type from `--commit-type`, else the HEAD commit-subject conventional prefix) and surgically write it into both version files (`plugin.json` top-level + the `marketplace.json` flow entry), preserving JSON formatting. The per-PR bump is gone; `stamp` runs server-side post-merge on `main` (the `version-stamp.yml` Action). | `stamp [--ref <ref>] [--cwd <path>] [--commit-type <type>]` writes both files then prints JSON `{"ref","current","next","bump","commit_type"}`; exit 0 = ok, 2 = tool error. Consumed by `.github/workflows/version-stamp.yml` (the server-side post-merge Action). |

## Memory / recall

| Script | Role | Contract notes |
|--------|------|----------------|
| `_memory_paths.py` (lib) | Namespace resolution + `.flow/memory/<namespace>/` path conventions + `load_semantic_config`. Layout v2 reads `.flow/runtime/memory-root`, treats `.flow/memory` as a relocatable workspace-local sentinel, and refuses missing/malformed pointers; unstamped workspaces retain legacy reads only until migration. | — |
| `memory_append.py` | Single-writer `knowledge.jsonl` append with sha-keyed idempotency. `--supersedes` is a CLI-level single string; the library `append()` also accepts a list of target ids (a canonical entry consolidating a whole cluster, used by `sweep_knowledge.apply_cluster`, not exposed as a CLI flag). `--labels` is an optional CSV `facet:value` array, metadata only (not a hash input). | `--type --text --branch --ticket [--supersedes --labels]` |
| `recall.py` | BM25 ranker over `knowledge.jsonl` with an OPTIONAL semantic-fusion overlay (cosine over the `memory_embed` sidecar index, rank-based top-K cosine selection, RRF over BM25; any failure → BM25 fallback + stderr backend status; `[memory.semantic]` absent/off → byte-identical BM25). `--threshold` is a low floor (drop non-positive cosines), NOT the candidate gate. `--label` is a HARD pre-filter over `labels[]` (exhaustive, bypasses `--top-n`, query optional). `--digest` (requires `--label`) renders that cluster as a markdown card (sections by type, newest-first) instead of JSON. `--reindex` dispatches to `memory_embed.py`. | `<query> [--branch --tickets --ticket --label --digest --top-n --include-superseded --semantic --threshold --query-file --record-pending --reindex --full]` |
| `memory_embed.py` | Embedder seam (shells a configured command — `[memory.semantic].embedder` or the default `uvx --with fastembed python embedder_fastembed.py`; pure stdlib, never imports the model) + derived sidecar index under `.flow/memory/<namespace>/knowledge.embed` (supersede-filtered, incremental, model-mismatch → full rebuild). | `reindex --workspace-root [--full --model --embedder]` / `embed [--workspace-root --model --embedder]` (stdin texts → JSON vectors); exit 1 = workspace invalid, 2 = embedder unavailable |
| `embedder_fastembed.py` | DEFAULT reference embedder, run BY `uvx` (`--with fastembed`, ONNX, no torch); stdin texts → JSON vectors via `fastembed.TextEmbedding(<model>).embed`. Default model `BAAI/bge-small-en-v1.5` (384-dim). Standalone subprocess entrypoint, imported by nothing. | `[--model <id>]` (stdin → JSON); exit 0 ok, 1 load/encode failure |
| `embedder_model2vec.py` | Lighter static ALTERNATIVE embedder (select via `[memory.semantic].embedder`), run BY `uvx` (imports model2vec/numpy); stdin texts → JSON vectors via `StaticModel.from_pretrained(<model>).encode`. Standalone subprocess entrypoint, imported by nothing. | `[--model <id>]` (stdin → JSON); exit 0 ok, 1 load/encode failure |
| `sweep_knowledge.py` | Retro-curation sweep over `knowledge.jsonl`; propose-only until a confirmed manifest applies append-only supersession tombstones. Usage ranking and semantic clustering support `FLOW memory prune`. | subcommand names in §Derived surfaces |
| `recall_pending.py` (lib) | Promote recall-pending entries into the per-ticket recall log; the promoting rewrite also moves >24h entries to `.stale`. The producer is the plan-phase `recall.py --record-pending` (post-gate write); the dispatcher promotes at init. | — |
| `recall_usage.py` | Recall observability: append deduplicated usage and near-duplicate miss records beneath the v2 namespace; feeds recall-quality and pruning rank. | `record-usage --ticket --ticket-dir [--used-ids]` / `detect-misses --ticket --ticket-dir [--threshold]` |
| `reflect_inputs.py` | Bundle the reflect-stage inputs (state + frontmatter + diff + subagent reports + friction + reflect_config + a best-effort `harness_eval` availability block advertising the corpus regression eval). | `--ticket --ticket-dir --ticket-frontmatter --cwd` |
| `observe_ship_event.py` | Sole writer of `ship-events/<ticket>.json` (atomic, dupe-safe). | `--ticket --evidence-json --run-id --arm --tier --acceptance-invariant --lane --workspace-root` |
| `observe_at_close.py` | Freeze the ship event from a doomed run's `state.json` before the post-merge reap (janitor sweep / drain step A) destroys it. `is_shipped`-gated (only `not_yet_observed` observes; PR#277's property never loosened) → run_id capture → tier/acceptance-invariant/lane gather → `observe_ship_event.observe` against the MAIN store, attribution stamped from the worktree's state.json. Never raises; returns an `{action}` dict (observed / skipped / failed). | `--workspace-root --key [--worktree]` |
| `senses_deadman.py` | Nightly SENSES deadman (twin of the RUNS deadman): join the window's closed beads to the ship-event store, bucket each close observed / within-lag / missing / covered / unmerged / ignored, file ONE deduped P0 on divergence, and print a folded health digest (telemetry freshness incl. quarantine-sidecar growth, metric-trend deltas, loop liveness). Reads `is_shipped` through the tracker seam, never modifies it (PR#277 untouched). Pure `classify_closes` / `decide_alarm` / `run_record_summary` / `render_digest`; the CLI gathers via the injectable Runner + tracker seam. | `--workspace-root [--window-days --lag-hours --min-missing --max-gap --json --dry-run --run-record]`; exit 0 healthy / 1 divergence / 2 bd-git error / 4 not maintainer. Consumed by `ops/nightly-evolve.sh.template` |

## Self-evolution

The reflect stage's self-repair path — see `../references/self-evolution.md` for the model.

| Script | Role | Contract notes |
|--------|------|----------------|
| `machinery_edit.py` | Flock-serialized applier for reflect lens-B self-edits to flow's OWN source. Refuses out-of-tree + snapshot-pinned paths + skill-root on a protected branch (main/master/dev/develop → propose+record instead). See `../references/self-evolution.md`. | `apply --skill-root --payload` |
| `flow_beads_create.py` | File a self-work (machinery) bead into flow's OWN beads, gated on maintainer mode; always targets flow's beads, never the run's tracker. | `--workspace-root --summary --description [--type --labels --parent --dedup-key --acceptance-invariant]`; exit 4 = not maintainer |

## Maintenance drain engine

The maintainer-gated `FLOW maintain evolution drain` and
`FLOW maintain backlog drain` loops plus their shared merge stage.

| Script | Role | Contract notes |
|--------|------|----------------|
| `maintainer_preflight.py` | Host-neutral scheduled-run deadman plus clean-boundary gate. Reports hung, failed, stale, disarmed, and unavailable ledgers; before checkout/plugin mutation it also refuses dirt and recursively finds live/corrupt base or revision leases. | `[--run-record --now --json] [--workspace-root --require-clean-boundary]`; boundary refusal exit 3, absent ledger is silent |
| `worker_pool.py` | Deterministic seams around host-native collaboration: reserves one driver slot, captures/compares strong read-only git receipts, and reduces durable driver-loss evidence. | `limit --configured --capacity` / `snapshot --workspace-root` / `guard --workspace-root --before` (exit 3 mutation) / `recover --evidence` |
| `_evolve_common.py` (lib) | Shared maintenance-drain helpers: `ToolError`/`NotMaintainer`, tolerant JSON parsing, branch-to-key mapping, liveness joins, and selector primitives. | — |
| `evolve_select.py` (lib) | Drain select core: select + partition the next bounded batch of evolve planning candidates (`bd ready -l evolve`, drop in-flight, backpressure, coarse hot/anchor serialization). Pure, no side effects; its internal `launch` field is selection data, not authorization. | raises NotMaintainer/ToolError. Consumed by `evolve_drain.py` (the `FLOW maintain evolution drain` loop) |
| `queue_select.py` (lib) | Day-job sibling of `evolve_select.py`: select + partition the next bounded batch of non-evolve planning candidates (`bd ready` unlabelled minus epic/evolve/proposal/hot, drop in-flight, queue-scoped backpressure counting only PRs outside the active-evolve set, anchor dedup, `model_per_key` per-key). No hot-serialization layer. Pure, no side effects; its internal `launch` field is selection data, not authorization. | raises NotMaintainer/ToolError. Consumed by `queue_drain.py` (the `FLOW maintain backlog drain` loop) |
| `queue_status.py` | Read-only backlog status: ready work, lease liveness, advisory next action, parked-PR feedback, and backpressure. | `--workspace-root [--cap --concurrency]`; exit 4 not maintainer / 2 tool error. Consumed by `references/command-maintain.md` |
| `fleet.py` | Fleet liveness ledger: one registration + heartbeat record per launched run at `<shared .flow>/fleet/<key>.json`; storage resolves via `_memory_paths.resolve_memory_base` and is maintainer-gated. `live_keys` uses a 7200-second heartbeat-staleness fallback matching the longest default stage lease; readers still reconcile it with the run lease. `is-live`, used before destructive drain actions, is lease-only and fails safe toward live on read errors. | exit 4 = not maintainer |
| `evolve_reap.py` | Drain reap-step core: classify open evolve PRs for auto-merge (green + leaf + mergeable → `merge`; a hot leaf also merges under `[evolve] auto_merge_hot` + isolation, one hot per pass; a green DIRTY → `blocked` reason `"DIRTY"` (branches carry no version line, so a DIRTY is a genuine code conflict for a human); else not_green/skipped_hot/blocked). Probes main's own CI health each turn (`main_ci_health.py`): a red main holds every would-be-merge in `held_main_red` and files one deduped P0. Pure `classify`; the loop does the `gh pr merge`, `reap()` does the probe + P0. Role: orphan safety-net (a run that died before self-merging) + worktree teardown. | `--workspace-root`; exit 4 = not maintainer. Consumed by `FLOW maintain evolution drain` (reap step) |
| `evolve_drain.py` | Drain loop's next-action decider: `decide(select_result, liveness, stranded=()) → {action: recover\|wait\|plan_required\|done, launch: [], plan_required, parked[, stranded]}` with precedence `recover > wait > plan_required > done`. CLI runs `evolve_select.select()` + annotates each in-flight bead with lease liveness. Fresh candidates require attended driver planning; they never authorize a launch. `stranded_pre_pr()` keeps pre-PR dead work from producing a false `done`. Pure `decide()`/`liveness_map()`/`stranded_pre_pr()` are reused by `queue_drain.py`. | `--workspace-root [--cap --concurrency]`; exit 4 = not maintainer, 2 = bd/git/gh error. Consumed by the `FLOW maintain evolution drain` loop |
| `queue_drain.py` | Day-job sibling of `evolve_drain.py`: the `FLOW maintain backlog drain` loop's next-action decider. CLI runs `queue_select.select()`, queue-scopes the wait gate (subtracts active-evolve keys from `live_runs`/`launched_pending` — the shared worktree pool + fleet ledger must never make the day-job loop wait on a live evolve run), annotates in-flight day-job runs with lease liveness, and classifies merged flow PRs with a registered worktree or pending launch for reaping (pure `classify_reap`; a reaped launch key is dropped — merged-but-unclosed beads divert to the close path, never relaunch). Also detects STRANDED pre-PR day-job runs (`evolve_drain.stranded_pre_pr` over a day-job-scoped in_progress set: all in_progress minus epics minus `evolve`/`proposal`/`hot`) → `decide` returns `recover` (never false-positive `done`); the `references/command-maintain.md` §Recover prose reaps + reopens, bounded by the same `STRANDED-RECOVERY:` ladder. NEVER merges PRs (day-job PRs park for the maintainer). | `--workspace-root [--cap --concurrency]`; exit 4 = not maintainer, 2 = bd/git/gh error. Consumed by the `FLOW maintain backlog drain` loop (`references/command-maintain.md` §drain) |
| `worktree_janitor.py` | Workspace-local stale-worktree janitor. Derives the primary checkout from Git, recognizes only its managed worktree directories, resolves Jira or Beads keys, reads normalized tracker and forge state, and classifies each candidate conservatively. Open PRs are preserved; merged PRs require local-tip/head equality; terminal no-PR branches require a verified remote default and zero unique commits. A real sweep requires the preview's target and candidate confirmation IDs, then sends each removal through revision-aware `flow_worktree.reap_worktree`. | `sweep --workspace-root [--dry-run --confirmed-target --confirmed-candidate]`; exit 2 = repository-level git error. Consumed by `references/command-maintain.md` |
| `evolve_self_merge.py` | Self-merge gate (the `merge` stage core): pure `decide(labels, is_maintainer, auto_merge_hot, ci_status, planned_files, eval_status, main_ci_status, changed_files) → {action, is_hot, reason}`, where `is_hot` is the `hot` label OR a guard-file hit in `planned_files` OR one in `changed_files` (the merge-time observed PR diff; can only raise hotness, never lower it; reuses `triage.is_hot_change`), and a non-"pass" `eval_status` (the `harness_eval` verdict, fed by the stage when the PR touches scripts) blocks the merge (Self-Harness no-degradation rule). The stage acts on it: a hot bead gets an independent reviewer subagent (§2) before `forge_cli merge`. | `--workspace-root --key --ci-status [--eval-status --main-ci-status --changed-files]`; consumed by `stage_merge.py` |
| `stage_merge.py` | Merge-stage absorber (flow-nu1w.1): the `merge` stage's mechanical plumbing (§1 eligibility probe, §3 merge + Cover-close), shelled as subprocess argument lists so the decision code stays byte-identical to `evolve_self_merge.py`/`main_ci_health.py`/`forge_cli.py` (property-equivalence by construction). `probe` re-reads CI, replays `harness_eval.py` when the PR touches scripts, probes main's CI health, asks `evolve_self_merge.py` for the verdict, and (on a hot merge) writes the PR diff for the §2 independent-reviewer prose; no merge/close side effects. `execute` rebuilds the §3 push-state guard in Python (returncode-driven, so a deleted remote branch skips rather than closes), merges through the forge seam on CLEAN/DRAFT, and closes the bead + covers only after a successful merge; `delete-branch` is remote-only (local worktree teardown stays with the drain reap). Only pure import: `ticket_frontmatter.read` for the covers list. | `probe --workspace-root --ticket-dir --key` / `execute --workspace-root --pr --key [--already-merged]`; consumed by `references/stage-merge.md` |

## Work-mode quality gate

| Script | Role | Contract notes |
|--------|------|----------------|
| `metric.py` | Metrics calculator: shipped tickets/week, time-to-PR, friction events/run, revert-rate, and arm-compare (per-arm flow-vs-control comparison) — from ship-event and friction-jsonl evidence (revert-rate joins ship-events to `bd history` AND scans the default-branch git log for reverts, emitting durable revert events via observe_ship_event), and a `trend` roll-up of all five window measures (table + `--json`), plus a `corpus-health` measure over `knowledge.jsonl` (live-vs-superseded entry counts, supersession rate, and oldest live DECISION age; the dead-set now folds list-valued tombstones via `recall.superseded_ids`) and a `recall-hit-rate` measure (precision = used/surfaced + `RECALL_MISS` count, from `recall-usage.jsonl`; auto-resolves `--namespace` from workspace.toml when omitted; the fifth trend measure), plus a `fix-efficacy` measure (per closed MACHINERY-fix bead, reusing `friction_recurrence`'s distinctive-anchor selection then a structural `(stage, type, anchor)` tuple join grounded in pre-fix friction, with a `recurred`/`clean` verdict + evidence, `claimed_tuples`, and a per-bead unmeasurable reason; lifetime metric, `--since`/`--until` ignored). | Consumed by `references/command-measure.md` |
| `harness_corpus.py` (lib) | ("harness" here = flow's OWN self-regression eval, not a test harness.) Frozen decider-fixture corpus loader/validator + replayer (regression-eval, epic flow-63q): replays held_in/held_out cases from the sibling `harness_corpus.json` data file against the four pure deciders (`evolve_select.partition`, `evolve_drain.decide`, `evolve_self_merge.decide`, `triage.is_hot_change`); candidate-checkout replay goes through `harness_eval.py`'s isolated `drive` subprocess driver, not the in-process `resolve=` hook. | Frozen by `tests/test_harness_corpus.py` (full replay each CI run) |
| `harness_eval.py` | Regression-eval scorer (epic flow-63q): replays the frozen corpus against a candidate skill-checkout AND a baseline via an isolated subprocess driver per checkout; emits per-split pass/regress delta + `non_regression`; raw data only, gating policy lives in `stage_merge.py` (which maps the exit code to `evolve_self_merge.py --eval-status`). | `score --candidate <scripts-dir> [--baseline <scripts-dir> --corpus <file> --timeout-secs N]` prints delta JSON; exit 0 = clean, 1 = bad dirs, 2 = corpus/driver error, 3 = regression. `drive` is the internal stdin-JSON replay driver. |
| `pending_mutations.py` (lib) | Transient tracker-mutation queue (create/transition/comment/link; entries with any other op are parked by sync). | — |
| `sync.py` | Drain `pending-mutations.jsonl` + reconcile against live tracker. | `--workspace-root` |

## Status / recovery / friction

| Script | Role | Contract notes |
|--------|------|----------------|
| `status.py` | Read-only run/stage/lease table (no network). | `[--ticket] --workspace-root [--json]` |
| `group_candidates.py` | `FLOW ticket group` core: fetch + normalize grouping candidates (explicit keys, or the `--mine` assigned selector) through the tracker seam, then surface empty-body title-twin duplicate hints. Read-only; the lead+covers clustering judgment lives in `references/command-ticket.md`. | `[<key> ...] --mine --filter --workspace-root`; exit 1 tracker / 2 config / 3 no input. Consumed by `references/command-ticket.md` |
| `group_persist.py` | `FLOW ticket group` defer-path persistence: record a cover set as a `flow-group covers:` marker comment on the lead (`persist`, idempotent), and read it back (`derive`) so a grouping survives propose→act across sessions. Cross-backend (only `comment`/`get`); `spec` auto-derives `--covers` from it. | `persist --lead --covers --workspace-root` / `derive --lead --workspace-root`; exit 1 tracker / 2 config / 3 args. Consumed by `references/command-ticket.md` + `references/delivery-plan.md` |
| `triage.py` | `list`: read-only `deferred` + decided-mode `blocked` queue with each one's defer comment (beads only), every row tagged `queue=evolve\|day-job` (evolve label); `--ready` opt-in adds the ready queues. `decided`: probe a bead's recorded triage decision; returns `{decided,answer,is_hot,hitl}` JSON. `lane`: resolve a bead's verification lane (express\|light\|full) from its tier labels (delegates policy to `tier_policy.lane_for`; spec-time twin of `flow_worktree._lane_for_bead`). Houses `_GUARD_FILES` + `is_hot_change`. | — |
| `tier_policy.py` (lib) | Pure tier→verification-lane decider: `lane_for(labels)` maps tier labels to a lane (`tier:trivial`→express, `tier:light`→light, hot/untiered→full). Scales gate depth to the cost of being wrong (the xqt verdict operationalized); same labels `evolve_select` reads for the worker model. The per-lane gate policy (what each lane skips) lives in the stage prose that branches on the lane string (`references/delivery-plan.md`, `stage-implement.md`, `stage-reflect.md`), not here. No I/O — callers supply the labels. `triage` uses it in `lane`; `flow_worktree` in `_lane_for_bead`. | — |
| `model_resolve.py` | Resolve an optional native-agent model hint from `[models].<stage>`. Missing, disabled, or unsupported hints inherit the driver session model. | `--workspace-root --stage`; consumed by native agent dispatch |
| `recover.py` | Inspect + remediate a broken run. | recipes in `references/delivery-repair.md` |
| `flow_friction.py` | Append-only `friction.jsonl` log (the reflect/self-evolution feedstock); imported by recover. | `--ticket --run-id --stage --type --body [--detail --severity]` |
| `friction_recurrence.py` | Read-only forward-join of `friction.jsonl` to MACHINERY-prefixed `knowledge.jsonl` entries: surfaces friction classes that recurred after a claimed fix, clustered two ways (`signature_classes`, a single distinctive anchor token, cross-cutting stage/type; `structural_classes`, `(stage, type, anchor)`), carrying evidence (entry ids, run ids, fix sha) for a downstream judge. Reads friction/knowledge/ship-events, never writes. | `--workspace-root` |
| `friction_escalate.py` | Propose-only recurrence escalation: consumes `friction_recurrence.analyze` and files ONE deduped `recurrent`-labelled bead per signature class that recurred `>=K` times since its LATEST claimed MACHINERY fix (not the detector's earliest-anchored `post_fix_count`, which over-counts a multi-fix class). `K` + an exempt-anchor set are `[evolve]` workspace.toml knobs (`recurrence_escalation_k` default 3, `recurrence_exempt_anchors` default `[planned_files]`). Dedup key is the bare anchor (no `::`), so at most one bead per anchor ever and only the exact `evid:` dedup net fires. Labels are `recurrent` only, never `evolve`, so the drain loop never picks these up. Auto-dormant outside maintainer mode via `flow_beads_create.resolve_maintainer_repo`. | `escalate --workspace-root` |

## Shared helpers (lib)

The highest-fan-in modules in the flat dir: a signature change here ripples through the import graph (the flat-dir invariant in AGENTS.md), so each helper's Imported-by column in §Derived surfaces is the blast radius — generated from the AST, so it cannot drift.

| Script | Role |
|--------|------|
| `_timeutil.py` | UTC ISO8601 parse + format; `utcnow_iso`/`utcnow_iso_ms`/`iso_z` emitters + the colon-free `ts_token` quarantine-filename stamp. |
| `_runner.py` | Subprocess-runner factories: positional-cwd `Runner`/`default_runner`, keyword-only `KwRunner`/`kw_default_runner`, cwd-bound `CwdRunner`/`cwd_default_runner`; each consumer picks the one flavor its call style needs. |
| `_workspace.py` | workspace.toml load + `plugin_version` manifest self-read. |
| `_locking.py` | Flock retry is the substrate under the lease, state, memory, and runtime migration writers. |
| `_atomicio.py` | Atomic temp-write + fsync + `os.replace` + parent-dir fsync (the rename itself is crash-durable). |
| `_jsonl.py` | JSONL quarantine parse + the read-only `read_jsonl_lenient` twin. |
| `maintainer.py` | Maintainer-mode detection via the `[maintainer]` marker. The public `--require-current` gate refuses configured redirects and names the external target. |
| `_registry.py` | Stage-registry parse + the single-source handler-string grammar `parse_handler`/`HANDLER_RE`. |
| `public_commands.py` (lib) | Load and validate `public-commands.toml`; classify static namespaces, configured tickets, PR targets, and removed-token errors; validate options; render deterministic help/router/trigger blocks without host syntax. `replace_generated_block`/`check_generated_block` are the managed-marker primitives `module_map.py` reuses. |

## Dev tooling

| Script | Role | Contract notes |
|--------|------|----------------|
| `public_commands_check.py` | Check-only registry generator gate: verifies SKILL trigger/router, logical help bytes, and every command reference without writing. | no args; exit 1 on drift |
| `module_map.py` | Render + check the generated derived surfaces: MODULE.md's §Derived surfaces table (subcommand names from AST `add_parser` constants, importers from the AST import graph) and stage-reflect.md's guard-file enumeration (from `triage._GUARD_FILES`). `check` is folded into `seam_check.py`, so CI and the prek hook catch staleness; `write` regenerates in place and is only ever run by a human or agent (hooks stay check-only). | `check` (default; exit 1 stale) / `write` |
| `seam_check.py` | Structurally validate documented absolute, call-local-harness facade commands against flowctl's allowlist and argparse surfaces. Reject cwd-dependent facades, host-specific public recipes, and stale direct scripts. Enforce managed guidance, descriptor, role, registry, and module-map contracts. | `[--verbose]`. Exit 1 on drift |

## Derived surfaces (generated)

Every script's argparse subcommand names and true importers, rendered from the AST by
`module_map.py`. Regenerate with `python3 module_map.py write`; hand edits inside the
markers are overwritten. `—` = none.

<!-- flow:module-map:begin -->
| Script | Subcommands | Imported by |
|--------|-------------|-------------|
| `_atomicio.py` | — | `diff_extract`, `dispatch_stage`, `fleet`, `flow_launcher`, `flow_worktree`, `init`, `lease`, `machinery_edit`, `memory_embed`, `pending_mutations`, `recall_pending`, `review_brief`, `runtime_layout`, `snapshot`, `state`, `ticket_frontmatter` |
| `_evolve_common.py` | — | `evolve_drain`, `evolve_reap`, `evolve_select`, `evolve_self_merge`, `observe_at_close`, `queue_drain`, `queue_select`, `queue_status`, `sweep_knowledge` |
| `_jsonl.py` | — | `friction_recurrence`, `memory_append`, `memory_embed`, `metric`, `pending_mutations`, `recall`, `recall_pending`, `recall_usage`, `reflect_inputs`, `senses_deadman`, `sweep_knowledge` |
| `_locking.py` | — | `dispatch_stage`, `fleet`, `flow_friction`, `flow_worktree`, `lease`, `machinery_edit`, `memory_append`, `memory_embed`, `observe_ship_event`, `pending_mutations`, `recall_pending`, `recall_usage`, `runtime_layout`, `state`, `ticket_frontmatter` |
| `_memory_paths.py` | — | `fleet`, `flow_friction`, `flow_worktree`, `friction_escalate`, `friction_recurrence`, `memory_append`, `memory_embed`, `metric`, `observe_at_close`, `observe_ship_event`, `recall`, `recall_usage`, `reflect_inputs`, `senses_deadman`, `sweep_knowledge` |
| `_registry.py` | — | `bundle_discover`, `dispatch_stage`, `init`, `lint_ticket`, `resolve_handler`, `validate_workspace` |
| `_runner.py` | — | `_evolve_common`, `branch_ticket`, `create_pr`, `diff_extract`, `evolve_drain`, `evolve_reap`, `evolve_select`, `flow_beads_create`, `flow_worktree`, `forge_bitbucket`, `forge_github`, `friction_escalate`, `init`, `queue_drain`, `queue_select`, `queue_status`, `recall_pending`, `review_brief`, `senses_deadman`, `tracker_beads`, `version`, `worktree_janitor` |
| `_timeutil.py` | — | `_evolve_common`, `dispatch_stage`, `evolve_drain`, `evolve_reap`, `fleet`, `flow_friction`, `flow_worktree`, `init`, `lease`, `memory_append`, `memory_embed`, `metric`, `observe_at_close`, `observe_ship_event`, `recall`, `recall_pending`, `recall_usage`, `recover`, `runtime_layout`, `senses_deadman`, `state`, `status`, `sweep_knowledge`, `ticket_frontmatter`, `tracker_cli`, `worktree_janitor` |
| `_workspace.py` | — | `_evolve_common`, `branch_ticket`, `create_pr`, `flow_friction`, `flow_worktree`, `forge`, `friction_escalate`, `maintainer`, `metric`, `model_resolve`, `observe_ship_event`, `recover`, `reflect_inputs`, `revise_config`, `snapshot`, `status`, `tracker_cli`, `triage` |
| `branch_ticket.py` | — | `worktree_janitor` |
| `bundle_discover.py` | — | `flow_launcher`, `flowctl`, `init`, `resolve_handler` |
| `cockpit.py` | — | `cockpit_cli` |
| `cockpit_cli.py` | `render` | — |
| `compose_commit.py` | — | — |
| `create_pr.py` | — | — |
| `diff_extract.py` | `capture-implement-diff` `check-ownership` `record-baseline` `since-stage` | `reflect_inputs` |
| `dispatch_stage.py` | `advance` `init` `next` `release` `revise-open` | — |
| `embedder_fastembed.py` | — | — |
| `embedder_model2vec.py` | — | — |
| `evolve_drain.py` | — | `queue_drain`, `queue_status` |
| `evolve_reap.py` | — | — |
| `evolve_select.py` | — | `evolve_drain` |
| `evolve_self_merge.py` | — | — |
| `fleet.py` | `is-live` | `_evolve_common`, `dispatch_stage` |
| `flow_beads_create.py` | — | `friction_escalate` |
| `flow_friction.py` | — | `recover` |
| `flow_launcher.py` | — | `flow_worktree`, `init` |
| `flow_worktree.py` | `create` `locate-or-reseed` `reap` | `worktree_janitor` |
| `flowctl.py` | — | `seam_check` |
| `forge.py` | — | `create_pr`, `forge_bitbucket`, `forge_cli`, `forge_github`, `queue_status`, `review_brief`, `revise_config`, `worktree_janitor` |
| `forge_bitbucket.py` | — | `forge` |
| `forge_cli.py` | `ci-rollup` `delete-branch` `detect-pr` `mark-ready` `merge` `post-reply` `resolve-thread` `review-status` `review-threads` | — |
| `forge_github.py` | — | `forge`, `main_ci_health` |
| `friction_escalate.py` | `escalate` | — |
| `friction_recurrence.py` | — | `friction_escalate`, `metric`, `reflect_inputs` |
| `group_candidates.py` | — | — |
| `group_persist.py` | `derive` `persist` | — |
| `harness_corpus.py` | — | `harness_eval`, `reflect_inputs` |
| `harness_eval.py` | `drive` `score` | — |
| `init.py` | — | — |
| `lease.py` | `acquire` `classify` `release` | `_evolve_common`, `dispatch_stage`, `evolve_drain`, `evolve_reap`, `fleet`, `flow_worktree`, `maintainer_preflight`, `recover`, `runtime_layout`, `status`, `worktree_janitor` |
| `lifecycle.py` | — | `lifecycle_cli` |
| `lifecycle_cli.py` | `coordinate` `reduce` | — |
| `lint_comments.py` | — | — |
| `lint_ticket.py` | — | — |
| `machinery_edit.py` | `apply` | — |
| `main_ci_health.py` | `probe` | `evolve_reap` |
| `maintainer.py` | — | `evolve_drain`, `evolve_reap`, `evolve_select`, `evolve_self_merge`, `fleet`, `flow_beads_create`, `queue_drain`, `queue_select`, `queue_status`, `senses_deadman` |
| `maintainer_preflight.py` | — | — |
| `memory_append.py` | — | `sweep_knowledge` |
| `memory_embed.py` | `embed` `reindex` | `recall`, `recall_usage`, `sweep_knowledge` |
| `metric.py` | `arm-compare` `corpus-health` `fix-efficacy` `friction-per-run` `recall-hit-rate` `revert-rate` `tickets-per-week` `time-to-pr` `trend` | `recall`, `senses_deadman` |
| `model_resolve.py` | — | `validate_workspace` |
| `module_map.py` | — | `seam_check` |
| `observe_at_close.py` | — | `worktree_janitor` |
| `observe_ship_event.py` | — | `metric`, `observe_at_close` |
| `pending_mutations.py` | — | `sync`, `tracker_cli` |
| `pr_body.py` | — | `create_pr` |
| `public_commands.py` | — | `module_map`, `public_commands_check`, `public_commands_cli` |
| `public_commands_check.py` | — | — |
| `public_commands_cli.py` | `help` `route` | — |
| `queue_drain.py` | — | `queue_status` |
| `queue_select.py` | — | `queue_drain`, `queue_status` |
| `queue_status.py` | — | — |
| `recall.py` | — | `memory_embed`, `metric`, `recall_usage`, `reflect_inputs`, `sweep_knowledge` |
| `recall_pending.py` | — | `dispatch_stage`, `recall` |
| `recall_usage.py` | `detect-misses` `record-usage` | `metric`, `sweep_knowledge` |
| `recover.py` | `abort` `detect` `reload-snapshot` `retry` `skip` `takeover` | — |
| `reflect_inputs.py` | — | — |
| `resolve_handler.py` | — | `snapshot` |
| `review_brief.py` | `freshness` `render` | — |
| `revise_config.py` | `apply-floor` | — |
| `runtime_layout.py` | — | `flow_launcher` |
| `scrub_ci_skip.py` | — | — |
| `seam_check.py` | — | — |
| `senses_deadman.py` | — | — |
| `snapshot.py` | — | `dispatch_stage`, `recover` |
| `stage_merge.py` | `execute` `probe` | — |
| `state.py` | — | `diff_extract`, `dispatch_stage`, `flow_worktree`, `recall_usage`, `recover`, `reflect_inputs`, `status` |
| `status.py` | — | — |
| `sweep_knowledge.py` | `apply` `apply-cluster` `cluster` `propose` | — |
| `sync.py` | — | — |
| `ticket_frontmatter.py` | `read` `update` | `diff_extract`, `evolve_self_merge`, `flow_worktree`, `lint_ticket`, `observe_at_close`, `reflect_inputs`, `review_brief`, `stage_merge` |
| `tier_policy.py` | — | `flow_worktree`, `triage` |
| `tracker.py` | — | `flow_worktree`, `group_candidates`, `group_persist`, `observe_at_close`, `senses_deadman`, `sync`, `tracker_beads`, `tracker_cli`, `tracker_jira`, `worktree_janitor` |
| `tracker_beads.py` | — | `tracker`, `triage` |
| `tracker_cli.py` | `comment` `create` `download-attachments` `get` `is-shipped` `link` `list-epics` `list-sprints` `list-types` `set-sprint` `state` `transition` | `group_candidates`, `group_persist`, `observe_at_close`, `senses_deadman`, `sync`, `triage`, `worktree_janitor` |
| `tracker_jira.py` | — | `tracker` |
| `triage.py` | `adjudicate-enabled` `adjudicate-hot-enabled` `decided` `lane` `list` | `evolve_reap`, `evolve_self_merge`, `flow_worktree` |
| `validate_workspace.py` | — | `dispatch_stage` |
| `version.py` | `stamp` | — |
| `worker_pool.py` | `guard` `limit` `recover` `snapshot` | — |
| `worktree_janitor.py` | `sweep` | — |
<!-- flow:module-map:end -->
