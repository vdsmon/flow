# Self-evolution: flow heals its own harness

This is the thesis, not a footnote. `/flow` is a self-evolving harness: every run that hits friction is an opportunity for the harness to fix itself while the context that produced the friction is still live. The agent running `reflect` is the highest-fidelity judge of the harness that will ever exist for that run — no later reviewer has its context. Treat harness-fixing as a primary duty of reflect, not an opt-in afterthought.

## The loop

1. **Friction is logged in-flight.** As the do-loop hits a snag (drift, lost lease, a missing tool, a blocker, a failed/retried stage, a planned-file reconcile), it appends a `flow_friction.py` entry. This is durable evidence a backgrounded reflect agent cannot reconstruct from `state.json` alone. See `references/verb-do.md` (Friction logging).
2. **Reflect lens-B reads the friction bundle.** `reflect_inputs.py` surfaces the friction array; lens B points the lens UP at the harness (did flow's own scripts/stages/loop serve the run or fight it?). See `references/stage-reflect.md` (step 2b) for the full protocol.
3. **Diagnose at `file:line`.** Re-read the script or reference behind each friction point — do not guess the cause. State the defect concretely + a one-line fix. Severity-tag (blocker / major / minor).
4. **Apply the fix through `machinery_edit.py`.** Surgical, high-confidence fixes to flow's OWN `scripts/*.py` and `references/*.md` apply on the spot. Use `machinery_edit.py apply` (NOT the raw Edit tool): it holds a single flock across read→replace→atomic-write so a fleet of concurrent reflect agents serialize safely, and it refuses out-of-tree and snapshot-pinned paths.
5. **Capture it.** Bump the plugin version in `.claude-plugin/plugin.json` and commit the touched skill files (do NOT push — publishing is a human call). Record the commit sha in the `MACHINERY:` knowledge entry so the change is traceable and revertible.

## Inputs that feed the loop

- The **friction log** (above) is the primary feedstock.
- The **prose↔CLI seam checker** (`scripts/seam_check.py`) is a self-heal input too: a drift it flags (prose naming a flag/subcommand a script lacks) is exactly the class lens-B should fix. Run it; if it errors, that's a harness defect to close.
- **CI** (ruff + ty + pytest + seam_check) running on the commits the loop produces is the safety net that makes unattended self-edits trustworthy — a bad self-edit is caught.

## Guardrails (load-bearing — preserve exactly)

- **machinery_edit flock + atomic write.** The cross-process serialization is what keeps a fleet safe. Do not route machinery fixes through the raw Edit tool.
- **Snapshot caveat.** Never self-edit `stage-registry.toml` or a WIRED handler skill mid-run — they are in the run's canonical snapshot, so editing them trips the drift guard and aborts the very run making the fix. Those go PROPOSE + RECORD, or apply then `/flow recover reload-snapshot`.
- **Blast-radius gate.** APPLY NOW for surgical, high-confidence, strictly-correct fixes that cannot break a sibling agent. PROPOSE + RECORD for structural changes, anything a concurrent run is mid-stage on, or anything you are not certain of. The `MACHINERY:` entry + human note IS the deliverable on that path.
- **Off by default.** Lens B is gated `[reflect] machinery=false` — a stranger running flow neither wants flow editing its own source nor cares about flow-internal findings. It is ON only in the skill developer's workspace (run from a git checkout of flow's own repo).
