# recover verb

`/flow recover [<ticket>]` inspects a run for stuck leases, failed stages, and config drift, then drives the matching remediation. Routed from SKILL.md's argument table.
It does not run stages; after a successful fix it hands back to `/flow do`.

1. Resolve the ticket. If adapter-supplied `arguments` had a positional, use it. Else:
   ```bash
   KEY=$(.flow/flow branch-ticket --workspace-root .)
   ```
   Exit 0 → use `$KEY`.
   Exit 3 → no key on branch; use the adapter's user-input capability.
   Exit 1 → workspace not initialized; abort with the `/flow init` hint.

2. Detect:
   ```bash
   .flow/flow recover detect \
     --ticket "$KEY" --workspace-root .
   ```
   Surface the report.
   It carries (at minimum) `lease.state`, the failed stage if any, `snapshot.ok`, and `ship_event_attention`.
   It also carries `holder_liveness`, an advisory best-effort probe of the recorded session process: `alive` is true/false, or null when the holder is cross-host, unrecorded, or the probe failed. Treat it as a hint only, a live result can be a reused pid, and it never gates reclaim; `takeover --force` stays the only reclaim path.

3. Drive remediation from the report + the user's intent.
   When a step is destructive, confirm through the adapter's user-input capability first.

   - **Stale / expired lease** — `lease.state` is `expired_foreign` or
     `expired_reboot_clearable` (or the user explicitly wants the ticket):
     ```bash
     .flow/flow recover takeover \
       --ticket "$KEY" --workspace-root .
     ```
     Confirm first: takeover clears the run lock and resets `in_progress` stages back to `pending`.
     It refuses (exit 1) when the lease is `live`; surface that and stop rather than forcing it.
     To reclaim a lease that still looks live anyway (the holder is provably dead before its TTL
     elapses), add `--force`; this is operator-explicit (a human asserts holder deadness), mirrors
     `abort --force`, and still does the full reclaim + `in_progress`->`pending` reset + snapshot.
     `lease.classify` is unchanged: no automatic pid-liveness, the force is the only bypass.

   - **Failed stage** — the report names a stage in `failed`.
     Offer the three choices through the adapter's user-input capability:
     - retry: `.flow/flow recover retry --stage <S> --ticket "$KEY" --workspace-root .`
     - skip: `.flow/flow recover skip --stage <S> --ticket "$KEY" --workspace-root .`
     - abort: `.flow/flow recover abort --ticket "$KEY" --workspace-root .`
       abort classifies the lease under the run flock and refuses (exit 1) when it is `live` — releasing a live lease would de-mutex a sibling run that re-acquired the ticket. To release a lease that still looks live anyway (the run is genuinely wedged), add `--force`; this is operator-explicit, so confirm first.

   - **Config / version drift** — `snapshot.ok` is false (workspace.toml,
     stage-registry, a handler plugin, or the `engine` tree changed since the
     run started). An `engine` drift means a mid-run `git pull` /
     `claude plugin marketplace update` on the main checkout swapped the engine
     code; the run aborts fail-closed rather than silently executing swapped
     machinery. Recovery is the same for every component:
     - accept the current config:
       `.flow/flow recover reload-snapshot --ticket "$KEY" --workspace-root .`
     - abort: `.flow/flow recover abort --ticket "$KEY" --workspace-root .`

4. After a successful recover action, tell the user to rerun
   `/flow do <KEY>`.

**Ship-event attention**: `ship_event_attention > 0` means duplicate or corrupt ship-event files exist for the ticket.
Surface the count and tell the user to review them manually.
Deep ship-event reconciliation is not automated in this phase.
