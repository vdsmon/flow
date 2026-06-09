# The pipeline, and running it unattended

`/flow` is one continuous pipeline that runs in a single session: spec splits the work at the PLANNING│IMPLEMENTING seam (the human/machine boundary), then enters a seeded worktree and runs the autonomous tail in the same conversation.

```
dev session, PLAN MODE
  /flow spec FT-X        fetch ticket + iterate plan (READ-ONLY)
  ExitPlanMode                                           ← THE one gate
       │ approved plan
       ▼
same session, normal mode (post-approval)
  flow_worktree.py create …    worktree + config + mise trust + seed state + plan
  EnterWorktree(path=…)        switch this session into the seeded worktree
       │   implement → code_review → e2e → commit → create_pr → review_loop → reflect
  draft PR                                               ← you review
```

Two human touchpoints: plan approval and PR review. No mid-flight gate.

## The pipeline is background-agnostic

The pipeline never asks whether it is attached to a terminal. It runs the same stages, calls the same tools, reads the same `state.json` whether you are watching or have walked away. "Run this unattended" is a *runtime* decision you make on the session, not something the pipeline orchestrates:

- **`/bg`** (or `←` on an empty prompt) backgrounds the current session at any point — before approving the plan, right after, or mid-implement. It "starts a fresh process that resumes from the saved conversation," so the full planning context carries through. The pipeline keeps running and the session shows up in `claude agents`.
- **Dispatch from the agents panel** (or `claude --bg "<prompt>"`) starts a session already in the background; it runs the same `/flow` from the first prompt.

Either way, `claude agents` is the cockpit: attach to peek, answer a blocker, detach. Background several tickets to run them in parallel.

The bridge from the read-only front half to the autonomous tail is the worktree switch, not a second process: after the bootstrap builds the worktree, `EnterWorktree(path=…)` moves the same conversation into it. That also pre-empts the harness's auto-worktree-on-first-edit (skipped once the session is inside a linked worktree), so the pipeline runs in the base-controlled, config-copied worktree the bootstrap built rather than a fresh one.

## What the bootstrap seeds (so the tail resumes at implement)

`flow_worktree.py create` marks the `plan` stage completed with the approved plan as its `plan.out`, and leaves `ticket` pending.
After `EnterWorktree`, continuing into `/flow do`'s `init` resumes (idempotent, same `run_id`), `pick_next_pending` returns `ticket` (self-fetches ticket.json + stamps frontmatter), then skips the completed `plan` and lands on `implement`, which reads `plan.out`.
The resume is driven entirely by `state.json` on disk and never consults in-context history, so it is identical whether spec flowed in or `do` was invoked standalone.

The bootstrap holds **no run lease** — the run's `init` acquires it under the seeded `run_id`, so there is no foreign-lease conflict. It does transiently hold a canonical per-ticket **bootstrap claim** (a flock under the main checkout's `.flow/tickets/`, released at bootstrap exit): two concurrent bootstraps of the same ticket serialize on it, and when a live sibling run exists the loser refuses with exit 4 instead of minting a duplicate worktree.

## Memory is shared, not per-worktree

Each ticket gets its own worktree, but the compounding-knowledge store must not fragment.
The bootstrap writes the main checkout's `.flow` path into a gitignored `.flow/memory-root` sibling file in the worktree.
So `reflect`'s `knowledge.jsonl` appends and `recall` reads all hit one store, serialized by the existing flock.
The tracked `workspace.toml` stays byte-identical — no per-machine absolute path rides into a commit, and the sibling file is gitignored.

## PR delivery

`create_pr` / `review_loop` default to `none`.
With `ship-it` installed, `/flow init --bundle recommended` auto-wires `create_pr → skill:ship-it:create` and `review_loop → skill:ship-it:feedback`, so the tail pushes + opens a draft PR + runs the CI/CodeRabbit loop.
ship-it's stack is Bitbucket + bkt + CodeRabbit; a GitHub-stack project supplies a different `create_pr` bundle.
A bare workspace ends at `commit` (committed branch, no PR).

When the PR is genuinely review-ready (after `review_loop` goes green — CI passed and every actionable reviewer thread resolved, not when the draft first opens at `create_pr`), the pipeline fires an unconditional best-effort `PushNotification` carrying the PR URL.
PushNotification is harness-local (terminal + phone via Remote Control): it renders in-terminal when you are attached and reaches your phone when you have backgrounded the session, and it does not ride MCP/claude.ai auth — so it fires even if the tail's tracker calls 401, which is how you learn an unattended run stalled on auth.

## Blockers

The front-half `--auto` plan gate never parks: when the headless planner cannot self-approve (a clarifying question, sub-90% confidence, or a `BAIL`), the run defers the ticket in place (status → `deferred`, open questions commented) and exits, rather than pausing (see `verb-spec.md`). That is distinct from a genuine mid-tail stage ambiguity below, which still pauses for a human.

A stage that needs a decision raises `AskUserQuestion`.
Attached, you answer inline. Backgrounded, the harness surfaces it as needs-input in `claude agents` — attach, answer, detach, and the run resumes.
To minimize pauses, the bootstrap pre-populates the frontmatter keys the tail would otherwise ask for: `planned_files` (read by the implement pre-handler hook that records the diff baseline, and reused by the commit stage), `commit_type` + `commit_summary` (read by the commit stage), and `e2e_recipe` when e2e is opted in.
Other tail stages avoid prompts; any genuine ambiguity pauses rather than guessing.

## Verify on ticket #1 (before relying on unattended runs)

Backgrounding via `/bg` starts a fresh process that resumes the conversation, so the unattended-run risks are real and worth confirming once before you trust them at scale:

- **cwd survives the resume.** After `/bg` post-`EnterWorktree`, confirm the resumed process is still in the seeded worktree (`pwd`), that implement edits land there (not the main checkout), and that no second auto-worktree was created (`git worktree list`).
- **auth survives the resume.** Confirm tracker / MCP / claude.ai calls (ticket fetch, transition, create_pr push) succeed in the backgrounded run — a refresh can require a browser and 401 silently. Fallback: an attached session has live auth.
- **git push permission.** The tail pushes at `create_pr` (ship-it). If `git push` is gated by an `ask` rule or a global "never push without permission" instruction, an unattended session stalls there with no way to grant it. Pre-authorize a feature-branch push (a `Bash(git push:*)` allow-rule, force-push still denied) and make any global push instruction recognize that an explicitly-invoked pipeline push is fine.
- **mise/toolchain.** The bootstrap only `mise trust`s; the first `mise run` in the tail installs the toolchain. If your repo's setup races a lock, validate the first run.
- **PushNotification delivery.** The desktop path needs a surface to render to; the phone push needs Remote Control connected. Confirm a ping actually reaches you from one backgrounded run.
