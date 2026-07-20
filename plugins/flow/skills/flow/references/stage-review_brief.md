# review_brief stage (inline, local reviewer companion)

Generate a beautiful, read-only HTML companion for the human reviewing the PR.
The brief answers the questions a maintainer arriving cold to a large codebase has
before a raw diff becomes legible: **why did this need to change, what happened
before, what happens now, which system slice matters, what must remain true, and
which exact lines prove the claims?**

This stage is deliberately not a review gate. It publishes one snapshot-bound
artifact, opens it when configured, records `STATUS=completed`, and advances to
`reflect` immediately. Human review continues asynchronously in the brief and in
Forge. Forge remains the source of truth for the full diff, comments, approval, and
merge authorization.

Authorship may run inline or in one fresh native agent. In either case, the author
returns only the validated content model. `review_brief.py` retains deterministic
snapshot binding, HTML rendering, publication, and freshness. No authoring agent may
render, publish, open a browser, or mutate source.

The registry default handler is `inline`, so newly initialized and explicitly
reconfigured workspaces receive the stage. Existing workspaces that omit the optional
stage keep their current behavior. When `create_pr` is `none`, no `[forge]` block
exists, or `[review_brief].mode = "off"`, complete as an explicit no-op: there is no
PR snapshot to explain. A workspace using Flow's native PR pipeline, including Flow's
dogfood workspace, renders the artifact.

On a genuinely unattended run there is no live reviewer to author the brief for, so
the stage records one explicit skip. `review_brief.freshness()` cross-checks that
record against the run's seeded frontmatter signal before merge treats it as
non-blocking. An attended run cannot silently lose its brief.

## 0. Resolve the run's attendedness once

Read the seeded `unattended` frontmatter boolean at the top of the stage and reuse
that one value for every decision below (this section's own skip-or-author choice,
stated next, and §4's `--no-open` choice). Never re-derive attendedness from lane,
browser configuration, or live prose judgment; the
bootstrap-stamped signal is the sole source of truth and is what
`review_brief.freshness()` cross-checks downstream:

```bash
UNATTENDED=$(FLOW_HARNESS="<harness>" "<facade>" frontmatter read .flow/tickets/<KEY>.md \
  | python3 -c "import json,sys; print(str(json.load(sys.stdin).get('unattended') is True).lower())")
```

**Revision override.** A revision sub-run (`references/delivery-revision.md`) reuses
the original launch's worktree, so this frontmatter value still reflects that
ORIGINAL launch, not this revision. A revision is opened by a human's `revise`
action, so when `$TICKET_DIR` contains `/revisions/`, treat the run as attended
(override `UNATTENDED` to `false`) unless the revision itself was launched by
unattended automation.

When `UNATTENDED` is `true`, skip §§1-3. Record this exact JSON as the stage's skill
output and complete without authoring, rendering, or opening:

```json
{"review_brief_skip": "unattended run has no live human reviewer"}
```

Pass the file containing that JSON through `dispatch advance --skill-output-from`.
Any other reason, or the same reason on an attended run, fails freshness authorization
and falls through to the blocking render path. When `UNATTENDED` is `false`, continue
with §§1-4 and author the brief.

## 1. Resolve the PR and exact snapshot

Use the original run's `create_pr.out` when it exists:

```bash
PR_URL=$(grep -oE '^PR_URL=.*' "$TICKET_DIR/stages/create_pr.out" | head -1 | cut -d= -f2-)
PR_ID=$(printf '%s' "$PR_URL" | grep -oE '[0-9]+$')
```

For a revision sub-run, resolve the already-open PR from the rooted worktree branch:

```bash
PR_ID=$(FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . \
  detect-pr --branch "$(git rev-parse --abbrev-ref HEAD)" | \
  python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("id","") if d else "")')
```

When the native PR pipeline is configured, an empty PR id is a stage failure, never a
silently empty brief. The renderer binds
the local `HEAD`, the Forge-reported PR head, every source excerpt, every source URL,
the artifact directory, and the receipt to the same full SHA. A mismatch fails with
an instruction to push/update before retrying.

## 2. Select compact or full mode

Workspace defaults live under:

```toml
[review_brief]
mode = "auto"   # auto | compact | full | off
open = true
```

An explicit line in the approved `plan.out` wins for this run:

```text
Review brief mode: compact
```

`compact`, `full`, `auto`, and `off` are valid stage selections; `off` completes the
stage without authoring or rendering. This is plan output, not new ticket
frontmatter and not a public-command grammar. With `auto`, use `compact` for a small,
linear change that can be understood from motivation + invariants + one or two
excerpts. Use `full` when the change crosses boundaries, alters a workflow, carries
meaningful risk, needs before/after scenarios, or needs a system map. When uncertain,
choose `full`: the reviewer is assumed not to know this subsystem.

Do not include an estimated reading time.

## 3. Author the evidence model

Write `$TICKET_DIR/stages/review_brief.input.json`. Read the approved plan, ticket,
diff against the PR base, `code_review.out`, `e2e.out`, and CI/review-loop result.
Inspect enough surrounding code to explain the relevant system slice; do not merely
rephrase filenames or commit messages.

Schema version 1:

```json
{
  "schema_version": 1,
  "mode": "full",
  "title": "Outcome-oriented title",
  "outcome": "One-sentence description of what is true now.",
  "risk": "low",
  "change_shape": "Linear | Cross-cutting | Workflow | Safety boundary",
  "motivation": {
    "observed_problem": "Concrete behavior before this change.",
    "why_it_matters": "User, operator, or system consequence."
  },
  "scenarios": [
    {
      "name": "A concrete situation",
      "before_label": "what could happen",
      "after_label": "what happens now",
      "before_steps": ["Cause", "Old path", "Old outcome"],
      "after_steps": ["Cause", "New path", "New outcome"]
    }
  ],
  "system_map": {
    "caption": "Why these are the only relevant components.",
    "nodes": [
      {"id": "boundary", "label": "Workspace", "kind": "Boundary", "changed": true}
    ],
    "edges": []
  },
  "decisions": [
    {"title": "A deliberate choice", "body": "Constraint or tradeoff and why."}
  ],
  "invariants": [
    {"title": "What must remain true", "body": "The reviewer-verifiable guarantee."}
  ],
  "code_evidence": [
    {
      "claim": "What these lines prove",
      "explanation": "Why this is the decisive seam, not merely a changed hunk.",
      "path": "src/example.py",
      "start_line": 40,
      "end_line": 52,
      "highlight_lines": [44, 49]
    }
  ],
  "verification": [
    {"claim": "Behavior verified", "evidence": "Exact test/probe result.", "status": "passed"}
  ],
  "limitations": ["Known residual risk or deliberately out-of-scope behavior."],
  "reviewer_prompts": ["The highest-value question to pressure-test."]
}
```

`risk` is `low|medium|high`; verification status is
`passed|pending|failed`. Paths are repository-relative and excerpt lines refer to the
snapshot file, not a pasted diff. The renderer rejects unknown fields, unsafe paths,
out-of-range excerpts, unknown/cyclic map edges, and unescaped-data hazards. Full
mode needs at least one scenario, map, or decision. Every mode needs motivation,
focused code evidence, and verification.

Authoring rules:

- Lead with motivation and consequences. A file inventory is not motivation.
- Use before/after scenarios when behavior changed; make the causal steps concrete.
- Map only the relevant system slice. Omit architecture untouched by the change.
- State invariants as reviewable guarantees, not feature slogans.
- Choose a few decisive excerpts. The full diff belongs in Forge.
- Separate observed verification from assertion. Carry pending/failed evidence
  visibly; never turn it green in prose.
- Name limitations honestly. The brief should increase confidence, not manufacture it.
- Keep every field plain text. The renderer escapes authored and source content.

## 4. Render, publish, and continue

Resolve `[review_brief].open` (default `true`) and run exactly one facade command:

```bash
FLOW_HARNESS="<harness>" "<facade>" review-brief render \
  --workspace-root . \
  --ticket-dir "$TICKET_DIR" \
  --pr-id "$PR_ID" \
  --content "$TICKET_DIR/stages/review_brief.input.json" \
  --open
```

Use `--no-open` when `[review_brief].open` is configured false or `UNATTENDED` (§0)
is `true` — the same seeded signal that gated §0's skip decision, never a fresh live
judgment. The browser-open result is convenience only: publication remains
successful when the artifact was written but the host could not confirm opening it;
the receipt carries a warning.

The renderer atomically publishes under the full SHA:

```text
<ticket-dir>/stages/review_brief/<full-sha>/
  brief.json
  review-brief-<short-sha>.html
  receipt.json
```

The HTML is a single local file with inline CSS, system fonts, no JavaScript, no
runtime network dependency, restrictive CSP, responsive/print layouts, server-side
highlighting, an inline SVG system map, exact Forge source links, and the full Forge
diff link. It is an ephemeral run artifact: no server, package install, durable host,
or Lavish session is involved.

On renderer success, record `STATUS=completed` and advance immediately to `reflect`.
Do not poll the file, ask for approval, wait for a comment, mark the PR ready, or
otherwise couple Flow's execution to human review latency.

## 5. Freshness contract

Any later commit makes the brief stale, including machinery edits produced by
`reflect` in Flow's self-target workspace. The merge stage runs `review-brief
freshness`; if local `HEAD`, PR head, receipt SHA, and artifact do not all agree, the
merge is blocked until this stage is retried/regenerated at the new pushed SHA.

The user may keep reading an older file, but it is visibly labeled with its exact SHA
and never qualifies as the merge-time companion for a newer branch.
