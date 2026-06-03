# Stage: reflect

## Purpose

Extract durable knowledge from this ticket's run, append entries to the compounding memory layer, and (if the ticket shipped) record an immutable ship-event evidence record.

Reflect is the closing stage.
The discipline here is what makes `/flow` compounding: every ticket's run produces 0..N knowledge entries that future tickets in the same workspace can recall via BM25.
Reflection runs on three lenses, two of them gated by `workspace.toml [reflect]` flags that `reflect_inputs.py` surfaces in the bundle as `reflect_config` (step 1):

- **A. Domain (always on).** DOWN at the ticket's domain — what the work taught you about the code, the libraries, the environment. Appends to the flow knowledge layer (steps 2 + 3 + 4).
- **B. Machinery (`reflect_config.machinery`, default false).** UP at the harness itself — did `/flow`'s scripts, stages, and loop serve the run, or fight it. This is where you, the agent that just ran the pipeline, are EXPECTED to fix the harness while your context is freshest, and to feed what you cannot safely fix yet to `/skill-polish`. Only a skill DEVELOPER wants this, and it edits flow's own source, so it is OFF unless the workspace opts in (step 2b).
- **C. Project knowledge (`reflect_config.claude_memory`, default true).** OUTWARD at the project's own durable knowledge stores — the on-disk project auto-memory and the checked-in project `CLAUDE.md` / `AGENTS.md`. Auto-writes broadly-useful facts to the project memory and PROPOSES (does not apply) any `AGENTS.md` rule change (step 2c).

The friction a run surfaces is the raw material that makes the next run smoother, so a run that merged cleanly but cost manual intervention is a reflect MISS if that friction goes neither fixed nor recorded (when lens B is on) and the durable project facts it taught go unrecorded (lens C).

The taxonomy is closed:
- **LEARNED** — technical insight that future-you should know about.
- **DECISION** — design choice + rationale that's not obvious from the code.
- **FACT** — codebase / environment observation that surprised you.
- **PATTERN** — recurring technique worth naming.
- **INVESTIGATION** — research artifact (links, papers, prior art).
- **DEVIATION** — unexpected surprise (a thing that didn't work as documented).

## Inputs

- `<ticket-dir>/state.json` — full run history.
- `.flow/tickets/<KEY>.md` — ticket frontmatter.
- `<ticket-dir>/stages/*.out` — captured subagent reports.
- The git diff since stage `ticket`.

## Steps

1. Bundle the reflect inputs:
   ```bash
   ${CLAUDE_SKILL_DIR}/scripts/reflect_inputs.py \
     --ticket <KEY> \
     --ticket-dir <ticket-dir> \
     --ticket-frontmatter .flow/tickets/<KEY>.md \
     --cwd .
   ```
   - Exit 0 → JSON payload to stdout: `{ticket, run_id, state,
     ticket_frontmatter, final_diff, subagent_reports[], friction[],
     reflect_config}`.
     `reflect_config` is `{machinery: bool, claude_memory: bool}` (machinery
     defaults false, claude_memory defaults true; a `[reflect]` block in
     `workspace.toml` overrides). It gates step 2b and step 2c below.
   - Exit 1 → state.json missing/corrupt, or the diff environment is broken (git not on PATH, bad `--cwd`).
     Abort with status=failed.
   - Exit 2 → git ran but returned an error (bad ref). Abort.
   - Exit 3 → I/O error reading state. Abort.

2. Read the bundle JSON carefully. Look for novel signal:
   - **What** did the ticket teach you that wasn't already documented?
   - **What** design choice did you make + why (DECISION)?
   - **What** about the codebase or environment surprised you (FACT /
     DEVIATION)?
   - **What** recurring technique did you discover (PATTERN)?
   - **What** would you want to know if you came back to this code in 3
     months?

   **REJECT** narrative summaries.
   "We added a feature" is not novel.
   "The X system's caching layer breaks when Y conditions hold" IS novel.

   **Surface missing REPO-artifact gaps, do NOT act on them.** Reflect runs AFTER `create_pr` and the review loop. If you notice the change shipped without something it should carry (a fixture with no provenance note, an absent doc stub, an un-added file IN THE TICKET REPO), record it as a one-line note to the user and, where it generalizes, a knowledge entry — but do NOT add the file here. Adding a repo file at reflect-time forces a new commit that re-triggers the entire CI + review loop, the exact churn the implement stage's definition-of-done exists to prevent. Reflect names the gap so it lands earlier next time; it does not close it. (This restraint is about repo / PR artifacts ONLY. For the HARNESS itself — the skill's own files — step 2b is the opposite: there you are empowered to fix on the spot, because skill files are not PR artifacts and carry no re-review cost.)

2b. **Machinery reflection (lens B — gated; mandatory when ON and the run hit any friction).** SKIP this entire step unless `reflect_config.machinery` is true. It is false by default: a stranger running flow neither wants flow editing its own source nor cares about flow-internal findings. When the flag is off, do not record `MACHINERY:` entries and do not apply harness fixes; go straight to step 2c. When it is on (the skill developer's workspace), run it in full. The steps above point the lens DOWN at the ticket's domain (the code, the tax rules, the library). This step points it UP at the harness that produced the work: did `/flow`'s own scripts, stages, exit codes, handler dispatch, and orchestration loop serve the run, or fight it? This is the feedstock `/skill-polish` consumes — produce it whether or not a human asked, at the depth of an engineering review, not a vibe check.

   Reconstruct friction from evidence, not memory (a backgrounded reflect agent has no live recall): the PRIMARY source is the in-flight friction log — the bundle's `friction` array (entries the do-loop appended via `flow_friction.py` as the run hit retries, missing tools, drift, lost leases, planned-file reconciles, failed stages). Corroborate and extend it with the stage `.out` reports in `<ticket-dir>/stages/` (subagents flag things like "created a file outside planned_files"), the `state.json` stage history (retries, `failed`->`retry` transitions, stages that needed a `recover`), and anything else the run had to work around. For EACH friction point:
   - **Re-read the script or reference file behind it** (`scripts/<x>.py`, `references/stage-<y>.md`) — do NOT guess at the cause. Cite `file:line`.
   - State the defect concretely + a one-line fix (e.g. "`diff_extract.check_ownership` runs bare `git status --porcelain`, which collapses a fully-untracked dir to `foo/` and false-positives against per-file `planned_files`; add `--untracked-files=all`").
   - Severity-tag: **blocker** (would fail an unattended run) / **major** (needed manual intervention or a confusing recovery) / **minor** (papercut).
   - Name the owning skill file.

   Emit each as a `DEVIATION` knowledge entry (step 3) with the text prefixed `MACHINERY:` so `/skill-polish` can grep them, AND list them in the human-facing reflect output.

   **You are empowered to FIX the process you just ran, right now.** You are the highest-fidelity judge of this harness that will ever exist for this run: you lived every stage, and no later reviewer (`/skill-polish`, a human, a future session) will have the context you have at this moment. Recording friction for someone else to maybe act on later is the lossy path — it decays, it gets deprioritized, the fix arrives with half the understanding. Default to fixing it yourself, here. The only gate is blast radius:

   - **APPLY NOW (the default).** Surgical, high-confidence fixes to flow's OWN process files: a `references/*.md` clarification, a localized bug in a flow engine script (`scripts/*.py`). These are NOT repo/PR artifacts — they live in the skill's own tree, outside the ticket repo, so they carry zero re-review churn — and they are version-controlled, so a bad edit is revertible. Flow's own scripts and reference docs are NOT in the run's canonical snapshot, so editing them mid-run is safe. Re-Read the file before editing (a sibling fleet agent may have shifted the anchor; "anchor not found" usually means it is already fixed — treat that as done). If you touch a script, run its test suite and add a regression test for the bug you fixed. Do not ask permission to improve the tool you are running; that is the whole point of reflecting from inside the run.
     - **Apply the edit through `scripts/machinery_edit.py`, NOT the raw Edit tool.** A fleet runs many `/flow` jobs at once and several can hit reflect together; the Edit tool has no cross-process serialization, so two concurrent machinery edits to the same flow source file race (lost update, or a torn read that crashes a third run importing the half-written module). `machinery_edit.py` holds a single global flock across the whole read -> replace -> atomic-write, so concurrent machinery writers serialize and any concurrent reader sees old-or-new (never a torn file). The flock is an OS advisory lock released on process exit — there is no lease to clear and nothing to leak on a crash. It also enforces the snapshot caveat below (refuses `stage-registry.toml` and any path outside the skill tree). Invoke it per fix:
       ```bash
       PAYLOAD=$(mktemp); printf '%s' "$(jq -n --arg f "<rel-or-abs path>" --arg old "<unique anchor>" --arg new "<replacement>" '{file:$f,old:$old,new:$new}')" > "$PAYLOAD"
       python3 ${CLAUDE_SKILL_DIR}/scripts/machinery_edit.py apply --skill-root ${CLAUDE_SKILL_DIR} --payload "$PAYLOAD"
       ```
       Exit 0 `applied` → done. Exit 0 `already_applied` → a sibling beat you to it, treat as done. Exit 3 `anchor_not_found` → re-derive the anchor (re-Read; it may already be fixed differently). Exit 4 `ambiguous` → narrow the anchor and retry. Exit 2 `refused` → snapshot-pinned or out-of-tree, route to PROPOSE + RECORD.
     - **After applying ANY machinery fix: bump the plugin version and commit (do NOT push).** A machinery edit changes the installed skill, so it MUST be captured: bump `version` in `plugins/flow/.claude-plugin/plugin.json` (patch bump for a papercut/bugfix, minor for new behavior), then commit the touched skill files + the version bump in the skill repo. Do NOT push — pushing is a human call (the fleet must not publish skill changes unattended). Record the commit sha in the `MACHINERY:` entry so the change is traceable. If you have no commit rights or the skill repo is dirty with unrelated work, fall back to PROPOSE + RECORD instead of applying.
     - **Snapshot caveat — two things you must NOT edit mid-run** (they ARE in the run's canonical snapshot, so editing them trips the drift guard on the closing `advance` and aborts the very run making the fix): `stage-registry.toml`, and any WIRED handler skill (the plugin behind a `skill:` entry in `pipeline.handlers`, e.g. the `create_pr` / `review_loop` skill). A fix to those is PROPOSE + RECORD, OR apply it and then run `/flow recover reload-snapshot <ticket>` to re-baseline before the loop closes.
   - **PROPOSE + RECORD, do not self-apply unattended.** Structural changes (the orchestration driver, the dispatch loop, a script rewrite), anything touching a file the fleet is actively mid-stage on, or anything you are not high-confidence is strictly correct. The blast radius across concurrent runs is too large to self-apply. Here the `MACHINERY:` entry + the human note ARE the deliverable, and recording them IS acting on the finding: it is durable, it survives the run, it is findable later. That is the requirement. If a skill-polishing or refactor capability happens to be installed (e.g. `/skill-polish`), you MAY invoke it here to carry the recorded finding into a reviewed edit while the context is fresh, but that is a loose best-effort dependency, not a mandate, and the run does not block on it: the record stands on its own when no such skill exists. A human gate sits on anything that would be pushed.
   - **NEVER at reflect-time:** the repo/PR artifacts (fixtures, docs, code in the ticket's tree). That is the post-PR-churn boundary, and it is the ONLY category reflect must not touch.

   The dividing question, asked once per finding: "Am I confident this edit is strictly correct AND cannot break a sibling agent running right now?" Yes -> apply it, and say so in the reflect output. No -> propose + record. When you apply, the `MACHINERY:` entry doubles as the changelog (name the file + the fix) so the change is findable and revertible.

   Friction is accumulated for you along the way — the bundle's `friction[]` array is the running log the do-loop appended as the run hit each snag — so this step is not "go hunting"; it is "drive every entry that is already sitting there to a terminal action." Terminal means one of: applied + committed (APPLY NOW), or propose + record — the `MACHINERY:` entry + human note, which is itself the act of handling it (optionally carried further by a polish skill if one is installed). An entry that only gets re-logged with no recorded decision, or left parked for the user to chase later, is a reflect MISS. This is a closing action OF the reflection step, not a new step after it.

   "The harness ran clean, no friction" is a valid outcome — do not manufacture findings. But do not skip the step just because the ticket merged: a smooth merge can still hide a stage that cost three manual round-trips, and that is exactly the friction worth fixing while you still remember it.

2c. **Project knowledge reflection (lens C — gated, default ON).** SKIP this step only when `reflect_config.claude_memory` is false. It points OUTWARD at the project's own durable knowledge, the stores that compound across EVERY session in this project, not just flow runs (flow's `knowledge.jsonl` is flow-namespaced and only recalled at flow SessionStart; these are broader). Two targets, deliberately different blast radii:

   - **Project auto-memory (AUTO-WRITE).** For each durable, broadly-useful fact this run taught — the same novelty bar as lens A, but the subset that helps ANY session, not just a flow run (an environment gotcha, a naming surprise, a workflow constraint) — write it into THIS PROJECT's on-disk auto-memory, the memory directory your harness names in its memory instructions (system context). Resolve it to the project, not the worktree (the project memory is shared across worktrees). Follow that memory system's own discipline exactly: one fact per file with the required frontmatter, a one-line pointer added to its `MEMORY.md` index, `[[links]]` to related entries, and a dedup check against existing files (update the covering file instead of creating a duplicate; delete one that this run proved wrong). Do NOT copy flow-internal machinery findings (lens B) here — those belong in `knowledge.jsonl`, not the project memory. A fact can legitimately land in BOTH lens A (flow knowledge) and here (project memory) when it generalizes; that is expected, not duplication to avoid.

   - **Project `CLAUDE.md` / `AGENTS.md` (PROPOSE, do NOT apply).** If the run taught a durable PROJECT RULE worth codifying in the repo's checked-in `AGENTS.md` (a convention, a recurring gotcha, a workflow rule future contributors should follow), SURFACE it as a one-block proposal in the reflect output: `Proposed AGENTS.md addition:` followed by the exact text and the one-line reason. Do NOT edit `AGENTS.md`. It is a repo artifact, and editing it at reflect-time forces a commit that re-triggers the CI + review loop (the same post-PR-churn boundary step 2 enforces). The user applies the proposal on their own schedule. Emit nothing here if the run taught no repo-rule-worthy fact.

   "No broadly-useful project fact this run" is a valid outcome; do not manufacture project-memory entries or AGENTS.md proposals to fill the section.

3. For EACH extracted entry (0 or more), append to knowledge.jsonl:
   ```bash
   ${CLAUDE_SKILL_DIR}/scripts/memory_append.py \
     --type <LEARNED|DECISION|FACT|PATTERN|INVESTIGATION|DEVIATION> \
     --text "<entry body>" \
     --branch "$(git rev-parse --abbrev-ref HEAD)" \
     --ticket <KEY> \
     --workspace-root .
   ```
   - Exit 0 → appended.
   - Exit 1 → duplicate id (no-op). Fine; continue to next entry.
   - Exit 2 → lock contention. Retry once. If retry fails, log and skip.
   - Exit 3 → invalid type. Bug in your prompt; fix and retry.
   - Exit 4 → I/O error, or the workspace memory config is missing/invalid.
     Log and skip.

4. **Zero novel signal path**: if you genuinely have nothing to append, emit exactly:
   ```
   no novel signal
   ```
   Skip all writes.
   Do NOT manufacture entries.
   Reflect-empty IS a valid outcome; the compounding rate doesn't need every ticket to add an entry.

5. Check the ship state:
   ```bash
   ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py \
     --workspace-root . \
     is-shipped --key <KEY>
   ```
   - Exit 0 → JSON `{state, shipped_at, evidence, source}`.
   - Any non-zero exit (1 tracker error, 2 workspace config invalid) → skip ship-event observation; reflect still completes successfully.
     Ship-event observation is best-effort.

   `state` decides what happens next.
   The immutable ship-event file is *created* exactly once, the first time the backend reports the ticket as landed-but-not-yet-frozen:
   - `state == "not_yet_observed"` → CREATE the frozen event (step 6).
     This is the only state that triggers observation.
     `evidence` is a non-null dict here (`source == "live_backend_query"`); `shipped_at` is `null`.
   - `state == "shipped"` → the frozen `.flow/` event already exists (`source == "frozen_event_file"`).
     Already observed; skip step 6.
   - `state == "not_shipped"` or `"indeterminate"` → not landed (or no confirming evidence yet).
     Skip step 6; reflect completes.

6. ONLY when `state == "not_yet_observed"`, observe the ship event:
   - Read `<ticket-dir>/state.json` to get `run_id`:
     ```bash
     RUN_ID=$(jq -r '.run_id' <ticket-dir>/state.json)
     ```
   - Synthesize `shipped_at`.
     The `shipped_at` from is-shipped output is `null` for `not_yet_observed`, so do NOT pass it through.
     Use the tracker's transition timestamp if the evidence carries one, else now:
     ```bash
     # SHIP_JSON holds the captured stdout of the step-5 is-shipped call.
     SHIPPED_AT=$(jq -r '.evidence.closed_at // empty' <<<"$SHIP_JSON")
     [ -z "$SHIPPED_AT" ] && SHIPPED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
     ```
     The value MUST match `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$` (`observe_ship_event.py` rejects anything else).
     If `evidence.closed_at` is present but not in that exact form, normalize it to UTC `...Z` seconds precision before use.
   - Build the evidence JSON.
     The shape MUST be exactly these three top-level keys (`observe_ship_event.py` rejects any extra key; it owns `observed_at` and `observed_by_run_id`):
     ```json
     {
       "ticket": "<KEY>",
       "shipped_at": "<synthesized UTC ...Z timestamp>",
       "evidence": {<the evidence dict from is-shipped output, verbatim>}
     }
     ```
     `evidence` MUST be the object from is-shipped output (e.g. jira `{tracker, tracker_status, resolution}`; beads `{tracker, tracker_status, commit_sha, closure_reason, closed_at}`), passed through as-is.
   - Observe the ship event:
     ```bash
     ${CLAUDE_SKILL_DIR}/scripts/observe_ship_event.py \
       --ticket <KEY> \
       --evidence-json '<json>' \
       --run-id "$RUN_ID" \
       --workspace-root .
     ```
     - Exit 0 → primary ship-event file written. Continue.
     - Exit 1 → bad evidence JSON or a malformed `--run-id` (not 16 hex chars).
       Abort stage with status=failed.
     - Exit 2 → duplicate (dupe.<n>.json written).
       Continue normally; this is informational, not an error.
     - Exit 3 → I/O error, lock contention, or workspace memory config missing/invalid (intent log written).
       Surface warning; continue.

7. Stage completes with status=completed.

## Outputs

- 0..N new lines in `.flow/<namespace>/knowledge.jsonl`.
- 0..1 ship-event file at `.flow/<namespace>/ship-events/<KEY>.json` (or
  `.dupe.<n>.json` on EEXIST).

## Errors

- `reflect_inputs.py` exit 1 → state missing/corrupt, or diff environment broken (git not on PATH / bad cwd); abort.
- `memory_append.py` exit 1 → duplicate id; fine, continue.
- `observe_ship_event.py` exit 1 → bad evidence JSON; abort.
- `observe_ship_event.py` exit 2 → duplicate ship-event; informational, continue.

## Skip conditions

- Skipped entirely if `workspace.toml [pipeline.handlers] reflect = "none"`.
  Reflect is `required_when_compounding = true` in the registry so it appears in any workspace with `[memory] compounding = true`.
