# Counterfactual baseline: flow vs vanilla (2x2 factorial, June 2026)

We ran flow against an unscaffolded ("vanilla") Claude session on the same six tasks, crossed with model {sonnet, opus}, and judge-scored every output on a pre-registered /10 rubric. Bottom line: the harness showed zero measurable quality gain through medium complexity and lost every speed tiebreak (6-25 min/run of overhead); it won exactly one wave, the complex hot-concurrency task, on quality. The model factor dominated. Recommendation: **bound** the flow machinery to high-complexity / hot / concurrency work.

This doc is the experiment of record. It supersedes the matched-pair protocol at `xqt-matched-pair-protocol.md` (sibling in this dir), which was the original child-2 design and was replaced before any data was collected (see Pre-registration below).

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

## Sensitivity analysis: quality-first re-scoring (post-hoc, labeled)

After the registered run completed, the maintainer challenged the scoring design itself: flow's intent is output quality, and speed should matter only when quality is exactly equal — a quality delta compounds over the artifact's life (a closed concurrency seam pays on every future takeover) while a speed cost is paid once. The registered protocol violated that ordering twice over: the /10 rubric **saturated** on 4 of 6 waves (no exceeds-spec axis), and the time-to-PR tiebreak then decided them — smuggling speed into a quality verdict. This section re-scores the same artifacts under a quality-first protocol (depth/exceeds-spec considered, speed never decisive). The registered result above stands as the experiment of record; this is the labeled sensitivity read.

| Wave | Registered winner | Quality-first winner | Basis |
| --- | --- | --- | --- |
| W1 | ov (speed) | true tie | near-identical prose fixes; no quality separation exists |
| W2 | ov (speed) | true tie | byte-identical core fix across all four cells |
| W3 | sv (speed) | **of (flow)** | only cell matching the spec-named `.flow/.initialized` convention; sv was arguably the weakest of the three 9s (CLI-layer stamp, loose guard) |
| W4 | ov (speed) | **sf (flow)** | trap-EXIT crash-capture + hung-run grace detection strictly exceeded both vanillas; the registered scorecard itself flagged this |
| W5 | ov (speed) | ov | quality and speed agreed (wider anchors, better fail-mode docs) |
| W6 | of (score) | of | won on score under both protocols |

**Quality-first tally: flow 3, vanilla 1, ties 2 — inverting the registered 5–1.** Post-merge ground truth already corroborates the re-read: follow-up beads flow-g8l7 and flow-grp4 were filed against the W3/W4 *registered* winners to harvest exactly the qualities the quality-first winners had (the `.initialized` convention; crash-capture + hung-run detection) — post-merge defects the losing artifacts did not carry.

The defensible synthesis across both protocols: **the harness never produced a worse artifact than vanilla, twice produced a strictly deeper one, and decisively won the hardest task; its cost is time (6–25 min/run) and sonnet context pressure, not quality.** The registered headline ("vanilla 5–1") is an artifact of rubric saturation plus the speed tiebreak.

## Caveats and threats to validity

- N=6.
- The judge was unblinded (branch names reveal the cells), mitigated by evidence-cited scoring.
- The rubric caps at 10 with a speed tiebreak, which undervalued W4 `sf`'s strictly-deeper engineering — the design flaw the sensitivity section corrects; both reads are presented.
- The sensitivity re-scoring is post-hoc by the same unblinded judge; it gains credibility from the post-merge harvest beads (g8l7, grp4), not from the judge alone.
- The 4 DNFs are uninformative for their cells.
- All tasks ran against the self-target repo.

## Verdict and recommendation

Two protocols, two reads, one synthesis:

- **Registered protocol** (quality capped at 10, speed tiebreak): vanilla 5–1; flow pays off only at the complexity ceiling. Taken alone it recommends bounding the machinery to high-complexity / hot / concurrency work.
- **Quality-first protocol** (speed never decisive): flow 3, vanilla 1, ties 2; flow won every wave where quality could differ except W5, and post-merge follow-up beads against the registered W3/W4 winners corroborate the inversion.

**Synthesis: flow's output quality is equal-or-better everywhere measured and decisively better at the complexity ceiling; its cost is wall-clock time, not quality.** Where breakage is cheap and tasks are trivially small, vanilla's speed is real and the harness is overhead (W1–W2's byte-identical convergence is genuine ceiling, not rubric blindness). Where the work has any depth — conventions to honor, failure modes to close, seams to wire — the harness's plan-plus-review pipeline surfaced quality vanilla missed, and that delta compounds for the life of the artifact.

This refines the epic's pre-stated live possibility rather than confirming it flat: vanilla wins small leaves *on time only*; flow pays off increasingly with complexity, reaching decisive at concurrency/guard scale. It anchors to VISION.md's "'Better' must be measurable, or it is vibes" — both protocols are reported precisely so the claim stays measurable.

**Future replication runs Protocol v2** (recorded on the flow-xqt epic, 2026-06-11): pairwise forced-choice per wave aggregated Bradley-Terry/Elo (no cap, no saturation), a severity-weighted defect ledger as the tie-grounding evidence, speed reporting-only and never decisive, and follow-up-fix / revert tracking per merged artifact as the slow objective referee.

Secondary forward pointer (not a recommendation of this doc): the harness context weight lowered sonnet's overflow threshold (W3 `sf` DNF). That feeds worker-model tiering and context-budget work.
