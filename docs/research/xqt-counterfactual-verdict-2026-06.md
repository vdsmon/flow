# Counterfactual baseline: flow vs vanilla (2x2 factorial, June 2026)

We ran flow against an unscaffolded ("vanilla") Claude session on the same six tasks, crossed with model {sonnet, opus}, and judge-scored every output on a pre-registered /10 rubric. Bottom line: the harness showed zero measurable quality gain through medium complexity and lost every speed tiebreak (6-25 min/run of overhead); it won exactly one wave, the complex hot-concurrency task, on quality. The model factor dominated. Recommendation: **bound** the flow machinery to high-complexity / hot / concurrency work.

This doc is the experiment of record. It supersedes the matched-pair protocol at `references/xqt-matched-pair-protocol.md`, which was the original child-2 design and was replaced before any data was collected (see Pre-registration below).

## Design

A 2x2 within-task factorial, pre-registration v3. Two factors:

- **model**: {sonnet, opus}
- **harness**: {vanilla, flow}

Four cells, abbreviated by their factor letters: `sv` (sonnet-vanilla), `sf` (sonnet-flow), `ov` (opus-vanilla), `of` (opus-flow). Every task was handed to all four cells (within-task, not between-subjects), so each task yields a four-way comparison rather than a single matched pair.

**Tasks.** N=6 stratified tasks, one per "wave," spanning the difficulty tiers from trivial doc edits up to complex hot-path concurrency changes.

**Judge.** The orchestrator session scored each cell against a pre-registered rubric, /10:

| Axis | Points |
| --- | --- |
| correctness vs bead evidence | 0-3 |
| completeness / all loci touched | 0-2 |
| test quality | 0-2 |
| scope discipline | 0-1 |
| gates green | 0-1 |
| doc fidelity | 0-1 |

**Winner rule.** Highest score wins. Ties break first on fewer interventions, then on faster time-to-PR. Only the winning cell merges.

**DNF.** A cell that did not produce a PR is a DNF (did-not-finish) and is recorded with its failure class (context overflow, infrastructure/transport, or unclassified). Per the pre-registration's no-nudge rule, a stuck cell received no intervention.

## Pre-registration and no moving goalposts

A reader could read "the design changed shortly before the runs" as moving the goalposts. Two facts and one robustness check defuse that.

### The timeline

The original child-2 design was a **matched-pair** experiment: a flow arm against a hand-driven control arm, N=8 pairs, with flow winning iff it took at least 2 of 3 axes {lower median time-to-PR, fewer interventions per PR, higher completion rate}, under a guard that any flow revert with zero control reverts counts as a flow loss.

That design was superseded by **pre-registration v3** (the 2x2 factorial described above), filed 2026-06-11 04:17. The **first run started 2026-06-11 05:38**. The protocol was therefore locked before any data was collected; the swap was not a response to incoming results.

### Robustness to the original criterion

The stronger defense: flow loses under the original matched-pair criterion too. Mapping the flow cells {sf, of} against the vanilla cells {sv, ov} onto the original three axes:

- **time-to-PR**: vanilla won the speed tiebreak in every wave that had a vanilla survivor. Axis goes to vanilla, not flow.
- **interventions/PR**: approximately zero on both arms (the no-nudge pre-registration rule). Tie / undecided, not flow.
- **completion rate**: counting DNFs as non-completions, flow cells completed 8/12 (flow DNFs: `sf` in W3, W5, W6 plus `of` in W4 = 4) versus vanilla 10/12 (vanilla DNFs: `sv` in W5, W6 = 2). Axis goes to vanilla, not flow. The 8/12-vs-10/12 figure rests on the explicit assumption that a DNF is a non-completion; it is stated here rather than asserted as a clean rate.

Net: flow takes at most 1 of 3 under the original axes (here, 0 of 3), so it loses the original criterion as well.

The verdict is the same under either criterion. The design change did not rescue flow. The evolution from the matched-pair protocol to the 2x2 factorial is a transparency improvement, not a goalpost move.

## Results

Each row is one wave. Scores list all four cells; the winner is marked `*`. Time-to-PR is the winning cell's time unless a cell-specific time is noted.

| Wave | Task | Tier | Cell scores (winner *) | Winning time-to-PR | Merged PR | Reading |
| --- | --- | --- | --- | --- | --- | --- |
| W1 | x2hx | trivial doc | ov 10* \| of 10 \| sf 10 \| sv 9 | 2m29s (#253 @0.37.1) | #253 | Ceiling effect on the trivial tier as predicted. All CI green, 0 interventions everywhere. Vanilla wins on speed at quality parity; harness overhead 6-10 min with no quality gain. Interaction datapoint: the harness lifted sonnet (sf 10 > sv 9) but did not lift opus (10 = 10). |
| W2 | mfjn | small code | ov 10* \| sv 10 \| of 10 \| sf 9 | 3m22s (#257 @0.37.2) | #257 | `sv` scored 10 at 3m23s, losing the tiebreak by one second. All four cells produced the byte-identical core fix (wire `_do_append` through `compute_key`): full convergence. Harness adds ~8 min overhead, zero quality delta. `sf` 9 (fidelity: a narrating comment plus a reorder). Interaction: sf < sv, against the hypothesis. |
| W3 | h8s7 | medium behavioral | sv 9* \| ov 9 \| of 9 \| sf DNF-context | 13m04s (#261 @0.37.3) | #261 | First divergence wave: three distinct designs, a 3-way 9-tie broken on speed. `ov` had the best tests and architecture but missed a convention; `of` (20m26s) was the only exact-convention cell but had thin tests. `sf` DNF: 'Prompt is too long', the harness prose payload overflowed sonnet context ~10 min in; no nudge per pre-reg. Key finding: the harness context weight killed its sonnet driver on a medium task. Interaction strongly against this wave. |
| W4 | zajc | medium cross-cutting | ov 10* \| sv 10 \| sf 10 \| of DNF-infra | 8m30s (#264 @0.37.4) | #264 | 3-way 10-tie broken on speed. `sf` (38m35s) shipped the deepest design (trap-EXIT crash-capture plus hung-run grace detection, beyond both vanillas) but the rubric caps at 10 with no exceeds-spec axis, so its strictly-better engineering loses to vanilla speed at the cap. `of` DNF: transport socket error 9 min in (outage class). Interaction: a push at score; sf > sv in depth (high variance). |
| W5 | qanq | complex non-hot | ov 10* \| of 10 \| sv DNF-context \| sf DNF-infra | 14m49s (#267 @0.38.0) | #267 | Both opus cells converged on the same architecture (AST emitted-key extraction plus anchored citations plus missing-only) and both avoided touching `dispatch_stage` (no hot path); `of` (22m18s) was zero-FP but narrower. `sv` DNF: 'Prompt is too long' on **vanilla** sonnet, no harness. `sf` DNF: plan-stage stream idle timeout (infra). Key finding: context overflow is sonnet-on-big-task, not purely harness weight; the harness lowers the threshold, complexity hits it regardless. Interaction: opus push, sonnet uninformative. |
| W6 | t7wu | complex hot concurrency | of 10* \| ov 8 \| sv DNF-context \| sf DNF-unclassified | 28m24s (#270 @0.38.1) | #270 | First flow win, by quality. `of` closed the init front door plus a takeover-rotation seam, made refresh/assert/release nonce-aware, wired SKILL.md, followed the k8f3 migration convention; the judge guard review APPROVED. `ov` 8: primary hole closed but the takeover seam left open and the nonce uncarried. `sv` DNF (context overflow #3); `sf` DNF-unclassified (ended with no PR and no defer). The only wave decided on quality rather than a speed tiebreak. |

**Wins tally:** opus-vanilla 4, sonnet-vanilla 1, opus-flow 1, sonnet-flow 0.

**Merged PRs:** #253, #257, #261, #264, #267, #270.

## Readings

These three readings were pre-registered.

1. **Harness effect.** Zero measurable quality gain through medium complexity. Vanilla won every speed tiebreak (harness overhead 6-25 min/run). The harness paid off only at the complexity ceiling: W6, 10-vs-8 on seam completeness, migration convention, and prose wiring, which is exactly what a plan-plus-review pipeline surfaces.

2. **Model effect.** Dominant. Opus delivered 11/12 cells; sonnet 7/12, with 3 context-overflow DNFs concentrated on big-read tasks regardless of harness.

3. **Interaction (the scaffolding hypothesis).** Not confirmed at N=6: 1 wave for, 2 against, 2 push, 1 uninformative. The harness lowered sonnet's overflow threshold (W3) rather than rescuing it.

## Caveats and threats to validity

- N=6.
- The judge was unblinded (branch names reveal the cells), mitigated by evidence-cited scoring.
- The rubric caps at 10 with a speed tiebreak, which undervalued W4 `sf`'s strictly-deeper engineering.
- The 4 DNFs are uninformative for their cells.
- All tasks ran against the self-target repo.

## Verdict and recommendation

**Bound** the flow machinery to high-complexity / hot / concurrency work.

W6 (complex hot concurrency) was the only wave flow won, and it won on exactly the dimensions a plan-plus-review pipeline surfaces: seam completeness, migration convention, and prose wiring. Below that complexity the harness is net overhead (6-25 min/run) with no measured quality gain; vanilla wins on speed at quality parity.

This confirms the epic's pre-stated live possibility: vanilla wins small leaves, and flow pays off at complexity and concurrency scale. That confirmation is itself the finding. It anchors to VISION.md's "'Better' must be measurable, or it is vibes" — the machinery earns its place where the measurement shows it does, and the measurement places that at the complexity ceiling.

Secondary forward pointer (not a recommendation of this doc): the harness context weight lowered sonnet's overflow threshold (W3 `sf` DNF). That feeds worker-model tiering and context-budget work.
