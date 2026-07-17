<!-- flow:activation-truth:begin -->
# Stage: plan

## Routed planning contract

New exact snapshots execute `planner` and any required fresh `plan_assessor` through
`cognitive-worker` read-only capsules. The planner compatibility surface retains live
thread continuity and rehydration, but the common worker owns the exact CLI, prompt and
schema digests, standalone exact-SHA clone, journal, terminal proof, Git guards, and
disposal receipt. Failure stops visibly; it never falls back to a native or alternate
model. Planning CAS, feedback, revalidation, the host-native approval gate, and
bootstrap approval verification remain authoritative here.

## Purpose

Produce an implementation plan for the ticket and return it as your report.
You are the `Plan` subagent for the `plan` stage of logical `FLOW`.
You read the ticket context, design the change, and hand back a plan a human will approve before any code is written.

You do NOT write code in this stage.
You do NOT touch the working tree.
Your entire output is the plan text returned as your response.

Plan approval is a human gate in the orchestration.
The do-loop captures your returned plan and the user reviews it before the implement stage runs.
You cannot wait for or solicit that approval yourself — just return a plan good enough to approve.

## Inputs

- `.flow/runs/<KEY>/ticket.json` — the full cached ticket payload (summary,
  description, type, comments, parent, links).
  This is your primary source of intent.
  When your prompt instead carries an embedded ticket-context block (the unattended
  pre-bootstrap path, where the subagent runs before the `ticket` stage writes
  this file), that block is your source of intent and this file will not exist yet.
- `.flow/tickets/<KEY>.md` — ticket frontmatter (status, any `planned_files`
  the user pre-seeded, commit hints).
  The body below the frontmatter may carry human notes.
- The current repository.
  Read the code you intend to change so the plan references real files and real call sites, not guesses.

## Steps

1. Read `.flow/runs/<KEY>/ticket.json` and `.flow/tickets/<KEY>.md` if present;
   otherwise (the unattended pre-bootstrap case) use the ticket-context block
   embedded in your prompt.
   Extract the actual goal — what behavior must exist when this ticket is done.

2. Explore the codebase enough to ground the plan.
   Locate the files, modules, and functions the change touches.
   Do not skim; an approver should be able to trust your file list.

   **Recall prior knowledge keyed on the ticket text + your intent (read-only).** This is where flow's memory layer pays off — full ticket text in hand. Write the ticket title+body to a temp file, PREPENDED with a short (1–2 line) intent preamble naming the form / domain / component you are about to touch and the shape of the change (the risk), then query recall against the whole file (a pure READ; the matching WRITE — `--record-pending` — is the orchestrator's post-gate step, NOT yours):
   ```bash
   QF="${TMPDIR:-/tmp}/flow-recall-$KEY.txt"   # intent preamble + ticket title + body
   B=$(git branch --show-current)
   FLOW_HARNESS="<harness>" "<facade>" recall --query-file "$QF" \
     --semantic --top-n 30 --branch "$B" --workspace-root .
   ```
   Use `--query-file` (not a shell positional — avoids the `"`/`\`/newline hazard). The intent preamble AUGMENTS the raw ticket text, it never replaces it — the identifier-rich ticket body stays the BM25 signal, while the preamble names the domain so the semantic side clusters prior work on the same form / component (e.g. "Working on the IVA form's validation; risk: rounding in the F.20 line totals"). `--semantic` is inert when the workspace has not opted into `[memory.semantic]` (recall stays pure BM25). Weave any relevant returned entries into the plan's Approach/Risks.

   **Verify any content/drift finding against the default base, not the working checkout.** General orientation reads stay on the working checkout via the Read tool (that is the normal way to explore, and you do NOT need to `git show` every file you look at). But the moment you would CITE a content/drift finding in the plan, or STAMP a file into `planned_files` BECAUSE OF its current content, re-read that specific file at the freshly-fetched default base before committing the finding. The unattended tail branches its worktree off `@default` (`origin/<default>`, fetched fresh), while the launcher checkout this exploration runs in can lag `origin/main`, so a drift a file shows here may already be fixed upstream, and the planned fix would land as a no-op (flow-749). Resolve the base the same way `FLOW_HARNESS="<harness>" "<facade>" worktree create --base @default` does and read the base version:
   ```bash
   git fetch --quiet origin
   DEFAULT=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD)   # e.g. origin/main
   git show "$DEFAULT:<path>"   # the base version of the file you'd cite
   ```
   The `git fetch` is read-only by discipline (it only updates remote-tracking refs / FETCH_HEAD, never the working tree). A content/drift finding is cited at plan time and may stamp `planned_files`, so it must be verified against the right base now, at plan time, and cannot be deferred to implement.

3. Draft the plan with these sections:
   - **Goal** — one or two sentences on what success looks like.
   - **Files to change** — explicit paths, each with a one-line note on what
     changes there.
     This list is load-bearing: the implement stage confines edits to the planned files, so be complete and precise.
     The implement stage is TDD, so it nearly always writes a NEW test file. List the concrete anticipated NEW test file path(s) it will create here — not only the cases (those live in Test strategy), the path itself — or state "no new test file" when the change adds none. This is what the bootstrap-derived `planned_files` stamps, so an unlisted test path makes the post-implement reconcile fire on essentially every run.
     A NEW test file usually also drags in enabling test-infra files that must ALSO be in this list, or the implement stage stalls on a reconcile: the package `__init__.py` a new test directory needs, and the target lib's test-runner config (e.g. a `[tool.pytest.ini_options] pythonpath` block) when the test or its conftest imports a shared test helper. Check whether the target test package is already importable and collectable under the chosen e2e runner; if not, the files that make it so are part of this plan, not an afterthought.
     **Flag any binary deliverable explicitly.** A binary file in the change set (an `.xlsx` template, an image, a compiled fixture) cannot be produced by the text-only implement subagent — mark its path as binary (e.g. `path/to/x.xlsx (binary, orchestrator-copied)`) so the orchestrator copies it into the worktree post-implement rather than expecting the subagent to write it (the post-implement reconcile in `references/delivery-loop.md`).
     When the change edits a shared constant or set, grep the repo for prose that spells out its members (sibling docstrings, MODULE.md rows, references prose) and add those files to this list too, so a stale enumeration does not slip past the plan and fire the post-implement reconcile.
   - **Approach** — the design.
     How the pieces fit, what existing patterns you reuse, any new module or interface and why.
   - **Test strategy** — what unit tests prove the change.
     The implement stage is TDD-mandatory, so name the cases the implementer should write.
     When those cases need a new test file, list that file's path under **Files to change** (not here) so `planned_files` covers it.
   - **Risks** — what could go wrong, edge cases, migration concerns, anything
     the approver should weigh.
   - **Confidence** — a first-pass self-rating: a **Score (0-100%)**, then
     **Proven** (bullets you directly verified) vs **Inferred** (from convention /
     naming / a 1:1-chain argument), and **What would raise it** (reachable
     artefacts). Library-API claims must be Context7-verified, not left under
     Inferred. Do NOT file a hot / non-hot / guard-file classification under
     Proven — you cannot run the orchestrator's hotness probe, so a model's
     own read is unreliable (flow-94l6: the plan and the advisor both
     asserted non-hot on a guard-file change); leave hotness to the main
     loop's `FLOW_HARNESS="<harness>" "<facade>" triage decided --files` probe rather than asserting it. This
     is only a first pass: the main loop re-rates your plan INDEPENDENTLY through
     the adapter mapping in `references/harness.md` (Claude Code may use its advisor;
     Codex uses a fresh collaboration agent or second model call without a Claude
     model parameter; generic uses its declared independent-call/defer behavior) before
     the human gate, because a plan's author is the worst judge of its own
     confidence.
   - **Lane** *(interactive runs only)* — propose the verification lane (`express` |
     `light` | `full`) the tail takes, conservatively: `express` only for
     behavior-preserving, tightly-bounded work, `light` for a small
     behavior-changing change, else `full` (the default). One line of
     justification; the user approves or overrides it at the gate. **Omit this
     section in unattended mode** — there the lane is derived from the bead's tier
     labels (`triage.py lane`), not proposed, so a proposal would be discarded.

4. Return the plan as your response.
   Keep it concrete and reviewable; an approver reading only your output should be able to say yes or no.

## Outputs

- The plan text, returned as your stage report.
  The do-loop captures it to `<ticket-dir>/stages/plan.out`.
  You do not write that file yourself.

## Errors

- `ticket.json` missing or empty AND no embedded ticket-context block in your
  prompt → you cannot plan without intent.
  Return a short report stating the ticket context is unavailable and the `ticket` stage must run first.
  Do not invent a plan from the ticket key alone.
  (If an embedded ticket-context block IS present — the unattended pre-bootstrap
  path — proceed normally from that block; do NOT bail.)
- Ticket goal genuinely ambiguous → first try to dissolve the ambiguity by investigation: read the code you would touch, recall prior runs, Context7 any library claim. A fork whose answer is reachable by reading is unfinished investigation, not ambiguity — resolve it and fold the answer into the plan.
  Do not guess silently, and do not punt a dissolvable fork to the approver.
  State the competing interpretations in your returned plan ONLY for tensions that survive investigation — genuine forks where the evidence cannot pick and a human must.

## Skip conditions

- Skipped entirely if `workspace.toml [pipeline.handlers] plan = "none"`.
  In that case the do-loop short-circuits and this doc is never read.
  The implement stage then works from `ticket.json` + frontmatter directly.
