# create_pr stage (inline)

Opens a PR for the run's feature branch — a draft by default, or ready for review when `[create_pr] draft = false` in `workspace.toml` (`create_pr.py` reads it; `--draft` forces a draft). Git mechanics (push, protected-branch refusal, title from the HEAD commit) stay in the script; the host calls (detect/open PR) go through the **forge seam**, so the same handler serves GitHub (`gh`) and Bitbucket (`bkt`). The inline handler requires a `[forge]` block (flow's own dogfood wires `create_pr = "inline"` + `[forge] backend = "github"`); the bare plugin default stays `none`.

**No `pr_title` gate.** Unlike the commit stage, do NOT call `lint_ticket` for a field. Nothing populates `pr_title`; the PR title comes from the HEAD (work) commit subject, which the commit stage built from `commit_summary`.

**The PR body is authored, not derived from the commit.** A great commit message and a great PR description have different jobs, so they are decoupled: the commit stays a clean conventional commit, and you author a separate, human-first PR body here (steps 1-3), then hand it to the script via `--body-file`, which is required. The script appends the deterministic `Closes` footer, runs a de-AI `scrub` floor, and on first open attaches the repo's default reviewers (Bitbucket supports it, GitHub degrades cleanly; a reviewer-API failure never fails an open PR).

## The template

Human-first: skimmable, short prose, rich markdown, a natural top-to-bottom flow. A reviewer lands cold on the diff, so the body orients them fast. Shape:

````
<lead: what thing changed and why, 2-5 short sentences, plain prose, no header>

## Changes

Start with `path`: <the heart of the change>; the rest follows from it.

- `path/area`: what + why, 1-2 short sentences
- ...

## Decisions

- <settled choice>: <the reason, one sentence>

## How to verify

```
<command(s) + result, from the implement stage>
```

## Evidence

<details>
<summary>command: N passed, M failed (duration)</summary>

```
<transcript tail: what the e2e run actually observed>
```

</details>
````

Rules:
- The body opens with the lead: headerless prose, 2-5 short sentences, written for a reader
  with little context. First say in plain words what thing is being changed, then why. When
  behavior changes, give one sentence for what happened before and one for what happens now.
  Prefer two or three short paragraphs over one long block. No summary or anchor line: the
  PR title already carries the one-line summary, and the body never repeats it.
- Blank line before every list. Bitbucket treats a list that directly follows a prose line
  as a continuation of that paragraph, gluing every bullet into one block with literal
  dashes; the blank line is a rendering requirement, not style.
- `## Changes` and `## How to verify` are mandatory.
- The `Start with` line under `## Changes` is optional: use it when the diff spans more than
  about three files, to point at the file that carries the real change so the reviewer reads the
  rest as fallout. Omit it on small diffs.
- An optional `## Decisions` section, right after `## Changes`, lists the settled design
  choices and the reason for each, one sentence per choice, sourced from planning. Only real
  choices earn a bullet: a path taken over a concrete alternative that a reviewer might
  reasonably challenge. OMIT the section when the change carries none. Settled only; open
  questions were resolved with the human before this stage (rule below).
- An optional `## Evidence` section, right after `## How to verify`, renders what the e2e run actually observed (the rerunnable command stays in `## How to verify`; this is the captured proof of running it). Evidence is e2e-ONLY: the single source is the e2e stage's captured report (`e2e.out`), read ONLY when its first line carries the `flow:e2e-evidence` sentinel (a report without it is free-form and is skipped). The implement stage's test run is NOT evidence: its command already lives in `## How to verify`, and a unit-test summary repeated here is noise, so an e2e `skip:` or `test-ci-only` run means NO `## Evidence` section, never a placeholder. Only the summary line and the fenced transcript carry over to the PR: report prose written for the pipeline (stage names, `review_loop`, CI-pending notes) stays in `e2e.out` and never reaches the description. One collapsed `<details>` per run: the `<summary>` is the run line in scrub-safe punctuation (`command: N passed, M failed (duration)`, no em-dash, since the scrub floor rewrites em-dashes outside fences), and the body is the fenced transcript tail, plus any fingerprint or delta blocks the run captured. Cap each transcript tail at roughly 15 lines, the verdict and the lines that prove it. Fenced content survives both humanize and the scrub floor untouched, so paste transcript tails verbatim. Author the `<details>` wrapper regardless of forge: on a Bitbucket forge the script flattens each `<details>` to a `###` heading + body, since Bitbucket renders no raw HTML in markdown.
- In an early-tail workspace (stage order places this stage before e2e), the PR simply opens with no `## Evidence` section; the e2e stage later appends its evidence to `stages/pr_body.md` and pushes the updated description through `forge update-body` (`stage-e2e.md` owns that recipe). Do not placehold the missing run here.
- An optional `## Notes` (edge cases, risk, follow-ups) goes last. OMIT it entirely when empty, never placehold. Reach for `<details>` only on genuine overflow (a long migration list, verbose logs) — authored regardless of forge here too; the script flattens it on Bitbucket.
- No open-decision section. The code_review stage resolves its ask-user findings with the human in the conversation before it completes, so by the time this stage runs there is nothing left to ask; a PR with open decisions is not ready for review.
- Size the body to the change. A small change (about three files or fewer, no behavior surprise) gets the minimum: a 1-3 sentence lead, `## Changes` bullets of one short clause each, `## How to verify`, and nothing else. The optional sections exist for changes that earn them; reaching for every section on a routine fix is the failure mode, not thoroughness.
- Keep prose short: people skip walls of text, which defeats the point. Lead 2-5 short sentences, each change bullet 1-2 short sentences, and paragraph breaks wherever a block runs past three sentences. Detail a reviewer only needs while reading the diff belongs in the code or the review brief, not here.
- Basic English: simple words, short sentences, written for a reader with little context. Spell out abbreviations on first use. No run or pipeline jargon (stage names, handler terms, internal file paths like `plan.out`): the reader knows the repository, not this run. No compound coinages that read as machine writing and cost a non-native reader a second parse: "load-bearing", "quote-bearing" and other "-bearing" forms, "hand-rolled", "battle-tested"; say it plainly ("other code depends on it", "written from scratch"). If a bullet needs a second reading, rewrite it.
- Do NOT write the `Closes` footer; the script appends it.

A worked example (this same change would render as):

````
flow opens a pull request at the end of each run. Until now its description was a scrubbed copy of the commit message, which capped it at plain-text quality.

Now the description is authored on its own, written for the reviewer, while the commit stays a clean conventional commit.

## Changes

- `scripts/create_pr.py`: accept an authored `--body-file`; append the deterministic Closes footer + scrub floor.
- `scripts/pr_body.py`: add `closes_footer` extracted from the commit trailer block.
- `references/stage-create_pr.md`: author + humanize the body, then pass it to the script.

## Decisions

- Author the description separately instead of improving the commit-derived one: a commit message and a PR description have different readers and different jobs.

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
   - captured verification evidence: `$TICKET_DIR/stages/e2e.out`, read ONLY when its first line carries the `flow:e2e-evidence` sentinel (the sole source of the optional `## Evidence` section below; the `implement.out` verify tail feeds `## How to verify` only, never Evidence),
   - settled design choices and their reasons: the approved plan's design prose in `plan.out` (and, on an attended run, decisions settled with the human during planning) seed the optional `## Decisions` bullets.

   **Compose `## Changes` by carrying the plan's per-file notes onto the diff.** The `git diff --stat` set above is the ground truth of what shipped, so walk THAT set (every bullet then has a real hunk). For each changed file: if it appears in `plan.out`'s Files-to-change list, start its bullet from that file's one-line note and update it where the implementation diverged — carry-then-update, never the plan note verbatim; if it does NOT appear there, it entered via the post-implement reconcile, so append `(added during implementation: <why>)`, taking `<why>` best-effort from a matching `RECONCILE` entry in the friction log (`.flow/memory/<namespace>/friction.jsonl`) or inferring it from what the file is. A planned file absent from the diff gets no bullet. File-level only: mapping a note to a specific hunk is out of scope (no metadata carries it today). When `plan.out` is absent (`plan = "none"`), compose `## Changes` straight from the diff as before, with no carryover or annotation.

2. **Humanize (mandatory-when-present, applied silently).** If `humanize:humanize` is in your available skills you MUST run the authored body through it, and the whole pass is a content transform, not a conversation: the skill's own Output contract (Draft rewrite / Residual-tells / Final rewrite / Changelog scaffold) describes its working steps, and NONE of that scaffold reaches the chat. Apply the rewrite passes internally, keep ONLY the final rewrite as the body, and write it straight into step 3's `pr_body.md`; the conversation carries at most one line ("body authored and humanized"), never the body text, a draft, or a tells list. The human reads the PR description on the PR, not in the chat (directive 2026-08-06, after FT-1576's driver printed three copies of the body mid-run). The skill preserves markdown structure (`##` headings, one-line bullets, and fenced code all survive); after it, re-check the blank line before every list, because the Bitbucket rendering rule above must survive the rewrite.

   Skip silently if the skill is absent; if it errors, log one line and proceed (a polish hiccup never fails the stage). Same mandatory-when-present rule flow applies to authored code comments.

   **Do not end the turn on the rewrite — continue the stage.** `create_pr` is an INLINE handler running in the driver conversation, so the humanize Skill executes there and its output would normally end the turn. Do NOT stop there: CONTINUE in the SAME reply — emit step 3's `pr_body.md` heredoc, run step 4's `create_pr.py`, capture step 5's `.out`, and issue the do-loop `advance`. Otherwise the do-loop stalls until the human continues it (witnessed twice: flow-gfz5, flow-qdal; friction `8f22583e41ee443fb6eb104b32bceece`; third and fourth witnesses FT-1576 2026-08-06, which stalled 37 minutes on exactly this boundary). This is the primary instance of the general inline-skill turn-continuation rule in `references/delivery-loop.md`.

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
   The base branch resolves from `[create_pr] base` in `workspace.toml`, default `main`; an explicit `--base` overrides both. When the run frontmatter carries `hotfix = true`, add `--hotfix`: the PR opens ready for review against the remote default branch, ignoring the `[create_pr]` base and draft settings, because a hotfix always targets what production builds from.
   - Exit 0 → prints `PR_URL=<url>`. Branch pushed, PR open (draft by default; idempotent: an existing open PR for the branch is reused, never double-opened on resume).
   - Exit 2 → git or forge error (incl. a missing `[forge]` block, or an unreadable `--body-file`); surface stderr, set `STATUS=failed`.
   - Exit 3 → refused (current branch is a protected/integration branch). Should never happen inside a run on a `feat/...` or `hotfix/...` branch; surface and set `STATUS=failed`.

5. **Capture the output.** Write the script's stdout (the `PR_URL=<url>` line) to `$TICKET_DIR/stages/create_pr.out` and pass `--output-path "$TICKET_DIR/stages/create_pr.out"` on `advance`. The final summary and the `review_loop` notification read the `PR_URL=` token from that file.

6. `STATUS=completed` on exit 0.
