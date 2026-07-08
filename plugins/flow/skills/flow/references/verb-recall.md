# recall verb

`/flow recall <query> [--branch X --top-n N]`. Query the compounding memory layer. Routed from SKILL.md's argument table.

**Sub-verb dispatch:** if the first post-verb token is exactly `prune` (`/flow memory prune`, `/flow recall prune`), this is NOT a query — skip the argv build and follow `## memory prune` below. (To recall the literal word "prune", use a longer query.)

Pass-through to `recall.py`.
Build the argv from `$ARGUMENTS`:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/recall.py "<query>" \
  [--branch <name>] \
  [--tickets <csv>] \
  [--top-n <n>] \
  --workspace-root .
```

- Exit 0 → JSON array to stdout. Surface as a formatted list to the user.
- Exit 1 → workspace unresolvable, OR no query supplied. Surface stderr + `/flow init` hint.

## Label-scoped recall (faceted memory)

`recall.py --label <facet:value>` (e.g. `--label form:iva_2083`) hard-filters to entries carrying that label BEFORE ranking and returns the WHOLE cluster (the `--top-n` cap is lifted to the corpus size — exhaustive retrieval, not top-n truncation). The query becomes optional: a label-only invocation returns the full live cluster ordered newest-first, and never reads stdin (safe in any harness Bash call). A label miss is `[]` exit 0, not an error. Facet vocabulary comes from `[memory] label_facets` in workspace.toml (ships empty here; a workspace with a natural facet opts in).

### `--digest` (markdown card over the label cluster)

`recall.py --label <facet:value> --digest` renders the exhaustive label cluster as a
human-readable markdown card instead of the raw JSON array: one section per entry
`type` (canonical order DECISION, FACT, LEARNED, PATTERN, INVESTIGATION, DEVIATION,
any other type sorted alphabetically after; only non-empty sections render), entries
newest-first (`ts` DESC) within a section, one line per entry —
`- <ts> · <ticket> · <first sentence of body>`. Superseded entries stay excluded (the
same `filter_superseded` upstream of ranking). `--digest` without `--label` is an
argparse error (exit 2). Plain JSON output (no `--digest`) is unchanged.

## Semantic recall (optional overlay)

When the workspace opts into `[memory.semantic] enabled = true`, recall fuses BM25 with a cosine ranking over a derived embedding sidecar (`knowledge.embed`), so a query worded differently from the stored body still surfaces it. It is byte-identical pure BM25 when the block is absent/off, and falls back to BM25 (with a stderr backend-status line) on any embedder/index failure — the `python3` runtime invariant holds because the embedder runs in a `uvx` subprocess, not in-process.

Extra flags (all optional; the plain `recall <query>` form above is unchanged):

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/recall.py \
  --query-file <path> \
  --semantic --threshold <τ> --top-n <n> \
  [--branch <name>] [--tickets <csv>] \
  --workspace-root .
```

- `--query-file <path>` (or stdin) — pass a multi-line query (the ticket title+body) WITHOUT a shell positional, avoiding the `"`/`\`/newline hazard. The positional still wins when given.
- `--semantic` — force the semantic path on regardless of config. Cosine candidates are selected by RANK (top-K, driven by `--top-n`); `--threshold <τ>` is only a low floor that drops non-positive (anti-correlated) cosines, NOT the candidate gate (default 0.0).
- **Per-context budgets.** The interactive `/flow recall` stays tight (modest `--top-n`, protects the live context budget). The plan-phase deep recall (verb-spec / stage-plan) runs looser (`--top-n 30`), latency-tolerant, where the semantic overlay pays off.

**Refresh the index** (incremental; embeds only new entries):

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/recall.py --reindex --workspace-root .
```

Add `--full` to force a full rebuild. **First-enable requires one bulk reindex** — flipping `enabled = true` on an existing workspace starts with an empty index, so recall is BM25-only until this runs once.

**Record-pending** (the post-gate producer that replaces the old SessionStart recall; a WRITE, so legal only after `ExitPlanMode`):

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/recall.py --query-file <path> \
  --semantic --top-n 30 --record-pending \
  --branch "feat/$KEY-<slug>" --ticket "$KEY" \
  --workspace-root "<the worktree path the bootstrap printed>"
```

`--record-pending` appends the recalled ids to the worktree's `recall-pending.jsonl`; `dispatch_stage init` later promotes them into the run's `recall-log.jsonl` (so reflect's `recalled_entries` still works). It needs both `--branch` and `--ticket`. **Target the run WORKTREE and its feature branch, never the main checkout.** Promotion runs from inside the worktree and matches exactly on branch + cwd (plus a head-sha-ancestor check), so recording with `--workspace-root .` and an integration branch writes a DIFFERENT `recall-pending.jsonl` that never promotes — `recalled_entries` stays empty with no error anywhere. Full promotion rules: `verb-spec.md` step 6.

## recall --metric (the 14-day checkpoint calculator)

`/flow recall --metric tickets-per-week [...]` is a pass-through to the metric calculator (recall.py forwards `--metric` to `metric.py`):

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/recall.py --metric tickets-per-week \
  --namespace <ns> --workspace-root . \
  [--since YYYY-MM-DD] [--until YYYY-MM-DD] \
  [--checkpoint --mode personal|work --manifest-path <p>]
```

It counts shipped tickets in the window from the immutable ship-event evidence and splits `shipped_via_flow` (ticket+run+reflect three-way binding verified) from `shipped_backend_not_attributed`.
`--checkpoint --mode` aggregates across the checkpoint manifest's participants of that mode.
Surface the JSON report.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/recall.py --metric time-to-pr \
  --namespace <ns> --workspace-root . \
  [--since YYYY-MM-DD] [--until YYYY-MM-DD]
```

It counts flow-attributed shipped tickets in-window and reports observed time-to-PR (plan-start → create_pr-finish) as median_hours / p90_hours, the trio's third leg.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/recall.py --metric friction-per-run \
  --namespace <ns> --workspace-root . \
  [--since YYYY-MM-DD] [--until YYYY-MM-DD]
```

Reads `.flow/<namespace>/friction.jsonl`, counts entries in the time window, and reports `total_events`, `runs`, `events_per_run`, `by_type`, and `by_severity`. `--namespace` is required. No `--checkpoint` option (friction-per-run has no checkpoint aggregation path).

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/recall.py --metric corpus-health \
  --namespace <ns> --workspace-root . \
  [--since YYYY-MM-DD] [--until YYYY-MM-DD]
```

Reads `.flow/<namespace>/knowledge.jsonl` and reports `total_entries`, `live_entries`, `superseded_entries` (entries whose `id` is named by another entry's `supersedes`), `supersession_rate`, `supersedes_in_window` (supersede records whose `ts` is in the window — the over-time axis), `decisions_total`, `decisions_live`, and `oldest_live_decision` (`{id, ts, age_days}` or null — the oldest LIVE, i.e. non-superseded, DECISION). `--namespace` is required. No `--checkpoint` option. An empty/missing knowledge.jsonl returns zeros (the h8s7 cwd guard only fires when there is no `.flow/.initialized`).

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/recall.py --metric revert-rate \
  --namespace <ns> --workspace-root . \
  [--since YYYY-MM-DD] [--until YYYY-MM-DD]
```

Joins each in-window ship-event to its tracker status history (`bd history <key> --json`): a revert is a shipped bead reopened and re-closed AFTER its `shipped_at`. Reports `shipped`, `n_reverts`, `revert_rate`, and the attribution split `reverts_via_flow` / `reverts_not_attributed`, plus per-ticket detail and a `skipped` list (`history_unavailable`, `tracker_unsupported` on non-beads, `reopened_not_yet_reclosed`). `--namespace` is required. No `--checkpoint` option. Dual-source: the tracker join is beads-only (on a non-beads backend every ship-event lands in `skipped` as `tracker_unsupported`), while a git-log scan counts revert commits naming in-window keys on ANY backend — reported as `reverts_by_source` `{tracker, git}` plus per-revert `git_reverts` detail, with a durable revert event emitted per reverting commit. A git-scan failure (e.g. not a git repo) fails loud with exit 1 rather than under-reporting.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/recall.py --metric recall-hit-rate \
  --namespace <ns> --workspace-root . \
  [--since YYYY-MM-DD] [--until YYYY-MM-DD]
```

Reads `.flow/<namespace>/recall-usage.jsonl` (the usage + miss records written at reflect) and reports `surfaced`, `used`, `hit_rate` (used / surfaced, 0.0 when nothing surfaced), `misses` (near-duplicate re-learns recall failed to surface, the false-negative proxy), and `runs` (distinct run_ids across both kinds). `--namespace` is required. No `--checkpoint` option.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/recall.py --metric trend \
  --namespace <ns> --workspace-root . \
  [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--json]
```

Rolls up all five window measures (tickets-per-week, time-to-pr, friction-per-run, revert-rate, recall-hit-rate) over one `[since, until)` window. Default output is a human-readable table, one row per measure with its headline numbers; `--json` emits a JSON object keyed by the five measure names (each carrying that measure's full report) plus top-level `since`, `until`, and `resolved_workspace_root`. The revert row surfaces the `reverts_by_source` `{tracker, git}` split. `--namespace` is required. No `--checkpoint` option. Inherits revert-rate's git-repo requirement, so it fails loud on a git-scan error rather than emitting an empty roll-up.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/recall.py --metric fix-efficacy \
  --namespace <ns> --workspace-root . \
  [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--json]
```

Per closed MACHINERY-fix bead (a `.flow/<namespace>/knowledge.jsonl` entry whose body starts with `MACHINERY`, grouped by `ticket`), reports whether the friction anchor(s) it claimed to fix recurred afterward: a `recurred` / `clean` verdict plus evidence (`post_fix_count`, `claimed_anchors`, `recurrence_run_ids`, `stages`, `types`, `recurrences`, `fix_shas`). A bead is `unmeasurable` (still counted `clean`, never a third verdict) when it claims no distinctive anchor or has no usable fix timestamp — it cannot forward-join, so it cannot recur. Default output is a per-bead table; `--json` emits `{beads, totals, resolved_workspace_root}`, where `totals` carries `fix_beads`/`recurred`/`clean`/`unmeasurable`/`recurrence_rate` (recurred / fix_beads, over ALL fix_beads including the unmeasurable ones). `--namespace` is optional (auto-resolves from workspace.toml when omitted). This is a lifetime metric: `--since`/`--until` are accepted for CLI-surface symmetry but IGNORED.

## memory prune

`/flow memory prune` (equivalently `/flow recall prune`). Retire the corpus's dead weight: entries recall keeps surfacing that no run ever uses, and project auto-memory files the repo has since captured or disproved. Repeatable, not exhaustive — each pass works the head of the ranking; run it again when the reflect-stage nudge reappears.

**Step 0 — interactive-only guard.** The flow below gates every write on `AskUserQuestion`, which a headless run cannot answer. Detect an `--auto` context by session context — the same signal the SKILL.md do-loop uses to suppress the PR-ready notification (`references/verb-do.md`). If this is an `--auto` run: print `memory prune is interactive-only; rerun without --auto` and stop.

### Phase 1 — flow knowledge corpus

1. Build the usage-ranked worklist, redirected to a file (never cat the whole corpus into context):
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/sweep_knowledge.py propose \
     --type all --with-usage --workspace-root . > prune-worklist.json
   ```
   Each item carries `{id, ticket, ts, type, body, surfaced_count, used_count, miss_count, last_surfaced, tier}`. Tiers, in worklist order: **0** = surfaced but never used (recall spent context on it; no run leaned on it — most-surfaced first), **1** = never surfaced (dead weight or just young — oldest first), **2** = used at least once (earned its place — prune only if disproved).

2. **Verify (the judgment step).** Walk the worklist top-down, capping the pass at ~30 candidates. Each body asserts something — grep/Read the CURRENT code (and any PR it names) to check the assertion still holds and still matters. `miss_count >= 1` is a KEEP bias: the corpus RE-learned that entry while recall failed to surface it — it is valuable, the recall side is what failed. A young tier-1 entry (recent `ts`) is not dead, just unproven — skip it. Batches of candidates may be delegated to read-only `Explore` agents. Only a CONFIRMED-dead entry enters the manifest, one record each:
   ```json
   {"superseded_id": "<id>", "superseding_ticket": "<KEY>", "rationale": "<why moot + what replaced it>"}
   ```
   `superseding_ticket` = this session's ticket key if it has one, else `memory-prune-<YYYY-MM-DD>`. Write the records to `prune-manifest.json`.

3. **The gate — ONE `AskUserQuestion` before any write.** Present: total candidates, counts by type and tier, and 3-5 samples (id + first sentence of body + rationale). Options: apply / show the full manifest first / abort. Nothing is written until the user picks apply.

4. Apply (append-only tombstones through the same seam the curate lane uses; idempotent, a re-run is a no-op):
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/sweep_knowledge.py apply \
     --manifest prune-manifest.json --workspace-root .
   ```
   Surface the applied/skipped/error summary. Exit 5 = at least one record errored (unknown id, empty id) — report those records; the rest applied. Recall filters superseded entries from the next run automatically.

5. **Optional consolidation pass.** Offer it after apply: near-duplicate live entries collapse to one canonical body via `cluster` → confirm → `apply-cluster`. The procedure (incl. manifest shape) is `references/verb-evolve.md` §curate → Consolidation — follow it verbatim, with the same one-question confirm discipline as step 3.

### Phase 2 — project auto-memory

The other store: the on-disk project memory directory your harness names in its memory instructions (system context) — the same one the reflect stage's lens C writes. If the harness names none, note that and skip this phase.

1. **Backup FIRST**, outside the memory dir: `tar -czf <parent>/memory-backup-<YYYY-MM-DD>.tar.gz -C <memory-parent> <memory-dirname>`. Print the path and the one-line restore command.
2. Classify every entry listed in the index (`MEMORY.md`), opening the entry file when the index line is ambiguous:
   - `metadata.type: feedback` → **NEVER pruned**, unconditional keep, do not even propose it;
   - obsolete — marked fixed/retired/superseded, or a one-off incident note whose residual lives in the tracker;
   - captured-in-repo — the fact now lives in a repo doc or check. **Grep the repo and VERIFY the capture before believing this class** — a memory that only claims to be captured stays;
   - live — keep.
3. Confirm in batches of ~10 via `AskUserQuestion` (entry name + one-line reason each). On approval: delete those entry files and remove exactly their lines from the index — never delete `MEMORY.md` itself; every unrelated line stays byte-identical. Dangling `[[links]]` in surviving entries are legal in that memory discipline — do not chase them.
4. Report: counts per class, files deleted, the backup path.
