# sync verb (work-mode)

Work-mode verb. Routed from SKILL.md's argument table.

## sync verb

`/flow sync` drains `.flow/pending-mutations.jsonl`, the tracker writes (create / transition / comment / link) that the commit-stage `.flow/flow tracker transition --enqueue-on-transient` chokepoint queued after a transient failure, and reconciles them against live tracker state.
Work-mode verb.

```bash
.flow/flow sync --workspace-root .
```

- Exit 0 → JSON report `{applied, applied_externally, superseded, failed, parked, removed}`.
  Surface counts; `applied_externally` = the op was already done (idempotency win), `superseded` = the pre-state changed under it (skipped), `parked` = entries whose op no adapter can replay, e.g. the retired generic edit (kept on disk with a warning and excluded from the exit code; drop via `.flow/flow pending-mutations --workspace-root . compact --drop-keys <key>`).
- Exit 1 → some entries still failed; they stay queued for the next sync.
- Exit 2 → workspace / tracker unavailable. Surface stderr.
