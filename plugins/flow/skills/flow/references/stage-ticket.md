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

2. Fetch ticket details from the tracker:
   ```bash
   ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py \
     --workspace-root . \
     get --key <KEY> > <ticket-dir>/ticket.json
   ```
   - Exit 0: ticket.json contains the full Ticket payload (key, summary,
     status, description, type, assignee, comments, parent, attachments,
     links).
   - Exit 1: tracker error (network / auth / unknown key).
     Surface stderr + `/flow recover --ticket <KEY>` hint.
     Abort stage with status=failed.
   - Exit 2: workspace config invalid.
     Should not happen at this point — surface stderr + abort.

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
