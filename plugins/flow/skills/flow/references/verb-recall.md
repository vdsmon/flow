# recall verb

`/flow recall <query> [--branch X --top-n N]`. Query the compounding memory layer. Routed from SKILL.md's argument table.

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
- Exit 1 → workspace unresolvable. Surface stderr + `/flow init` hint.

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
python3 ${CLAUDE_SKILL_DIR}/scripts/recall.py --metric revert-rate \
  --namespace <ns> --workspace-root . \
  [--since YYYY-MM-DD] [--until YYYY-MM-DD]
```

Joins each in-window ship-event to its tracker status history (`bd history <key> --json`): a revert is a shipped bead reopened and re-closed AFTER its `shipped_at`. Reports `shipped`, `n_reverts`, `revert_rate`, and the attribution split `reverts_via_flow` / `reverts_not_attributed`, plus per-ticket detail and a `skipped` list (`history_unavailable`, `tracker_unsupported` on non-beads, `reopened_not_yet_reclosed`). `--namespace` is required. No `--checkpoint` option. Beads-only; non-beads workspaces short-circuit to an all-skipped report.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/recall.py --metric trend \
  --namespace <ns> --workspace-root . \
  [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--json]
```

Rolls up all four window measures (tickets-per-week, time-to-pr, friction-per-run, revert-rate) over one `[since, until)` window. Default output is a human-readable table, one row per measure with its headline numbers; `--json` emits a JSON object keyed by the four measure names (each carrying that measure's full report) plus top-level `since`, `until`, and `resolved_workspace_root`. The revert row surfaces the `reverts_by_source` `{tracker, git}` split. `--namespace` is required. No `--checkpoint` option. Inherits revert-rate's beads-only + git-repo requirement, so it fails loud on a git-scan error rather than emitting an empty roll-up.
