# Control-arm run recipe (maintainer lane)

A control-arm run is the counterfactual: ship a leaf bead WITHOUT the flow machinery, then stamp its ship-event with `--arm control` so it sits in the same corpus as flow-arm events. Sibling `metric.py` work (flow-xqt.2) owns the arm-aware JOIN; this doc only covers producing a record with `arm == "control"`.

This is a maintainer-only manual lane. It does NOT run through the dispatcher, lease, snapshot, or any registered stage.

## How a control run differs from a flow run

- Bare `claude --bg` session, plan mode on. You drive it by hand, no `/flow` verb.
- A worktree cut by hand off `origin/main` (no `flow_worktree.py`, no lease, no snapshot).
- A manually opened PR (no `create_pr` stage, no CI-wait loop, no auto-review).
- No dispatcher, no `state.json`, no stages. Because there is no live run `state.json`, no `flow_attribution` block is stamped, which is correct: a control run has no flow attribution.

You self-report the per-run evidence as you go and carry it in the evidence payload below.

## Evidence payload

`arm` is script-owned and set via `--arm`; it is NOT a top-level evidence key (the script rejects an `arm` key inside `--evidence-json` as an extra top-level key). The payload stays the canonical three top-level keys:

```json
{
  "ticket": "<KEY>",
  "shipped_at": "<UTC ...Z timestamp>",
  "evidence": {
    "start_ts": "<UTC ...Z, when you started the control run>",
    "pr_ts": "<UTC ...Z, when the PR opened>",
    "outcome": "merged",
    "interventions": 0,
    "bead_key": "<KEY>"
  }
}
```

- `shipped_at` MUST match `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`.
- `outcome` is `merged` or `abandoned`.
- `interventions` is your self-reported count of manual interventions during the run.

## Stamp the control ship-event

Synthesize a run-id (it will not match any live `state.json`, so no attribution stamp, which is intended):

```bash
RUN_ID=$(openssl rand -hex 8)   # 16 hex chars
```

Always dry-run into a throwaway workspace first so you do NOT pollute the live `.flow/flow/ship-events/` corpus. `resolve_namespace` requires `.flow/workspace.toml`, so seed a minimal one:

```bash
DRY="$TMPDIR/control-arm-dry"
mkdir -p "$DRY/.flow"
cat > "$DRY/.flow/workspace.toml" <<'EOF'
[memory]
namespace = "dry"
EOF

${CLAUDE_SKILL_DIR}/scripts/observe_ship_event.py \
  --ticket <KEY> \
  --evidence-json '{...}' \
  --run-id "$RUN_ID" \
  --arm control \
  --workspace-root "$DRY"
```

Confirm the written record has `"arm": "control"`, then re-run against the real workspace (`--workspace-root .`) to land it in the live corpus.

- Exit 0 then primary ship-event written.
- Exit 1 then bad evidence JSON, malformed `--run-id`, or an invalid `--arm` value.
- Exit 2 then duplicate (`dupe.<n>.json` written); informational.
- Exit 3 then workspace memory config missing/invalid, or I/O error.
