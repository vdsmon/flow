# Flow review brief

Status: approved design
Date: 2026-07-13

## Intent

Flow should give a human reviewer a beautiful, self-contained explanation of what a
PR changes and why. The primary reviewer may maintain the repository while still
arriving cold to the affected subsystem. In a large, confusing codebase, the raw
diff is then the wrong first encounter with the change: it exposes implementation
before motivation, system boundaries, and behavioral consequences are clear.

The review brief is a local, ephemeral HTML artifact that teaches the change before
the reviewer inspects it in the forge. It emphasizes motivation, concrete before and
after scenarios, the relevant system slice, invariants, focused code evidence, and
verification. The forge remains authoritative for the full diff, line comments, PR
state, and merge.

The artifact should feel like a carefully typeset technical case study, not a CI
report or a second code-review application. “One HTML file” describes its packaging,
not its quality.

## Goals

- Help a reviewer understand an unfamiliar change before reading the complete diff.
- Lead with the observed problem, why it matters, and concrete behavioral scenarios.
- Present only the relevant architecture and code evidence, tied to explicit claims.
- Produce consistently polished output from a Flow-owned visual system.
- Open locally without hosting, a server, a framework, or a network dependency.
- Bind every brief to an exact PR commit and make the snapshot identity unmistakable.
- Adapt the depth of the brief to the complexity of the change.
- Let Flow reflect on its delivery work without waiting for human review or merge.

## Non-goals

- Replacing the forge diff, comments, review state, approval, or merge controls.
- Rendering the entire patch in a custom diff viewer.
- Accepting annotations, comments, or approval inside the HTML.
- Hosting or preserving the brief after the Flow run workspace is reaped.
- Requiring Lavish, a hosted service, or a long-lived local serving process.
- Letting the agent invent arbitrary HTML, CSS, or JavaScript for each PR.
- Estimating or displaying a reading time.

## Product boundary

The two surfaces have different jobs:

| Surface | Responsibility |
|---|---|
| Review brief | Motivation, scenarios, mental model, focused evidence, reviewer orientation |
| Forge | Complete diff, exact code navigation, comments, formal review state, merge |

The reviewer normally uses both at the same time. The brief makes the forge review
more intelligible; it is not a gate that must be approved independently. Human
authorization may still be required before merge. That authorization belongs to the
forge and Flow's existing merge policy, not to the HTML artifact.

Lavish remains useful for interactive planning and design decisions, where temporary
choice collection and visual comparison are central. The review brief does not need
those session semantics. Its smaller requirement is a deterministic, read-only
document, so Flow should own a purpose-built renderer instead.

## Artifact architecture

### Structured input and deterministic renderer

Generation has three explicit steps:

```text
agent-authored ReviewBrief data
              |
        validate and resolve
              |
     Flow-owned HTML renderer
              |
 review-brief-<short-sha>.html
```

The agent authors structured review content, not presentation code. A versioned
schema captures at least:

```text
ReviewBrief
  schema_version
  mode                     compact | full
  ticket and PR identity
  snapshot_sha
  title and outcome
  risk and change shape
  motivation
    observed_problem
    why_it_matters
  scenarios[]
    name
    before_steps[]
    after_steps[]
  system_map
    nodes[]
    edges[]
    changed_paths[]
  decisions[]
  invariants[]
  code_evidence[]
    claim
    repository_path
    commit_sha
    line reference or symbol
  verification[]
  limitations[]
  reviewer_prompts[]
  forge_links[]
```

The exact serialization format is an implementation decision, but it is an
intermediate run artifact rather than the user-facing deliverable. The renderer
validates the schema, resolves code evidence from the bound commit, escapes all
repository-controlled content, and emits one self-contained HTML file.

Raw HTML from the model is forbidden. Diagrams use declarative nodes and edges that
the renderer turns into inline SVG. Code excerpts come from repository files at the
snapshot commit instead of copied model text. These boundaries keep visual quality,
security, and source accuracy under Flow's control.

### Adaptive depth

Every normal ticket PR receives a review brief unless the user explicitly disables
it. The renderer supports two content depths:

- **Compact:** why, outcome, focused change summary, verification, and forge links
  for a small local change.
- **Full:** the complete narrative hierarchy for a cross-cutting, unfamiliar, or
  risky change.

Flow chooses the default from behavioral complexity, affected modules, important
invariants, and risk. Raw line count is not sufficient. A user may force `compact`,
`full`, or `off`. Empty or irrelevant sections disappear; the renderer never pads a
simple change with boilerplate.

### Pipeline attachment

The renderer attaches through a dedicated, generation-only `review_brief` stage after
the forge-driven `review_loop` has reached its automated terminal condition and before
`reflect`:

```text
create_pr -> review_loop -> review_brief -> reflect -> merge
               automation       |             |
                                |             +-- continues immediately
                                +-- opens human-review lane
```

The stage records the selected mode, snapshot SHA, structured-input path, final HTML
path, validation result, and browser-open result. It never waits for human feedback.
A missing PR, an explicit `off`, or a documented unsupported context completes with a
visible skip reason. A generation failure follows the degraded fallback contract
below and then completes so reflection can proceed.

Revision delivery invokes the same generator after its automated verification and
review loop. A pre-merge snapshot guard compares the current branch and PR head with
the latest valid brief; any mismatch invokes verification and regeneration without
pretending the old stage artifact is current.

## Information architecture

A full brief presents information in this order:

1. **Orientation:** title, one-sentence outcome, ticket, PR, snapshot commit, risk,
   and change shape.
2. **Why this changed:** the observed problem, who encounters it, why it matters, and
   why the previous behavior is costly or unsafe.
3. **Before and after scenarios:** paired storyboards that follow the same actor and
   situation through the old and new behavior.
4. **System map:** only the relevant architectural slice, with changed paths
   emphasized and surrounding context visually muted.
5. **Decisions and invariants:** guarantees, meaningful tradeoffs, and behavior that
   deliberately remains unchanged.
6. **Focused code evidence:** normally two to four decisive excerpts, each attached
   to an explanatory claim and an exact forge link.
7. **Verification and risk:** evidence for important claims, remaining limitations,
   and areas that deserve reviewer attention.
8. **Review handoff:** precise prompts and links into the forge diff or relevant
   files.

The first half tells the story before exposing implementation. Technical detail uses
progressive disclosure so a cold reviewer can build a mental model without losing
access to exact evidence. A compact brief uses the same visual language with only the
sections its change requires.

## Visual and interaction design

The approved direction is an editorial technical brief with engineering depth:

- calm, spacious typography and a restrained, accessible palette;
- a strong narrative column with a sticky section rail on wider screens;
- paired before and after scenario cards;
- inline system maps that omit unrelated architecture;
- claim cards connected to code and verification evidence;
- syntax-highlighted excerpts with commit-specific forge links;
- progressive disclosure for secondary implementation details;
- responsive behavior, keyboard navigation, dark-mode compatibility, and clean
  print/PDF output.

The core document remains readable with JavaScript disabled. JavaScript may enhance
navigation or disclosure, but it cannot own content or correctness. Styles, scripts,
any font assets, syntax data, and SVG are embedded. The finished file performs no
network requests.

The surface should remain document-like. Metrics appear only when they prove a claim;
dashboard chrome, decorative charts, and vanity statistics do not belong in the
default layout.

## Snapshot and storage model

The brief is a snapshot, not a live PR client. Its header says `Snapshot · <sha>` and
never makes a permanent claim that it represents the current PR head.

Before opening a brief, Flow compares its bound SHA with the live PR head. A mismatch
causes Flow to verify the revised change as required, generate a new SHA-named file,
and open that version. An old tab therefore remains honestly bound to its original
snapshot rather than silently changing underneath the reviewer.

Forge links target commit-specific files and lines wherever the forge supports them.
The complete-diff link may target the live PR, but the page always labels that
distinction.

Both the structured intermediate and final HTML live under the run workspace. Flow
opens the final file with the host's normal browser mechanism and prints its absolute
path when automatic opening is unavailable. Reaping the run workspace removes the
brief. The PR description retains a concise durable explanation and verification
summary, but does not attempt to preserve or link to the local artifact.

## Delivery lifecycle

The human-review lane and Flow's reflection lane begin together when a verified PR
snapshot and its brief are ready:

```text
implementation -> verification -> PR + review brief
                                      |
                     +----------------+----------------+
                     |                                 |
              Flow reflection                   Human review
          timing, friction, lessons        brief for orientation
                     |                       forge for review
                     |                                 |
               reflection done              feedback -> revisions
                                                       |
                                                      merge
```

Flow does not wait for the reviewer, formal approval, or merge before running the
reflect stage. Reflection analyzes the delivery work already completed: execution
timing, friction, surprises, and process lessons. The review brief is not a Flow
approval checkpoint.

Reflection completing does not bypass merge authorization. If the reviewer has not
finished when reflection ends, Flow may park at its existing ready/merge boundary
while the human-review lane continues. Ordinary projects remain human-merged, and
self-target merge behavior retains its independent authorization and guard policy.
This design changes when reflection may run, not who may merge.

Any branch mutation after brief generation invalidates that snapshot. This includes
human review fixes, automated review-loop fixes, and a machinery edit made by reflect
when Flow is dogfooding against its own feature branch. Flow verifies the new head,
generates a new SHA-named brief, and makes the replacement visible before merge. New
review-driven friction or lessons may be appended to the existing reflection. None of
this introduces state or controls into the HTML.

## Failure behavior

Flow never silently opens a partial or invalid artifact.

1. Validate the structured data and every source reference.
2. Attempt deterministic rendering and post-render checks.
3. If generation fails because the brief data is repairable, repair it and retry.
4. If a valid artifact still cannot be produced, report a degraded review package,
   keep the concise PR explanation as the fallback, and log the failure as friction.
5. Continue into reflection instead of waiting indefinitely.

Because the brief is an aid rather than the source of truth, a renderer failure does
not make the PR or forge diff unavailable. It is nevertheless visible and measured;
Flow must not normalize repeated fallback as success.

## Verification strategy

### Schema and source binding

- Accept valid compact and full fixtures and reject missing required claims or
  malformed scenario/map structures.
- Resolve code excerpts from the declared commit and fail closed on an absent path,
  symbol, line reference, or commit mismatch.
- Verify that repository text, code, titles, and URLs cannot inject arbitrary HTML,
  script, styles, or unsafe link protocols.
- Prove deterministic output for identical structured input and repository state.

### Artifact integrity

- Assert that the final HTML has no external resource or network dependency.
- Apply a restrictive content security policy that permits the renderer's embedded
  assets while forbidding network access and unsafe repository-supplied markup.
- Render successfully from a `file://` URL in supported modern browsers.
- Verify that all required content remains readable with JavaScript disabled.
- Check internal anchors, commit-specific forge links, and snapshot labeling.
- Keep representative large fixtures within an explicit size and render-time budget
  chosen during implementation planning.

### Visual quality

- Maintain compact and full visual fixtures at desktop and narrow viewport widths.
- Run automated accessibility checks for structure, contrast, focus order, and
  keyboard access.
- Exercise dark mode, overflow behavior, long paths, long titles, large code lines,
  missing optional sections, and print/PDF output.
- Inspect visual snapshots for regressions in scenario pairing, system maps, code
  evidence, and navigation.

### Lifecycle

- Open an existing matching-SHA brief without changing its identity.
- Regenerate to a new SHA-named artifact when PR head changes.
- Prove that reflect can start and finish while human review remains pending.
- Apply a review revision, regenerate the brief, and append any new reflection signal
  without rewriting the earlier snapshot.
- Mutate the open branch from self-target reflection and prove the resulting head is
  verified and receives a new brief before merge.
- Force rendering failure and verify the visible PR-native fallback, friction entry,
  and non-blocking reflection path.

## Acceptance criteria

1. A reviewer unfamiliar with the affected subsystem can learn why the change exists,
   compare old and new behavior, and locate the decisive code from the brief.
2. Every normal ticket PR produces an adaptive compact or full brief unless explicitly
   disabled.
3. The user-facing result is one polished, self-contained HTML file that opens locally
   with no server or network access.
4. The renderer, not the model, owns presentation, escaping, diagrams, and code
   extraction.
5. Every artifact is visibly bound to one commit and is regenerated rather than
   overwritten when PR head changes.
6. The brief contains focused evidence and forge links but never duplicates the full
   diff or review system.
7. Human review and Flow reflection proceed independently after the PR snapshot is
   ready, while existing merge authorization remains intact.
8. Renderer failure is visible, measured, and recoverable without indefinitely
   blocking reflection.
9. Compact and full fixtures pass deterministic, accessibility, responsive, offline,
   security, and visual-regression checks.

## Rejected alternatives

### Use Lavish as the review surface

Lavish is valuable when the artifact is part of an interactive planning session and
the user needs to compare or annotate choices. A PR briefing is read-only, tied to a
commit, and consumed alongside the forge. Depending on a Lavish session adds lifecycle
and interaction semantics that this surface does not need. Flow can achieve higher
consistency with its own narrow renderer while continuing to use Lavish for planning.

### Put the entire briefing in the PR description

PR-native Markdown is durable and convenient, so the PR should retain a concise
summary. It is a weak primary surface for paired scenarios, spatial system maps,
progressive disclosure, and evidence placed beside explanatory claims. Making it the
only surface optimizes permanence at the expense of comprehension.

### Build a hosted review application

Hosting would make artifacts durable and shareable, but it would also introduce
deployment, access control, retention, authentication, and synchronization concerns.
The current reviewer needs a local aid, so a hosted application is unnecessary. This
decision can be revisited if remote or asynchronous reviewers need direct access to
the artifact itself.

### Generate bespoke HTML for each PR

Model-authored HTML could vary freely, but it would produce inconsistent hierarchy,
unreliable accessibility, unsafe escaping, and hard-to-test visual drift. Structured
content plus a deterministic renderer preserves authoring flexibility where it
matters while making quality an owned product property.

### Build another full diff viewer

A custom diff viewer would duplicate mature forge behavior and create a second place
for code navigation and review state. The brief should link to the decisive lines and
let the forge do the exact review work it already does well.
