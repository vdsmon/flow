# Flow review brief implementation plan

Status: implemented
Date: 2026-07-13
Design: `docs/specs/2026-07-13-flow-review-brief-design.md`

Implemented on `feat/flow-review-brief`. Final verification: Python lint/type checks,
3,307 Python tests (1 skipped), command/seam checks, and four Playwright visual,
responsive, accessibility, JavaScript-disabled, and print tests all pass.

## Outcome

Replace Flow's attended, Lavish-backed gate-2 review packet with a read-only local
review brief. The brief is one polished, SHA-bound HTML file that explains why a PR
exists, contrasts before and after behavior, shows the relevant system slice, and
connects claims to focused code and verification evidence. The forge remains the
review system.

The same change set also applies the two previously approved delivery-loop polish
fixes:

1. recover a sole independent writer that stops making observable progress; and
2. report total elapsed time, the three slowest stages, and recorded friction for
   long or non-clean runs.

## Module design

### ReviewBrief module

Create one deep, stdlib-only module at
`plugins/flow/skills/flow/scripts/review_brief.py`. Its external interface has two
operations:

```python
render(request: RenderRequest, *, forge: Forge, runner: CwdRunner, opener: Opener) -> Receipt
freshness(request: FreshnessRequest, *, forge: Forge, runner: CwdRunner) -> Freshness
```

The CLI exposes the same interface as `review-brief render` and
`review-brief freshness`. Callers provide only workspace, ticket-directory, PR, and
authored-content paths. The module hides:

- forge and local-head binding;
- authored-content validation;
- compact/full mode resolution;
- commit-pinned source extraction;
- deterministic map layout;
- evidence highlighting and HTML escaping;
- the editorial template and content-security policy;
- atomic artifact writes and SHA-named paths;
- browser opening and its non-fatal fallback;
- receipt creation and stale-snapshot comparison.

Tests cross this same interface using the existing in-memory `Forge`, command-runner,
and browser-opener adapters. No extra public seam is introduced for template parts;
CSS, layout helpers, lexical highlighting, and SVG layout remain implementation
details.

The trusted envelope—ticket, PR URL/id, base/head names, snapshot SHA, and forge
links—is derived by the module. The agent-authored JSON contains only narrative and
evidence requests. It cannot claim a different commit or inject presentation code.

Extend the existing `Forge` interface with one required source-link method implemented
by both GitHub and Bitbucket adapters:

```python
source_url(pr_id: str, sha: str, path: str, start_line: int, end_line: int) -> str
```

This is a real seam because two production adapters vary. It keeps host URL grammar
out of the renderer and stage prose without creating a hypothetical wrapper.

### RunReport module

Create a second small, pure module at
`plugins/flow/skills/flow/scripts/run_report.py`:

```python
summarize(ticket_dir: Path, workspace_root: Path, *, now: datetime) -> RunReport
```

It reads `state.json` and the current run's friction entries, calculates wall-clock
stage durations, and renders JSON or concise text. This removes timestamp arithmetic
and friction reconstruction from delivery prose. Stage durations are explicitly
wall-clock durations: `review_loop` is labelled as including CI/review-bot wait, while
other stages are not misrepresented as pure command CPU time.

## Implementation sequence

### Task 1: Freeze the approved visual source

Files:

- Add `docs/specs/assets/flow-review-brief-concept.html`.
- Add `docs/specs/assets/flow-review-brief-design-tokens.md`.

Actions:

1. Promote the approved `editorial-review-brief.html` companion screen into a
   standalone, self-contained design reference before removing session scratch.
2. Preserve its exact information hierarchy and visible concept copy.
3. Record the accepted token inventory:
   - paper `#fbfaf6`, ink `#18221d`, muted `#66716b`;
   - moss `#274f3f`, rust `#9d4736`, gold `#b58a42`;
   - editorial serif headings, disciplined system UI text, and monospace code;
   - 14–24px radii, thin neutral borders, restrained elevation, and generous rhythm.
4. Record desktop and narrow layout behavior, component families, and allowed chrome.
5. Treat this reference as immutable during implementation unless the user approves a
   design change.

Verification:

- Open the standalone concept through the in-app browser.
- Capture its desktop and mobile screenshots for later side-by-side fidelity review.
- Confirm that no content or asset depends on the brainstorming server.

### Task 2: Define authored data and failing interface tests

Files:

- Add `plugins/flow/skills/flow/scripts/review_brief.py`.
- Add `plugins/flow/skills/flow/scripts/tests/test_review_brief.py`.
- Add fixtures under
  `plugins/flow/skills/flow/scripts/tests/_fixtures/review_brief/`:
  `compact.json`, `full.json`, `adversarial.json`, and `invalid.json`.

Actions:

1. Define frozen dataclasses for authored content, trusted PR envelope, render request,
   receipt, and freshness result.
2. Define the version-1 authored shape:
   - `schema_version`, `mode`, `title`, `outcome`, and risk/change shape;
   - motivation and why-it-matters;
   - paired scenarios;
   - DAG system-map nodes, edges, and changed paths;
   - decisions, invariants, limitations, and reviewer prompts;
   - code evidence with claim, repository path, and either an exact line range or a
     unique textual anchor plus context;
   - verification evidence and forge-link intent.
3. Reject unknown root fields, missing required fields, invalid enums, duplicate map
   ids, missing edge endpoints, cyclic maps, unsafe paths, non-unique code anchors,
   overlong excerpts, and unsafe URL schemes.
4. Keep optional-section absence valid so compact mode has no empty placeholders.
5. Write failing tests for:
   - valid compact and full data;
   - all validation failures above;
   - repository text containing HTML/script/style payloads;
   - missing files and commit mismatches;
   - stable JSON error output and exit codes.

Verification command:

```bash
cd plugins/flow/skills/flow/scripts
pytest tests/test_review_brief.py -q
```

### Task 3: Implement commit binding, extraction, and receipts

Files:

- Modify `plugins/flow/skills/flow/scripts/review_brief.py`.
- Modify `plugins/flow/skills/flow/scripts/tests/test_review_brief.py`.
- Modify `plugins/flow/skills/flow/scripts/forge.py`.
- Modify `plugins/flow/skills/flow/scripts/forge_github.py`.
- Modify `plugins/flow/skills/flow/scripts/forge_bitbucket.py`.
- Modify their existing adapter tests and `plugins/flow/skills/flow/scripts/MODULE.md`.

Actions:

1. Resolve the forge through the existing `Forge` interface and read the PR through
   `pr_info`.
2. Require the local `HEAD`, remote PR head, and trusted envelope SHA to agree before
   rendering. Use normalized `head_sha` where available; derive the fetched remote
   head through git only for an adapter that cannot supply it.
3. Read every code excerpt with `git show <sha>:<path>` and then resolve the declared
   range or unique anchor. Never read the mutable working tree for evidence.
4. Derive commit-specific file links through the new required `Forge.source_url`
   method; test both adapters and do not put host URL grammar in the renderer or stage
   prose.
5. Write artifacts under:

   ```text
   <ticket-dir>/stages/review_brief/<full-sha>/brief.json
   <ticket-dir>/stages/review_brief/<full-sha>/review-brief-<short-sha>.html
   <ticket-dir>/stages/review_brief/<full-sha>/receipt.json
   ```

6. Use atomic replacement for every generated file. A receipt contains status, mode,
   full snapshot SHA, PR identity, artifact paths, open result, warnings, and renderer
   version.
7. Implement `freshness` by deriving the current PR head and checking the receipt at
   its deterministic full-SHA path. Report any other valid receipt as stale context,
   never as the current artifact. Return `current`, `stale`, `missing`, `disabled`, or
   `unavailable` without mutation.
8. Test resumption, same-SHA idempotence, new-SHA non-overwrite, stale detection,
   absent forge metadata, remote/local mismatch, and paths containing spaces.

### Task 4: Build the dependency-free editorial renderer

Files:

- Add `plugins/flow/skills/flow/scripts/assets/review_brief.css`.
- Modify `plugins/flow/skills/flow/scripts/review_brief.py`.
- Modify `plugins/flow/skills/flow/scripts/tests/test_review_brief.py`.
- Add deterministic golden HTML fixtures under
  `plugins/flow/skills/flow/scripts/tests/_fixtures/review_brief/golden/`.

Actions:

1. Implement semantic, document-first HTML matching the approved concept:
   top snapshot bar, sticky rail, editorial hero, motivation, paired scenarios,
   relevant system map, guarantees, focused code evidence, verification/risk, and
   forge handoff.
2. Use system font stacks. Embed the CSS content in the result; no font file, script,
   CDN, framework, image, or network dependency is required.
3. Compute a SHA-256 style hash for a restrictive meta CSP. Use no JavaScript in v1;
   native anchors and `<details>` provide navigation and disclosure.
4. Render deterministic inline SVG for an acyclic layered system map. Reject invalid
   graphs and provide a readable linear fallback for a valid map that exceeds the
   visual width budget.
5. Add dependency-free, server-side lexical highlighting for an initial closed set:
   Python, shell, JavaScript/TypeScript, JSON, TOML, YAML, and Markdown. Unknown
   languages remain escaped, line-numbered code with the decisive lines emphasized.
6. Use no inline style attributes, arbitrary authored classes, or unescaped content.
7. Add `prefers-color-scheme`, `prefers-reduced-motion`, print rules, visible focus,
   narrow layout, overflow handling, and long-path wrapping.
8. Golden tests assert byte-deterministic compact/full HTML, semantic heading order,
   no external resources, CSP presence, exact SHA labelling, no arbitrary markup, and
   readable content when disclosure is expanded without scripting.

### Task 5: Verify visual fidelity and accessibility in a real browser

Files:

- Add `plugins/flow/skills/flow/scripts/ui-tests/package.json` and lockfile.
- Add `plugins/flow/skills/flow/scripts/ui-tests/review-brief.spec.mjs`.
- Add approved desktop/mobile screenshot goldens under
  `plugins/flow/skills/flow/scripts/ui-tests/golden/`.
- Modify `.github/workflows/test.yml`.

Actions:

1. First use the in-app browser to render full and compact fixtures from `file://`, at
   the accepted concept's viewport and a mobile viewport.
2. Capture the implementation and compare it with the accepted concept using
   `view_image`. Keep a fidelity ledger covering at least hierarchy, typography,
   palette, spacing, scenario pairing, map layout, code evidence, and responsive
   collapse. Fix every material mismatch before freezing goldens.
3. Add pinned Playwright and axe-core development dependencies for repeatable CI
   regression checks; they are not packaged into or needed by the generated artifact.
4. Assert:
   - zero network requests;
   - clean browser console;
   - no horizontal overflow at desktop and mobile widths;
   - keyboard-reachable navigation and disclosure;
   - axe has no serious or critical findings;
   - JavaScript-disabled rendering remains complete;
   - screenshot diffs stay within the reviewed threshold;
   - print rendering preserves hierarchy and code wrapping.
5. Add a separate cached visual-test job so Python unit tests remain fast and failures
   identify the visual surface directly.

### Task 6: Attach a non-blocking `review_brief` stage

Files:

- Modify `plugins/flow/skills/flow/stage-registry.toml`.
- Add `plugins/flow/skills/flow/references/stage-review_brief.md`.
- Modify `.flow/workspace.toml`.
- Modify `plugins/flow/skills/flow/scripts/flowctl.py`.
- Modify `plugins/flow/skills/flow/scripts/MODULE.md`.
- Modify `plugins/flow/skills/flow/scripts/init.py` only if its registry-driven default
  does not already place the new stage correctly.
- Modify registry/init/validation/seam tests that assert the current stage set.

Actions:

1. Register `review_brief` after `review_loop` and before `reflect`, with an inline
   default handler, a short timeout, `create_pr` as its required predecessor, and its
   new reference document.
2. Add the stage to Flow's dogfood pipeline and handler map. New and explicitly
   reconfigured workspaces receive it through the registry-driven init path; existing
   workspaces that omit the optional stage keep their current behavior.
3. Add `review-brief` to the facade allowlist and MODULE map.
4. In the stage reference:
   - resolve PR identity and the approved-plan/workspace mode override;
   - skip visibly for `off`, missing PR, or unsupported context;
   - in `auto`, select compact/full from behavioral complexity, affected modules,
     invariants, and risk, never line count alone;
   - read ticket, plan, code-review, e2e, review-loop, and PR evidence;
   - author only the versioned narrative JSON with the host's exact-write primitive;
   - call `review-brief render`, capture its receipt as the stage output, and advance
     immediately;
   - open the result when configured and print the path when opening fails.
5. Record an explicit per-run `review_brief_mode: compact|full|off` in the approved
   `plan.out` when the user requests one. The stage reads that durable approved plan;
   workspace `[review_brief] mode = "auto"` and `open = true` provide defaults. This
   avoids a public grammar change and a new ticket-frontmatter migration.
6. Move the single PR-ready notification to the completed review-brief handoff when
   the stage exists. Preserve the current review-loop/create-PR fallback when it does
   not, so there is still exactly one notification.
7. Test descriptor order, handler dispatch, `off`, auto compact/full authoring paths,
   missing artifact retry, open failure, and notification single-fire behavior.

### Task 7: Remove the Lavish review gate but preserve revision planning

Files:

- Delete `plugins/flow/skills/flow/references/review-packet.md` after extracting the
  revision-only content.
- Add `plugins/flow/skills/flow/references/revision-triage-board.md`.
- Modify `plugins/flow/skills/flow/references/stage-review_loop.md`.
- Modify `plugins/flow/skills/flow/references/delivery-revision.md`.
- Modify `plugins/flow/skills/flow/references/delivery-plan.md` only where it names the
  old gate-2 exception.
- Modify prose/seam tests that cite the old file or gate.

Actions:

1. Remove the attended review-packet attachment, polling, approval semantics, human
   fix rounds, packet heartbeat, and Lavish degradation text from `review_loop`.
2. Let `review_loop` complete as soon as its existing automated condition is met: CI
   green, bot gate settled, and zero unresolved Major+ findings.
3. Preserve the revision triage board as a Lavish planning surface. Make the extracted
   document self-contained rather than inheriting mechanics from the deleted packet.
   Its job is to plan dispositions for already-existing forge feedback before a
   revision begins, not to review or approve the shipped change.
4. Update human-round cap text and revision disposition references to the new file.
5. Confirm that plan approval still uses Lavish unchanged.
6. Add a seam test proving no ordinary review-loop path invokes Lavish or waits for a
   human end-session event.

### Task 8: Add revision and pre-merge freshness handling

Files:

- Modify `plugins/flow/skills/flow/scripts/dispatch_stage.py`.
- Modify `plugins/flow/skills/flow/scripts/tests/test_dispatch_revise.py`.
- Modify `plugins/flow/skills/flow/scripts/stage_merge.py`.
- Modify `plugins/flow/skills/flow/scripts/tests/test_stage_merge.py`.
- Modify `plugins/flow/skills/flow/references/stage-merge.md`.
- Modify `plugins/flow/skills/flow/references/stage-reflect.md`.

Actions:

1. Add `review_brief` to the default revision-stage subset, in workspace order, so a
   same-PR revision produces a new artifact after automated review convergence.
2. Extend the merge probe with review-brief freshness fields. If the workspace has
   the stage and the latest receipt is stale or missing, return a refresh action before
   any merge side effect.
3. In `stage-merge.md`, handle that action by following the same authored-data and
   render protocol, then re-probe. Never silently relabel an old file as current.
4. Note in `stage-reflect.md` that a self-target machinery edit can stale the open
   brief; it does not block reflection, but the merge freshness guard must refresh the
   artifact afterward.
5. Test:
   - revision head receives a new SHA-named brief;
   - ordinary reflection can complete while the initial brief remains open;
   - self-target reflection mutation yields a refresh verdict;
   - current, disabled, and unsupported brief states preserve existing merge policy;
   - stale status can never reach `execute`.

### Task 9: Add deterministic run timing and friction reporting

Files:

- Add `plugins/flow/skills/flow/scripts/run_report.py`.
- Add `plugins/flow/skills/flow/scripts/tests/test_run_report.py`.
- Modify `plugins/flow/skills/flow/scripts/flowctl.py`.
- Modify `plugins/flow/skills/flow/scripts/MODULE.md`.
- Modify `plugins/flow/skills/flow/references/delivery-loop.md`.

Actions:

1. Parse stage timestamps through `state.read` and `_timeutil`, rejecting or explicitly
   skipping absent, negative, or unparseable pairs.
2. Calculate total run wall time and the top three completed stage durations in
   pipeline order for ties.
3. Join friction entries by `run_id`; summarize count, severity, type, stage, and body
   without treating an intentional human review delay as Flow friction.
4. Render concise JSON and text. Label `review_loop` as external-wait-inclusive and
   label every figure as wall time rather than command execution time.
5. In delivery finish, invoke the report for any clean run over 30 minutes or any run
   with friction. Include total elapsed, the three slowest stages, and the friction
   summary before the PR link.
6. Test clean short omission, long clean inclusion, friction-triggered inclusion,
   timestamp corruption, in-progress/failed records, ties, revision directories, and
   filtering foreign-run friction.

### Task 10: Add stalled-writer recovery guidance

Files:

- Modify `plugins/flow/skills/flow/references/delivery-loop.md`.

Actions:

1. Extend the independent-agent section with a host-neutral liveness protocol:
   - use bounded status checks after dispatch;
   - count an agent message, tool-call activity, or relevant workspace change as
     progress;
   - after two consecutive intervals with no observable progress, send one status
     nudge;
   - if the next interval is also idle, interrupt the sole writer, preserve its edits,
     inspect the diff, and resume the handler inline;
   - never start a competing writer against the same workspace;
   - log the stall and recovery as friction.
2. Keep stage timeouts advisory and allow a visibly active long-running command to
   continue.
3. Run prose seam validation to ensure the new guidance uses supported host-neutral
   operations and rooted paths.

### Task 11: Full verification and release evidence

Files:

- Modify any generated docs or exact-surface tests identified by the gates.
- Do not change the accepted visual design or public Flow grammar during cleanup.

Actions:

1. Run targeted review-brief, merge, revision, init, registry, facade, run-report, and
   seam tests after each owning task.
2. Run the complete engine suite and static gates:

   ```bash
   cd plugins/flow/skills/flow/scripts
   mise run lint
   mise run test
   mise run check:commands
   python3 seam_check.py
   ```

3. Run the browser visual/accessibility suite and inspect the final desktop/mobile
   screenshots with `view_image` beside the accepted concept.
4. Render one compact and one full artifact from realistic Flow fixtures, open both
   directly from disk, disconnect network access, and verify forge links, printing,
   dark mode, keyboard navigation, and no console errors.
5. Exercise a disposable end-to-end Flow run through:

   ```text
   review_loop completed
     -> review_brief rendered/opened
     -> reflect began without human input
     -> stale refresh after a simulated revision
     -> merge policy unchanged
     -> final timing/friction summary
   ```

6. Run the self-harness regression corpus because the change touches stage and merge
   machinery.
7. Run `git diff --check`, inspect the complete diff, and verify that visual-companion
   scratch and browser QA output are not committed.

## Suggested commit sequence

1. `feat: add deterministic review brief renderer`
2. `feat: attach non-blocking review brief stage`
3. `refactor: retire lavish review gate`
4. `feat: guard review brief snapshot freshness`
5. `feat: report run timing and friction`
6. `docs: recover stalled flow stage writers`
7. `test: verify review brief visual fidelity`

Each commit should pass its targeted tests; the final commit must pass all static,
engine, seam, browser, and harness-evaluation gates.

## Completion criteria

- The approved editorial concept is faithfully implemented at desktop and mobile
  widths, with no material design deviations.
- A full and compact brief render deterministically from structured data and open as
  one offline HTML file.
- Authored content cannot spoof the PR/SHA, escape HTML, read outside the repository,
  or inject code/styles/scripts.
- Ordinary `review_loop` no longer waits on Lavish or human review.
- `reflect` proceeds immediately after the review-ready snapshot, while merge policy
  remains unchanged.
- Revision and self-target reflection changes cannot merge behind a stale brief.
- Forge remains the only full-diff, comment, formal-review, and merge surface.
- Long/frictional runs explain where wall time went and what friction occurred.
- A stalled sole writer has a bounded, non-competing recovery path.
- All Python, seam, browser, accessibility, visual, and end-to-end gates pass.
