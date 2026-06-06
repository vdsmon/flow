# create_pr stage (inline)

Opens a PR for the run's feature branch — ready for review by default, or a draft when `[create_pr] draft = true` in `workspace.toml` (`create_pr.py` reads it; `--draft` overrides). Git mechanics (push, protected-branch refusal, title from the HEAD commit) stay in the script; the host calls (detect/open PR) go through the **forge seam**, so the same handler serves GitHub (`gh`) and Bitbucket (`bkt`). The inline handler requires a `[forge]` block (flow's own dogfood wires `create_pr = "inline"` + `[forge] backend = "github"`); the bare plugin default stays `none`.

**No `pr_title` gate.** Unlike the commit stage, do NOT call `lint_ticket` for a field. Nothing populates `pr_title`; the PR title comes from the HEAD (work) commit subject, which the commit stage built from `commit_summary`.

1. Open or resolve the PR:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/create_pr.py \
     --workspace-root . --ticket "$KEY" --base main
   ```
   - Exit 0 → prints `PR_URL=<url>`. The branch is pushed and the PR is open — ready by default, draft when configured (idempotent: an existing open PR for the branch is reused, never double-opened on resume).
   - Exit 2 → git or forge error (incl. a missing `[forge]` block); surface stderr, set `STATUS=failed`.
   - Exit 3 → refused (current branch is a protected/integration branch). Should never happen inside a run on a `feature/...` branch; surface and set `STATUS=failed`.

2. Capture the output. Write the script's stdout (the `PR_URL=<url>` line) to `$TICKET_DIR/stages/create_pr.out` and pass `--output-path "$TICKET_DIR/stages/create_pr.out"` on `advance`. The final summary and the `review_loop` notification read the `PR_URL=` token from that file.

3. `STATUS=completed` on exit 0.
