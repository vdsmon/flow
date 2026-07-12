# spec verb

Full procedure for `/flow spec <ticket>`, `$flow:flow spec <ticket>`, and the bare
ticket default. SKILL.md keeps the narrative and the one gate; this is the
step-by-step. The rooted shorthand contract in `references/harness.md` applies to
every command and file path below.

The read-only front half fetches the ticket and designs the plan WITH the user, then
seeds a worktree, binds the execution context to its absolute root, and runs the
autonomous `do` tail in this same session. Backgrounding is a host-level choice.

If the adapter-supplied `arguments` carries `--auto` (alias `--aa` / `--yolo`),
follow the **Auto-approve path (`--auto`)** below instead of steps 1-7. That path
swaps the interactive gate for a headless Plan subagent that self-approves only when
it has no clarifying questions; otherwise it defers or blocks and exits.
Everything from the bootstrap onward is shared by the self-approve branch; the defer-and-exit branch never reaches the bootstrap.

1. **Select the plan gate.** The front half must perform no repository writes.
   Claude Code enters native plan mode. Codex uses native Plan mode when already
   active; otherwise it uses the soft turn boundary. A generic adapter also uses the
   soft boundary. The soft path still ends the turn on the plan and waits for explicit
   approval before step 6.

2. Resolve the ticket key (positional `arguments`, else
   `.flow/flow branch-ticket --workspace-root .`).

3. Fetch ticket context **into the conversation** — do NOT write files (plan
   mode forbids it):
   ```bash
   .flow/flow tracker --workspace-root . get --key "$KEY"
   ```
   Read the stdout.
   Explore the codebase read-only (Read/Grep/Glob, or a subagent).
   **Recall against the ticket text + your intent (plan-phase, read-only).** This is the SOLE recall now (SessionStart no longer recalls). Write the ticket title+body to a temp file, PREPENDED with a short (1–2 line) intent preamble naming the form / domain / component you are about to touch and the shape of the change (the risk), then query recall keyed on the whole file — a pure READ, legal in plan mode, NO `--record-pending` here:
   ```bash
   QF="${TMPDIR:-/tmp}/flow-recall-$KEY.txt"   # intent preamble + ticket title + body (adapter exact writer)
   B=$(git branch --show-current)
   .flow/flow recall --query-file "$QF" \
     --semantic --top-n 30 --branch "$B" --workspace-root .
   ```
   Pass the query via `--query-file` (not a shell positional — avoids the `"`/`\`/newline hazard). The intent preamble AUGMENTS the raw ticket text, it never replaces it — the identifier-rich ticket body stays the BM25 signal, while the preamble names the domain so the semantic side clusters prior work on the same form / component (e.g. "Working on the IVA form's validation; risk: rounding in the F.20 line totals"). `--semantic` is a no-op when the workspace has not opted in (recall stays pure BM25). Weave the returned entries into the plan. The matching WRITE (`--record-pending`) happens post-gate in step 6.
   **Verify any content/drift finding against the default base, not the working checkout.** General orientation reads stay on the working checkout via the Read tool (you do NOT need to `git show` every file). But the moment you would CITE a content/drift finding in the plan, or derive a `--planned-files` entry (step 6) BECAUSE OF a file's current content, re-read that specific file at the freshly-fetched default base first. The tail branches off `@default` (`origin/<default>`, fetched fresh) while this checkout can lag `origin/main`, so a drift seen here may already be fixed upstream and the planned fix would land as a no-op (flow-749). Resolve the base the way `.flow/flow worktree create --base @default` does and read the base version:
   ```bash
   git fetch --quiet origin
   DEFAULT=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD)   # e.g. origin/main
   git show "$DEFAULT:<path>"   # the base version of the file you'd cite
   ```
   The `git fetch` is read-only by discipline (only remote-tracking refs / FETCH_HEAD, safe under plan mode, same as the `aws s3 cp` artefact step 5 lists). A content/drift finding is cited at plan time and may stamp `planned_files`, so it must be verified against the right base now and cannot be deferred to implement.

4. Iterate the implementation plan with the user: goal, files to change, approach, test strategy, risks.
   This is the same depth a `subagent:Plan` handler would produce — but interactive, so the user shapes it.

   **When the files-to-change mapping is a wide mechanical refactor that cannot land as one green PR** — a rename / API change / column migration whose call sites span many packages, where the tree goes red partway through — do NOT force it into this one run. Stop spec and recommend `/flow slice <KEY>` instead: it maps the blast radius and mints an expand→migrate→contract ladder of children that each land green alone (`references/verb-slice.md`). A refactor that DOES fit one green PR stays here.

   **Lavish plan-review surface — the DEFAULT presentation for this loop.** Render this iterate loop as an interactive HTML review surface via the `lavish-axi` CLI whenever the gate below holds. Plain prose is the FALLBACK for a failed gate, never a coequal choice: "the user probably prefers prose" is not part of the gate, and skipping on a passing gate is a defect. Anti-pattern this wording directly fixes: under the earlier "consider rendering (optional)" phrasing, interactive spec runs read this block and rendered plain prose anyway, every time, with nothing in the transcript to show the gate was ever evaluated. The gate has two legs; check leg (b) NOW, as the first action of this step, with a real command rather than a judgment call:
   ```bash
   command -v node && command -v npx   # leg (b): both must resolve
   ```
   (a) this is the interactive step-4 iterate loop itself. `--auto` runs the headless `Plan` subagent and never reaches step 4, so it is structurally excluded already, which is what guarantees a human is on the other end. Foreground/background state is not the signal: a backgrounded interactive session still has a human who can attach via the harness's own cockpit, e.g. Claude Code's `claude agents`, at any point. (b) the presence check above succeeds. This is the best-effort proxy for `lavish-axi` being runnable and, by extension, a local browser the human can actually open. **`${CLAUDE_JOB_DIR}` (a Claude Code background-job detail; see `references/harness.md`'s Capability matrix and Waits, questions, and notifications) is deliberately NOT part of this gate.** Backgrounding a session (via `/bg`, or a harness/user workflow that always runs sessions backgrounded under a daemon) does not mean nobody is watching, so it must not disable a human-facing review surface. `${CLAUDE_JOB_DIR}` still correctly gates the unrelated Claude Code `--auto` self-teardown recipe in `references/verb-do.md`; that one is genuinely headless-only, since `--auto` never reaches step 4 in the first place. A failed leg skips lavish and proceeds straight to the plain-prose plan below, announced in one line and never silent (see the degradation contract's skip line below).
   **Render/converge loop, markdown-first.** The markdown plan drafted in this step stays ground truth throughout — the HTML is a disposable rendering of it, never the reverse. Build it from the drafted markdown (the lavish `plan` + `comparison` + `input` playbooks) to a THROWAWAY temp path, `${TMPDIR:-/tmp}/flow-lavish-$KEY/plan.html`, NOT `.lavish/` (rationale below). Author the file via a Bash heredoc (`cat > "$TMPDIR/..." <<'HTML' ... HTML`) — never the Write/Edit tools, which plan mode blocks regardless of target path (a Write attempt here would trip the degradation contract and silently kill the surface). Design source follows lavish's documented priority, never hand-rolled ad-hoc CSS: the user-requested look first, else the subject project's design system, else the `npx -y lavish-axi@0.1.35 design` DaisyUI fallback.

   Independent of which design source you pick, MANDATORY in every authored artifact: paste lavish's layout-safety CSS snippet verbatim into the HTML `<head>` (the `layout_safety_snippet` that `npx -y lavish-axi@0.1.35 design` prints). `lavish-axi design` frames it as optional; for flow's dense authored surfaces — diffs, badges, code, tables, the overflow-prone case — it is REQUIRED. DaisyUI's `.label` does NOT wrap long text by default; the snippet's `overflow-wrap: anywhere` set already INCLUDES `.label`, so mandating it verbatim IS the fix (a verdict-form helper line went 1113px wide at an 833px viewport and kept lavish's open-time curtain up across opens — flow-qdal). Verbatim, for the pinned 0.1.35:
   ```
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

   Then:
   - `npx -y lavish-axi@0.1.35 <html>` opens or resumes the browser review session — PINNED to this version, unlike the installed `/lavish` skill's unpinned `npx -y lavish-axi`, which is the exact npx supply-chain exposure a pinned version closes. **Pin cadence: pin-and-hold.** The pin is a supply-chain freeze, not a currency target — hold it, do not chase latest. Re-pin only deliberately, when a needed feature lands (e.g. a poll flag that omits `dom_snapshot` — none exists through 0.1.38 as of 2026-07-10) or on a periodic review. A re-pin is a coupled edit, never a bare version-string swap: changelog glance + one smoke render and poll cycle (re-verify the `dom_snapshot` single-line poll format the strip below depends on, and re-fetch the version-coupled `layout_safety_snippet`) + sync EVERY pin site (this file + `references/review-packet.md`) + the user-settings autoMode Bash pin (flow-ym48).
   - `npx -y lavish-axi@0.1.35 poll <html>` long-polls for annotations, queued prompts, and layout_warnings — ONE persistent poll, run as a background task, stays armed for the whole loop (it sits silent until the user acts). Queued feedback is never lost, so the poll is never killed or re-armed around a re-render: killing it shows the user "no agent listening". **Strip the redundant `dom_snapshot` from every poll read** — pipe EVERY poll invocation (the bare `poll` AND the `poll --agent-reply` re-arm below) through a line-strip that drops the top-level `dom_snapshot:` line: `npx -y lavish-axi@0.1.35 poll <html> | python3 -c 'import sys; sys.stdout.writelines(l for l in sys.stdin if not l.startswith("dom_snapshot:"))'`. The poll return carries a ~13-18KB `dom_snapshot` per feedback event the agent never needs (it already has the source markdown), ~19KB/turn of pure context burn (flow-xypg). Poll stdout is a YAML-like presenter (NOT JSON) and `dom_snapshot` renders as a single top-level line (embedded newlines escaped inline), so the line-strip is exact — a `json.load` filter would throw on the YAML and, failing open, pass the raw payload straight through: a silent no-op. The strip is stdout-only, so poll's own stderr diagnostics (its interrupt / re-run-me notice) still surface. The strip is coupled to the pinned version's single-line rendering; re-verify it on any re-pin (the pin note above).
   - On returned annotations, revise the MARKDOWN plan (still ground truth), then re-render the HTML from the revised markdown — lavish watches the artifact file and live-reloads the re-render in place (scroll preserved), so never re-run the open command mid-loop; the HTML is never edited as source, only regenerated each cycle; `poll --agent-reply "<msg>"` replies inside the browser to keep the loop going. Mirror every RESOLVED decision into the markdown plan the moment it lands: the artifact mutates in place with no history, so an unmirrored resolution is lost on the next re-render.
   - Convergence is the USER's built-in end-session signal, not an agent decision: the poll returns `status: ended` carrying the final feedback batch (the browser's **Send & end session** submits the queued prompts + user-ended attribution together). WAIT for that return before step 5's `ExitPlanMode` — even when the session is backgrounded, the armed poll's return is the wake signal — never pre-empt the gate. A user-ended session is never reopened without `--reopen` and an explicit ask. Agent-side `npx -y lavish-axi@0.1.35 end <html>` is ONLY for agent-initiated termination (a mid-loop degradation), never the normal convergence path.

   **Fork surfacing: surface, not bypass.** The `input`-playbook controls render ONLY the decision forks that survive the "De-fork before you ask" investigation directly below. Investigate-first still holds, and a fork closeable by reading stays unfinished reading, not a control. When lavish is unavailable, forks fall back to the adapter's user-input capability.
   **Degradation contract — fallback content byte-identical to today, plus one visible line.** When the gate above fails for ANY reason — `--auto` (never reaches step 4, so no announcement either), `npx`/`node` absent, offline, a plan-mode write/exec refusal, or any non-zero `lavish-axi` exit at any point in the loop — fall back to the plain-prose plan presentation and proceed with the rest of step 4 (de-fork, e2e recipe, lane proposal), then the "Confidence rating" paragraph and step 5's `ExitPlanMode`. The fallback differs from the pre-lavish presentation by exactly one line at the top: `Lavish: skipped — <reason>` (or, when the loop was already running, `Lavish: degraded mid-loop — <reason>`; the markdown plan is ground truth, so nothing is lost). That line is an announcement, never a prompt for permission to proceed — a skip used to be silent, and silence is precisely what let gate bugs and never-firing wording go unnoticed for thirteen releases (0.82.0 through 0.94.9). Lavish is a purely additive branch: nothing about it can block or fail this gate. Boundary, binding on future edits: lavish is an ADD-ON, never a dependency — no flow verb, stage, or script may require it, reference it outside its sanctioned sites (a closed set: this block, plus the gate-2 review packet — `references/review-packet.md` and its pointer / skip-line mentions in `references/stage-review_loop.md` and `references/verb-do.md` — and the revision triage board — the `## Revision triage board (/flow revise)` section of `references/review-packet.md` and its pointer / skip-line mentions in `references/verb-revise.md`), or degrade anything but this rendering when it is absent; lavish-absent IS the original plan-mode implementation plus that one skip line.
   **Curtain / live-reload fragility (lavish-axi 0.1.35).** Observed in flow-qdal: a wide horizontal overflow can keep lavish's open-time curtain (loading overlay) up across opens (the user sees "nothing loads"), and live-reload churn can kill the iframe SDK send path (**Send & end session** triggers nothing). The mandated layout-safety snippet above is the primary prevention; if a surface still hangs, recover by restarting the lavish server and re-opening with `--no-gate`: `npx -y lavish-axi@0.1.35 --no-gate <html>`. A degradation-recovery path, not a normal step.
   **Plan-mode footing.** This whole surface runs inside plan mode, so it leans on the same precedent step 3 already established: step 3 writes `$QF` to TMPDIR and runs `git fetch` under plan mode, "read-only by discipline" — the lavish HTML likewise writes only to its own throwaway TMPDIR path and runs a local server that touches no repo file, so the same footing covers it. A plan-mode write/exec refusal is listed among the degradation triggers above precisely because a stricter harness could still deny that footing.
   The loop above converges — the run WAITS for the user's `status: ended` return, it never pre-empts the gate — BEFORE the rest of step 4 (de-fork, e2e recipe, lane proposal), the "Confidence rating" paragraph, and step 5's `ExitPlanMode`; `ExitPlanMode` remains the one gate, lavish never replaces it. The TMPDIR HTML is disposable: the user's end-session (or an agent-side `end` on a mid-loop degradation) closes it around or before `ExitPlanMode`, never treat it as `$PLAN` at bootstrap (step 6's `$PLAN` is the markdown) — it never rides into the worktree or a commit. (Rationale: `.lavish/` is NOT gitignored in this repo, and step 4 runs in the main checkout before the worktree exists, so a repo-tree HTML there could ride into a commit — hence TMPDIR, not `.lavish/`.)

   **De-fork before you ask: investigate first, surface only what survives.** Before surfacing ANY clarifying question through the adapter's user-input capability, first try to dissolve it by read-only investigation. Classify each candidate question as a **fact** or a **decision**. A **fact** (*dissolvable*) has an answer in a reachable source (the code via the adapter's read/search tools or an independent exploration agent, prior runs via recall, authoritative library documentation, the web, logs); find the answer, fold it into the plan, and do NOT ask. A **decision** (*genuine tension*) needs user-only information or a product/preference decision the repo does not encode; surface it with specifics. Only **decisions** reach the user; a lookupable fact is never surfaced. This is the same closeability test the `--auto` adjudication applies (the "closeable in-run vs uncloseable" classification later in this file): a fork the planner can close by reading is unfinished reading, not a question for the human. Most questions dissolve under investigation; spoon-feed the user by raising only the irreducible ones. (The `--auto` path already enforces this via its `## CLARIFYING QUESTIONS` block + the close-the-holes revision round; this brings the same discipline to the interactive iterate loop.)
   **Unless the workspace explicitly disabled e2e** (`workspace.toml [pipeline.handlers] e2e = "none"`), the plan MUST settle the **e2e recipe** at this point, while you (and any live tracker/AWS auth) are present. Read the absolute `<skill_root>/references/e2e-recipes.md` authoring template first. Then: if `<main-root>/.flow/e2e-recipes.md` exists, author the recipe FROM its decision table (pick the row matching what the ticket touches, fill in the fixture), confirming with the user. If it does NOT exist, EXPLORE the repo read-only (CI config, mise/make/npm tasks, docker-compose, test layout), PROPOSE 1-3 concrete candidate recipes (runner, exact command, env-prep, fixture, expected pass signal) and settle one with the user; after the gate (step 6, normal mode) SEED the settled pattern into `<main-root>/.flow/e2e-recipes.md` so the next ticket starts from a cookbook.
   A ticket with no meaningful e2e settles that consciously too — the recipe value becomes `skip: <reason>` or `test-ci-only`, the deliberate exception, never the convenient path.
   The bootstrap in step 6 **refuses** when e2e is enabled and no recipe is passed, so do not skip this.

   **Propose the verification lane (a `## Lane` section).** Phase 2 of the express-lane machinery brings the lanes to the interactive path (phase 1 wired `--auto`/drain via tier labels). Settle which lane the tail runs and record it as a `## Lane` section with one line of justification. **Be conservative:** propose `express` ONLY for behavior-preserving, tightly-bounded work (a doc-drift fix, a proven-dead-code deletion, a comment/typo correction — nothing new to pin); `light` for a small behavior-changing change; `full` (the default) for anything broader, cross-cutting, or when unsure. The user approves the proposal with the plan or overrides it at the gate — `--lane express|light|full`, or in plain words ("run this express"); an explicit `--lane` on the command pre-sets it (still surface the effective lane in the plan). **Compute the EFFECTIVE lane now** = explicit-or-proposed, THEN hot-clamp: a hot change (a guard file in `planned_files`, or a `hot`-labelled bead) is forced to `full` regardless. Compute it BEFORE the confidence-rating decision below, so a forced `--lane express` on a guard-file change still takes the full lane and the full probe.

   **Skip the confidence rating when the EFFECTIVE lane is `express`/`light`.** Explicit
   approval at the selected adapter's boundary IS the vetting (Claude Code implements
   that boundary with `ExitPlanMode`). This makes the same trade as the `--auto` tier
   short-circuit: drop the re-judgment, never the net; CI, the review bot, and the
   deterministic safety machinery still run on every lane. The rating is the
   **`full`-lane** gate.

   **Confidence rating (MUST for the `full` lane, before step 5's gate; assessed independently, not self-scored).** A plan's author is the worst judge of its confidence; optimism bias makes a self-reported score self-justifying. Hand it to a second mind using `references/harness.md`'s independent-reasoning order: Claude Code prefers its advisor capability when available, then a fresh independent agent; Codex uses a fresh collaboration agent or second model call and never passes a Claude model parameter; a generic adapter uses an independent call or follows the documented defer behavior when independence is unavailable. Give the assessor the ticket context, drafted plan, and this rubric: **Score (0-100%)**, **Proven** (bullets directly verified: code read, spec quoted, real data/DB inspected), **Inferred** (from convention / naming / a 1:1-chain argument), **What would raise it** (concrete reachable artefacts). Record the result as the plan's `## Confidence` section, attributed to the assessor. Library-API claims must be verified from available authoritative documentation, never left under "Inferred". **A hotness/guard-file classification is a probe to run, never a confidence input to assert:** neither the plan nor the assessor can be trusted to call a change "non-hot" (on flow-94l6 both asserted non-hot on a `SKILL.md` change), so the self-approve branches classify hotness ONLY via `.flow/flow triage decided --files` on the derived `planned_files`, never from a model's own read.

5. **Present the plan for approval: Gate 1, the one human gate.** Claude Code uses
   `ExitPlanMode`; Codex uses the native Plan boundary when active. Otherwise present
   the full plan, STOP, and wait for explicit approval before any repository write.
   **(Full lane only) Gate on the rating: < 90% → do not cross the selected adapter boundary yet.** First exhaust every reachable read-only artefact through the adapter's read/search capabilities, then re-run the assessor. For a gap that needs user action (an SSO refresh, a bucket name, an owner's confirmation, an internal doc), ask through the adapter's user-input capability with specifics; never wave at it. Present only at >=90%, or when every reachable source is exhausted and the residual is documented as a risk the user can weigh. Anti-pattern this directly fixes: producing the confidence number only after the user asks for it. The rating is part of the plan, surfaced unprompted, every time.
   **For an effective `express`/`light` lane there is no rating; explicit approval at
   the selected adapter's boundary itself IS the gate (no threshold).** Claude Code
   uses `ExitPlanMode` for that boundary. The user may also override the lane at this
   gate (`--lane …`, or in words); a hot change can never be lowered below `full`
   (re-clamped at bootstrap).
   On approval you return to normal mode.

6. (Normal mode) Persist the approved plan and bootstrap the worktree.
   Bootstrap always fetches origin first (never branch off a stale ref), then resolves `--base`. Interactive: pass the current branch — on a feature branch it stacks (stacking is a feature), but on the local default branch (or a detached HEAD) it redirects to the freshly-fetched `origin/<default>` so a lagging local `main` never leaks already-merged commits into the PR. Autonomous (`--auto`): pass `--base @default` AND `--auto`, so the run branches off the fetched default branch (never inheriting the launcher's HEAD) and the bootstrap code-enforces the hot hard-floor (see step 5's auto-approve branch).
   Allocate a temporary file through the adapter and capture its returned absolute path
   as the logical `plan_path`. Exact-write the approved Markdown plan to `plan_path` and
   verify it exists. Keep that absolute value in orchestration context; do not rely on a
   `$PLAN` variable or a prior shell call. Materialize the logical ticket key and
   `plan_path` as literal arguments in the one bootstrap call:
   ```bash
   .flow/flow worktree create \
     --ticket "<KEY>" \
     --plan-from "<absolute plan_path>" \
     --base "$(git rev-parse --abbrev-ref HEAD)" \
     --branch "feat/<KEY>-<slug>" \
     --main-root . \
     --planned-files "<comma-separated files the plan will touch>" \
     --commit-type <feat|fix|chore|...> \
     --commit-summary "<one-line summary from the plan>" \
     --covers "<comma-separated sibling keys this run co-delivers — omit when not grouping>" \
     --e2e-recipe "<the e2e recipe from step 4 — omit ONLY when the workspace explicitly disabled e2e (none)>" \
     --lane "<the effective hot-clamped lane from step 4 — omit when full; interactive-only>"
   ```
   Parse stdout JSON immediately. Set `run_root` to the absolute `result.worktree`
   value and set `facade` to the absolute `<run_root>/.flow/flow`. Verify the launcher
   exists and the returned branch owns that worktree. Every subsequent command uses
   this new `run_root` as explicit workdir and the new absolute `facade`; do not run
   another command from the pre-bootstrap checkout by default.
   Derive `<slug>` from the ticket summary: keep it **short and descriptive — aim for ~3 meaningful words, ≤24 chars**. Drop articles/prepositions/filler and anything already in `$KEY`, abbreviate long words (`feature`→`feat`, `invoice`→`inv` only when still legible). The slug rides into the worktree dir name (`branch.replace("/","-")`) and the status line, so a 33-char slug like `tenant-filter-reinf-invoice-query` should land as `tenant-inv-filter`. Derive `--planned-files` from the plan's "Files to change" list — which (per stage-plan.md) already includes any anticipated NEW test file paths the TDD implement will create, so the stamped `planned_files` covers them.
   **Cross-check every stamped TEST path against the tree AND recall (the test-path analogue of the drift-vs-base re-verify in step 3).** However the "Files to change" list was derived — the interactive Plan, or an `--auto` branch's self-derive below — before you pass `--planned-files` to `create`, take each entry that is a test path (`test_*.py` / `*_test.py`, or one under a `tests/` dir) and run `git ls-files -- <path>` (does the exact file exist yet) and `git ls-files -- <dir>/` on its directory (does that dir hold a tracked test sibling). A TDD test file legitimately does not exist yet, so an empty first probe is expected — the signal is the second probe crossed with recall. Three dispositions: the dir holds a tracked `test_*.py`/`*_test.py` sibling → stamp as-is (a normal new test beside its neighbours); no sibling, but a step-3/6 recall entry names a test path for the same module → the path was stamped by analogy to the source tree into a test-less dir while the real test lives in the recalled location (a `tests/` mirror) → re-root the entry onto the recalled path; no sibling AND no recall corroboration → keep the path but record a one-line note of the uncertainty in the plan text carried into the run, and do NOT drop it unless you can positively identify it as a phantom (a genuinely new test location — a fresh dir with no siblings yet, absent from recall — is legitimate, and dropping it would unlist the file and fire the post-implement reconcile on essentially every run; that reconcile is the backstop for any residual mis-stamp). This complements the bootstrap's `_typo_planned` guard in `flow_worktree.py`, which only WARNS when a planned path AND its parent dir are BOTH absent: a test path stamped into an EXISTING but test-less source dir (parent present) never trips that guard, so this derivation-time cross-check is the net for the test-less-sibling case the parent-dir guard cannot see.
   **`--covers` (grouped runs only):** pass it when this run folds sibling tickets into one piece of work (the SKILL.md multi-key fold). `$KEY` is the LEAD and owns identity; the covers ride its frontmatter and the commit/PR/reflect steps fan out to close each. Each cover must be a distinct, live, non-epic ticket — create refuses (exit 2 self-reference, exit 6 terminal, exit 7 epic) otherwise. The plan must have settled the lead + cover set at the gate (the cover scopes are part of the approved plan). Omit entirely for a normal single-ticket run.
   **Auto-derive (the `group` defer path):** when no `--covers` was passed on the command, a grouping may have been persisted earlier by `/flow group` (a `flow-group covers:` marker comment on the lead). Derive it before building the create command:
   ```bash
   .flow/flow group-persist derive --lead "$KEY" --workspace-root .
   ```
   A non-empty `covers` in the JSON means a deferred grouping exists — surface it at the gate so the human confirms it is still right (the plan must cover those scopes), then pass it as `--covers`. Empty `covers` → a normal single-ticket run; do not ask. An explicit `--covers` on the command always wins over the marker.
   `--e2e-recipe` carries the recipe settled in step 4 (runner + command + env-prep + fixture + expected, or `skip: <reason>` / `test-ci-only`); pass it unless the workspace explicitly disabled e2e, and omit it only when the handler is `none`.
   **Cookbook seeding happens here, post-gate.** When step 4 explored and proposed fresh recipes (no `<main-root>/.flow/e2e-recipes.md` existed yet), write the settled pattern into that file now — machine-local, since `.flow/` is gitignored in user repos.
   **`--lane`** carries the effective hot-clamped lane settled in step 4: pass `--lane express|light` to stamp it into frontmatter (the implement stage relaxes mandatory-new-test for behavior-preserving `express`, reflect collapses to friction-only), omit it for `full` (the absent-field default). It is **interactive-only** — an `--auto` run ignores any `--lane` and derives the lane from the bead's tier labels (`triage.py lane`). `create` re-enforces the hot clamp itself (a guard-file `planned_files` set, or a `hot`-labelled bead, lands `full` even if you pass `--lane express`), so the stamp can never under-gate a hot change.
   The bootstrap seeds state (plan pre-completed, ticket left pending), injects the plan, stamps `planned_files` + `commit_type` + `commit_summary` (+ `e2e_recipe` / `covers` when given) into frontmatter (so the implement pre-hook, the commit stage, and the e2e stage never pause to ask the user — which is what lets the tail run unattended if you background it), points the worktree's memory store at this checkout's `.flow` (shared, so memory compounds across worktrees), copies gitignored config, and `mise trust`s the worktree.
   Unless the workspace explicitly disabled e2e, omitting `--e2e-recipe` makes create exit 2 (`_ConfigError`) — go back to step 4 and settle the recipe.
   **Hot hard-floor (code-enforced).** When the bootstrap is autonomous (`--auto`, or a `@default` base) on a beads tracker, create refuses (exit 2) if `--planned-files` trips `is_hot_change` (a guard/safety file, or a `hot`-labelled bead) and the bead carries no recorded `DECISION:`/`TRIAGE-DECISION:` comment. This is the floor's real enforcer: step 5's prose only carried it in the adjudication/decided sub-branches, so a clean re-plan could self-ship a hot change past it (flow-aen). On this refusal the auto path treats it as a hot block (defer-stem comment + `bd status blocked`, per step 5), exactly as if adjudication had blocked. Interactive runs are not gated here — `ExitPlanMode` is the human floor. `[evolve] adjudicate_hot = true` (maintainer self-target, default off) makes `create` skip this refusal, so a hot change bootstraps on an advisor proceed; the merge-time guard-property review remains the gate.
   **Duplicate-claim refusal (exit 4).** `create` transiently holds a canonical per-ticket bootstrap claim (a flock on the main checkout's `.flow/tickets/$KEY.claim`, released at bootstrap exit) and refuses with **exit 4** when a live sibling run already holds this ticket — a sibling worktree on the ticket's feature branch with a live (or corrupt) run lease, or a seeded non-terminal `state.json`. This is NOT the exit-2 hot block: the defer/block recipes above never apply to an exit 4.
   - Interactive: exit 4 → a live sibling run already holds this ticket (the message names its worktree + state). Surface it + the `/flow recover $KEY` hint; STOP. Do not retry, do not reap by hand.
   - `--auto`: exit 4 → a sibling run owns this ticket. Emit one terse `superseded <KEY>: sibling run live` line and STOP — exit silently-clean: NO `tracker_cli comment`, NO `bd update` (no defer, no block), NO friction entry, no follow-up bead. The sibling owns the bead and its status.
   **Terminal-bead refusal (exit 6).** `create` re-reads the bead's authoritative status at the bootstrap chokepoint (tracker-agnostic, before any git mutation) and refuses with **exit 6** when it is terminal (normalized `done`/`cancelled`). This catches the flow-d6gq case: a bead that was open at spec-fetch but closed during planning (e.g. a parent epic merged). Unconditional — interactive and `--auto` alike, since bootstrapping a closed bead is wrong either way. Fail-open is narrow: a genuine tracker read *exception* proceeds (a flaky read never strands a legit run), but a successful-but-incoherent status read also refuses. This is NOT the exit-2 hot block and NOT the exit-4 dup-claim: the bead is legitimately done, so there is nothing to defer or block.
   - Interactive: exit 6 → the bead is closed/done; surface "bead <KEY> is closed — nothing to bootstrap" + the reopen hint; STOP.
   - `--auto`: exit 6 → emit one terse `closed <KEY>: bead already terminal` line and STOP — exit silently-clean: NO `tracker_cli comment`, NO `bd update`, NO friction entry, no follow-up bead. The bead is done; its status is already correct.
   **Epic refusal (exit 7).** `create` reads the bead's issue type at the bootstrap chokepoint (tracker-agnostic, before any git mutation) and refuses with **exit 7** when it is an epic (`issue_type`/type `epic`). This mirrors `evolve_select.py`'s unconditional `issue_type != "epic"` filter at the one chokepoint that filter never covered: a manual or misrouted `/flow <epic> --auto`. An epic is a container — decompose it via the expand recipe (`verb-evolve.md` §E), then run each child key. Bootstrapping the epic directly would cram-ship fragments of an unaccepted epic as one PR (the ouroboros `verb-evolve.md` §epic names). Unconditional — interactive and `--auto` alike. Fail-open matches exit 6: a tracker read *exception* proceeds; a successful read of a non-epic type proceeds normally. This is NOT exit-2/4/6: the bead is live and legitimate, just at the wrong altitude.
   - Interactive: exit 7 → surface "bead <KEY> is an epic — expand it (`/flow evolve epic <KEY>` §E), then run a child" + STOP.
   - `--auto`: exit 7 → emit one terse `epic <KEY>: not a single-PR unit` line and STOP — exit silently-clean: NO `tracker_cli comment`, NO `bd update` (leave the epic OPEN — `--status deferred` would wrongly shelve a build-now epic and mutate maintainer accept/shelve state; defer's loop-prevention is moot since drain already filters epics), NO friction entry, no follow-up bead.
   **HITL refusal (exit 8).** When the bootstrap is autonomous (`--auto`, or a `@default` base) on a beads tracker, `create` refuses (exit 8) if the bead is marked `hitl` (human-in-the-loop) and carries no recorded `DECISION:`/`TRIAGE-DECISION:` comment: a decision-bound bead resolves only through a live exchange, so unattended pickup is structurally wrong. It shares the hot floor's single `triage.decided` probe and is checked FIRST — `[evolve] adjudicate_hot` does NOT lift it (that flag lifts only the hot half). A recorded decision clears it (the human input the label demanded now exists). This is NOT the exit-2 hot block: the disposition is a defer, not a safety block. On the disciplined `--auto` path step 4's early refusal already deferred before this floor is reached, so exit 8 is the backstop for a label that landed late (a probe→bootstrap race) or a manual/misrouted `create --auto`.
   - Interactive: NOT gated — the label only gates unattended pickup, and `ExitPlanMode` is the human floor.
   - `--auto`: exit 8 → treat it as a defer (same recipe as step 5's defer-and-exit): comment the defer stem + `[defer-reason: open-question]` tag (reason "marked hitl (human-in-the-loop): resolves only through a live exchange; run interactively without --auto, or /flow triage <KEY> \"<answer>\" to record the decision and clear the label"), `bd update "$KEY" --status deferred`, emit one terse `hitl <KEY>` line, and STOP. No worktree, no PR.
   Surface any `WARN` lines (e.g. mise trust failures — the tail would die on the first `mise run`).

   **Record the recalled ids (post-gate WRITE).** Now that the selected adapter's
   approval boundary is crossed, record the plan-phase recall into `recall-pending` so
   the dispatcher promotes it into the run's `recall-log.jsonl` (and reflect surfaces
   it as `recalled_entries`). Use the logical absolute query-file path captured in step
   3 and the logical key/branch values; do not depend on shell variables surviving the
   gate. Same query text as step 3, but with `--record-pending`:
   ```bash
   .flow/flow recall --query-file "<absolute query_path>" \
     --semantic --top-n 30 --record-pending \
     --branch "feat/<KEY>-<slug>" --ticket "<KEY>" \
     --workspace-root "<the worktree path the bootstrap printed>"
   ```
   **Critical: target the WORKTREE, not the main checkout.** `--workspace-root` must be the bootstrap's `result.worktree` path and `--branch` must be the `.flow/flow worktree create --branch` feature branch (NOT `$B`/the integration branch). The dispatcher's `init` promotes from inside the worktree (`recall_pending.promote_matching` with `cwd=worktree`, `branch=feat/$KEY-<slug>`, reading the worktree's `recall-pending.jsonl`), and its promotion rules are exact matches on branch + cwd + a head-sha-ancestor check. Recording against the main checkout (`--workspace-root .`, `--branch "$B"`) writes a DIFFERENT `recall-pending.jsonl` with mismatched branch/cwd, so nothing promotes and `recalled_entries` stays empty. The step-3 READ stays main-root (it is only a query, it matches nothing). `--auto` has no plan mode, so it runs this single `--record-pending` form once here (the step-3 READ and this WRITE collapse to one call, against the worktree). Best-effort: a failure here never blocks the bootstrap.

7. **Bind the worktree and continue in this same session.**
   The rooted context was updated immediately after bootstrap. Claude Code may now call
   `EnterWorktree(path="<run_root>")` as an ergonomic optimization, then verify it did
   not create or select a different worktree. Codex and generic adapters need no cwd
   switch. On every adapter, pass `run_root` as each command's explicit workdir, invoke
   the absolute `facade`, and use absolute worktree paths for reads and edits. A failed
   native switch is not repaired with `cd`; rooted execution is already sufficient.

   Continue straight into the `do` orchestration loop for `$KEY`. Its `init` resumes
   idempotently under the seeded `run_id` (plan already completed, ticket pending) and
   lands on `implement`, which reads `plan.out`. State on disk makes this identical
   whether spec flowed in or `do` was invoked standalone. Backgrounding remains a
   host-owned user choice; see `references/background-pipeline.md`.

## Auto-approve path (`--auto`)

For tickets you already know are simple and whose body is descriptive: auto-approve the plan WITHOUT your intervention, but ONLY when the planner has no clarifying questions.
This is a conditional gate, not a blanket skip. It branches on the headless planner's output: a clean, high-confidence plan self-approves; a shaky one first spends exactly ONE close-the-holes revision round (step 4); only a wall that survives it — user-only information, or a change genuinely unsafe to auto-ship — defers/blocks the ticket in place and exits.
It replaces interactive steps 1-5. The self-approve branch then runs shared steps 6-7 (bootstrap + enter worktree) exactly as above; the defer-and-exit branch runs neither.

1. **Do NOT `EnterPlanMode`.**
   The headless path performs only reads until the intended bootstrap write — there is no interactive plan to gate, so the plan-mode lock is unnecessary.
   Keep the reads read-only by discipline; the first write is the bootstrap in shared step 6 (or, when the planner cannot self-approve, the defer-and-exit comment in step 5, the only non-bootstrap write).

2. Resolve the ticket key (positional `arguments` minus the flags, else
   `.flow/flow branch-ticket --workspace-root .`); same as step 2.

3. Fetch ticket context into the conversation via `.flow/flow tracker --workspace-root . get --key "$KEY"` (read the stdout); explore the codebase read-only; run the plan-phase READ recall keyed on the ticket text + a short intent preamble (the augmented `.flow/flow recall --query-file ... --semantic --top-n 30` form from interactive step 3, NO `--record-pending`) and weave the entries in, as in step 3. The matching post-gate WRITE (`--record-pending`) runs once in shared step 6.
   The drift-vs-`@default` rule (verify any cited content/drift finding against the freshly-fetched default base) lives in the `stage-plan.md` embedded into the Plan subagent in step 4; it is that subagent's plan, not this orchestrator's own explore, that derives `planned_files`, so the rule is enforced there.

4. **Decided-mode probe — then the headless plan.**
   First probe whether the maintainer already triaged + reopened this bead with a recorded decision. Without this, an `--auto` relaunch re-defers on the exact question already answered (the triage→reopen→re-defer loop never converges):
   ```bash
   .flow/flow triage decided --workspace-root . --key "$KEY"
   ```
   It always emits one JSON object `{"decided": bool, "answer": str|null, "is_hot": bool, "hitl": bool}` (never raises; a bd-read failure reads as `decided:false, is_hot:true, hitl:false`). When `decided` is true, INJECT the `answer` into the Plan subagent prompt as AUTHORITATIVE — a line like: "the maintainer has already decided this: <answer>; treat it as settled, do NOT raise it as a clarifying question." Carry the `decided` flag into step 5's branch.

   **HITL early refusal.** When `hitl` is true AND `decided` is false, the bead is marked human-in-the-loop and resolves only through a live exchange — do NOT spawn the Plan subagent (it would burn a round on work that cannot self-approve). Go straight to step 5's defer-and-exit recipe with the standard defer stem + `[defer-reason: open-question]` tag, wording the reason: "marked hitl (human-in-the-loop): resolves only through a live exchange; run interactively without --auto, or /flow triage <KEY> \"<answer>\" to record the decision and clear the label." A `hitl` bead that IS `decided` proceeds as a normal decided-mode run (the recorded decision is the human input the label demanded), so it does not refuse here. This is the prose twin of the `flow_worktree.py` bootstrap hitl floor (exit 8, the shared-step-6 refusal list); the code floor is the real enforcer (prose-only floors leak), this branch just saves the wasted Plan round.

   **Also resolve the verification lane** (the same labels that pick the worker model now also pick how much re-judgment this run does):
   ```bash
   LANE=$(.flow/flow triage lane --workspace-root . --key "$KEY")
   ```
   It prints `express` (a producer-stamped `tier:trivial` bead — vetted behavior-preserving), `light` (a `tier:light` bead), or `full` (everything else, incl. any `hot` bead and any non-beads tracker; fail-open to `full`). A `tier` label is a vetted Opus-producer judgment from the audit step, so on `express`/`light` the run does NOT re-run that judgment: it **skips the confidence rating at the tail of this step AND the close-the-holes revision round below AND step 5's advisor adjudication**, routing on the Plan subagent's structural output instead (see step 5's tier short-circuit). The Plan subagent still runs (it derives `planned_files` and surfaces clarifying questions), the hot hard-floor still holds (a hot bead is `full` by construction), and CI + the review bot + the merge keystone are unchanged. Carry `LANE` into step 5's branch.

   Read the absolute `<skill_root>/references/stage-plan.md`, then spawn the adapter's
   independent Plan agent embedding that protocol PLUS the output contract below. When
   e2e is not explicitly disabled and `<task_root>/.flow/e2e-recipes.md` exists, also
   embed that cookbook's content in the prompt.
   The subagent runs PRE-bootstrap, so `.flow/runs/<KEY>/ticket.json` and `.flow/tickets/<KEY>.md` do NOT exist yet (the `ticket` stage writes ticket.json; `flow_worktree.py create` writes tickets/<KEY>.md — both run later). Do NOT point the subagent at those files. Instead, INLINE the ticket JSON you already fetched in step 3 (`tracker_cli.py get`) into the embedded ticket-context block below, pasting it verbatim where the placeholder sits:
   ```
   Independent Plan agent

     Workspace root: <absolute task_root>
     Skill root: <absolute skill_root>
     Harness: <claude-code|codex|generic>
     Reference path: <absolute skill_root>/references/stage-plan.md
     Artifact path: none (pre-bootstrap; return the report to the orchestrator)
     Ticket: <KEY>
     You are the Plan subagent for the plan stage of Flow, running in --auto mode.
     Your inherited cwd is non-authoritative. Run every read-only command with
     Workspace root as its explicit workdir. Prefix every Flow facade call with
     `FLOW_HARNESS=<Harness>` in that same command; never rely on an export. Do not
     write repository files.

     Ticket context (fetched by the orchestrator in step 3 — this is your primary
     source of intent; the pre-bootstrap files do NOT exist yet):
     <the ticket JSON from step 3's tracker_cli.py get, pasted here verbatim>

     Per-stage protocol (from references/stage-plan.md):
     <contents of stage-plan.md>

     Produce the plan with its normal sections, THEN end your report with a
     machine-readable block, exactly one of:
       - the literal line `NONE` under a `## CLARIFYING QUESTIONS` heading when
         the ticket is unambiguous and you are confident the plan is approvable
         as-is;
       - a `## CLARIFYING QUESTIONS` heading followed by one `- <question>`
         bullet per genuinely open decision a human must settle before code is
         written (competing interpretations, an unconfirmed assumption, a missing
         input). Only raise a question if its answer would change the plan.
     If you cannot produce a plan at all (the embedded ticket context is empty, or
     zero usable intent), return a single line `BAIL: <reason>` instead of a plan.
   ```
   A `BAIL` line routes to the defer-and-exit branch in step 5 (the bail reason becomes the comment text).
   Capture the full response.
   Then (only when `LANE` is `full`) get the same INDEPENDENT confidence rating as interactive step 4 through the adapter mapping in `references/harness.md`. On Claude Code prefer its advisor when available; on Codex use a fresh collaboration agent or second model call without a Claude model parameter; generic follows its declared independent-call/defer capability. Its score feeds the branch below. **When `LANE` is `express` or `light`, skip the rating entirely.** The producer's tier stamp is the confidence judgment; there is no score, and step 5 routes on the Plan subagent's `## CLARIFYING QUESTIONS` block alone.

   **Close-the-holes revision round (exactly one).** **Skip this whole round when `LANE` is `express`/`light`** (there is no sub-90% score to trigger it, and re-spinning the planner to raise confidence on a vetted-trivial bead is the redundant judgment the lane removes; a Plan that still raised a genuine clarifying question routes to step 5's defer as usual). **Trigger**: the assessor rated sub-90%, OR the plan's clarifying questions include any answerable from the repo itself. Closeable holes are the flow-5fp witness classes: a missing test the implementer can write, an unmapped error/edge case it can handle, an unacknowledged-but-correct behavior change it can document, an unverified claim about code it can read. **Skip** the round on a `BAIL` (no plan to revise), on a plan whose only gaps are genuinely user-only, and on a decided bead (the decided sub-branch rules on hotness, not plan quality — a revision round there is a category error). Otherwise re-spawn the `Plan` subagent ONCE (same embedding as above), handing it the prior plan + the assessor's named gaps + every self-answerable question, instructing it to (a) fold each closeable hole into the plan as an explicit implementer commitment — the named failing test into Files to change/Test strategy, the error mapping into Approach, the behavior change documented in Approach/Risks; (b) raise confidence via read-only verification only (read the cited code paths, read-only probes, Context7 for library claims) — the path stays read-only until the bootstrap, so closing a hole here means committing the implement stage to close it, not editing now; (c) re-emit the full plan keeping only genuinely user-only questions in the `## CLARIFYING QUESTIONS` block. Re-score once with the same independent assessor. **Hard bound: ONE round per run** — never a second revision, never a loop. Rationale (maintainer policy, flow-5fp): this repo is low-stakes, merges revert cheaply, the merge-time gates are unchanged, false negatives are accepted — a closeable hole parked on a human is the costlier error.

   **Infra-failure branch (the spawn itself errors).** **Trigger**: any agent/advisor spawn in steps 4-5 — the Plan subagent, the independent assessor, the step-5 adjudication agent — errors environmentally: a spend/usage-limit error, an API/provider outage, the harness refusing to create the subagent. This is the exception to this section's "branches on the headless planner's output" framing — no planner ran at all, so there is no output to branch on. It is NOT a `BAIL`: `BAIL` is the planner's *output* (ticket-intrinsic, routed to step 5's defer-and-exit), and a planner that ran but returned something unusable also stays with the `BAIL`/defer machinery. **Disposition**: leave the bead **open and untouched** — NO `tracker_cli comment`, NO `bd update` (no defer, no block), NO friction entry, no follow-up bead, no bootstrap, no worktree. Emit one terse `failed <KEY>: <infra reason>` line and STOP. The wall is environmental, not ticket-intrinsic — `deferred`/`blocked` would drop the bead out of `bd ready` even after the limit resets, forcing a manual reopen, while an open bead relaunches cleanly from scratch (proven on flow-aod). When a drain loop fanned the run out, per-key backoff already exists: drain registers the key in the fleet ledger at launch time (`fleet.py register`, `run_id=""`), and the entry drops out of `launched_pending` once the run registers a lease/branch — an infra-failed run never registers, so the key stays throttled until the fleet entry ages past `STALE_AFTER_S` (1800s, ~30 min), the same launch site and the same ceiling the retired launch-ledger TTL carried. Accepted residual: a drain pass can still launch OTHER keys into the same global wall; each such launch also exits leave-open/no-writes, so the damage is wasted launches, not state corruption.

5. **Branch on the returned block.** When there IS no returned block because the spawn itself failed, this step never fires — route to step 4's infra-failure branch instead of forcing a defer. Whether a judgment fork is adjudicated or deferred depends on the `advisor_adjudicates` flag:
   ```bash
   ADJ=$(.flow/flow triage adjudicate-enabled --workspace-root .)
   ```
   `ADJ=true` (**the default** — on unless explicitly disabled) → skip to the **advisor-adjudication branch** below. `ADJ=false` (explicit opt-out via `[evolve] advisor_adjudicates = false`, restoring the old defer-on-fork behavior) → follow the **opt-out branch** directly here. The safety nets hold either way (the hot hard-floor, the broad-blast block, and the PR review/merge keystone are in both branches); the only difference is whether a judgment fork is ruled on or parked for the human.

   **Tier short-circuit (`LANE` is `express` or `light`) — checked FIRST, before either branch below.** A producer-stamped tier bead already carries the audit step's vetted judgment, so there is no confidence score and no advisor adjudication to run — route on the Plan subagent's structural output alone:
   - **`NONE` (clean plan, no clarifying questions)** → **self-approve** and go to shared step 6, deriving `--planned-files` + `--commit-*` and re-verifying any drift-stamped entry against `@default`, basing off `--base @default` — exactly as the clean branches below, just without the `>=90%` gate (the tier stamp stands in for it). The bootstrap stamps `LANE` into the run frontmatter, so the tail's implement/reflect stages read it (TDD relaxes for behavior-preserving `express`; reflect collapses to friction-only). `tier:light` keeps full TDD. The hot hard-floor is not bypassed: a hot bead resolves to `LANE=full` upstream and never reaches here.
   - **Clarifying questions present, OR a `BAIL` line** → a genuine wall the tier stamp does NOT clear (the producer vetted the work as cheap+safe, not the missing intent). Follow the **opt-out branch**'s disposition exactly: the decided short-circuit wins first (if step 4's probe reported `decided`, take the **Decided** sub-branch — re-probe hotness, proceed/block accordingly), otherwise **defer-and-exit** (comment the open questions / bail reason with the defer stem + `[defer-reason: ...]` tag, `bd update --status deferred`, STOP). No confidence floor, no adjudication call.

   **Opt-out branch (`advisor_adjudicates = false`):**
   - **`NONE` (clean plan) AND the assessor rated >=90%** → auto-approve, no human gate.
     Derive `--planned-files` from the plan's "Files to change" list — which (per stage-plan.md) already includes any anticipated NEW test file paths the TDD implement will create, so the stamped `planned_files` covers them — and `--commit-type` + `--commit-summary` from the Goal.
     And mind drift: any `planned_files` entry the plan stamped because of a file's CURRENT content (a content/drift finding — "this row/line is stale, so touch this file") is advisory, since it was read pre-bootstrap from the launcher checkout, which can lag `origin/main`. Before stamping it, re-verify the finding against the base `--base @default` will resolve to — `git fetch origin`, then `git show origin/<default-branch>:<path>` to re-read the cited content there — and DROP the entry if that base already has it fixed. This keeps the drift-vs-base discipline even though the self-derive shortcut skips the `Plan` subagent (where the plan would otherwise be re-grounded). Also run step 6's test-path cross-check on the derived list before stamping.
     For `--e2e-recipe`, honor step 6's contract, cookbook-aware: when e2e is not explicitly disabled, pass the user-given value if the ticket carries one; else, if `<main-root>/.flow/e2e-recipes.md` exists, author a real recipe from its decision table (embed the cookbook content in the step-4 Plan subagent prompt so the plan derives it — deliberate-by-proxy, since the cookbook itself was human-confirmed when it was seeded); else default it to `test-ci-only` (the floor — never a silent skip, never a block). When the workspace explicitly disabled e2e (`e2e = "none"`), omit the flag.
     **Base off `--base @default`, NOT the current branch.** An autonomous run (the evolve `drain` loop fires `claude --bg "/flow <key> --auto"` from whatever branch the cockpit is on) must branch off the freshly-fetched default branch, never the launcher's HEAD — else the PR inherits the launcher's unmerged/stale commits and lands DIRTY. `@default` makes `flow_worktree.py` fetch origin and resolve `origin/<HEAD>`.
     **Re-probe hotness before self-approving — a model's "non-hot" read is not enough (flow-94l6).** A clean, high-confidence plan can still touch a guard file (a `planned_files` entry in `triage._GUARD_FILES` — `SKILL.md` / `stage-registry.toml` / `CLAUDE.md` / the script guards), and on flow-94l6/PR#462 BOTH the Plan subagent AND the advisor asserted "non-hot" on exactly such a plan, so classify hotness by the probe, never by a model's assertion. Once `--planned-files` is derived, run:
     ```bash
     .flow/flow triage decided --workspace-root . --key "$KEY" --files "<final derived planned-files, comma-separated>"
     ```
     Read `is_hot`:
     - **`is_hot` false** → self-approve; go straight to shared step 6 — there is no `ExitPlanMode` to call, because you never entered plan mode.
     - **`is_hot` true** → do NOT bare self-approve. A bare self-approve here is the flow-6tyf leak: it leans solely on the bootstrap's code-enforced hot hard-floor, which with `[evolve] adjudicate_hot` ON bootstraps the guard change with no on-record rationale for the merge-time guard-property review, and with it OFF silently refuses a clean ≥90 plan at bootstrap. Mirror the **Decided** sub-branch's hot dispositions, gated on `.flow/flow triage adjudicate-hot-enabled --workspace-root .`:
       - flag **ON** (maintainer self-target, default off) → record the guard-property rationale as an authoritative decision, THEN go to shared step 6:
         ```bash
         .flow/flow tracker --workspace-root . comment \
           --key "$KEY" \
           --text "DECISION: clean --auto plan touches a guard file (<which>) — <the guard-property rationale for the merge-time review>. Safe to auto-ship: CI + the merge-time guard-property review gate it."
         ```
         Unlike the Decided sub-branch (which proceeds WITHOUT recording, because a decision is already on record), a clean self-approve has no pre-recorded decision, so it records one here. This is the flow-94l6 fix: the `DECISION:` stem makes a relaunch idempotent — a relaunch's step-4 `--files`-less probe reads back `decided:true` and routes to the Decided sub-branch, which itself re-probes hotness with `--files`.
       - flag **OFF** (the default) → **block** — never blind-ship a guard change. Comment a wall distinct from a re-asked question (a clean plan that touches a guard file while `adjudicate_hot` is off), keeping the `flow --auto could not self-approve` stem so `/flow triage` surfaces it, then set status `blocked` (NOT `deferred`; no `[defer-reason: ...]` tag and no `hitl` label — this is a safety block, not a defer):
         ```bash
         .flow/flow tracker --workspace-root . comment \
           --key "$KEY" \
           --text "flow --auto could not self-approve: clean, high-confidence plan touches a guard file (<which>) and adjudicate_hot is off — never blind-ship a guard change. To unstick: reopen (status->open) and re-run WITHOUT --auto, or merge by hand."
         bd update "$KEY" --status blocked
         ```
         Then emit a terse `blocked <KEY>: <reason>` line and STOP. No bootstrap, no worktree, no PR.
   - **Clarifying questions present, a sub-90% rating with any user-reachable gap, OR a `BAIL` line** (a residual wall — read against the POST-revision plan and score: step 4's single close-the-holes round is already spent where it applied, so reaching here means self-raising was exhausted, not skipped) → the disposition depends on whether step 4's probe reported `decided`:
     - **NOT decided** → **defer-and-exit** (unchanged). **A defer is for a *decision*, never a *fact*:** the open questions that justify a defer must be genuine *decisions* needing user-only input; a *fact* gap (anything a Read/Grep/recall/Context7 lookup would answer) is not grounds to defer but unfinished read-only legwork that belonged upstream — the Plan subagent's investigation and the close-the-holes round, before the confidence probe. An `--auto` run that defers on something a Grep would have answered is the bug this guard prevents. An `--auto` run never parks for a human (the launcher walked away, so there is nobody to ask). Instead the run comments the open questions on the original ticket, sets its status to `deferred`, and exits cleanly. It does NOT `EnterPlanMode`, does NOT degrade to interactive, does NOT bootstrap a worktree, and does NOT mint a follow-up bead. A `deferred` ticket drops out of `bd ready`, so an autonomous relaunch loop (the evolve `drain` loop) stops re-launching it. Run exactly, in order:
       ```bash
       # 1. comment the open questions / bail reason on the original ticket (tracker-agnostic seam)
       #    APPEND the structured [defer-reason: ...] tag — see the classification rule below.
       .flow/flow tracker --workspace-root . comment \
         --key "$KEY" \
         --text "flow --auto could not self-approve: <clarifying questions, or the BAIL reason>. To unstick: answer here, reopen (status->open), and re-run WITHOUT --auto to plan interactively. [defer-reason: <no-question|open-question>]"
       # 2. defer the ticket in place so it leaves bd ready (beads-native; tracker_cli transition has no deferred target)
       bd update "$KEY" --status deferred
       ```
       Then emit a terse `deferred <KEY>: <reason>` line (so an attended `--auto` run shows why it stopped) and STOP. No `EnterPlanMode`, no bootstrap (`flow_worktree.py create`), no `EnterWorktree`, no do-loop, no follow-up bead.
       The behavior ("`--auto` never parks") is universal; the `bd update --status deferred` command is the beads instance (the autonomous relaunch loop this serves, the evolve `drain` loop, is beads/maintainer-only).

       **Structured defer-reason (the `[defer-reason: ...]` tag).** The tag is a sub-field appended INSIDE the existing `flow --auto could not self-approve` triage-stem comment — NOT a competing stem (`/flow triage` still matches the stem, and the drain deferred-scan reads the tag from it). It distinguishes a defer a stronger model could clear from one that genuinely needs a human:
       - `[defer-reason: open-question]` — the defer carries a SUBSTANTIVE open question a human must answer: the plan's `## CLARIFYING QUESTIONS` block is non-empty, OR the wall is maintainer-only information, OR the `BAIL` reason is empty/zero-usable-intent ticket context (a stronger model cannot invent the missing intent). NOT escalatable — it needs an answer, not a bigger model. **Whenever a defer comment carries this `open-question` tag, ALSO mark the bead `hitl`** so the next unattended drain never re-picks a decision-bound bead: `bd update "$KEY" --add-label hitl` (the behavior is universal; the `bd` command is the beads instance, matching the relaunch loop this serves). The stamp rides the TAG, so every branch that stamps `open-question` — the main defer-and-exit recipe above, the sub-70% confidence floor, and the advisor-`defer` verdict — inherits it with no per-branch duplication; a `/flow triage <KEY> "<answer>"` reopen clears the label (verb-triage.md §Reopen). A `no-question` defer does NOT stamp `hitl` (it stays reachable by the verb-evolve.md §A3 sonnet→opus reopen ladder, which a `hitl` label would wrongly park).
       - `[defer-reason: no-question]` — the run gave up WITHOUT a substantive question: a planning-give-up `BAIL` (a plan a stronger model could plausibly produce), or a bare confidence shortfall (the assessor scored low but the plan raised no specific user-answerable gap). Escalatable — the evolve drain deferred-scan (verb-evolve.md §A3) reopens these and the sonnet→opus ladder retries once at opus.

       The two adjudication-branch defers below (the sub-70% floor and the advisor-`defer` verdict) append this SAME tag, each stamping the reason per this rule.
     - **Decided** → NO plain re-defer (the judgment question is already answered; re-deferring on it would just re-loop). The wall now is an *implementation* block, not a judgment one. Re-probe with the plan's planned-files to classify hotness:
       ```bash
       .flow/flow triage decided --workspace-root . --key "$KEY" --files "<plan's planned-files, comma-separated>"
       ```
       - **`is_hot` true** → gated on `[evolve] adjudicate_hot`:
         ```bash
         .flow/flow triage adjudicate-hot-enabled --workspace-root .
         ```
         When that flag is **on** (maintainer self-target, default off) → **proceed** like a clean decided bead: self-approve the strongest plan and go to shared step 6 (bootstrap), CI + merge-time guard-property review gated (mirrors the advisor-branch hot floor at step 5.3). When it is **off** (the default) → **block** (never blind-ship a guard change). To block: comment the NEW residual wall — word it distinctly so it reads as a *post-decision implementation block*, not a re-ask of the answered question, but still CONTAIN the stem `flow --auto could not self-approve` so the `/flow triage` scan surfaces it — then set status to `blocked` (NOT `deferred`, NOT a `tracker_cli` transition):
         ```bash
         .flow/flow tracker --workspace-root . comment \
           --key "$KEY" \
           --text "flow --auto could not self-approve: post-decision implementation block on a hot change — <the residual wall>. The judgment is settled, this is an implementation/safety concern. To unstick: answer here, reopen (status->open), and re-run WITHOUT --auto."
         bd update "$KEY" --status blocked
         ```
         Then emit a terse `blocked <KEY>: <reason>` line and STOP. No bootstrap, no worktree, no PR. A `blocked` bead drops out of `bd ready` (no relaunch loop) and surfaces in `/flow triage`.
       - **`is_hot` false** (clean change) → **proceed best-effort**: self-approve the strongest plan and go to shared step 6 (bootstrap), exactly as the clean-and-≥90% branch above. A clean decided bead self-ships, CI-gated only — wrong-but-compiling can land for clean decided beads.

   **Advisor-adjudication branch (`advisor_adjudicates = true`, the default):**
   Same clean self-approve, but a judgment fork is RULED ON by a strong independent mind instead of parked, and the flat 90% number stops being a hard cliff — it folds into the ruling.
   - **`NONE` (clean plan) AND the assessor rated >=90%** → auto-approve exactly as the flag-off clean branch (derive `--planned-files` + `--commit-*`, re-verify any drift-stamped entry against `@default`, run step 6's test-path cross-check, **re-probe hotness with `.flow/flow triage decided --files` and route a hot change through the flag-off clean branch's `adjudicate_hot` ON/OFF dispositions above**, base off `--base @default`, go to shared step 6). No *judgment* adjudication is needed for a clean ≥90 plan, but the hot re-probe still runs. In THIS branch a hot `adjudicate_hot`-ON self-approve records its rationale with the `(advisor)` marker (`DECISION: (advisor) …`, matching the proceed-route convention below) so `/flow triage` surfaces it.
   - **Otherwise** (clarifying questions, a sub-90% rating, or a `BAIL` — all read against the POST-revision plan and score: step 4's single round is already spent where it applied, so reaching here means self-raising was exhausted, not skipped) → a judgment fork. The decided short-circuit still wins first: if step 4's probe reported `decided`, follow the **Decided** sub-branch above (re-probe hotness; `is_hot` true → block unless `[evolve] adjudicate_hot` is on, then proceed; clean → proceed) — a recorded maintainer decision outranks a fresh advisor ruling. Otherwise adjudicate:
     1. **Confidence floor.** If the assessor's score is below **70%**, defer immediately via the flag-off defer-and-exit recipe — the plan is too shaky to rule on; don't spend an adjudication call. Stamp `[defer-reason: no-question]` (a bare sub-70% shortfall is escalatable) UNLESS the plan raised a substantive `## CLARIFYING QUESTIONS` block, in which case stamp `[defer-reason: open-question]`. STOP.
     2. **Get a ship verdict from a strong, independent mind.** Prefer `advisor` on
        Claude Code when available; otherwise use a fresh strong-tier independent
        agent or second model call. Claude Code may pin its supported strong model;
        Codex omits Claude model parameters and inherits the active model. Hand it the
        drafted plan plus confidence evidence and ask for a verdict on two separate
        axes: which option is right, and whether it is safe to auto-ship. The verdict
        is `proceed`, `block`, or `defer` with a short ruling. Evaluate blast radius,
        reversibility, and CI coverage. Classify every weakness as closeable in-run or
        uncloseable; a closeable hole becomes an implementer commitment, not a block.
        Use a refute-style default for genuinely uncloseable or unsafe walls. If no
        independent mind is reachable, defer with the flag-off recipe and STOP.
     3. **`is_hot_change` hard floor.** Re-probe hotness with the plan's planned-files (`.flow/flow triage decided --workspace-root . --key "$KEY" --files "<...>"` reads `is_hot`). A hot change can NEVER `proceed`; downgrade any `proceed` on a hot change to `block`. Never blind-ship a guard/lease/safety change, regardless of the verdict. This downgrade is gated by `[evolve] adjudicate_hot` (read via `.flow/flow triage adjudicate-hot-enabled --workspace-root .`): when that flag is on (maintainer self-target, default off), a hot `proceed` is NOT downgraded. It ships like a non-hot one, gated instead by the merge-time guard-property review + CI. Default off → behavior unchanged.
     4. **Route the verdict:**
       - **`proceed`** → record the ruling as an authoritative decision the way a maintainer triage would, then self-approve and go to shared step 6:
         ```bash
         .flow/flow tracker --workspace-root . comment \
           --key "$KEY" \
           --text "DECISION: (advisor) <the ruling — which option, and why it is safe to auto-ship>"
         ```
         The `DECISION:` stem makes a relaunch idempotent (step 4's probe reads it as `decided`, so it never re-asks); the `(advisor)` marker lets `/flow triage` surface it for optional maintainer review. When the ruling carries mandatory implementer commitments (closeable holes converted in step 2), fold any not already in the revised plan into the derived `--planned-files` (including new test file paths) and record them in the `DECISION: (advisor)` comment text so they survive a relaunch. Then derive `--planned-files` + `--commit-*`, re-verify drift against `@default`, run step 6's test-path cross-check, and base off `--base @default` exactly as the clean branch.
       - **`block`** → rulable, but unsafe to auto-ship (broad blast radius / hard to reverse / hot). `block` is reserved for walls that survived step 2's closeability test — user-only information, true irreversibility, broad blast, hot. Comment with the DEFER-stem (NOT a `DECISION:` comment) and set status `blocked`:
         ```bash
         .flow/flow tracker --workspace-root . comment \
           --key "$KEY" \
           --text "flow --auto could not self-approve: advisor ruled <which option> but blocked auto-ship — <why unsafe: blast radius / irreversibility / hot>. Judgment settled, this is a safety hold. To unstick: answer here, reopen (status->open) and re-run WITHOUT --auto, or slice it: /flow slice <KEY> (when the block is broad blast radius), or merge by hand."
         bd update "$KEY" --status blocked
         ```
         Then emit a terse `blocked <KEY>: <reason>` line and STOP. **Critical: a `block` MUST NOT write a `DECISION:` comment.** If it did, a relaunch's probe would read `decided` and — for a non-hot change — route straight to the Decided sub-branch's `proceed`, silently defeating the block. The whole reason the verdict is three-way (not "proceed unless hot") is to catch the non-hot-but-unsafe case (the broad-blast helper); writing a decision for it throws that away.
       - **`defer`** → needs maintainer-only information the advisor cannot supply, or the advisor itself is not confident. Defer via the flag-off defer-and-exit recipe. Stamp `[defer-reason: open-question]` when the advisor's ruling cites maintainer-only information (a human must answer); stamp `[defer-reason: no-question]` when it cites the advisor's OWN lack of confidence (escalatable — a stronger model may clear it). STOP.

   The two outcomes: (a) **self-approve** → shared bootstrap + enter-worktree (steps 6-7), then the tail; or (b) **cannot self-approve** → defer-and-exit (no bootstrap, no worktree, no tail).
   `--auto`'s only effect on the self-approve branch is skipping the interactive plan gate; it does not change how the tail runs. As always, whether the tail runs unattended is the user's separate `/bg` choice (see step 7), independent of `--auto`.
