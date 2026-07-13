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
| `experiment` | configured experiment report | cohort outcome against its manifest |
| `trend` | `trend` | combined delivery and memory window |
| `memory-health` | `corpus-health` | live, superseded, and aging knowledge |
| `recall-quality` | `recall-hit-rate` | surfaced, used, and miss proxy |
| `fix-efficacy` | `fix-efficacy` | recurrence after machinery fixes |

Invoke the internal calculator through the facade, for example:

```bash
FLOW_HARNESS="<harness>" "<facade>" recall --metric <internal-name> \
  --namespace <namespace> --workspace-root . \
  [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--json]
```

`--since` is inclusive and `--until` is exclusive. Use the calculator's resolved
defaults when omitted and always display the resolved window. `fix-efficacy` is a
lifetime measure; reject window flags instead of accepting and ignoring them.

## Metric semantics

### Throughput

Count immutable ship events in the window. Split deliveries whose ticket, run, and
reflection evidence bind correctly from backend deliveries that cannot be attributed
to Flow. Do not infer shipment from a closed tracker ticket alone.

For a checkpoint:

```text
FLOW measure throughput --checkpoint personal|work [--manifest <path>]
```

aggregate only participants in the selected manifest mode. The internal seam receives
the resolved manifest and mode; surface missing participants rather than silently
dropping them.

### Lead time

For Flow-attributed ship events, measure plan start through PR creation. Report sample
size with median and p90 so a tiny sample is not presented as stable trend.

### Friction

Read the namespaced friction log. Report total events, distinct runs, events per run,
and breakdowns by stage/type/severity.

### Reverts

Join ship events with tracker reopen/reclose history where supported and scan git
revert commits keyed to shipped tickets. Report both sources. A failed git scan fails
loud rather than returning a misleading zero.

### Experiment

Read the configured experiment manifest and its immutable cohort evidence. State the
hypothesis, cohort boundaries, metric, sample size, and outcome. Missing cohort data
is `insufficient`, not success or failure.

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
