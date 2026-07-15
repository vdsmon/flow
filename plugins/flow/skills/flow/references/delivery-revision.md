# Same-PR revision delivery

A lifecycle `revise` action updates a delivered run's open PR. The terminal base run
is immutable. Revision state lives under
`.flow/runs/<ticket>/revisions/<revision-id>/` with its own lease, snapshot, state, and
fix-only stages. It always pushes the existing branch and never creates another PR.

## Resolve and guard

Resolve `pr:<number>` and forge URLs through the forge seam. A ticket target resolves
its Flow worktree/branch, then detects that branch's PR. Derive the ticket from the
head branch and verify the binding to the terminal base run.

Refuse when the PR is missing, merged, closed, or belongs to no Flow ticket. A request
against terminal delivery needs a new ticket. If the base run is not terminal, return
to its ordinary lifecycle rather than opening a revision.

## Open the sub-run

```bash
FLOW_HARNESS="<harness>" "<facade>" dispatch revise-open --workspace-root . --ticket "<ticket>"
```

Capture `rev_id`, `run_id`, `session_nonce`, `revision_dir`, and the returned stage
subset. Only one revision may be live. Persist public `--request` text as the exact
instruction artifact using the host's safe writer. Without an instruction, unresolved
actionable review threads are the fix source.

Locate or re-materialize the existing feature worktree from the PR branch, then bind
the absolute `run_root` and runtime facade. Report reseeding; never use the base
checkout for revision edits.

## Feedback disposition

In attended mode, fetch unresolved review threads through the forge seam and present
one disposition set: fix now, defer, or dismiss, with reasons. Persist the whole set
atomically under the revision directory. If the interactive board capability is
unavailable, fall back to the stage protocol's severity floor and say why. A board
failure never changes which PR is being updated. The complete board mechanics and
artifact schema live in `references/revision-triage-board.md`.

## Execute

Drive `delivery-loop.md` with one addition: pass the revision id on every dispatcher
`next`, `advance`, and `release`. The fix-only subset normally covers implementation,
review, e2e, commit, review-loop, review-brief regeneration, and reflection stages.
PR creation is absent.

Implementation and review consume the persisted instruction/dispositions or forge
threads as their fix set. A revision's fixes route through the `revision_fixer` capsule
writer (`references/stage-review_loop.md` §2): it applies the instruction inside a private
capsule and Flow compare-and-swap imports its patch under a sole-writer claim, keeping a
human-requested revision distinct from ordinary pipeline remediation (`review_fixer`). The
same baseline, artifact, friction, snapshot, lost-lease, and rooted-execution rules apply.
Resolve addressed threads through the forge seam only after their fixes are verified.

Release the revision lease on every post-open exit. Surface the updated existing PR
as the final link. Preserve the terminal base receipt and earlier revision receipts.
