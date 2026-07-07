# Matched-pair flow-vs-control protocol (maintainer lane)

> Archived experiment protocol — the xqt experiment concluded (see `xqt-counterfactual-verdict-2026-06.md`); no live stage loads this doc. Moved out of the skill's `references/` runtime surface 2026-07.

A pre-registered, maintainer-only experiment that measures whether the flow machinery beats a hand-driven control arm on the SAME work. It pairs leaf beads, runs one arm of each pair through `/flow` and the other through the manual `control-arm-recipe.md` (sibling in this dir) lane, and scores the result with `recall.py --metric arm-compare`. This doc is the pre-registration: the constants below are MAINTAINER-SIGNED and authoritative. Do NOT change them, and do NOT make post-hoc changes to the success criterion.

This is a manual maintainer lane. It is NOT wired into any stage registry, lease, snapshot, or dispatcher. The only engine touch-point is the `arm-compare` metric, which reads the ship-event corpus both arms write to.

## Design summary

Bead difficulty is the dominant confound in a flow-vs-control comparison. We hold it with matched pairs: beads are drawn from the live queue in filing order and assigned by strict ALTERNATION within a size class, so the two arms see comparable work and neither arm is cherry-picked. Both arms run unattended (flow via `--auto`, control via a hand-driven `claude --bg` that mirrors the same no-mid-run-steering discipline), so the operator effect is held across arms. Each arm self-reports its per-run evidence (`interventions`, `outcome`) into its ship-event; `arm-compare` partitions the corpus on the ship-event `arm` field and renders the pre-registered verdict.

## Pre-registered constants (authoritative — do not change)

- **Pair rule:** ALTERNATE assignment. Take leaf, non-hot beads from the live queue and alternate flow / control in filing order within a size class. No cherry-picking — the next eligible bead in filing order takes the next arm in the alternation.
- **N target:** 8 pairs (16 runs).
- **Success criterion (pre-registered, NO post-hoc changes):** flow wins iff it takes **>= 2 of 3** axes: {lower median time-to-PR, fewer interventions per shipped PR, higher completion rate}.
- **GUARD:** any flow-arm revert with zero control-arm reverts = flow loses regardless of the axis count.
- **Token budget:** uncapped per run, bounded only by N (16 runs).
- **Pair pool:** the 2026-06-10 audit batch (20 evolve beads). The experiment runs AS the next drain over this pool — do NOT plain-drain the pool before pairing it.

## Confounds held

- **Bead difficulty heterogeneity:** size-class pairing + alternation within the class. The two arms see comparable work; the next eligible bead in filing order is forced onto the next arm, so neither arm can be steered toward easy beads.
- **Operator effect:** both arms run unattended. Flow runs are `--auto`; control runs are `--auto`-equivalent (hand-driven `claude --bg`, plan mode on, no mid-run steering).

## Metric axes and data sources

`arm-compare` partitions in-window ship-events on `event["arm"]` (absent arm reads as `flow`; legacy events read as flow) and computes per arm {flow, control}:

| axis | per-arm statistic | favors flow when | data source |
| --- | --- | --- | --- |
| time-to-PR | `median_time_to_pr_hours` | flow < control | flow: the `flow_attribution` stamp (`plan_started` -> `create_pr_finished`); control: `evidence.start_ts` -> `evidence.pr_ts` |
| interventions | `interventions_per_pr` (mean over events carrying the field) | flow < control | `evidence.interventions` (int), both arms |
| completion | `completion_rate` = merged / (merged + abandoned) | flow > control | `evidence.outcome` in {`merged`, `abandoned`}, both arms |
| reverts (GUARD) | `reverts` (count per arm) | n/a — see GUARD | ship-event joined to `bd history` (reopen-then-reclose after `shipped_at`) |

Per-event time-to-PR precedence: the `flow_attribution` stamp first, else `evidence.start_ts`/`evidence.pr_ts`. A missing, unparseable, or negative duration is skipped (recorded per arm), never crashes the metric. An axis is undecidable (reported `null` / `"undecided"`) when either arm has no measurable value for it; an undecidable axis does NOT count toward flow.

## How interventions + outcome are captured per arm

- **Control arm:** the `control-arm-recipe.md` (sibling in this dir) evidence payload already carries `evidence.interventions` (self-reported manual-intervention count) and `evidence.outcome` (`merged` / `abandoned`). These land on the control ship-event when you stamp it with `--arm control`.
- **Flow arm:** flow ship-events do NOT auto-stamp `interventions` / `outcome`. The flow run must self-report `evidence.interventions` and `evidence.outcome` into its own ship-event so the two arms carry the same fields. (The `flow_attribution` block is stamped automatically and supplies the flow-arm time-to-PR; interventions and outcome are the two fields a flow run must add by hand.)

## Verdict

`arm-compare` reports, per axis, `"flow"` / `"control"` / `null` (undecidable). Then:

- `favored_flow_count` = number of axes favoring flow.
- `flow_wins` = (`favored_flow_count >= 2`).
- **GUARD override:** if `flow.reverts > 0` and `control.reverts == 0`, `flow_wins` is forced `false` and `guard_triggered` is `true` (else `false`).

## Assignment log

Rows are filled as pairs run. The two outcome columns and the two intervention columns come from each arm's ship-event evidence (control via the recipe payload, flow via the run's self-reported evidence).

| pair | size class | flow bead | control bead | flow outcome | control outcome | flow interventions | control interventions |
| --- | --- | --- | --- | --- | --- | --- | --- |

## Running the comparison

From the repo root, against the live corpus:

```bash
${CLAUDE_SKILL_DIR}/scripts/recall.py --metric arm-compare --namespace <ns> --workspace-root . [--since YYYY-MM-DD] [--until YYYY-MM-DD]
```

`--workspace-root .` resolves the live `.flow/<ns>/ship-events/` corpus; `--since` / `--until` are optional (they default to the standard 14-day window). The output stamps `resolved_workspace_root` and `total_ship_events`; an EMPTY in-window corpus fails loud (non-zero exit, the resolved ship-events dir named on stderr) rather than emitting an all-zeros verdict.
