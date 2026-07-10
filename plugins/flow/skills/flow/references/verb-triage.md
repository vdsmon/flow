# triage verb

`/flow triage [<key> "<answer>"]`. Surfaces the deferred queue and reopens one
with an answer. Routed from SKILL.md's argument table. Deferred is a beads
concept; on a non-beads backend the list step prints "nothing to triage".

## List (no positional)

1. Run:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/triage.py list --workspace-root .
   ```
   Lists every `deferred` bead PLUS every `blocked` bead carrying the defer stem
   (decided-mode hot blocks — a `--auto` run that hit a post-decision
   implementation wall on a hot change), each with a `status` column and its last
   "could not self-approve" open-question comment inline. Every row also carries
   a `QUEUE` column: `evolve` when the bead has the `evolve` label, else
   `day-job`. Add `--ready` to also surface the ready queues (one extra
   `bd ready` call, partitioned by the same label; ready rows have no
   open-question comment) — the default output without it is unchanged. A bare
   `triage.py --workspace-root .` still works (defaults to `list`). Add `--json`
   for a machine consumer; default is the human table.

2. Handle the exit:
   - Exit 0 → surface the table verbatim.
   - Exit 1 → workspace not initialized; surface stderr + the `/flow init` hint; stop.
   - Exit 2 → workspace config error; surface stderr; stop.

## Reopen (`<key>` + answer text)

The decision stays human; this step automates the reopen mechanics only, over
the existing `tracker_cli` seams. Comment FIRST (mirroring the defer recipe
order), so a failed transition still leaves the recorded answer. The answer
comment carries the stable stem `TRIAGE-DECISION:` so a later `--auto` relaunch
detects it as a recorded decision (decided mode) and does not re-defer on the
answered question:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . \
  comment --key <KEY> --text "TRIAGE-DECISION: <answer>"
python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . \
  transition --key <KEY> --to-state open
bd update <KEY> --remove-label hitl
```

The `bd update <KEY> --remove-label hitl` clears the human-in-the-loop mark so
the reopened bead is auto-pickable again — the recorded `TRIAGE-DECISION:` IS the
live-exchange input the label demanded, so the ticket is AFK once more. It is a
no-op on a bead that was never marked (a plain triage answer on an unlabelled
bead), so it is safe to run unconditionally. Beads instance; on Jira the
maintainer removes the label by hand.

This works identically for a `blocked` bead (a decided-mode hot block): comment
the answer + transition to open, same as a deferred one.

Then print the hint: re-run the ticket WITHOUT `--auto` to plan interactively.
(An `--auto` retry now ingests the `TRIAGE-DECISION:` answer as authoritative
rather than re-deferring; a hot change still blocks on a residual
implementation wall UNLESS `[evolve] adjudicate_hot` is on (then it proceeds,
merge-time-guard-gated), a clean one proceeds — see verb-spec.md's decided-mode
branch.)

Note: the already-reopened beads carry legacy `DECISION:` comments; detection
accepts that stem too, so no backfill is needed.

## Advisor-minted decisions (`advisor_adjudicates`, default on)

By default an `--auto` run RULES on a judgment fork itself instead of deferring
(see verb-spec.md step 5, the advisor-adjudication branch); set
`[evolve] advisor_adjudicates = false` to opt out and restore defer-on-fork. The
two outcomes a maintainer sees here:

- A `proceed` ruling writes a `DECISION: (advisor) <ruling>` comment and ships.
  The `DECISION:` stem means a relaunch reads it as already-decided (no re-ask);
  the `(advisor)` marker is informational. `triage.py list` tags any surfaced row
  whose open-question carries `(advisor)` so an advisor ruling is distinguishable
  from a human `TRIAGE-DECISION:` at a glance. A proceed may carry mandatory
  in-run commitments — closeable holes (a missing test, an unmapped error, an
  undocumented-but-correct behavior change) the run closes itself instead of
  parking (maintainer policy, flow-5fp), recorded in the `DECISION:` comment.
  These beads are usually already
  in-flight or shipped, so they appear in the deferred/blocked queue only if a
  later wall lands them there.
- A `block` ruling (rulable, but unsafe to auto-ship — broad blast radius,
  irreversibility, or hot) does NOT write a `DECISION:` comment. It uses the
  ordinary defer-stem (`flow --auto could not self-approve: advisor ruled … but
  blocked auto-ship …`) + status `blocked`, so it surfaces in this list exactly
  like any other hot block, and the reopen flow above applies unchanged. Writing
  a `DECISION:` for a block would let a relaunch re-proceed a non-hot block,
  defeating it — so block deliberately reuses the defer-stem, not a decision.
  The bar is raised (flow-5fp): a closeable hole is never block-grounds — block
  is reserved for uncloseable/unsafe walls (user-only information, true
  irreversibility, broad blast, hot).

Note: the defer-comment pick is coupled to verb-spec.md's wording
(`flow --auto could not self-approve`). If that stem changes, triage degrades to
showing the last comment overall.

## Lifting the hot floor (`adjudicate_hot`, default off)

`[evolve] adjudicate_hot` (default off, maintainer self-target) lifts the hot
hard-floor so hot changes auto-adjudicate like non-hot ones: an advisor `proceed`
on a hot change ships instead of being downgraded to a block, the
flow_worktree bootstrap stops refusing a hot change with no recorded decision,
and the decided short-circuit stops blocking — a hot, already-decided bead
proceeds (merge-time-guard-gated) on relaunch instead of re-blocking on the
residual implementation wall.
The merge-time guard-property review + CI are the retained gate. Read the flag via
`python3 ${CLAUDE_SKILL_DIR}/scripts/triage.py adjudicate-hot-enabled --workspace-root .`.
