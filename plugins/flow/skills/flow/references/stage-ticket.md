# Stage: ticket

## Purpose

Resolve the ticket key, fetch ticket context from the tracker, write a local cache, and stamp the ticket's frontmatter `status` to `in_progress`.

This is the first stage of `/flow do`.
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
   ${CLAUDE_SKILL_DIR}/scripts/branch_ticket.py --workspace-root .
   ```
   Exit 3 (no match) → abort stage with status=failed;
   the user must rerun with an explicit `--ticket` arg.

2. Fetch ticket details into `<ticket-dir>/ticket.json` — the canonical Ticket payload downstream stages read (key, summary, status, description, type, assignee, comments, parent, attachments, links).

   **MCP-first.** When the Atlassian MCP is available (an attached session usually has it — `getJiraIssue` etc.), fetch via the MCP and write the result into `<ticket-dir>/ticket.json` in that Ticket shape. The MCP is auth-fresh and needs no env credentials, so it is the primary path in an attached run (this is what production already reaches for).

   **REST fallback** — when the MCP is absent (a backgrounded / headless run), or for any workspace where it is unavailable:
   ```bash
   ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py \
     --workspace-root . \
     get --key <KEY> > <ticket-dir>/ticket.json
   ```
   - Exit 0: ticket.json written.
   - Exit 1: tracker error (network / auth / unknown key). If env creds are simply absent and the Atlassian MCP is reachable, use the MCP path above instead of failing. Otherwise surface stderr + `/flow recover --ticket <KEY>` hint; abort stage with status=failed.
   - Exit 2: workspace config invalid. Surface stderr + abort.

2b. **Download ticket attachments** so the plan / implement stages can see screenshots, specs, and sample files. When `ticket.json` lists any under `attachments` (Jira; beads has none):
   ```bash
   ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py \
     --workspace-root . \
     download-attachments --key <KEY> --out <ticket-dir>/attachments
   ```
   - Exit 0 → JSON `{supported, key, downloaded[]}`. `supported=false` (beads) or an empty `downloaded[]` is normal — continue. Each entry is `{filename, size, path}`, or `{filename, size, skipped}` when over the 25 MiB cap. Note the saved paths so later stages can read them.
   - The Atlassian MCP has **no** attachment-download tool, so this always uses the REST adapter, which needs `ATLASSIAN_EMAIL` / `ATLASSIAN_API_TOKEN`. If those are absent, log a `MISSING_TOOL` friction entry and continue — attachment download is **best-effort**, never a stage blocker.

3. Stamp ticket frontmatter `status` + `started_at`:
   ```bash
   ${CLAUDE_SKILL_DIR}/scripts/ticket_frontmatter.py update \
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

## Outputs

- `<ticket-dir>/ticket.json` — full cached ticket payload.
- `<ticket-dir>/attachments/` — downloaded ticket attachments (best-effort; absent for beads or when REST creds are unavailable).
- `.flow/tickets/<KEY>.md` — ticket frontmatter with `status=in_progress`
  and `started_at` set.

## Errors

- Exit 1 from `tracker_cli.py get` → `/flow recover --reset-ticket <KEY>`
  (recover is phase 8c; for now, manual retry).
- Exit 2/3 from `ticket_frontmatter.py` → `/flow recover --reset-frontmatter
  <KEY>` (manual fix).

## Skip conditions

None.
This stage always runs in the bare workspace pipeline.

## Note: no `lint_ticket` HARD GATE

Other stages call `${CLAUDE_SKILL_DIR}/scripts/lint_ticket.py --stage <name> --ticket-path .flow/tickets/<KEY>.md` as a HARD GATE before doing any work.
The `ticket` stage is the exception: this stage CREATES the ticket frontmatter file.
Running `lint_ticket` here would always fail (universal `ticket` + `status` fields don't exist yet because step 3 is what writes them).
Future stages can safely lint because step 3 leaves a valid frontmatter behind.
