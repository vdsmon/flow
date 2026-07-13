# create_pr stage (inline)

Opens a PR for the run's feature branch — a draft by default, or ready for review when `[create_pr] draft = false` in `workspace.toml` (`create_pr.py` reads it; `--draft` forces a draft). Git mechanics (push, protected-branch refusal, title from the HEAD commit) stay in the script; the host calls (detect/open PR) go through the **forge seam**, so the same handler serves GitHub (`gh`) and Bitbucket (`bkt`). The inline handler requires a `[forge]` block (flow's own dogfood wires `create_pr = "inline"` + `[forge] backend = "github"`); the bare plugin default stays `none`.

**No `pr_title` gate.** Unlike the commit stage, do NOT call `lint_ticket` for a field. Nothing populates `pr_title`; the PR title comes from the HEAD (work) commit subject, which the commit stage built from `commit_summary`.

**The PR body is authored, not derived from the commit.** A great commit message and a great PR description have different jobs, so they are decoupled: the commit stays a clean conventional commit, and you author a separate, human-first PR body here (steps 1-3), then hand it to the script via `--body-file`. The script appends the deterministic `Closes` footer, runs a de-AI `scrub` floor, and on first open attaches the repo's default reviewers (Bitbucket supports it, GitHub degrades cleanly; a reviewer-API failure never fails an open PR). With no `--body-file` the script falls back to the old commit-derived body.

## The template

Human-first: skimmable, short prose, rich markdown, a natural top-to-bottom flow. A reviewer lands cold on the diff, so the body orients them fast. Shape:

````
**<one-line summary — a scannable anchor; survives PR-list title truncation>**

<why: the problem this solves, 1-3 sentences, plain prose, no header>

## Changes
- `path/area`: what + why, one line
- ...

## How to verify
```
<command(s) + result, from the implement stage>
```

## Evidence
<details>
<summary>command: N passed, M failed (duration)</summary>

```
<transcript tail — what the run actually observed>
```

</details>

## Your call
Decisions I flagged for you rather than settling them myself (from code_review):
- [Major] <finding> — <decision> (<file>:<loc>)
````

Rules:
- Bold summary line first (anchors the read).
- Why is a headerless lead paragraph, 1-3 sentences.
- `## Changes` and `## How to verify` are mandatory.
- An optional `## Evidence` section, right after `## How to verify`, renders what the verification runs actually observed (the rerunnable command stays in `## How to verify`; this is the captured proof of running it). One collapsed `<details>` per run: the `<summary>` is the run line in scrub-safe punctuation (`command: N passed, M failed (duration)` — no em-dash, since the scrub floor rewrites em-dashes outside fences), and the body is the fenced transcript tail, plus any fingerprint or delta blocks the run captured. Sources: the e2e stage's captured report (`e2e.out`), read ONLY when its first line carries the `flow:e2e-evidence` sentinel (a `skill:<name>` handler's free-form `.out` lacks it and is skipped, exactly as `## Your call` skips a sentinel-less `code_review.out`); plus the implement stage's verify command + result tail, lifted best-effort from `implement.out` (the same input step 1 already reads for `## How to verify`). ONE degrade rule, mirroring `## Your call`: render `## Evidence` ONLY IF at least one source yields a real transcript or fingerprint; OMIT the whole section otherwise (e2e skipped and no usable implement transcript means no section, never a placeholder). Fenced content survives both humanize and the scrub floor untouched, so paste transcript tails verbatim. Author the `<details>` wrapper regardless of forge — on a Bitbucket forge `create_pr.py` flattens each `<details>` to a `###` heading + body, since Bitbucket renders no raw HTML in markdown.
- An optional `## Notes` (edge cases, risk, follow-ups) goes last. OMIT it entirely when empty, never placehold. Reach for `<details>` only on genuine overflow (a long migration list, verbose logs) — authored regardless of forge here too; the script flattens it on Bitbucket.
- An optional `## Your call` lists the code_review ask-user findings: "Decisions I flagged for you rather than settling them myself (from code_review):" then plain `- [Major] <finding> — <decision> (<file>:<loc>)` bullets, authored here so it rides the humanize pass in step 2. One rule covers all three ways it is absent — render it ONLY IF `code_review.out` exists, carries the `flow:code_review-taxonomy` sentinel, AND its `## ask-user` section has at least one bullet (this omits it alike for no ask-user findings, a missing `code_review.out` (`code_review = "none"`), or a `skill:<name>` handler's free-form `.out` lacking the sentinel). OMIT the whole section rather than rendering an empty one.
- Keep prose short: people skip walls of text, which defeats the point. Summary 1 line, why ≤3 sentences, each change bullet 1 line.
- Do NOT write the `Closes` footer; the script appends it.

A worked example (this same change would render as):

````
**Decouple the PR description from the commit body and author a human-first template.**

flow's PR body was a scrubbed derivation of the commit, capping it at plain-text-git quality. This authors a separate, skimmable PR body while the commit stays a clean conventional commit.

## Changes
- `scripts/create_pr.py`: accept an authored `--body-file`; append the deterministic Closes footer + scrub floor.
- `scripts/pr_body.py`: add `closes_footer`; keep `build_body` as the no-`--body-file` fallback.
- `references/stage-create_pr.md`: author + humanize the body, then pass it to the script.

## How to verify
```
mise run test   # scripts pytest root green
```
````

## Steps

1. **Author the body** per the template above. Gather inputs:
   - changed files: `git diff --stat "$(git merge-base origin/<base> HEAD)"..HEAD` (`<base>` resolves as the script does: `[create_pr] base`, default `main`),
   - the verify command + result: `$TICKET_DIR/stages/implement.out`,
   - the premeditated file set + per-file rationale: `$TICKET_DIR/stages/plan.out` — its Files-to-change bullets (however the plan renders that section: `- **Files to change**` bold-label list per stage-plan.md, or a heading; each bullet an explicit path + one-line note) seed the `## Changes` bullets below (see the authoring note after this list); plus the ticket (`ticket.json`) for the overall why,
   - decisions flagged for the human: `$TICKET_DIR/stages/code_review.out`, read ONLY when it exists AND its first line carries the `flow:code_review-taxonomy` sentinel (feeds the optional `## Your call` section below).
   - captured verification evidence: `$TICKET_DIR/stages/e2e.out`, read ONLY when its first line carries the `flow:e2e-evidence` sentinel, plus the same `implement.out` verify tail already gathered above (feeds the optional `## Evidence` section below).

   **Compose `## Changes` by carrying the plan's per-file notes onto the diff.** The `git diff --stat` set above is the ground truth of what shipped, so walk THAT set (every bullet then has a real hunk). For each changed file: if it appears in `plan.out`'s Files-to-change list, start its bullet from that file's one-line note and update it where the implementation diverged — carry-then-update, never the plan note verbatim; if it does NOT appear there, it entered via the post-implement reconcile, so append `(added during implementation: <why>)`, taking `<why>` best-effort from a matching `RECONCILE` entry in the friction log (`.flow/<namespace>/friction.jsonl`) or inferring it from what the file is. A planned file absent from the diff gets no bullet. File-level only: mapping a note to a specific hunk is out of scope (no metadata carries it today). When `plan.out` is absent (`plan = "none"`), compose `## Changes` straight from the diff as before, with no carryover or annotation — the same graceful degrade the `## Your call` input above uses.

2. **Humanize (mandatory-when-present).** If `humanize:humanize` is in your available skills you MUST run the authored body through it. Two things the skill's behavior forces (verified against a filled template):
   - It returns a **4-part scaffold** (Draft rewrite / Residual-tells / Final rewrite / optional Changelog). Take ONLY the **Final rewrite** section as the body. Never paste the scaffold.
   - It preserves markdown structure (`##` headings, one-line bullets, and fenced code all survive), but it STRIPS the bold on the summary line (its mechanical-boldface rule). **Re-apply `**...**` to the summary line** after humanizing, so the scannable anchor stays.

   Skip silently if the skill is absent; if it errors, log one line and proceed (a polish hiccup never fails the stage). Same mandatory-when-present rule flow applies to authored code comments.

   **Do not end the turn on the rewrite — continue the stage.** `create_pr` is an INLINE handler running in the orchestrator's main conversation, so the humanize Skill executes there and the orchestrator's reply IS the Final rewrite, which by default ends the turn. Do NOT stop there: take the Final rewrite and CONTINUE in the SAME reply — emit step 3's `pr_body.md` heredoc, run step 4's `create_pr.py`, capture step 5's `.out`, and issue the do-loop `advance`. Otherwise the do-loop stalls until the user pokes (witnessed twice: flow-gfz5, flow-qdal; friction `8f22583e41ee443fb6eb104b32bceece`). This is the primary instance of the general inline-skill turn-continuation rule in `references/delivery-loop.md`.

3. **Write the body worktree-safely.** Prefer the adapter's exact file writer at the
   absolute `$TICKET_DIR/stages/pr_body.md`. If the host rejects its native writer in
   a backgrounded linked worktree, use the collision-safe quoted-heredoc fallback in
   `references/delivery-loop.md` from explicit workdir `run_root`:
   ```bash
   cat > "$TICKET_DIR/stages/pr_body.md" <<'FLOW_PR_BODY_9f3a'
   <the authored + humanized body, verbatim>
   FLOW_PR_BODY_9f3a
   ```

4. **Open or resolve the PR:**
   ```bash
   FLOW_HARNESS="<harness>" "<facade>" create-pr \
     --workspace-root . --ticket "$KEY" --body-file "$TICKET_DIR/stages/pr_body.md"
   ```
   The base branch resolves from `[create_pr] base` in `workspace.toml`, default `main`; an explicit `--base` overrides both.
   - Exit 0 → prints `PR_URL=<url>`. Branch pushed, PR open (draft by default; idempotent: an existing open PR for the branch is reused, never double-opened on resume).
   - Exit 2 → git or forge error (incl. a missing `[forge]` block, or an unreadable `--body-file`); surface stderr, set `STATUS=failed`.
   - Exit 3 → refused (current branch is a protected/integration branch). Should never happen inside a run on a `feat/...` branch; surface and set `STATUS=failed`.

5. **Capture the output.** Write the script's stdout (the `PR_URL=<url>` line) to `$TICKET_DIR/stages/create_pr.out` and pass `--output-path "$TICKET_DIR/stages/create_pr.out"` on `advance`. The final summary and the `review_loop` notification read the `PR_URL=` token from that file.

6. `STATUS=completed` on exit 0.
