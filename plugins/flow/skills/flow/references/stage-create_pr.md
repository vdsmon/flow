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
````

Rules:
- Bold summary line first (anchors the read).
- Why is a headerless lead paragraph, 1-3 sentences.
- `## Changes` and `## How to verify` are mandatory.
- An optional `## Notes` (edge cases, risk, follow-ups) goes last. OMIT it entirely when empty, never placehold. Reach for `<details>` only on genuine overflow (a long migration list, verbose logs).
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
mise run test   # scripts + hooks green
```
````

## Steps

1. **Author the body** per the template above. Gather inputs:
   - changed files: `git diff --stat "$(git merge-base origin/<base> HEAD)"..HEAD` (`<base>` resolves as the script does: `[create_pr] base`, default `main`),
   - the verify command + result: `$TICKET_DIR/stages/implement.out`,
   - the why: the ticket (`ticket.json`) and plan.

2. **Humanize (mandatory-when-present).** If `humanize:humanize` is in your available skills you MUST run the authored body through it and use the rewrite. Skip silently if the skill is absent; if it errors, log one line and proceed (a polish hiccup never fails the stage). Same rule as the code-comment bar in `references/stage-implement.md`.

3. **Write the body worktree-safely.** The orchestrator's own `Write` to a worktree path is rejected in bg mode, so emit the body via a quoted heredoc (the pattern in `references/verb-do.md`) to `$TICKET_DIR/stages/pr_body.md`:
   ```bash
   cat > "$TICKET_DIR/stages/pr_body.md" <<'FLOW_PR_BODY_9f3a'
   <the authored + humanized body, verbatim>
   FLOW_PR_BODY_9f3a
   ```

4. **Open or resolve the PR:**
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/create_pr.py \
     --workspace-root . --ticket "$KEY" --body-file "$TICKET_DIR/stages/pr_body.md"
   ```
   The base branch resolves from `[create_pr] base` in `workspace.toml`, default `main`; an explicit `--base` overrides both.
   - Exit 0 → prints `PR_URL=<url>`. Branch pushed, PR open (draft by default; idempotent: an existing open PR for the branch is reused, never double-opened on resume).
   - Exit 2 → git or forge error (incl. a missing `[forge]` block, or an unreadable `--body-file`); surface stderr, set `STATUS=failed`.
   - Exit 3 → refused (current branch is a protected/integration branch). Should never happen inside a run on a `feat/...` branch; surface and set `STATUS=failed`.

5. **Capture the output.** Write the script's stdout (the `PR_URL=<url>` line) to `$TICKET_DIR/stages/create_pr.out` and pass `--output-path "$TICKET_DIR/stages/create_pr.out"` on `advance`. The final summary and the `review_loop` notification read the `PR_URL=` token from that file.

6. `STATUS=completed` on exit 0.
