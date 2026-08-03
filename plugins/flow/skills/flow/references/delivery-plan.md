# Plan

Planning is an attended conversation owned by the driver. Its durable output is one complete
Markdown plan approved by the human. Flow does not maintain plan versions, feedback ledgers,
assessment receipts, approval receipts, or a second planning state machine.

Vocabulary is precise throughout this contract:

- **driver**: the main agent/session that talks to the human and continues the workflow;
- **human**: the one person, who approves the plan — the same person who maintains flow, here in a different role;
- **host**: the Claude Code or Codex adapter that supplies agent and input tools.

Keep `owner` for real resource ownership such as leases, repositories, branches, or content.

## 1. Ground the work

Before touching the new ticket, close out what already merged: run the finalize sweep once
from the primary checkout. Merged deliveries have been witnessed parking for days with open
claims, live worktrees, and no frozen ship event, because close-out waited on a human typing
"merged" into the right session; the sweep makes every fresh start heal them instead. It
writes only behind merged-PR proof, reports still-parked tickets without touching them, and
one line of its report (`finalized: [...]`) is worth relaying to the human:

```bash
FLOW_HARNESS="<harness>" "<facade>" finalize --workspace-root . --all
```

Planning's first act on the ticket itself then mutates it: transition it to `in_progress` in
the tracker backend (Atlassian MCP first when available; REST fallback):

```bash
FLOW_HARNESS="<harness>" "<facade>" tracker \
  --workspace-root . \
  transition --key <KEY> --to-state in_progress
mkdir -p .flow/tickets && [ -f ".flow/tickets/<KEY>.planning-started" ] || \
  date -u +"%Y-%m-%dT%H:%M:%SZ" > ".flow/tickets/<KEY>.planning-started"
```

The second line marks when attended planning began. It is what lets the frozen ship event
separate human-facing planning time from machine delivery time (`metric time-to-pr` reports
the split as `attended_hours`), it writes only when absent so a resumed or revised planning
conversation keeps the first start, and like the claim it is best-effort: a failure to write
it never blocks planning.

The claim is best-effort and never blocks planning: exit 3 (already `in_progress`, or the
tracker has no such state) continues silently; any other failure logs one warning and
continues. The point is that the tracker shows the ticket claimed the moment work starts,
not after approval, so nothing else picks it up as available. This is the one sanctioned
ticket mutation before the human gate.

The driver reads the ticket, relevant repository files, and directly applicable project
instructions. Fetch the default branch and record its SHA. Resolve factual questions read-only.
When the data already in hand shows a coupled small sibling, the fetched ticket's own links or a ready-ticket list a recall pass already loaded, say so to the human in one line before the gate ("FT-xxxx looks coupled and small; group it?"); never make an extra tracker call to go looking, and no sibling in hand means no mention. Grouping stays the human's call because it changes run identity and review shape.
If an answer, access grant, permission, or scope choice is needed, the driver asks the human
directly through the host adapter's user-input capability and waits. Raise such a blocker as soon
as it is discovered; do not navigate around it toward an alternative path unless the detour is
very short and obviously equivalent. Working around a missing grant or decision wastes time and
tokens and drifts the plan toward a less precise result. An assessor never relays those
questions.

A push to a repository other than this workspace's own is one of those grants, and the one
easiest to miss. The driver assumes the approved plan covers it, so nothing is asked until the
push is denied, deep in the autonomous tail where no human is watching. Ask for it while
planning, name the other repository and what will be pushed there, and keep the human's own
words. A plan that merely mentions the repository is not a grant, and a host that gates pushes
reads the grant, not the approval.

When the workspace compounds memory (`[memory] compounding = true`), recall prior
knowledge before writing the plan — this read is what makes past runs pay into this
one. Write the ticket's intent plus its text to a temporary file and query:

```bash
FLOW_HARNESS="<harness>" "<facade>" recall \
  --query-file "<absolute-intent-file>" [--semantic] \
  --top-n 5 --workspace-root .
```

Weave genuinely relevant entries into the plan's approach and risks, citing their ids.
An empty result is normal. Further memory or history reads are useful only when they
answer a concrete planning question; do not expand planning into a general repository
audit.

This is the run's first query against the corpus, not its only one. A problem that
first appears during implement, code_review, or commit is keyed off the friction entry
the driver logs for it, and the `friction` command answers that entry with the matching
live knowledge (`delivery-loop.md`). Planning does not have to anticipate every snag.

When the ticket names a concrete failing artifact — a generated file, a payload, a load id —
fetch and inspect the real artifact read-only during grounding. The actual bytes settle questions
code reading cannot, and they anchor the plan's verification to reality.

## 2. Write one complete plan

The driver writes and revises one canonical plan containing:

- the problem and intended outcome;
- current behavior and the smallest proposed design;
- exact files expected to change;
- constraints and behavior that must remain intact;
- implementation steps in dependency order;
- proportionate verification, including an E2E recipe only when behavior requires one;
- the verification lane the driver proposes (express, light, or full) with a one-line class rationale, unless the invocation already fixed one with `--verify`; and
- the default-branch SHA used for inspection.

The lane comes from the ticket's class, not from optimism. Express is for a ticket that is one defect, one call site, one test; light is the default for bounded work on known patterns; full is for cross-cutting, novel, or hot work. Hot changes clamp to full, and so does any change on a path the workspace treats as high-stakes (in a tax-forms workspace, the money arithmetic), whatever its size. When in doubt between two lanes, propose the slower one; the human demotes with one word at the gate.

Write the plan in basic English: simple words, short sentences, as brief as completeness
allows, for a reader arriving with little context. Name files and behaviors explicitly, spell
out abbreviations on first use, and cut anything that does not change what gets built or
verified.

Prefer deletion and reuse over new layers. A revision replaces this conversational plan text. Do
not create a version graph, feedback object, schema, receipt, or model-authored envelope.

## 3. Run the adversarial assessment

Every plan receives one independent assessment. Launch one fresh independent agent; it acts as
assessor and did not author the plan. Prefer the bundled `subagent:flow:codex-assessor`, which runs
the assessment on a different engine than the one that wrote the plan, so the independence is
structural rather than instructed; a host-native agent is the fallback when Codex is unavailable,
and unavailable includes an exhausted Codex quota (a live `~/.flow/codex-cooldown.json`, whose
mechanics `codex-reviewer.md` owns) so the fallback engages without burning a timeout.
The human may skip the assessment entirely for a ticket that is one defect, one call site, one
test, and says so at spawn. On an express-lane plan that skip is the proposed default: the plan's lane bullet carries the class rationale, and the human approving an express lane at the gate is the say-so. A light or full lane always runs the pass.

The assessor answers ONE question: is this the right thing to build. It checks whether the plan
targets the right actor and the right seam, whether the evidence the plan claims is obtainable at
all, and whether any factual claim about this repository is false when checked against the
repository rather than against the plan's own reasoning.

It returns blockers only, each naming a concrete failure mode with repository evidence or a
specific counterexample, and what would close it. Vague preferences are not blockers. It does not
score, does not assess style or the completeness of enumerations, does not recompute arithmetic,
and does not list improvements. There is no rubric and no number.

The pass is bounded: it reads the files the plan names and the seams they touch, not the repository at large, and it returns its verdict in minutes. It is a targeted refutation, not an audit; a short blocker list that arrives now is worth more than a thorough one that arrives late.

Design errors are the reason this pass exists: code review checks the diff against the plan, so it
cannot tell you the plan targets the wrong thing.

The driver supplies the assessor two absolute paths it cannot derive: one to write its verdict
to, and one to the previous verdict on a confirm pass (or `none` on the first). Both live outside
any ticket dir, because planning runs before approval and no run, worktree, or stage directory
exists yet. An assessor told to write under `<ticket_dir>/stages/` produces nothing parseable and
fails closed.

A verdict is accepted only with read-proof. Before acting on it, the driver verifies the assessor
actually read this repository: every file path the verdict cites exists in the tree, and the
assessor's transcript shows at least one local read. A schema-valid envelope is no evidence of
reading; a Codex assessor has been witnessed exiting clean with zero local commands, web-searching
the private repository it was standing in, and citing a directory spelling that does not exist.
A verdict that fails read-proof is not a pass and its findings are not findings: stop that
assessor, record "assessor did not read the repository", and continue under the replacement rule
below, disclosing the replacement at the gate.

The path half of that check is mechanical. Blocker evidence is free text, so extract everything
path-shaped from it and require each to exist; run from the workspace root, and exit 2 is the
stop above:

```bash
python3 - "<verdict path>" <<'EOF'
import json, pathlib, re, sys
verdict = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
cited = set()
for blocker in verdict.get("blockers", []):
    for text in blocker.values():
        cited.update(re.findall(r"[A-Za-z0-9_./-]+\.(?:py|md|toml|json|yml|yaml|sql|sh)\b", str(text)))
missing = sorted(p for p in cited if "/" in p and not pathlib.Path(p).exists())
if missing:
    print("read-proof FAILED; cited paths not in the tree:")
    for gone in missing:
        print(" ", gone)
    raise SystemExit(2)
print(f"read-proof ok: {len(cited)} path-shaped citations checked")
EOF
```

When blockers come back, the driver fixes them and asks the SAME assessor to confirm the fixes
only, against the changed text, never to re-read the whole plan. Same assessor means continuity of
judgment, not a live session: a bundled Codex assessor runs each call fresh, so the driver hands it
the prior verdict and the changed text rather than relying on it to remember. A second full pass happens only
when an assessor says the design is wrong, never because it listed improvements. A failed
invocation returning no assessment is not a pass; prompt the same assessor once for its verdict.

If blockers survive the confirm pass, stop and show the current plan, the unresolved findings, and
the exact human decision, access, or evidence needed. A substantive human clarification may start
one new bounded round.

If the assessor context is lost, one disclosed replacement is allowed for the entire planning
effort. Give it the complete current plan and prior findings. If that replacement is also lost,
stop visibly.

## 4. Recheck the base

Immediately before the human gate, fetch the default branch again.

- Unchanged: continue.
- Proven-disjoint movement: update the recorded base and continue.
- Movement in a planned or behaviorally relevant path, including ambiguous overlap: update the
  plan against the new base and reassess it.

This recheck and its reassessment remedy run before presentation. The post-convergence recheck
in section 5 is settled with the human directly and never re-enters the assessment.

## 5. Human gate

The gate opens when no blocker remains. Show:

- the exact complete plan;
- the recorded base SHA;
- the verification lane the plan proposes and its one-line class rationale;
- whether a replacement assessor was used, and whether the assessment was skipped;
- findings resolved during assessment; and
- residual non-blocking risks.

When the plan pushes to a repository outside this workspace, show the section 1 authorization
too, quoted in the human's own words, beside the plan. The approved evidence then carries the
grant the autonomous tail depends on, so delivery does not stop at a denied push after the gate
has closed.

Present through the Lavish plan surface when its gate passes (`references/plan-surface.md`); on
a failed gate, fall back to this plain presentation plus one visible
`Lavish plan surface: skipped — <reason>` line, never silently. From presentation onward,
revision is strictly between the human and the driver: annotations revise the plan and the
surface re-renders, and nothing re-enters the assessment loop; the displayed evidence stays as
assessed. After the surface's end-session signal, fetch the default branch once more: unchanged
or proven-disjoint movement proceeds to approval; movement in a planned or behaviorally
relevant path is shown to the human as a plan delta and settled directly, without an assessor.

The human approves that exact plan and evidence. No branch, worktree, run state, or approval
artifact exists before explicit approval; the ticket status claim made when planning began is
the one prior mutation. A fresh unattended invocation stops here;
it cannot cross the gate.

## 6. Bootstrap the approved plan

Write the approved Markdown to a plan file and create the ticket worktree:

```bash
FLOW_HARNESS="<harness>" "<facade>" worktree create \
  --ticket "<ticket>" \
  --plan-from "<approved-plan.md>" \
  --base "<approved-base-sha>" \
  --branch "feat/<ticket-slug>" \
  --main-root "<workspace-root>" \
  --planned-files "<comma-separated-paths>" \
  --commit-type "<type>" \
  --commit-summary "<summary>" \
  --lane "<gate-confirmed lane>" \
  --e2e-recipe "<recipe or skip: reason>"
```

`--lane` carries the lane the human confirmed at the gate (section 2's lane bullet) into durable run state: bootstrap persists it in the ticket frontmatter, a hot change still clamps to full there, and the sweep's lane watch splits its metrics on that recorded field rather than on plan text.

`--branch` must begin with `feat/<ticket>` even when the repository normally uses
`fix/`, `bugfix/`, `chore/`, or another type prefix. Flow's finalize, janitor sweep,
and revision discovery identify newly minted ticket worktrees through that stable
prefix; `--commit-type` carries the actual change type. Do not translate a bug-fix
commit into a non-`feat/` Flow branch.

Do not pass `--recover-spill` automatically; it is an explicit operator recovery action.

If grounding recalled entries that shaped the plan, record them right after the
worktree exists — rooted at the NEW run root and reusing §1's exact query file, so
the recorded surfaced set is the one the plan actually saw and the dispatcher's
init-time promotion (which joins on the run root and branch) can pick it up:

```bash
FLOW_HARNESS="<harness>" "<facade>" recall \
  --query-file "<absolute-intent-file>" [--semantic] --top-n 5 \
  --record-pending --branch "feat/<ticket-slug>" --ticket "<KEY>" \
  --workspace-root "<worktree>"
```

Best-effort, never blocking: a failed record costs recall observability, not the run.

For a grouped run whose cover set was persisted earlier (`FLOW ticket group`), derive
it back and pass it as `--covers`:

```bash
FLOW_HARNESS="<harness>" "<facade>" group-persist derive --lead "<ticket>" --workspace-root .
```

An empty derived result means the group was dissolved, so omit `--covers` entirely
rather than passing it through as an empty or literal value.

Bootstrap preserves the isolated ticket worktree, single-ticket claim, current-base resolution,
atomic run state, planned-file ownership, and spill protection. It writes the approved text to
`stages/plan.out` and marks `plan` complete so delivery resumes at implementation. Bind
`result.worktree` as the absolute run root for every later operation. That absolute path is the
binding, not the convenience switch a host may offer.

The review brief remains an optional reviewer-facing output later in the pipeline. It is not part
of planning authorization.
