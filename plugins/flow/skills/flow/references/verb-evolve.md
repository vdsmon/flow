# evolve verb

`/flow evolve`. Maintainer-only. Routed from SKILL.md's argument table. The cold-audit producer of the self-evolution loop: scan flow's OWN codebase for evidence-backed improvements and file them as beads in flow's backlog, so the harness proposes improvements to itself. Read-then-file; it does not implement or open PRs (that is the deferred shipper).

This is **Producer B**. Producer A is the reflect sling (`references/stage-reflect.md`): lived friction during real runs. Both land in the same `evolve`-labelled backlog, both dedup through the same `--dedup-key` seam.

## 1. Gate — maintainer only

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/maintainer.py --workspace-root .
```

- Exit 0 → prints the flow repo root; you are the maintainer, continue. Run the audit against that repo.
- Exit 1 → not a maintainer setup (no `[maintainer]` marker). Print: "`/flow evolve` is maintainer-only; this workspace is not the flow self-improvement target." Stop. Do NOT audit a user's project.

## 2. Audit — fan out evidence miners (read-only)

Spawn parallel read-only audit agents (the `Agent` tool with `Explore` / `general-purpose`, or a `Workflow` fan-out when available), one per evidence source. Every finding MUST cite concrete evidence — a `file:line` or a reproduced command — or it is not a candidate. No "could be cleaner". Mine, at least:

- **quality gates** — run `mise run lint`, `mise run test`, `python3 seam_check.py` from `scripts/`; every real failure / warning / lint-suppression is a finding.
- **test gaps** — public functions / branches with no test (use `MODULE.md` to map script → test).
- **dead code & complexity** — unused defs (prove zero refs), very long / tangled functions.
- **doc drift** — `MODULE.md` / `inventory.md` / `SKILL.md` / `references/*.md` claims vs the actual code.
- **friction & history** — unaddressed `MACHINERY:` entries in `knowledge.jsonl`, `TODO`/`FIXME`, recent git-log pain.
- **robustness** — real gaps in the load-bearing machinery (run lease, snapshot TOCTOU, atomic writes, ownership gate, flock). Tighten, never erode.
- **architecture / seam** — SKILL.md thinness, registry↔reference-doc consistency, prose↔CLI seam risks.

## 3. Synthesize, rank, assign stable ids

Dedup the raw findings (merge ones about the same root issue), drop the vague / unevidenced. Rank by evidence strength × value × blast-radius-safety × reviewability — prefer small, isolated, high-evidence items. Give each survivor a **stable kebab-case `id`** derived from the finding itself (e.g. `git-porcelain-quotepath-parsing`), and flag `hot` if it touches `SKILL.md` / `stage-registry.toml` / `CLAUDE.md` / a wired handler.

## 4. File each candidate (dedup through the seam)

For each candidate, file it into flow's beads. The `--dedup-key` is the stable `id`; it stops refiling open work AND re-proposing findings already closed or rejected, so the loop converges:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/flow_beads_create.py \
  --workspace-root . \
  --summary "<finding title>" \
  --description "<evidence (file:line / repro) + value + blast radius>" \
  --type <bug|chore|task> --labels evolve,audit \
  --dedup-key "<stable-id>"
```

- Exit 0 → filed; prints the new bead key.
- Exit 5 → a bead for this `--dedup-key` already exists (open or closed); prints that key. Skip — do NOT refile. This is the normal converged path on a re-run.
- Exit 4 → not maintainer (should not happen after step 1's gate). Exit 2 → bd error; report and continue.

## 5. Report

Summarise: candidates found, filed (with keys), skipped-as-duplicate, dropped-as-noise. Be honest if the audit found little — a quiet run as the easy wins drain is success (the loop is self-limiting), not failure. Do not manufacture findings to fill the report.

The user reviews the backlog (`bd ready --label evolve`) and ships from it. Autonomous implementation of the filed beads is the deferred nightly shipper, out of scope here.
