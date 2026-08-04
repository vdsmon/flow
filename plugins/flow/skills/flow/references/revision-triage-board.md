# Revision triage board

The revision board is the disposition step of an attended same-PR revision sub-run:
the human decides which unresolved comments to fix now, defer, or dismiss and why.
It runs natively in the driver conversation through the host's input capability
(Claude Code's native question surface; Codex's plain question and wait). It is not
the ordinary review companion, not a second Forge diff, and not part of the original
run's `review_loop` tail.

This document is self-contained. It does not inherit behavior from the retired
ordinary review packet.

## Gate and fallback

Run triage only in attended revision mode, after `revise-open` created the
revision directory and before its first stage is dispatched. If no disposition set
is produced (the human is absent, declines to triage, or the revision is
unattended), fall back to the stage-review-loop severity floor and say why. A
missing disposition set never blocks the revision and never changes the target PR.

## Fetch and present

Fetch the open PR's unresolved threads through the forge seam. Capture the command's
exit code before parsing; a forge error is not an empty thread list. Read the current
merge-base diff with local Git for anchoring and interdiffs.

Present every unresolved thread as one triage item carrying `id`, `file`, `line`,
`severity`, `title`, `body`, and `author`, and collect a per-thread disposition of
`fix`, `defer`, or `dismiss`; defer and dismiss require a reason. A thread with
`file: null`, `line: null`, or a stale anchor is presented in a visible
**Unanchored threads** group. Never silently drop it. An instruction-driven revision
with no unresolved threads has nothing to triage: the persisted instruction is the
fix source and triage records an explicit empty set only if the human asks for one.

## Durable disposition artifact

Persist every triage batch as `$REVISION_DIR/dispositions.json`, written atomically as
one complete object. `$REVISION_DIR` is the revision sub-run's `<ticket-dir>`, so the
implement and review-loop stages read the same file directly.

```json
{
  "version": 1,
  "pr_id": "325",
  "round": 1,
  "round_sha": "4f2c9e1a0b3d5f6a7c8e9d0b1a2c3d4e5f6a7b8c",
  "generated_at": "2026-07-10T14:03:22Z",
  "threads": [
    {
      "id": "PRRT_kwDOabc123",
      "file": "src/query.py",
      "line": 118,
      "severity": "major",
      "title": "N+1 query in loop",
      "body": "This re-queries per row; batch it.",
      "resolved": false,
      "author": "coderabbitai",
      "parent_id": null,
      "disposition": "fix",
      "reason": ""
    }
  ]
}
```

Contract:

- `version` is integer `1`; `pr_id` is the forge handle as a string.
- `round` is 1-based. `round_sha` is the full 40-hex `HEAD` before that batch's
  fixes. `generated_at` is ISO-8601 UTC.
- Each thread preserves all nine normalized Forge thread fields. `severity` is
  `critical|major|minor|nit|unknown`; `file`, `line`, and `parent_id` may be null.
- `disposition` is `fix|defer|dismiss`. `reason` is non-empty for defer/dismiss and
  may be empty for fix.
- The **fix pile** is exactly the entries whose disposition is `fix`. File present
  with an empty fix pile is authoritative and supersedes severity inference. File
  absent means the stage-review-loop floor applies.
- Unanchored threads remain in the same array; presentation, not storage, separates
  them.

## Rounds and audit trail

Collect round-one dispositions before dispatching the revision's implement stage so
they exist before work begins. One answered triage batch is one fix round. Record
`ROUND_SHA=$(git rev-parse HEAD)`, persist the batch, apply its fix pile,
verify, commit, and push. The review loop then re-greens CI, replies to fixed threads,
and resolves them through the forge seam only after verification. Deferred/dismissed
threads receive the recorded reason as a reply and stay open.

Rounds after the first use the review-loop delegated-fix recipe. Human-requested
rounds are exempt from its unattended three-cycle cap. Present only the local Git
interdiff `git diff "$ROUND_SHA"..HEAD` between rounds; never depend on a Forge
review-round API. Out-of-set changes still run the normal widening reconcile.

## Lease heartbeat

Before the do-loop begins, do not call dispatcher `next` merely as a heartbeat: an
all-pending revision would begin implement before dispositions exist. The
`revise-open` lease may outlive its initial TTL; refresh-past-expiry is legal for the
same lease holder and the first real `next --revision` re-covers it.

Once a revision stage is in progress, refresh after every answered triage batch and
whenever control returns to the driver:

```bash
FLOW_HARNESS="<harness>" "<facade>" dispatch next \
  --workspace-root . --ticket "$KEY" --revision "$REV_ID" \
  --session-nonce "$NONCE"
```

Discard the resumed descriptor. Exit 1/7 routes to normal workspace repair.

## Convergence

The human's explicit word in the conversation is the verdict; there is no separate
approve control. A mid-revision batch persists its dispositions and keeps triage
open for further rounds. When the human declares triage complete:

- If the final batch contains a `fix`, apply it as one last round, push, post the
  audit replies/resolutions, and deliver the interdiff in-thread.
- If it contains only defer/dismiss or nothing, persist the set and continue the
  revision. An explicit empty set remains authoritative.
- An ambiguous closing answer is a question to resolve, never guessed approval.

Mark the PR ready through the capability-gated forge command only when the final
batch has no fix and `detect-pr --branch <pr-branch>` still reports `draft`. Merge remains human on Forge.
A completed triage is terminal: never reopen without an explicit request.
