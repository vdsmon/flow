# recover verb

`/flow recover [<ticket>]` inspects a run for stuck leases, failed stages, and config drift, then drives the matching remediation. Routed from SKILL.md's argument table.
It does not run stages; after a successful fix it hands back to `/flow do`.

1. Resolve the ticket. If `$ARGUMENTS` had a positional, use it. Else:
   ```bash
   KEY=$(python3 ${CLAUDE_SKILL_DIR}/scripts/branch_ticket.py --workspace-root .)
   ```
   Exit 0 → use `$KEY`.
   Exit 3 → no key on branch; ask via AskUserQuestion.
   Exit 1 → workspace not initialized; abort with the `/flow init` hint.

2. Detect:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/recover.py detect \
     --ticket "$KEY" --workspace-root .
   ```
   Surface the report.
   It carries (at minimum) `lease.state`, the failed stage if any, `snapshot.ok`, and `ship_event_attention`.

3. Drive remediation from the report + the user's intent.
   When a step is destructive, confirm with AskUserQuestion first.

   - **Stale / expired lease** — `lease.state` is `expired_foreign` or
     `expired_reboot_clearable` (or the user explicitly wants the ticket):
     ```bash
     python3 ${CLAUDE_SKILL_DIR}/scripts/recover.py takeover \
       --ticket "$KEY" --workspace-root .
     ```
     Confirm first: takeover clears the run lock and resets `in_progress` stages back to `pending`.
     It refuses (exit 1) when the lease is `live`; surface that and stop rather than forcing it.

   - **Failed stage** — the report names a stage in `failed`.
     Offer the three choices via AskUserQuestion:
     - retry: `recover.py retry --stage <S> --ticket "$KEY" --workspace-root .`
     - skip: `recover.py skip --stage <S> --ticket "$KEY" --workspace-root .`
     - abort: `recover.py abort --ticket "$KEY" --workspace-root .`
       abort classifies the lease under the run flock and refuses (exit 1) when it is `live` — releasing a live lease would de-mutex a sibling run that re-acquired the ticket. To release a lease that still looks live anyway (the run is genuinely wedged), add `--force`; this is operator-explicit, so confirm first.

   - **Config / version drift** — `snapshot.ok` is false (workspace.toml,
     stage-registry, a handler plugin, or the `engine` tree changed since the
     run started). An `engine` drift means a mid-run `git pull` /
     `claude plugin marketplace update` on the main checkout swapped the engine
     code; the run aborts fail-closed rather than silently executing swapped
     machinery. Recovery is the same for every component:
     - accept the current config:
       `recover.py reload-snapshot --ticket "$KEY" --workspace-root .`
     - abort: `recover.py abort --ticket "$KEY" --workspace-root .`

4. After a successful recover action, tell the user to rerun
   `/flow do <KEY>`.

**Ship-event attention**: `ship_event_attention > 0` means duplicate or corrupt ship-event files exist for the ticket.
Surface the count and tell the user to review them manually.
Deep ship-event reconciliation is not automated in this phase.
