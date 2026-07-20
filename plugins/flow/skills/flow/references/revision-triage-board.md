# Revision triage board (Lavish planning surface)

The revision board is the one review-adjacent use of Lavish that remains. It is a
planning and disposition surface opened before a same-PR revision sub-run: the human
decides which unresolved comments to fix now, defer, or dismiss and why. It is not
the ordinary review companion, not a second Forge diff, and not part of the original
run's `review_loop` tail.

This document is self-contained. It does not inherit behavior from the retired
ordinary review packet.

## Gate and degradation

Open the board only in attended revision mode, after `revise-open` created the
revision directory and before its first stage is dispatched. Run the presence check
as the first board action:

```bash
npx -y lavish-axi@0.1.35 --help >/dev/null
```

Use the pinned version for every `open`, `poll`, and `end` operation in the session.
If the check or any later Lavish action fails, say
`Lavish revision triage: skipped — <reason>` or
`Lavish revision triage: degraded mid-loop — <reason>`, then fall back to the
stage-review-loop severity floor. A board failure never blocks the revision, changes
the target PR, or becomes a friction event merely because Lavish is unavailable.

## Author and open

Fetch the open PR's unresolved threads through the forge seam. Capture the command's
exit code before parsing; a forge error is not an empty thread list. Read the current
merge-base diff with local Git for anchoring and interdiffs.

Author `${TMPDIR:-/tmp}/flow-lavish-$KEY/revise.html` with every unresolved thread as
a card containing `id`, `file`, `line`, `severity`, `title`, `body`, and `author`.
Render per-thread `input`-playbook controls for `fix`, `defer`, or `dismiss`; defer and
dismiss require a reason. A thread with `file: null`, `line: null`, or a stale anchor
goes into a visible **Unanchored threads** section. Never silently drop it. An
instruction-driven revision with no threads may still open the board as an interdiff
and convergence surface.

Paste this layout net verbatim into `<head>`; the board contains dense authored text
and must remain sendable at narrow widths:

```html
<style>
  *, *::before, *::after { box-sizing: border-box; }
  :where(.grid, .flex, .layout-grid, .layout-flex) > *,
  :where([style*="display: grid"], [style*="display:grid"], [style*="display: flex"], [style*="display:flex"]) > * {
    min-width: 0;
  }
  :where(p, h1, h2, h3, h4, h5, h6, li, dd, blockquote, figcaption, td, th, .badge, .label) {
    overflow-wrap: anywhere;
  }
  :where(img, svg, video, canvas, iframe) {
    max-width: 100%;
    height: auto;
  }
</style>
```

Open once, then run one persistent poll owned by the orchestration session for the
whole board. Claude Code may use a background task; Codex keeps the long-running
command session and waits/polls it. Do not delegate continuation to a child agent.
Lavish live-reloads the same file, so do not kill/re-arm the poll on every render.
Strip `dom_snapshot` from poll reads and batch annotations into one send.

If the open-time curtain hangs or the iframe send path dies, restart the Lavish
server and reopen with `--no-gate` as a degradation-recovery path, not the default.

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

Wait for the first poll return before dispatching the revision's implement stage so
round-one dispositions exist before work begins. A mid-session batch is one fix
round. Record `ROUND_SHA=$(git rev-parse HEAD)`, persist the batch, apply its fix pile,
verify, commit, and push. The review loop then re-greens CI, replies to fixed threads,
and resolves them through the forge seam only after verification. Deferred/dismissed
threads receive the recorded reason as a reply and stay open.

Rounds after the first use the review-loop delegated-fix recipe. Human-requested
rounds are exempt from its unattended three-cycle cap. Re-render only the local Git
interdiff `git diff "$ROUND_SHA"..HEAD`; never depend on a Forge review-round API.
Out-of-set changes still run the normal widening reconcile.

## Lease heartbeat

Before the do-loop begins, do not call dispatcher `next` merely as a heartbeat: an
all-pending revision would begin implement before dispositions exist. The
`revise-open` lease may outlive its initial TTL; refresh-past-expiry is legal for the
same lease holder and the first real `next --revision` re-covers it.

Once a revision stage is in progress, refresh on every poll return and whenever
control returns to the driver:

```bash
FLOW_HARNESS="<harness>" "<facade>" dispatch next \
  --workspace-root . --ticket "$KEY" --revision "$REV_ID" \
  --session-nonce "$NONCE"
```

Discard the resumed descriptor. Exit 1/7 routes to normal workspace repair.

## Convergence

Lavish's built-in end-session signal is the verdict; there is no custom approve
button. **Send to Agent** persists a mid-session batch and keeps the board alive.
**Send & end session** is terminal:

- If its batch contains a `fix`, apply it as one last round, push, post the audit
  replies/resolutions, and deliver the interdiff in-thread. Do not re-render/reopen.
- If it contains only defer/dismiss or nothing, persist the set and continue the
  revision. An explicit empty set remains authoritative.
- A malformed ended batch is degradation, never guessed approval.

Mark the PR ready through the capability-gated forge command only when the ended
batch has no fix and `pr-info` still reports a draft. Merge remains human on Forge.
A user-ended session is terminal: never reopen without an explicit request.
