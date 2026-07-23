# Plan surface (Lavish planning surface)

The plan surface renders the human gate of `references/delivery-plan.md` section 5 as an
interactive Lavish session: the exact complete plan plus the frozen assessment evidence,
iterated between the human and the driver, converged by Lavish's built-in end-session signal
before explicit approval. When the gate below passes, this surface is the DEFAULT presentation;
plain prose is the fallback for a failed gate, never a coequal choice. "The user probably
prefers prose" is not part of the gate, and skipping on a passing gate is a defect. Under an
earlier optional phrasing, attended planning runs rendered plain prose every time with nothing
in the transcript showing the gate was evaluated; the mandatory wording exists to close exactly
that hole.

This document is self-contained. The review-adjacent Lavish use lives in
`references/revision-triage-board.md`; the review brief remains a static HTML artifact with no
Lavish session involved.

## Gate and degradation

Open the surface only at the section 5 human gate of an attended planning conversation, after
the confidence gate holds (unrounded weighted score at least 90.0, zero blockers) and after the
section 4 base recheck. A fresh unattended invocation stops before the gate, so attendance is
structural, not probed. Run the presence check as the first surface action:

```bash
npx -y lavish-axi@latest --help >/dev/null
```

Run `@latest` for every `open`, `poll`, and `end` operation. Version-coupled details must be
re-verified when behavior shifts: the single-line `dom_snapshot` poll rendering the strip below
depends on (the strip fails open, so a format change shows up as sudden poll bulk, not an
error), the layout-safety snippet, and the user-settings Bash allow rule, which must match the
unpinned form. The revision board (`references/revision-triage-board.md`) keeps its own pinned
version policy.

If the presence check or any later Lavish action fails at any point, say
`Lavish plan surface: skipped — <reason>` or
`Lavish plan surface: degraded mid-loop — <reason>`, then fall back to the plain-prose section 5
presentation and proceed. The line is an announcement, never a prompt for permission. The
surface is an add-on, never a dependency: no stage, command, or script may require it, and a
failed surface never blocks planning, changes the plan, or becomes a friction event merely
because Lavish is unavailable.

## Author and open

The Markdown plan stays ground truth throughout; the HTML is a disposable rendering of it,
never the reverse. Author `${TMPDIR:-/tmp}/flow-lavish-$KEY/plan.html` via a Bash heredoc, not
the Write/Edit tools, which plan mode blocks regardless of target path. Never author under the
repository tree: planning runs in the main checkout before any worktree exists, `.lavish/` is
not gitignored, and a repo-tree HTML could ride into a commit.

Design source follows Lavish's documented priority, never hand-rolled ad-hoc CSS: the
user-requested look first, else the subject project's design system, else the
`npx -y lavish-axi@latest design` DaisyUI fallback. Paste this layout net verbatim into
`<head>`; the surface carries dense authored text and must remain sendable at narrow widths:

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

The surface renders exactly what section 5 mandates: the exact complete plan; the recorded base
SHA; the weighted confidence and all five category scores out of 100; the completed pass count
and whether a replacement assessor was used; findings resolved during assessment; and residual
non-blocking risks. Render no input-playbook decision controls: section 1 raised every
human-only answer, grant, and scope choice when it was discovered, so the surface reviews a
settled plan rather than collecting decisions.

## Human-driver loop

Open once, then run one persistent poll owned by the session for the whole surface. Strip
`dom_snapshot` from every poll read and batch annotations into one send. Lavish live-reloads
the same file, so never kill/re-arm the poll around a re-render and never re-run the open
command mid-loop.

On returned annotations, revise the Markdown plan, then re-render the HTML from the revised
Markdown to the same path. Mirror every resolved point into the Markdown the moment it lands:
the artifact mutates in place with no history, so an unmirrored resolution is lost on the next
re-render.

From presentation onward, revision is strictly between the human and the driver. It never
re-enters the assessment loop: no assessor contact, no re-scoring, no new passes. The evidence
panel stays stamped as assessed before presentation, and the human's approval needs no score to
back it.

If the open-time curtain hangs or the iframe send path dies, restart the Lavish server and
reopen with `--no-gate` as a degradation-recovery path, not the default.

## Convergence

Lavish's built-in end-session signal is the verdict; there is no custom approve control. WAIT
for the poll to return `status: ended` carrying the final feedback batch before the approval
gate; even when the session is backgrounded, the armed poll's return is the wake signal. Never
pre-empt the gate. A user-ended session is never reopened without an explicit request.
Agent-side `npx -y lavish-axi@latest end <html>` is only for agent-initiated termination on a
mid-loop degradation, never the normal convergence path.

After the ended signal, fetch the default branch once more. Unchanged or proven-disjoint
movement proceeds to explicit approval. Movement in a planned or behaviorally relevant path is
shown to the human as a plan delta and settled directly through the host adapter's user-input
capability, without an assessor.

Explicit human approval then closes planning; where the host has a plan-approval gate such as
ExitPlanMode, convergence completes before it and the surface never replaces it. The TMPDIR
HTML is disposable: it is never the plan file at bootstrap and never rides into a worktree or a
commit. Bootstrap consumes the approved Markdown.
