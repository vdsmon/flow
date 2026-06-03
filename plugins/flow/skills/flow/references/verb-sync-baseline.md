# sync + baseline verbs (work-mode)

The two work-mode verbs. Routed from SKILL.md's argument table.

## sync verb

`/flow sync` drains `.flow/pending-mutations.jsonl` — tracker writes (transition / comment / link / edit) that an adapter queued after a transient failure — and reconciles them against live tracker state.
Work-mode verb.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/sync.py --workspace-root .
```

- Exit 0 → JSON report `{applied, applied_externally, superseded, failed,
  removed}`.
  Surface counts; `applied_externally` = the op was already done (idempotency win), `superseded` = the pre-state changed under it (skipped).
- Exit 1 → some entries still failed; they stay queued for the next sync.
- Exit 2 → workspace / tracker unavailable. Surface stderr.

## baseline verb

`/flow baseline` manages the pre-migration time-to-PR baseline the work-mode gate compares against (±30%).
Live collection from Jira/Bitbucket is manual; this verb owns the file + the statistics.

```bash
# build from samples (a JSON list of {ticket, time_to_pr_hours}):
python3 ${CLAUDE_SKILL_DIR}/scripts/baseline_collect.py build \
  --samples-json <file-or-inline-json> [--path <p>] [--source <s>]
# show the stored baseline:
python3 ${CLAUDE_SKILL_DIR}/scripts/baseline_collect.py show [--path <p>]
```

- Exit 0 → writes/prints the baseline (median + p90 + n).
- Exit 1 → no samples, or an unparseable `--samples-json` value.
- Exit 2 → argparse usage error (missing subcommand or `--samples-json`).
- Exit 3 → I/O error, or `show` found no stored baseline.
