# Review packet — gate-2 in-run HTML review surface

Gate 1 (plan approval) renders the plan as a lavish-axi HTML surface (`references/verb-spec.md` step 4, the `## Lavish plan-review surface` block). This is the gate-2 analogue: when an interactive run reaches ready-to-review, render the change as a local interactive HTML review packet, loop on batched human feedback (fix commits + interdiff per round), and hand off to the forge PR only when the user ends the session. All binding discipline — the pinned npx, the TMPDIR heredoc HTML, the degradation contract, the additive-only boundary, forge-neutrality — is inherited from the plan surface. `references/verb-spec.md` step 4's boundary sentence sanctions this doc plus its two pointer sites (`references/stage-review_loop.md`, `references/verb-do.md`) as the gate-2 reference set; nothing here makes lavish a dependency.

**Attachment point.** This protocol is read from `references/stage-review_loop.md`'s packet-attachment section, at the tail of the review_loop stage: AFTER §5's terminal condition is met (CI green AND zero unresolved Major+ threads, the bot-gate satisfied per §3) and BEFORE `STATUS=completed` is recorded via `advance`. A revision sub-run (`<ticket-dir>` contains `/revisions/`) is excluded from THIS gate-2 packet — it attaches its own surface instead, the `## Revision triage board (/flow revise)` section below, opened from `references/verb-revise.md` step 5a rather than from this tail.

**Gate (mirrors the plan surface exactly).** Two legs; a failed gate skips the packet and delivers today (the skip line + the PR-link block), never blocking. Leg (a): an interactive run — NOT `--auto` (detected by session context, the same signal the PR-ready notification's `--auto` skip uses), NOT a revision sub-run, and review_loop must have actually run (its handler is wired, not `none`). Leg (b): the presence check, run with a real command as the first action, never a judgment call:
```bash
command -v node && command -v npx   # leg (b): both must resolve
```
`${CLAUDE_JOB_DIR}` (the backgrounded-job marker) is deliberately NOT part of the gate: a backgrounded-but-attended session still qualifies, because the human attaches through the harness cockpit (e.g. Claude Code's `claude agents`) — backgrounding does not mean nobody is watching.

**Data assembly (all local git + the forge seam; forge-neutral by construction).** Each source degrades independently when its handler is `none` or its `.out` is absent — the same per-section degrade shape `create_pr`'s `## Your call` / `## Evidence` sections use.
- `PR_URL` / `PR_ID` from `$TICKET_DIR/stages/create_pr.out` (review_loop's existing `## Inputs` read).
- The base branch from `forge_cli.py pr-info` (the normalized `base` field), feeding the full merge-base diff. Mirror `stage-review_loop.md` §3's capture-then-parse shape so a transient `pr-info` never dumps a traceback:
  ```bash
  BASE=$(python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . pr-info --pr "$PR_ID" 2>/dev/null \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("base","") if d else "")' 2>/dev/null)
  git diff "$(git merge-base "origin/$BASE" HEAD)"..HEAD
  ```
  An empty `$BASE` (a failed `pr-info`) is a degradation trigger like any other failure — the merge-base substitution would error and the diff silently collapse to empty, so never render a packet around a silently-empty core diff; take the skip line instead.
- Chapters from `plan.out` (per-file rationale + plan steps); adopt / dismiss / discuss triage items from `code_review.out`'s taxonomy sentinel sections (`flow:code_review-taxonomy v1`); evidence from `e2e.out`'s `flow:e2e-evidence` sentinel; the CI chip from `forge_cli.py ci-rollup --pr "$PR_ID"`; the bot-loop summary from review_loop's own `stage-review_loop.md` §1-§4 results.
- `stage-review_loop.md` §3's not-reviewed caveat ("proceeding on CI-green only — automated review did not happen") renders as a packet banner when present.

**Authoring.** The branch / PR is ground truth; the HTML is a disposable render at `${TMPDIR:-/tmp}/flow-lavish-$KEY/review.html`, authored via a Bash heredoc (`cat > "$TMPDIR/..." <<'HTML' ... HTML`), regenerated per round, NEVER edited as source. Pinned `npx -y lavish-axi@0.1.35` for open / `poll` / `end` — the same version-pin the plan surface uses, closing the npx supply-chain exposure the unpinned `/lavish` skill leaves open. Open the `code` (diff rendering) and `input` (triage controls) playbooks via `npx -y lavish-axi@0.1.35 playbook <id>` BEFORE authoring the HTML. Design source follows lavish's documented priority, never hand-rolled ad-hoc CSS: the user-requested look first, else the subject project's design system, else the `npx -y lavish-axi@0.1.35 design` DaisyUI fallback.

Independent of which design source you pick, MANDATORY in every authored artifact: paste lavish's layout-safety CSS snippet verbatim into the HTML `<head>` (the `layout_safety_snippet` that `npx -y lavish-axi@0.1.35 design` prints). `lavish-axi design` frames it as optional; for flow's dense authored surfaces — diffs, badges, code, tables, the overflow-prone case — it is REQUIRED. DaisyUI's `.label` does NOT wrap long text by default; the snippet's `overflow-wrap: anywhere` set already INCLUDES `.label`, so mandating it verbatim IS the fix (a verdict-form helper line went 1113px wide at an 833px viewport and kept lavish's open-time curtain up across opens — flow-qdal). Verbatim, for the pinned 0.1.35:
```
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

- Chapter the walkthrough by PLAN STEP — core change first, consequences next, glue last — never repo file order. Per-chapter reviewed checkmarks.
- Plan-step→hunk traceability, with a flag on any hunk that traces to no plan step (an orphan hunk).
- Render each piece of evidence INSIDE the chapter whose claim it supports (e2e output, the CI chip), not in a trailing appendix.
- Render the `code_review.out` ask-user items as `input`-playbook adopt / dismiss / discuss triage controls, not flattened PR-body bullets.

**The loop.** Open the packet, fire the PR-ready notification at packet-open, then poll.

The PR-ready notification fires exactly once per run. On a packet-gated run it fires at packet-open — this satisfies the do-loop step-e firing point (no duplicate ping), and the packet loop then runs inside `review_loop`'s tail (see `references/review-packet.md`). On a gate-failed run (the packet never opens) it fires at step e exactly as today. The packet never attaches at the `create_pr` fallback firing point (`review_loop` handler `none` → skip line, no packet).

Run ONE persistent poll as a background task for the whole loop: lavish watches the artifact file and live-reloads a re-render in place (scroll preserved), so NEVER kill / re-arm the poll or re-run the open command around a re-render — killing it shows the user "no agent listening". Strip the redundant `dom_snapshot` from every poll read exactly as the plan surface does (`references/verb-spec.md` step 4's poll bullet): pipe each poll invocation through `| python3 -c 'import sys; sys.stdout.writelines(l for l in sys.stdin if not l.startswith("dom_snapshot:"))'` so the ~19KB/turn snapshot never enters context (flow-xypg). Annotations batch into ONE send — a review round costs implement-verify-commit-push, so never fire per-comment. On send, record the round SHA:
```bash
ROUND_SHA=$(git rev-parse HEAD)
```
Then apply the batch as ONE fix round via `references/stage-review_loop.md` §2's delegated-fix recipe verbatim — a `subagent:general-purpose` spawn pinned with `model_resolve.py --stage review_loop`, the existing commit machinery, `git push` — followed by a bounded `references/stage-review_loop.md` §1-style CI re-probe (an advisory refresh). Pin the fix subagent the same way §2 does:
```bash
M=$(python3 ${CLAUDE_SKILL_DIR}/scripts/model_resolve.py --workspace-root . --ticket "$KEY" --stage review_loop)
```
Then re-render, showing only the interdiff since the last reviewed round:
```bash
git diff "$ROUND_SHA"..HEAD
```
The interdiff is LOCAL git only — never a forge review-round API (Bitbucket has none). Adopted triage items join the round; dismissed ones are recorded; a `discuss` rides `poll --agent-reply`. Human-requested rounds are EXEMPT from `references/stage-review_loop.md` §2's 3-fix-cycle cap — a present human is the judgment the cap substitutes for, and the cap bounds unattended loops (§2's cap line carries the matching carve-out). An out-of-set fix follows the existing widening reconcile (`ticket_frontmatter.py update` + re-record the baseline, `references/verb-do.md`'s post-implement reconcile). A grouped / covers run needs nothing special: one PR, one review_loop, one packet — the covers fan-out stays create_pr-anchored.

**Lease heartbeat.** Before each render, on every poll return, and whenever control returns to the orchestrator, re-issue and discard the descriptor:
```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/dispatch_stage.py next \
  --workspace-root . --ticket "$KEY" --session-nonce "$NONCE"
```
`next` refreshes the lease and re-verifies the snapshot (SKILL.md step-a semantics; `pick_next_pending` returns the in_progress review_loop again, never skips ahead). Exit 1 / 7 routes to the standard `/flow recover` path. Documented residual: an IDLE long-poll returns nothing, so a review idle past ~120 min expires the lease before any heartbeat fires — refresh-past-expiry is legal for the owner (`lease.assert_lease_still_mine` deliberately skips the expiry check) and re-entry is idempotent, but on the self-target repo the expired-lease + green-PR window is briefly reapable.

**Verdict — convergence is the USER's built-in end-session signal, not an agent decision, never a competing control.** Saving and closing IS the approval: there is NO queued approve control — no `input`-playbook approve question, no custom in-page approve button. The user's built-in **Send & end session** submits the final feedback batch (if any — queued triage dispositions and/or change requests) + user-ended attribution together (the CLI's designed convergence signal), so the final poll batch carries `status: ended`. A change request is ordinary batched feedback sent mid-session via **Send to Agent**, which keeps the loop alive (another fix round, re-render, poll again); only the end is terminal. On the ended batch:
- **the ended batch carries no unresolved change request** (only dismissed triage items, or nothing queued) → THIS end IS the approval → capability-gated `forge_cli.py mark-ready` (the `ready_toggle` capability; a `{"supported": false}` return renders as advisory text — the user flips the draft manually):
  ```bash
  python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . mark-ready --pr "$PR_ID"
  ```
  If the last round's CI is not green at approve time, surface that one line in-thread BEFORE mark-ready. Then `STATUS=completed`, the round log into `review_loop.out`, `advance` — the normal step-e / step-5 delivery (the PR-link block, `references/verb-do.md`) runs unchanged. Merge stays human on the forge.
- **the ended batch carries an unresolved change request AND `status: ended`** (a queued change request, or an adopted triage item — the user asked for changes and left) → apply that batch as one last fix round, push, then deliver the interdiff summary in-thread with the PR link — no re-render, no reopen, NO `mark-ready`. `STATUS=completed`, today's delivery.

A genuinely unusable end signal — a malformed / unparseable ended batch, or lavish degraded at the moment of end — is a lavish failure, not a verdict: it falls through to the degradation contract below (`Lavish: degraded mid-loop — <reason>`, today's delivery, NO `mark-ready`), never presumed to be either verdict.

A user-initiated end is terminal on every branch — never reopen without `--reopen` and an explicit ask; deliver everything remaining in-thread. Agent-side `npx -y lavish-axi@0.1.35 end` is used ONLY when the AGENT terminates the loop (a mid-loop degradation).

**Degradation contract.** ANY failure at ANY point — npx absent, offline, a non-zero lavish-axi exit, a heredoc refusal — falls back to today's delivery plus exactly one visible line, never silent, never blocking, and never a friction entry (parity with the plan surface). Before the packet opens: `Lavish: skipped — <reason>`, and the PR-ready notification fires at do-loop step e exactly as today. After the packet has opened (the notification already fired at packet-open): `Lavish: degraded mid-loop — <reason>` plus the PR-link block, with no second notification. The packet is an ADD-ON: no verb, stage, or script may require it, and lavish-absent IS today's delivery plus that one line.

**Curtain / live-reload fragility (lavish-axi 0.1.35).** Observed in flow-qdal: a wide horizontal overflow can keep lavish's open-time curtain (loading overlay) up across opens (the user sees "nothing loads"), and live-reload churn can kill the iframe SDK send path (**Send & end session** triggers nothing). The mandated layout-safety snippet above is the primary prevention; if a surface still hangs, recover by restarting the lavish server and re-opening with `--no-gate`: `npx -y lavish-axi@0.1.35 --no-gate <html>`. A degradation-recovery path, not a normal step.

## Revision triage board (/flow revise)

Opened from `references/verb-revise.md` step 5a — NOT from the §6 tail attachment above (a revision sub-run stays excluded from the gate-2 packet). Inherited verbatim from the packet above: the pinned `npx -y lavish-axi@0.1.35` for open / `poll` / `end`, the TMPDIR heredoc authoring (here at `${TMPDIR:-/tmp}/flow-lavish-$KEY/revise.html`), the layout-safety snippet pasted into `<head>`, the ONE persistent poll (live-reload in place, never killed / re-armed), batch-one-send, the degradation contract, and the additive-only boundary. Only the deltas below are new; every discipline not restated here is inherited by reference.

**Board content.** Every unresolved thread renders as a triage card — `id` / `file` / `line` / `severity` / `title` / `body` / `author` — against the merge-base diff (the `pr-info` recipe above). The `input`-playbook controls are per-thread: fix now / defer / dismiss, with a reason REQUIRED for defer and dismiss (a `fix` needs none). A thread whose anchor cannot be pinned to the current diff (`file: null` / `line: null`, or a stale anchor) renders in a visible **Unanchored threads** section — never silently dropped. An instruction-driven revise with zero unresolved threads still opens the board (verb-revise step 5a skips only when both are absent): the card list is empty and the board serves as the interdiff + convergence surface for the instruction's fix rounds.

**Durable artifact.** Each triage batch persists to `$REVISION_DIR/dispositions.json`, written whole per batch with the step-4 `printf '%s\n'` precedent (the same durable-source pattern verb-revise step 4 uses for `instruction.md`). `$REVISION_DIR` IS the revision sub-run's `<ticket-dir>`, so the consuming stages read this same file as `<ticket-dir>/dispositions.json`. One JSON object:

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

Field contract:
- `version` — int, schema version, starts at 1.
- `pr_id` — str, the `--pr` id the threads were fetched with.
- `round` — int, 1-based triage round that produced this file.
- `round_sha` — str (40-hex), `git rev-parse HEAD` at persist time, i.e. before this batch's fixes land; the base for the re-render after those fixes (`git diff "$ROUND_SHA"..HEAD`).
- `generated_at` — str, ISO-8601 UTC.
- `threads[]` — every triaged thread, full self-contained snapshot: the nine keys of forge.py's `ReviewThread` TypedDict, verbatim — `id` str, `file` str|null, `line` int|null, `severity` one of `"critical"|"major"|"minor"|"nit"|"unknown"` (forge.py's `THREAD_SEVERITY`; stored RAW, pre-floor — dispositions supersede the floor so the bump is irrelevant here), `title` str, `body` str, `resolved` bool, `author` str, `parent_id` str|null — plus two disposition keys: `disposition` enum `"fix"|"defer"|"dismiss"`, and `reason` str (MUST be non-empty for defer/dismiss; `""` allowed for fix).
- Fix pile (the one definition): `threads[]` entries with `"disposition": "fix"`. stage-implement source #1 reads it as the work list; stage-review_loop's supersede branch computes the Major+-replacement fix set and the §5 terminal check from it. An explicit empty triage — file exists, fix pile empty (all defer/dismiss, or `"threads": []`) — supersedes the floor: no floor-bumped thread enters the fix set, so the terminal check has nothing left to chase. Termination timing stays with the convergence rules below — a mid-session all-defer/dismiss batch (**Send to Agent**) keeps the loop alive; only a user-ended session closes it. No file → floor applies (the empty-vs-absent distinction).
- Unanchored threads (`file: null` / `line: null` / unpinnable anchor) live in the SAME array; the board renders them in a visible section; the schema needs no split.

**Rounds.** Round 1: the queued fix dispositions seed the fix-only stage subset — WAIT for the first poll return before dispatching, so the first batch is in hand before implement runs. Rounds 2+: at review_loop's revision tail — each mid-session batch applied as ONE fix round via `references/stage-review_loop.md` §2's delegated-fix recipe verbatim (the fix subagent pinned with `model_resolve.py --stage review_loop`; human rounds cap-exempt per §2's carve-out), followed by a bounded CI re-probe, then a re-render showing only the interdiff `git diff "$ROUND_SHA"..HEAD` — LOCAL git only, never a forge review-round API.

**Lease heartbeat (two regimes).** Inside the review_loop tail (rounds 2+): the packet's heartbeat block above applies, with `--revision "$REV_ID"` appended to the `next` call — review_loop is in_progress there, so `next` is state-idempotent (`pick_next_pending` resumes the in_progress stage rather than beginning a new one). During the step-5a board wait (before the do-loop): do NOT heartbeat — a `next` on an all-pending sub-run begins the first pending stage (implement) before any triage exists. Documented residual: the `revise-open` lease carries a 10-min init TTL, and a long triage can outlive it; refresh-past-expiry is legal for the owner (`lease.assert_lease_still_mine` skips the expiry check) and the do-loop's first real `next --revision` re-covers it — the same residual class the packet's heartbeat block documents.

**Convergence (post-gpo7, inherited).** The user's built-in end-session signal is the verdict, keyed on the same discriminator the packet uses. **Send to Agent** mid-session = a batched disposition set, loop alive (another fix round, re-render, poll again). **Send & end session** is terminal, and its batch is read for an unresolved change request:
- the ended batch carries an unresolved change request (any queued fix disposition) → apply that batch as one last fix round, push, post the replies / resolves, deliver the interdiff summary in-thread with the PR link — no re-render, no reopen, NO `mark-ready`; `STATUS=completed`, today's step-7 delivery.
- the ended batch carries none (only defer / dismiss, or nothing queued) → THIS end IS convergence: persist the (possibly empty) disposition set — an explicit empty triage supersedes the floor — then `STATUS=completed` and today's step-7 delivery; capability-gated `forge_cli.py mark-ready --pr "$PR_ID"` only when `pr-info` still reports `draft: true` (a non-draft PR is already promoted; a `{"supported": false}` return renders as advisory text). Merge stays human on the forge.
- a malformed / unparseable ended batch, or lavish degraded at the moment of end → the degradation contract (`Lavish: degraded mid-loop — <reason>`, today's delivery, NO `mark-ready`), never a guessed verdict.

**Audit trail.** A fixed thread → `post-reply` ("Fixed in $FIX_SHA. <one line>") + host-verified `resolve-thread` (the bkt adapter re-reads `.resolution != null`); a deferred or dismissed thread → `post-reply` carrying the human's reason, thread stays open. Reply-posting is independent of fix-pile emptiness — an all-defer/dismiss batch still posts every reason. The single-fire rule in `references/verb-do.md` is untouched: no verb-do edit rides this section.
