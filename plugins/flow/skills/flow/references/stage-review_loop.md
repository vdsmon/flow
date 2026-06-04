# review_loop stage (none default, opt-in)

The post-PR wait loop: after `create_pr` opens the PR, this stage waits on CI and the review bot, driving any fixes until both are green. The bare plugin default handler is `none`, so the stage is a no-op skip; workspaces opt in by wiring a wait-loop skill (e.g. ship-it for Bitbucket) through the init wizard.

The predecessor is `create_pr`, which only opens the draft. review_loop is what proves the PR is genuinely ready: it reaches `completed` when CI is green AND every actionable reviewer thread is resolved.

On `completed`, the PR-ready notification fires with the PR URL (see `references/verb-do.md` for the notification protocol). When the handler stays `none` (no CI/review loop wired), there is nothing to wait on, so the notification falls back to firing after `create_pr` completes instead.

This stage stays descriptive. There is no `review_loop.py`; the wiring lives in whatever wait-loop skill the workspace opts into, not in a script this doc invokes.
