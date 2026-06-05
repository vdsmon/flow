# triage verb

`/flow triage [<key> "<answer>"]`. Surfaces the deferred queue and reopens one
with an answer. Routed from SKILL.md's argument table. Deferred is a beads
concept; on a non-beads backend the list step prints "nothing to triage".

## List (no positional)

1. Run:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/triage.py --workspace-root .
   ```
   Lists every `deferred` bead (whole queue, unscoped) with each one's last
   "could not self-approve" open-question comment inline. Add `--json` for a
   machine consumer; default is the human table.

2. Handle the exit:
   - Exit 0 → surface the table verbatim.
   - Exit 1 → workspace not initialized; surface stderr + the `/flow init` hint; stop.
   - Exit 2 → workspace config error; surface stderr; stop.

## Reopen (`<key>` + answer text)

The decision stays human; this step automates the reopen mechanics only, over
the existing `tracker_cli` seams. Comment FIRST (mirroring the defer recipe
order), so a failed transition still leaves the recorded answer:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . \
  comment --key <KEY> --text "<answer>"
python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . \
  transition --key <KEY> --to-state open
```

Then print the hint: re-run the ticket WITHOUT `--auto` to plan interactively
(an `--auto` retry would re-defer on the same question).

Note: the defer-comment pick is coupled to verb-spec.md's wording
(`flow --auto could not self-approve`). If that stem changes, triage degrades to
showing the last comment overall.
