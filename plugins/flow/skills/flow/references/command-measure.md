# Measure commands

`FLOW measure` reads immutable delivery evidence, tracker history where required,
and memory telemetry. It never mutates runs, tickets, or the corpus. Resolve the
workspace namespace from `workspace.toml` and map public metric names to the internal
calculator:

| Public metric | Internal measure | Headline output |
|---|---|---|
| `throughput` | `tickets-per-week` | shipped count and Flow attribution |
| `lead-time` | `time-to-pr` | median and p90 plan-to-PR hours |
| `friction` | `friction-per-run` | events per run, type, and severity |
| `reverts` | `revert-rate` | revert rate and tracker/git attribution |
| `trend` | `trend` | combined delivery and memory window |
| `memory-health` | `corpus-health` | live, superseded, and aging knowledge |
| `recall-quality` | `recall-hit-rate` | surfaced, used, and miss proxy |
| `fix-efficacy` | `fix-efficacy` | recurrence after machinery fixes |

Invoke the internal calculator through the facade, for example:

```bash
FLOW_HARNESS="<harness>" "<facade>" metric trend \
  --namespace <namespace> --workspace-root . \
  [--since YYYY-MM-DD] [--until YYYY-MM-DD]
```

Substitute `trend` with the internal name from the table above.

`--since` is inclusive and `--until` is exclusive. `--json` exists on `trend` and
`fix-efficacy` only; the other measures print their table form. Use the calculator's resolved
defaults when omitted and always display the resolved window. `fix-efficacy` is a
lifetime measure; reject window flags instead of accepting and ignoring them.

## Metric semantics

### Throughput

Count immutable ship events in the window. Split deliveries whose ticket, run, and
reflection evidence bind correctly from backend deliveries that cannot be attributed
to Flow. Do not infer shipment from a closed tracker ticket alone.

### Lead time

For Flow-attributed ship events, measure plan start through PR creation. Report sample
size with median and p90 so a tiny sample is not presented as stable trend.

Report the attended half alongside it, never instead of it: `median_attended_hours`, `p90_attended_hours`, `median_total_hours`, and `attended_share` cover planning start through the plan gate, the span the headline figure deliberately excludes because flow does not control it. Excluding it from the headline is right; leaving it unaggregated was not. Attended time exceeded the machine span in four of six runs on the 2026-08-10..13 delivery window and held roughly 55% of each ticket's wall clock, and reading that balance meant rebuilding it from transcripts by hand. A workspace whose events predate the planning stamp reports `n_attended: 0` and a zero share rather than a ratio over an empty set.

### Friction

Read the namespaced friction log. Report total events, distinct runs, events per run,
and breakdowns by stage/type/severity.

The denominator counts every run the window has evidence for, which is the friction log's run ids unioned with the run ids of ship events frozen in the window. Counting only the runs that logged something makes the denominator identical to the set of runs with at least one event, which floors events per run at 1.0 and leaves the measure unable to report its own success; a window where half the runs went clean would read the same as one where none did. `runs_with_friction` stays in the report so the two populations remain separable, and a run with friction but no ship event (an open PR, no frozen event) still counts once rather than being dropped.

### Reverts

Join ship events with tracker reopen/reclose history where supported and scan git
revert commits keyed to shipped tickets. Report both sources. A failed git scan fails
loud rather than returning a misleading zero.

### Trend

Roll up throughput, lead time, friction, reverts, and recall quality for the same
window. Default output is a compact table; `--json` returns the full reports keyed by
public metric name.

### Memory health

Count total, live, and superseded knowledge plus supersession rate and the oldest live
decision. Missing source files produce an empty report only in a valid initialized
workspace.

### Recall quality

Report surfaced entries, used entries, hit rate, recorded near-duplicate misses, and
distinct runs. A zero-surface window has a zero rate and an explicit zero sample.

### Fix efficacy

For each closed machinery-fix ticket, compare its claimed stage/type/anchor tuples to
strictly later friction. Report `recurred` or `clean`, plus unmeasurable reasons and
the exact recurrence evidence. Never manufacture an anchor from generic words.

## Output

Without `--json`, lead with the headline and then the evidence needed to interpret
it. With `--json`, surface the calculator object without renaming its data fields,
while keeping public metric names at the outer routing boundary. Every report includes
the resolved workspace root, namespace, and window or lifetime marker.
