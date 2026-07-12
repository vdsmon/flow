# The pipeline, and running it unattended

Flow is one continuous pipeline in a single session: spec splits work at the
PLANNING│IMPLEMENTING seam, then binds a seeded worktree by absolute path and runs the
autonomous tail in the same conversation.

```
dev session, PLAN PHASE
  Flow spec FT-X         fetch ticket + iterate plan (READ-ONLY)
  explicit approval                                      ← THE one gate
       │ approved plan
       ▼
same session, post-approval
  flow_worktree.py create …    worktree + config + mise trust + seed state + plan
  bind absolute run_root       root every operation in the seeded worktree
       │   implement → code_review → e2e → commit → create_pr → review_loop → reflect
  draft PR                                               ← you review
```

Two human touchpoints: plan approval and PR review. No mid-flight gate.

## Backgrounding belongs to the host

The pipeline never asks whether it is attached to a terminal. It runs the same stages
and reads the same `state.json` whether watched or unattended. The host owns that
runtime decision:

- Claude Code may use `/bg`, `claude --bg`, and `claude agents`.
- Codex may use its host-owned task or background surface when available. Flow does not
  invoke or toggle it.
- A generic harness may remain foreground-only.

The bridge from the read-only front half to the tail is the rooted worktree binding,
not a second process. After bootstrap, set `run_root` to `result.worktree`, set `facade`
to `<run_root>/.flow/flow`, and pass that root on every command, edit, and subagent.
Claude Code may additionally use `EnterWorktree`, but correctness never depends on a
persistent cwd or native switch.

## What the bootstrap seeds (so the tail resumes at implement)

`flow_worktree.py create` marks the `plan` stage completed with the approved plan as its `plan.out`, and leaves `ticket` pending.
After binding `run_root`, continuing into Flow `do`'s `init` resumes (idempotent,
same `run_id`), `pick_next_pending` returns `ticket` (self-fetches ticket.json and
stamps frontmatter), then skips the completed `plan` and lands on `implement`, which
reads `plan.out`.
The resume is driven entirely by `state.json` on disk and never consults in-context history, so it is identical whether spec flowed in or `do` was invoked standalone.

The bootstrap holds **no run lease** — the run's `init` acquires it under the seeded `run_id`, so there is no foreign-lease conflict. It does transiently hold a canonical per-ticket **bootstrap claim** (a flock under the main checkout's `.flow/tickets/`, released at bootstrap exit): two concurrent bootstraps of the same ticket serialize on it, and when a live sibling run exists the loser refuses with exit 4 instead of minting a duplicate worktree.

## Memory is shared, not per-worktree

Each ticket gets its own worktree, but the compounding-knowledge store must not fragment.
The bootstrap writes the main checkout's `.flow` path into a gitignored `.flow/memory-root` sibling file in the worktree.
So `reflect`'s `knowledge.jsonl` appends and `recall` reads all hit one store, serialized by the existing flock.
The tracked `workspace.toml` stays byte-identical — no per-machine absolute path rides into a commit, and the sibling file is gitignored.

### The namespace seam (individual vs batch runs converge)

`_memory_paths.resolve_memory_base` resolves the base dir that holds the store, most specific first:

1. `.flow/memory-root` (gitignored sibling, plain text single abs path): the worktree bootstrap writes it to redirect the store to the shared (main) `.flow` without touching the tracked workspace.toml.
2. `.flow/workspace.toml` `[memory].root` when set (the init-time render path).
3. the workspace-local `.flow` (non-worktree runs stay byte-identical).

A knowledge entry's id is `sha256(namespace + ticket + type + normalized_body)[:16]` (`memory_append.compute_id`); workspace root, worktree path, branch, and timestamp are all excluded, so the same reflect finding produces the same id whether the ticket ran individually or as one of N concurrent drain worktrees, and the flock-serialized scan-before-append suppresses the second write as a duplicate.
`knowledge_lock_path` resolves through the same base, so every redirected worktree contends on the one shared lock file, not a per-worktree copy.
Shared through the redirect: `knowledge.jsonl`, `friction.jsonl`, `ship-events/`. Workspace-local by design: `recall-pending.jsonl` (`recall_pending.recall_pending_path` builds from the workspace root, not the memory base) and `.flow/runs/<ticket>/`. The `recall-pending.jsonl` producer is now the plan-phase `.flow/flow recall --record-pending` call (the post-gate WRITE in `verb-spec.md` step 6), not the old SessionStart hook; the dispatcher still promotes it into the run's `recall-log.jsonl` at init.
`friction.jsonl` events are uuid4-keyed — distinct by design, never deduplicated; the convergence story above is knowledge-only.
A ship-event rerun never mutates the immutable primary `<ticket>.json`; `observe_ship_event.py` writes `<ticket>.json.dupe.<n>.json` beside it.
These are preconditions, not unconditional guarantees: the redirect exists only for bootstrap-created worktrees (every batch launch passes through `flow_worktree.py create`, and reflect appends from the worktree cwd). A hand-made worktree without the sibling falls through to its own local `.flow` — fragmentation, not collision.
Verified end-to-end by `scripts/tests/test_cross_queue_memory.py`.

## PR delivery

`create_pr` / `review_loop` default to `none` — a bare workspace ends at `commit` (committed branch, no PR).
The primary path is the inline forge seam: a workspace wires `create_pr = "inline"` + `review_loop = "inline"` and supplies a `[forge]` block, and the same handlers drive either GitHub (`gh`) or Bitbucket (`bkt`) per `[forge] backend` — flow's own dogfood uses `[forge] backend = "github"`. The tail then pushes + opens a draft PR (`create_pr.py` via the forge seam) and waits on the CI / review-bot loop (`forge_cli.py`).
As a legacy alternative, `ship-it` (a Bitbucket + bkt + CodeRabbit bundle) wires the PR stages as skills instead: with it installed, `/flow init --bundle recommended` auto-wires `create_pr → skill:ship-it:create` and `review_loop → skill:ship-it:feedback`.

When the PR is genuinely review-ready (CI passed and actionable threads are
resolved), a non-`--auto` run uses the adapter's best-effort notification capability
with the PR URL. Claude Code may use `PushNotification`; Codex and generic adapters
surface it in-thread and use the durable forge fallback. An `--auto` run skips this
notification; its drain report, ticket close, and `create_pr.out` carry completion.

## Blockers

The front-half `--auto` plan gate never parks: when the headless planner cannot self-approve (a clarifying question, sub-90% confidence, or a `BAIL`), the run defers the ticket in place (status → `deferred`, open questions commented) and exits, rather than pausing (see `verb-spec.md`). That is distinct from a genuine mid-tail stage ambiguity below, which still pauses for a human.

A stage that needs a decision uses the adapter's user-input capability. Claude Code
may surface needs-input in `claude agents`; Codex and generic adapters ask plainly and
wait. A detached or `--auto` run follows the documented defer/block behavior.
To minimize pauses, the bootstrap pre-populates the frontmatter keys the tail would otherwise ask for: `planned_files` (read by the implement pre-handler hook that records the diff baseline, and reused by the commit stage), `commit_type` + `commit_summary` (read by the commit stage), and `e2e_recipe` unless the workspace explicitly disabled e2e.
Other tail stages avoid prompts; any genuine ambiguity pauses rather than guessing.

## Verify on ticket #1 before relying on unattended runs

Host continuation can reset cwd and process state, so confirm these once before
trusting unattended runs at scale:

- **Rooting survives cwd reset.** Deliberately start each command and a subagent from
  the original checkout. Confirm the explicit `run_root` and absolute facade keep all
  edits/state in the seeded worktree and no second worktree appears.
- **auth survives the resume.** Confirm tracker / MCP / claude.ai calls (ticket fetch, transition, create_pr push) succeed in the backgrounded run — a refresh can require a browser and 401 silently. Fallback: an attached session has live auth.
- **git push permission.** The tail pushes at `create_pr` (the inline forge handler, or ship-it on a legacy bundle). If `git push` is gated by an `ask` rule or a global "never push without permission" instruction, an unattended session stalls there with no way to grant it. Pre-authorize a feature-branch push (a `Bash(git push:*)` allow-rule, force-push still denied) and make any global push instruction recognize that an explicitly-invoked pipeline push is fine.
- **mise/toolchain.** The bootstrap only `mise trust`s; the first `mise run` in the tail installs the toolchain. If your repo's setup races a lock, validate the first run.
- **Notification delivery.** Confirm the adapter's in-thread/native notification and
  durable forge fallback are visible from one unattended run.
