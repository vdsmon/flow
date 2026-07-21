# Stage: ticket

## Purpose

Resolve the ticket key, fetch ticket context from the tracker, write a local cache, and stamp the ticket's frontmatter `status` to `in_progress`.

This is the first stage of the delivery loop.
Subsequent stages depend on `<ticket-dir>/ticket.json` being present.

## Inputs

- `<ticket-dir>` (passed by the dispatcher).
- Current git branch (used by `branch_ticket.py` when the verb caller did not
  provide an explicit ticket key).
- `.flow/workspace.toml` `[tracker]` block.

## Steps

1. Confirm the ticket key.
   The dispatcher already passed it in its descriptor, but verify it is non-empty.
   If empty:
   ```bash
   FLOW_HARNESS="<harness>" "<facade>" branch-ticket --workspace-root .
   ```
   Exit 3 (no match) → abort stage with status=failed;
   the user must rerun with an explicit `--ticket` arg.

2. Fetch ticket details into `<ticket-dir>/ticket.json` — the canonical Ticket payload downstream stages read (key, summary, status, description, type, assignee, comments, parent, attachments, links). This cache must contain the complete tracker payload returned by the selected adapter, never a summary.

   **Codex.** Fetch with the rooted facade, capture its complete stdout JSON, then use
   Codex's rooted exact writer to write that JSON to `<ticket-dir>/ticket.json`:
   ```bash
   FLOW_HARNESS=codex "<facade>" tracker \
     --workspace-root . \
     get --key <KEY>
   ```
   Do not use shell redirection and do not reconstruct or summarize the payload. Use
   the same exact writer to create the descriptor's `<artifact-path>` (`ticket.out`)
   with a concise fetch/status report before advancing the stage.

   **Claude Code with Atlassian MCP.** When the Atlassian MCP is available, fetch via
   the MCP and use the host writer to store the full result in
   `<ticket-dir>/ticket.json` in that Ticket shape. The MCP is auth-fresh and needs no
   environment credentials.

   **Native-host fallback.** If Claude Code has no Jira MCP, use the same facade capture
   plus exact-writer path described for Codex, with `FLOW_HARNESS=claude-code`.

   **Generic/headless fallback.** When no host exact writer is available, redirect the
   tracker facade output directly to the cache:
   ```bash
   FLOW_HARNESS="<harness>" "<facade>" tracker \
     --workspace-root . \
     get --key <KEY> > <ticket-dir>/ticket.json
   ```
   - Exit 0: ticket.json written.
   - Exit 1: tracker error (network / auth / unknown key). If env creds are simply absent and the Atlassian MCP is reachable, use the MCP path above instead of failing. Otherwise surface stderr + `FLOW workspace repair <KEY>`; abort stage with status=failed.
   - Exit 2: workspace config invalid. Surface stderr + abort.

2b. **Download ticket attachments** so the plan / implement stages can see screenshots, specs, and sample files. When `ticket.json` lists any under `attachments` (Jira; beads has none):
   ```bash
   FLOW_HARNESS="<harness>" "<facade>" tracker \
     --workspace-root . \
     download-attachments --key <KEY> --out <ticket-dir>/attachments
   ```
   - Exit 0 → JSON `{supported, key, downloaded[]}`. `supported=false` (beads) or an empty `downloaded[]` is normal — continue. Each entry is `{filename, size, path}`, or `{filename, size, skipped}` when over the 25 MiB cap. Note the saved paths so later stages can read them.
   - The Atlassian MCP has **no** attachment-download tool, so this always uses the REST adapter, which needs `ATLASSIAN_EMAIL` / `ATLASSIAN_API_TOKEN`. If those are absent, log a `MISSING_TOOL` friction entry and continue — attachment download is **best-effort**, never a stage blocker.

3. Stamp ticket frontmatter `status` + `started_at`:
   ```bash
   FLOW_HARNESS="<harness>" "<facade>" frontmatter update \
     .flow/tickets/<KEY>.md \
     --set ticket=<KEY> \
     --set status=in_progress \
     --set started_at=NOW
   ```
   - Exit 0: continue.
   - Exit 1: lock contention.
     Retry once after 1s.
     If retry also fails, abort.
   - Exit 2: schema invalid in existing frontmatter.
     Abort with status=failed.
   - Exit 3: I/O error.
     Abort + recover hint.

3b. **Claim the ticket in the tracker backend** (`open` → `in_progress`) so a parallel fleet does not double-pick it.
   Step 3 stamped the frontmatter `status`, but that is local to `.flow`; the tracker backend (the bd issue) still reads `open`, so `bd ready` keeps listing this in-flight ticket as available until this transition lands. This is distinct from the run lease (which only guards against two *flow* runs colliding): this keeps the backend's own status truthful for external fleet consumers.

   **MCP-first:** when the Atlassian MCP is available, transition via it (`transitionJiraIssue`) — auth-fresh, the primary path in an attached run. **REST fallback** (a backgrounded / headless run, or beads):
   ```bash
   FLOW_HARNESS="<harness>" "<facade>" tracker \
     --workspace-root . \
     transition --key <KEY> --to-state in_progress
   ```
   This claim is **best-effort: it never fails the stage.** No git work has happened yet, so the only thing at stake is the double-pick window; aborting the whole run because the backend would not move is worse than proceeding on the local stamp plus a logged warning. Read the printed JSON for `failure_kind` + `failure_detail`. Exit-code handling:
   - Exit 0 → claimed; continue.
   - Exit 3 → no transition to `in_progress` available. This is **ambiguous**: the ticket is already `in_progress` (a resumed run) or the tracker has no such state (benign), OR the bead is **terminal** (closed/done/cancelled) and the only transition left is `to open`. The benign case continues silently; a terminal bead must NOT — that is the flow-d6gq leak (an unattended run bootstrapped a CLOSED bead, the close having landed between spec-fetch and here, and this exit-3 swallowed it). Disambiguate by reading the authoritative status:
     ```bash
     FLOW_HARNESS="<harness>" "<facade>" tracker \
       --workspace-root . \
       state --key <KEY>
     ```
     Read the printed `normalized`. If it is `done` or `cancelled` → the bead is terminal; **abort the stage with status=failed** (the bead is closed — there is nothing to implement; it likely closed mid-run, e.g. a parent epic merged). Surface it + the `FLOW workspace repair <KEY>` hint. Otherwise (`in_progress` / `open` / no-such-state, or the `state` read itself errors) → the desired end state or a no-op tracker; continue silently.
   - Exit 1 / 2 / 4 / 5 → log a one-line warning naming `failure_kind` + `failure_detail` (from the printed JSON, else the stderr message), append one friction entry, and continue. Do **NOT** mark the stage failed.
     ```bash
     FLOW_HARNESS="<harness>" "<facade>" friction \
       --ticket <KEY> --run-id <run_id> --stage ticket \
       --type RECONCILE --severity minor \
       --body "ticket-stage backend claim to in_progress did not apply" \
       --detail "<failure_kind>: <failure_detail>" \
       --workspace-root . || true
     ```

## Outputs

- `<ticket-dir>/ticket.json` — full cached ticket payload.
- `<ticket-dir>/attachments/` — downloaded ticket attachments (best-effort; absent for beads or when REST creds are unavailable).
- `.flow/tickets/<KEY>.md` — ticket frontmatter with `status=in_progress`
  and `started_at` set.
- Tracker backend transitioned to `in_progress` (best-effort; left unchanged if the backend has no such state or the claim failed — the stage never fails on this).

## Errors

- Exit 1 from `tracker_cli.py get` → abort status=failed; once the tracker/creds
  cause is fixed, `FLOW workspace repair <KEY>` → `retry --stage ticket`.
- Exit 2/3 from `ticket_frontmatter.py` → abort status=failed; once the
  frontmatter input is fixed, `FLOW workspace repair <KEY>` → `retry --stage ticket`.
- `FLOW_HARNESS="<harness>" "<facade>" tracker transition` exit 3 in step 3b → ambiguous: read `FLOW_HARNESS="<harness>" "<facade>" tracker state --key <KEY>` and check `normalized`. `done`/`cancelled` → terminal bead, abort status=failed (`FLOW workspace repair <KEY>`). Otherwise (`in_progress` / no such state) → continue silently (not an error).
- `tracker_cli.py transition` exit 1 / 2 / 4 / 5 in step 3b → best-effort claim; warn + append a `RECONCILE` friction entry + continue. Never fails the stage.

## Skip conditions

None.
This stage always runs in the bare workspace pipeline.

## Note: no `lint_ticket` HARD GATE

Other stages call `FLOW_HARNESS="<harness>" "<facade>" lint-ticket --stage <name> --ticket-path .flow/tickets/<KEY>.md` as a HARD GATE before doing any work.
The `ticket` stage is the exception: this stage CREATES the ticket frontmatter file.
Running `lint_ticket` here would always fail (universal `ticket` + `status` fields don't exist yet because step 3 is what writes them).
Future stages can safely lint because step 3 leaves a valid frontmatter behind.
