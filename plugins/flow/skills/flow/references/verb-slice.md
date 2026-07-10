# slice verb

`/flow slice <ticket>`. Interactive-only. Maps a wide refactor's blast radius,
designs an expand → migrate → contract ladder of independently-green child
tickets, and — after `ExitPlanMode` approval — mints the children through the
tracker seam, wires the dependency edges, prints the frontier, and stops. It is
`group`'s inverse: `group` folds sibling tickets into one PR, `slice` unfolds one
oversized refactor into a chain of PRs that each land green alone.

There is no `--auto` route to `slice`. A slice is a judgment call about how a
refactor decomposes, and the refusal gate (step 5) needs a human. The whole verb
is read-only until `ExitPlanMode`; only the post-approval mint (step 7) writes.

The arc: `slice` → `/flow <expand>` → `/flow <migrate-1>` → … → `/flow <contract>`
(one PR per rung, each closing its child; the contract PR also closes the parent
umbrella).

1. **Be in plan mode.** The front half performs no writes.

2. **Resolve the key and fetch context.** Key is the positional `$ARGUMENTS`
   (else `branch_ticket.py --workspace-root .`). Read the ticket:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . get --key "$KEY"
   ```
   Run the plan-phase READ recall exactly as verb-spec step 3 does (keyed on the
   ticket text plus a short intent preamble, no `--record-pending`), and weave the
   prior-art entries in.

   **Re-run guard.** If the parent already carries a `flow-slice children: …`
   marker comment (from a prior mint), this ticket was already sliced. Surface the
   recorded children and their frontier, then STOP — do not re-map or double-mint.

3. **Map the blast radius, read-only.** Enumerate every call site, import edge,
   and definition the refactor touches — grep / Read, or an `Explore` agent for a
   wide sweep. Group the sites per package / directory / module. This grouping is
   the raw material for the migrate batches; a batch that cannot be named as a
   coherent set of sites is a batch that will not land green alone.

4. **Design the ladder.** Three rung kinds:
   - one **expand** child: introduce the new surface alongside the old. Purely
     additive — the new API, the new column, the compat shim — so it lands green
     with the old surface untouched and every existing call site still valid.
   - **migrate** children, batched per the step-3 grouping: each moves one batch of
     call sites from the old surface to the new. Independent because expand already
     shipped both surfaces; a batch touches only its own sites.
   - one **contract** child: delete the old surface once every site has moved. Green
     because nothing references the old surface any more.

   Dependency edges (all kind `blocks`, the default): each migrate is blocked by
   expand; contract is blocked by every migrate. Add a migrate→migrate edge only
   where a real code coupling forces one batch ahead of another. An edge means
   "the blocker must be merged first"; on beads it also gates `bd ready`.

5. **Green-independence gate — the refusal.** Every child must land green off
   `origin/main` alone, where a satisfied dep edge means the blocker is already
   merged. Refuse, and explain what would unlock slicing, when:
   - **old and new cannot coexist** — there is no additive expand seam: an in-place
     behavior change, a breaking wire-format flip, an exhaustive-enum change that
     breaks the moment the new variant appears. The unlock is a compat shim or a
     versioned surface landed first (which is itself a legitimate expand child — say
     so).
   - **the batches cannot be decoupled** — every site must change in one atomic
     commit or the tree is red between them. Then it is one PR, not a ladder.
   - **the blast radius fits one green PR** — that is ordinary `/flow spec`, not a
     slice. Slicing buys nothing and adds ceremony.

   No integration branches, no stacked-PR tooling, zero changes to the branching or
   safety machinery — the ladder is plain sibling tickets wired by dep edges.

6. **Present the ladder and gate.** Show it as a table: child (expand / migrate-N /
   contract), scope (the sites or surface it owns), dependency edges, and why it is
   independently green. This is the plan; `ExitPlanMode` is the one gate.

7. **Mint (post-approval only).** Write in this order so a partial mint is
   resumable; the parent marker (written before the edges) doubles as the re-run
   tripwire in step 2. One window is unguarded: a crash between the child creates
   and the marker leaves children with no tripwire. The created keys are in this
   session's transcript — recover by writing the marker by hand (the step-7.2
   comment) with the keys minted so far, then resume from where the mint stopped;
   never re-mint from scratch without first searching the tracker for the rung
   summaries.

   1. **Children first.** Per rung:
      ```bash
      python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . create \
        --summary "<rung summary>" --description "<scope + green rationale>" --type task
      ```
      Infer `--type` the way `verb-new.md` does (a task-like leaf under this
      workspace's type vocabulary). Capture each returned key.
   2. **Parent marker.** Record the child set on the parent for discoverability and
      the re-run guard:
      ```bash
      python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . comment \
        --key "$PARENT" --text "flow-slice children: <expand>, <migrate-1>, …, <contract>"
      ```
   3. **Edges.** `from-key` is the blocked/dependent child, `to-key` is the blocker
      that lands first:
      ```bash
      python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . link \
        --from-key "<migrate-1>" --to-key "<expand>"
      python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . link \
        --from-key "<contract>" --to-key "<migrate-1>"
      ```
      On a re-run these are resumable warnings, not failures: `bd dep add` tolerates
      a repeat, and a duplicate Jira `issueLink` may 400 — log and continue.
   4. **Covers persist (non-epic parent only).** So the contract run closes the
      parent umbrella through the existing covers fan-out (verb-spec step 6
      auto-derives `--covers` from the marker):
      ```bash
      python3 ${CLAUDE_SKILL_DIR}/scripts/group_persist.py persist \
        --lead "<contract>" --covers "$PARENT" --workspace-root .
      ```
      **Epic parent → skip this step and say so.** `_refuse_invalid_covers` rejects
      an epic cover; the epic stays the umbrella and its children close normally
      under it. The parent must stay OPEN either way — it is the umbrella the ladder
      lands under.

8. **Print the frontier and stop.** No worktree, no lease, no run state — `slice`
   only mints and wires. Emit:
   - the **expand key** and the suggested next command (`/flow <expand>`, or
     `/flow <expand> --auto`).
   - the parked ladder with its edges (the rungs behind expand).
   - a note that the parent stays open as the umbrella.

   On beads the edges make merge-gating mechanical: only expand is in `bd ready`,
   and each merge-and-close unblocks the next rung. On Jira the edges are
   informational, so the printed frontier command is the driving wheel.
