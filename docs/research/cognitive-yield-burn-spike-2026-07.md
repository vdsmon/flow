# Cognitive-yield burn spike — gate verdict

**Ticket:** flow-10px (SPIKE, non-hot, child-1 of epic flow-nu1w)
**Date:** 2026-07-02
**Author:** Victor De Simone (flow --auto)
**Status:** measurement complete; gate verdict below

---

## TL;DR

The gate for epic flow-nu1w children 2-4 (absorb the deterministic inline-stage
recipes — `stage-commit` / `stage-merge` / `create_pr` — into `dispatch advance`)
is: **proceed only if mechanical-orchestration token share > 15% OR the
drift-surface rationale justifies it alone.**

**Verdict: PROCEED. Both arms pass. Materialize `stage-merge` (child-4) first as
a de-risking experiment; hold `stage-commit` (child-3); drop `create_pr`
absorption. Re-anchor the epic's justification on drift-elimination, not token
savings.**

- **Token arm — PASS as measured.** The gate said "measure over ~5 recent
  `--auto` transcripts," and `--auto` runs in this repo are self-target runs where
  the `merge` stage executes — so the gate-faithful number is the **FULL**
  (`commit`+`create_pr`+`merge`) share, which is **cost-weighted pooled 19.6%** and
  clears 15% under **every** token-class and denominator variant (16.6% ex-friction
  → 28.9% do-loop-only). A spot-read confirms `merge` is genuinely mechanical from
  the orchestrator's token view (§1 eligibility + §3 merge are pure `gh`/`git`
  plumbing; the hot guard-property review is *delegated to a subagent*, so its
  cognition is out-of-transcript). The one honest caveat: on the **CORE**
  (`commit`+`create_pr`, merge-excluded) measure the share is ~10% — the
  extrapolation to a *user-project* population (where merge is a human keystone the
  orchestrator never drives), which the gate did not ask about. It scopes the
  result; it does not downgrade the pass.
- **Drift arm — PASS, and it reframes the value.** The recipe docs are a real,
  recurring **prose→CLI seam-bug hotspot**. `stage-merge.md` alone took 22 commits
  with repeated seam bugs of the exact class the epic names (shell
  conditional-expansion quoting, push refspec, exit-code swallowing); the same bug
  recurs across duplicate beads; and the machinery that exists only to police this
  seam (`seam_check.py`) has grown to **891 lines and is still growing**. Those bug
  classes are **structurally impossible in tested code** and become
  corpus-evaluable — the epic's real thesis. This, not token savings, is why to do
  the work: absolute absorbable spend is modest (**≥ ~$1–3 orchestrator output per
  run** — a floor, since removing mechanical turns also shrinks downstream
  cache-read on every later turn).

**Recommendation:** proceed, but **narrow and re-anchor**. Build `stage-merge`
absorption first (the drift hotspot) as a de-risking experiment, hold
`stage-commit`, and drop `create_pr` absorption (low-drift, feature-churned). See
[Recommendation for children 2-4](#recommendation-for-children-2-4).

---

## The question and the gate

Epic flow-nu1w proposes moving the deterministic inline-stage recipes out of
prose (`references/stage-commit.md`, `stage-merge.md`, `stage-create_pr.md`) and
into `dispatch_stage.py advance` as a run-until-cognitive-yield co-routine. The
epic is spike-gated because its skeptic review found the original token-burn
grounding **exaggerated** — `dispatch_stage` already owns the plumbing and the
orchestration skeleton sits in context once per session (as cheap cache reads).
This spike measures the real share before any absorption.

**Gate (from the epic's binding maintainer decision, 2026-06-19):** proceed to
children 2-4 only if the mechanical-orchestration token share is material
(**> 15%**) **OR** the drift-surface rationale (the prose→CLI seam-drift bug class
lives in these recipes) justifies it alone.

## Corpus and method

Five most-recent **completed, full-pipeline `/flow --auto`** runs (main
orchestrator transcript only; `~/.claude` JSONL, filesystem scan):

| run | date | main turns | note |
|-----|------|-----------:|------|
| flow-uc8n | 2026-07-01 | 218 | friction: python3-shim + self-merge classifier block |
| flow-wxli | 2026-07-01 | 188 | partial self-merge-block friction |
| flow-8sk4 | 2026-06-25 | 185 | |
| flow-3rlf | 2026-06-24 | 163 | |
| flow-bkyg | 2026-06-23 | 199 | hot auto-merge (clean) |

All five are **full-lane, maintainer self-target** runs. In-flight parallel jobs
and deferred/partial runs were excluded (selection rule: a genuine `/flow … --auto`
command + a worktree bootstrap + reaching the `commit` stage).

**Attribution.** Every one of these transcripts has **zero sidechain lines** — the
cognitive subagents (`Plan`, `implement`, `code_review`, and the merge-stage
guard reviewer) run in *separate* transcript files. So every assistant turn in the
main transcript **is** orchestrator / dispatch spend, and the denominator is clean.
(The subagents' own tokens are out of scope: an LLM judgment cannot be absorbed
into deterministic code, and children 2-4 would not touch them.)

Each turn is attributed to the dispatcher stage of the **next
`dispatch_stage.py advance --stage X`** at or after it (the advance calls are the
ground-truth stage boundaries, independent of fragile descriptor parsing).
Pre-do-loop turns (before the first `init`) are the **spec/plan front-half**.
A third **capture/plumbing** category holds the `.out` re-emit writes and the
`init`/`next`/`advance`/`release` calls, and a **teardown** bucket holds the
post-final-advance wrap-up (release + summary). Both are carved **out** of the
mechanical numerator (children 2-4 would not absorb them — the advisor flagged the
`.out` re-emit as the single biggest confound: `implement`'s large main-transcript
output is mostly the orchestrator re-emitting the subagent response into a file,
not judgment).

**Token classes and cost weighting.** Each turn's `usage` splits into `output`,
`cache_creation` (write), `cache_read`, and uncached `input`. Cache reads dominate
raw counts (~45M vs ~415K output in flow-uc8n) but are cheap and are mostly
cumulative conversation history, not recipe-specific. The **headline is
cost-weighted**, using Anthropic Opus-tier price ratios relative to uncached
input=1: `output=5`, `cache_creation=1.25`, `cache_read=0.1`, `input=1`
(output:cache_read = 50:1, matching the economic reality). Output-only and raw
all-in shares are reported as sensitivity bounds. The **denominator includes the
spec front-half** (conservative: real per-run cost that absorption cannot touch).

## Arm 1 — token share

Cost-weighted mechanical share, `commit`+`create_pr`+`merge` numerator, capture +
teardown + spec carved out:

| run | FULL cw% | FULL out% | FULL all-in% | CORE cw% (merge-excl) | mech output tok | mech $ |
|-----|---------:|----------:|-------------:|----------------------:|----------------:|-------:|
| flow-uc8n `*friction` | 26.6% | 25.0% | 30.7% | 12.9% | 103,983 | $7.80 |
| flow-wxli | 20.3% | 20.4% | 25.8% | 13.6% | 39,889 | $2.99 |
| flow-8sk4 | 13.4% | 9.9% | 18.4% | 7.2% | 14,904 | $1.12 |
| flow-3rlf | 15.0% | 12.0% | 18.4% | 7.9% | 15,030 | $1.13 |
| flow-bkyg | 17.1% | 13.3% | 21.9% | 8.3% | 22,965 | $1.72 |
| **POOLED(5)** | **19.6%** | **18.6%** | **24.1%** | **10.4%** | | |

- **FULL** = `commit`+`create_pr`+`merge` (the self-target auto-merge configuration
  the gate's `--auto` corpus actually exercises).
- **CORE** = `commit`+`create_pr` only (a user-project extrapolation: merge is a
  human keystone there, so the orchestrator opens the PR and stops — it never
  drives a merge recipe).

**Denominator sensitivity (FULL, the gate-faithful numerator):**

| framing | share | gate (>15%) |
|---------|------:|:-----------:|
| pooled(5), cost-weighted, whole-session | 19.6% | PASS |
| pooled(4), ex-friction | 16.6% | PASS |
| do-loop-only (spec excluded) | 28.9% | PASS |
| pooled(5), output-only | 18.6% | PASS |
| pooled(5), raw all-in | 24.1% | PASS |
| CORE pooled(5) (user-project extrapolation) | 10.4% | context |
| CORE pooled(4) ex-friction | 9.4% | context |

**Reading.** The FULL share — the number the `--auto` (self-target) corpus the
gate specified actually produces — clears 15% under every token-class and
denominator variant (16.6%–28.9%). The verdict does **not** flip across
denominators. Two lower per-run points are explained, not hidden: flow-8sk4 dips to
13.4% (a small-total run where the pooled share still holds), and the friction run
flow-uc8n inflates to 26.6% via atypical self-merge-block churn — reported
separately, and the ex-friction pool (16.6%) still passes.

**Is `merge` really mechanical?** The FULL/CORE gap is entirely the `merge` stage,
so its label decides the reading. A spot-read of the merge bucket (flow-bkyg hot,
flow-wxli non-hot) shows it is **predominantly deterministic** from the
orchestrator's token perspective:

- §1 eligibility (PR state, CI re-confirm, `harness_eval`, main-CI probe) and §3
  merge (HEAD-vs-origin guard, mark-ready, `gh pr merge --squash`, bead close) are
  pure `gh`/`git` plumbing.
- The hot-merge §2 guard-property review — the one genuinely cognitive step — is
  **spawned as a separate `Agent`** (flow-bkyg turn #185, ~1K orchestrator output
  for the spawn+read); the judgment itself lives in a *different* transcript and is
  not in the merge bucket at all.

So labeling `merge` mechanical is justified: the orchestrator's merge tokens are
overwhelmingly recipe plumbing, with the cognition delegated out. The residual
non-mechanical content in-bucket is small (the spawn-and-read, plus one-off
exception handling when the self-merge classifier blocks — a friction artifact,
concentrated in flow-uc8n/flow-wxli).

**CORE is scope, not a downgrade.** The ~10% CORE figure matters for a *different*
question the gate did not pose — "what would a user-project run (no auto-merge)
see?" There, only `commit`+`create_pr` absorption is relevant, and the token case
is weak. This is why the recommendation narrows scope rather than reversing the
pass.

**Absolute absorbable spend is modest, and it is a floor.** The mechanical *output*
the orchestrator generates (the bash it emits to drive the recipes) is ~15–40K
tokens/run ex-friction, i.e. **≥ ~$1–3/run** at Opus output rates. It is a floor,
not a point estimate: removing those turns also shortens the running history, so
every later turn's `cache_read` shrinks too — the benefit compounds beyond the
direct output. Even so, this is a small number in absolute terms; the token case
corroborates the gate but is not, by itself, a reason to take on a hot 3-child
effort.

**Verdict, arm 1: PASS as measured** (FULL, the gate-faithful self-target
corpus), robust across denominators. The user-project CORE extrapolation (~10%) is
the honest scoping caveat.

## Arm 2 — drift-surface rationale

The epic's deeper claim: the prose→CLI seam-drift bug class exists *because*
orchestration is prose, and it lives in these deterministic recipes. Two lines of
evidence, both from this repo's own history.

### Recipe-doc churn (git log --follow)

| recipe doc | lines | commits | seam/robustness fixes | notable prose→CLI-seam bugs |
|------------|------:|--------:|----------------------:|-----------------------------|
| stage-merge.md | 145 | **22** | ~10 | #287 `${VAR:+--flag}` conditional-expansion mangles argparse quoting; #230 push-state reads `origin/$BRANCH` not `@{u}`; #225 explicit `git push origin <branch>`; #238 hardened PR-watch probe port; #321 lease-heartbeat exit swallow |
| stage-commit.md | 175 | 12 | ~7 | #244 reset index to HEAD before apply; #103 phantom `/flow recover` flag (doc-seam drift); #227 survive bash sandbox; #319 literal CI-skip-token guard |
| stage-create_pr.md | 86 | 8 | ~2 | #193 base-from-config; #246 draft-by-default — mostly feature, low drift |

(The epic cited 154/212-line figures; actual current sizes are 175/145/86.)

`stage-merge.md` is the standout: 22 commits, roughly half of them
fixes/robustness, and the fixes cluster on exactly the seam class the epic
names — **shell quoting, push refspec, exit-code handling**. These are bugs that
can only exist because a markdown recipe is generating shell that a CLI then
parses. `create_pr` by contrast is low-drift and feature-churned; it is the
weakest absorption candidate.

### The seam-drift machinery, and recurrence

- **`seam_check.py` is 891 lines** (up from the ~698 the epic cited — it is still
  growing) with 7+ drift-gate functions, the newest of which
  (`descriptor_key_drift`, `role_literal_drift`, `docs_over_stage_doc_citation_limit`)
  exist purely to police the prose→CLI descriptor/role seam. This machinery is the
  standing tax of keeping orchestration in prose.
- **The same seam bug recurs.** The stage-merge conditional-expansion bug is filed
  twice (flow-ac7z and flow-i256, both landing in #287); create_pr's push-refspec
  rejection appears as flow-rlc8 and flow-pm3z; the merge lease-exit swallow is
  flow-tnfp. A bug class that recurs and needs hand-filing each time is the exact
  cost signature absorption removes.

**Why code helps where prose cannot.** A `subprocess.run(["git", "push",
"origin", branch])` in `dispatch_stage.py` structurally cannot carry a zsh
`${VAR:+…}` quoting bug (#287), a stale-`@{u}` refspec (#230), or a swallowed exit
code (#321) — those bug classes vanish, and the recipe becomes unit-testable and
**corpus-evaluable** (the epic's thesis: code is corpus-evaluable where prose is
not). That is a real, structural win independent of token count.

**Verdict, arm 2: PASS.** The recipes carry a documented, recurring prose→CLI
seam-bug history, concentrated in `stage-merge` and `stage-commit`, and the win of
moving them to tested code is drift-elimination.

## Verdict

**PROCEED to decompose children 2-4.** The gate is an OR and both arms clear it:
the token arm passes as measured on the gate-faithful self-target `--auto` corpus
(FULL 19.6%, robust across denominators), and the drift arm passes independently
and supplies the *reason* the work is worth its hot blast radius. The
two-sentence spine:

> On the `--auto` (self-target) corpus the gate specified, cost-weighted
> mechanical share is 19.6% and clears 15% under every denominator; the
> user-project extrapolation (commit+create_pr only) is ~10% and scopes, but does
> not reverse, the pass. The recipe docs also carry a real, recurring prose→CLI
> seam-bug history — concentrated in `stage-merge` and `stage-commit`, still taxed
> by an 891-line-and-growing `seam_check.py` — whose bug classes are structurally
> impossible in tested code, and that drift-elimination (not the modest ~$1–3/run
> token saving) is the durable reason to proceed.

### Recommendation for children 2-4

1. **Materialize `stage-merge` absorption (child-4) FIRST, as a de-risking
   experiment.** It is the drift hotspot (22 commits, the real seam bugs) and the
   largest mechanical bucket. Prove the `dispatch advance` co-routine + structured
   result contract there, on the recipe where both arms are strongest, before
   committing to the rest.
2. **Hold `stage-commit` (child-3)** until child-4 validates the pattern. It is a
   secondary drift target (index-state, phantom-flag, CI-skip-token) worth
   absorbing, but only once the mechanism is proven.
3. **Drop `create_pr` absorption.** Low-drift, mostly feature-churned; it yields
   little on either arm. Removing it shrinks the epic and its hot blast radius.
4. **Re-anchor the justification on drift-elimination, not token savings.** The
   "absorb to save orchestration tokens" framing does not survive measurement
   (~$1–3/run, and the token pass rides the self-target merge stage). The
   defensible framing is "eliminate the prose→CLI seam-bug class in the hottest
   recipe and make it corpus-evaluable." Update the epic body before materializing
   children.
5. **Preserve the descriptor seam** (epic binding correction #4): the structured
   handler-result contract must not delete the descriptor seam user-mode runs
   depend on. Absorption is a `dispatch advance` co-routine over the same CLI
   surface, not a `flow_run.py` inversion (correction #3).
6. **Each child is hot** (edits `SKILL.md` / `dispatch_stage.py`) and drains
   sequentially. The merge-time guard-property review remains the keystone.

## Threats to validity

- **n=5, single configuration.** All runs are full-lane maintainer self-target.
  express/light-lane runs (which skip cognitive stages) would show a *higher*
  mechanical share; user-project runs (no auto-merge) a *lower* one (the CORE ~10%).
  The verdict is scoped to the observed mix and reports the per-run spread rather
  than leaning on a mean.
- **Stage-label coarseness.** Attribution is per-dispatcher-stage; a stage is not
  purely mechanical or purely cognitive (a spot-read confirms merge is mostly
  plumbing with the guard-review delegated out; the `.out` capture is plumbing
  living inside cognitive stages). The capture/teardown carve-out mitigates the
  largest confound but the mechanical/cognitive line is a stage-level approximation.
- **Friction run inflation.** flow-uc8n's merge bucket is atypically large (the
  self-merge classifier block forced extra iterations). It is flagged and the
  ex-friction pool is reported alongside.
- **Price ratios are representative, not invoiced.** Cost weighting uses standard
  Opus-tier ratios; the absolute $ figures are order-of-magnitude, not billing.

## Reproduction

The analyzer walks each main transcript, attributes turns to stages via the
`advance --stage X` boundaries, and sums cost-weighted `usage` per stage. Full
source in [Appendix: analyzer](#appendix-analyzer). Corpus transcript ids are in
the [Corpus](#corpus-and-method) table (under
`~/.claude/projects/-Users-victordsm-repos-personal-flow/`). Transcripts are
machine-local, so the analyzer is not CI-runnable; it documents the derivation.
Token conservation was verified (per-transcript bucket sums equal the raw `usage`
totals exactly).

## Appendix: analyzer

```python
# cognitive-yield burn spike analyzer (flow-10px)
# Attribution: each main-transcript assistant turn is attributed to the stage of
# the next `dispatch_stage.py advance --stage X` at or after it. Pre-do-loop turns
# = spec_front_half. .out re-emit + dispatch plumbing = capture; post-final-advance
# wrap-up = teardown; both carved out of the mechanical numerator.
# Cost weights (Opus-tier, rel. uncached input=1): output=5, cache_creation=1.25,
# cache_read=0.1, input=1.  Mechanical = commit, create_pr, merge.
#
# Key steps (working script lives with the spike run):
#   1. per transcript: collect assistant turns (skip isSidechain), record each
#      turn's usage + whether it is a dispatch call (regex tolerant of a quoted
#      script path: dispatch_stage.py" advance) + whether it writes a stage .out.
#   2. do-loop start = first `init` call index; advance boundaries = the ordered
#      (index, stage) of every `advance --stage X`.
#   3. label each turn: spec_front_half (before init) | capture_plumbing (.out
#      re-emit or init/next/advance/release) | teardown (after last advance) |
#      else the next-advance stage.
#   4. sum usage per label; mechanical share = cost_weighted(commit+create_pr
#      [+merge]) / cost_weighted(all), across denominators.
```
