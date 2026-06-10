# Where flow sits in the field: a verified novelty survey (June 2026)

Raw material for a possible future writeup (deferred). This records what a deep-research pass found about how flow's architecture compares to commercial ticket→PR agents, open-source scaffolds, and self-improving-agent research — including the claims that did NOT survive verification, the criticisms, and the open questions the field has not answered.

**Method.** 103-agent research workflow, 2026-06-09/10: 5 search angles → 21 sources fetched → 100 claims extracted → top 25 adversarially verified (3 independent refutation votes per claim, 2/3 refutes kills). 20 confirmed, 5 killed. Vendor-doc claims are accurate as of fetch date; this field moves monthly.

## The system under evaluation

flow, at v0.27.x: ticket (Jira | beads) → draft PR with one human gate (plan approval at ExitPlanMode). A deterministic Python state machine (`dispatch_stage.py` + per-ticket leases + canonical snapshots + TOCTOU drift guards) orchestrates an LLM do-loop whose per-stage protocol is prose (`references/*.md`), kept honest by `seam_check.py`. Above that, the self-evolution loop: runs log their own friction in-flight, the reflect stage patches harness source mid-run (`machinery_edit.py`, flock-serialized, protected-branch-refusing), and the evolve loop audits its own code, files beads, launches background runs, and auto-merges CI-green leaf PRs into its own main (maintainer-gated, hot changes guard-reviewed). ~17k LOC stdlib-only engine, ~20k LOC tests, 200+ PRs mostly self-produced.

## Confirmed findings (each survived 3-0 adversarial verification unless noted)

### 1. Commercial ticket→PR agents put the human gate at PR review, not plan approval

GitHub Copilot coding agent, OpenAI Codex cloud, and Devin all execute in isolated vendor-managed sandboxes (ephemeral GitHub Actions environments; per-task parallel cloud containers; a full Linux desktop VM respectively). Their issue-to-PR entry points (issue assignment, @codex tagging) terminate in a pull request reviewed by a human. None documents an auto-merge capability. Codex's Plan mode is opt-in, not a mandatory gate.

- https://github.blog/ai-and-ml/github-copilot/github-copilot-coding-agent-101-getting-started-with-agentic-workflows-on-github/
- https://developers.openai.com/codex/cloud
- https://cognition.ai/blog/introducing-devin-2-2

flow's contrarian move: the single gate sits at plan approval (where human judgment is densest), with PR review as the deliverable rather than the gate.

### 2. The industry default is that agents must NOT modify their own harness

OpenAI Codex hard-protects agent scaffolding from self-modification: `.git`, `.agents`, and `.codex` paths remain read-only even in writable sandbox modes. Commercial state of the art treats agent self-editing of its own harness as a threat to be blocked by construction, not a feature.

- https://developers.openai.com/codex/agent-approvals-security

flow's `machinery_edit.py` path deliberately routes mid-run edits INTO its own harness — an inversion of this default. The compensating controls (flock serialization, snapshot-pinned-path refusal, protected-branch refusal, bead→PR→CI→review for everything non-surgical) are the part with no published counterpart (see finding 6).

### 3. Human gates are dialable to zero, and agent-reviews-agent gates are shipping

Codex's default requires human approval for sandbox escalations / network / external tools; a "never" mode disables prompts entirely; an "Auto-review" mode routes approval requests through a second reviewer agent screening for data exfiltration, credential probing, persistent security weakening, and destructive actions (critical-risk actions always denied, failures fail closed).

- https://developers.openai.com/codex/agent-approvals-security
- https://developers.openai.com/codex/concepts/sandboxing/auto-review

This is the closest commercial precedent to flow's in-pipeline agent diff review gating a hot auto-merge — though Codex's gates action approvals, not PR merges.

### 4. A deterministic state-machine dispatcher is rare in open-source scaffolds

In the only published source-code taxonomy (13 scaffolds at pinned commits, April 2026), exactly one agent (Prometheus, via a LangGraph compiled state machine with explicit edges) uses graph-as-control-flow; imperative while-loops dominate, and 11 of 13 agents compose multiple loop primitives. The same paper states "no standard approach has emerged" for scaling safety with autonomy: surveyed strategies span human supervision (Aider), rule-based policy engines (Gemini CLI), OS sandboxing plus an LLM Guardian risk-scoring tool calls (Codex CLI), and Docker containers.

- https://arxiv.org/pdf/2604.03515

Caveats: single-author preprint, N=13, corpus explicitly non-exhaustive. "Rare" generalizes only to the surveyed corpus. Notably, the taxonomy catalogs control loops and sandboxing but NOT state-integrity machinery — flow's lease/snapshot/drift-guard layer has no measured baseline anywhere in the literature.

### 5. Runtime self-evolution of an agent's own scaffold is a Nov-2025 research frontier — and the only published system is the opposite design

Live-SWE-agent (UIUC, Lingming Zhang group) claims to be "the first live software agent that can autonomously and continuously evolve itself on-the-fly during runtime." Its mechanism is deliberately minimal and freeform: an initial-prompt instruction plus per-step reflection asking whether to synthesize a custom tool, starting from bash-only mini-SWE-agent, with explicitly no state machine, no imposed workflow, no changes to the agent loop. Its framing ("software agents are also software... they can be modified and updated on the fly") validates the self-evolution thesis while occupying the opposite design corner from flow's structured, stage-gated, serialized self-patching pipeline.

- https://arxiv.org/pdf/2511.13646

### 6. Even flagship self-improving-agent research never let self-modification reach a live main branch

The Darwin Gödel Machine replaced the original Gödel machine's formal proofs with empirical benchmark validation, raised SWE-bench 20.0%→50.0% and Polyglot 14.2%→30.7%, cost ~$22,000 per run, and ran entirely inside sandboxes with human oversight and traceable modification lineage. Verifier nuance: the oversight was monitoring/post-hoc archive review, not per-edit approval — but the loop never auto-merged into a live harness. Live-SWE-agent positions online evolution as the cheaper fix (65.0% vs DGM's 53.3% on SWE-bench Verified-60, self-reported, subset-measured).

- https://arxiv.org/abs/2505.22954
- https://sakana.ai/dgm/
- https://arxiv.org/pdf/2511.13646

flow's gated self-merge into its own main exceeds the containment posture of all surveyed published systems. Defensible because the blast radius is a maintainer's own dev tool behind CI + guard review + leaf-only + one-hot-at-a-time isolation — but it is past published norms, and that should be stated plainly in any writeup.

### 7. The closest hobbyist analog shares the spec gate, not the self-patching

nodeglobal/agents (Claude Code + LangGraph, 6 agents) requires human approval of the spec before coding and replaces human output review with a Validator agent scoring 0-100 (≥75 approved, <75 retries up to 3x, then human escalation). But its self-improvement is offline and advisory only: a weekly Self-Improve agent analyzes SQLite memory and recommends workflow changes — no mid-run harness patching, no auto-merge. Caveat: 1-star/0-fork repo; evidence of one project's design, not ecosystem practice.

- https://github.com/nodeglobal/agents

### 8. Novelty assessment (synthesized, medium confidence)

flow's full combination — one human gate at plan approval, deterministic leased/snapshotted state-machine dispatcher, friction-log-driven mid-run self-patching via serialized edits, and an evolve loop that audits its own code, files tickets, launches background runs, and auto-merges CI-green agent-reviewed leaf PRs into its own main — is not represented in any surveyed commercial product, research system, or open-source project. Each ingredient has at most one partial precedent: state machine (Prometheus, 1/13), runtime self-evolution (Live-SWE-agent, freeform only), agent-reviews-agent gate (Codex Auto-review), plan-gate + validator scoring (nodeglobal/agents), empirically-validated self-modification (DGM, sandboxed).

Medium rather than high confidence because this is an absence-of-evidence judgment over a finite survey: commercial internals are partially opaque, and OpenAI's internal "harness engineering" writeup describes internal agents that "squash and merge their own pull requests" — practiced inside frontier labs, just unpublished as a system. The defensible claim is "no published system does X," not "nobody does X."

## Claims killed in verification (worth remembering)

Adversarial verification refuted five claims — all in the direction of vendor human-gates being SOFTER than marketing implies:

- "Copilot coding agent is architecturally prevented from approving/merging its own PRs" — refuted (1-2).
- "CI on Copilot's PRs requires human approval to run, so it cannot self-drive a CI-green-then-merge loop" — refuted (0-3).
- "Copilot's write access is absolutely branch-scoped (copilot/* only, never main)" — refuted (1-2).
- "Devin 2.2's human gate sits strictly at the PR after an autonomous self-review loop" — refuted (1-2).
- "Cross-task compounding memory is absent from all benchmark-oriented SWE agents in the taxonomy corpus" — refuted (0-3).

The last one matters for flow: compounding memory exists elsewhere (e.g. Codex CLI's two-phase background memory extraction/consolidation), so flow's memory layer is a quality differentiator, not a uniqueness claim.

## Honest criticisms (internal, from the same pass)

1. **Navel-gazing risk.** Self-target work dominates output; part of the evolve loop's history is fixing bugs in the evolve loop. External validation (work-repo forge seam, flow-2je) was still gated at survey time. Until a foreign project ships PRs through flow regularly, "self-evolving" is partly "self-occupying." Watch `metric.py` revert-rate.
2. **No counterfactual baseline.** Zero evidence the state machine beats plain Claude Code + plan mode + a worktree per ticket. flow-ml7 closes the internal measurement loop but has no control arm. Filed as epic flow-xqt (matched-pair flow-vs-vanilla comparison with a pre-registered success criterion).
3. **Prose-as-control-plane is fragile.** The executor is an LLM reading SKILL.md prose every iteration; seam_check + the MODULE.md gates are strong mitigations of an inherent weakness, and the project's own memory index is full of caught drift instances.
4. **Maintenance surface.** ~58 scripts, 24 reference docs, 20k test LOC for one maintainer. The bet is that the loop pays its own maintenance; true so far in PR count, unproven in value (see #2).

## Open questions the field has not answered (potential writeup angles)

1. What do frontier labs' internal merge gates actually do? OpenAI's internal agents auto-merge their own PRs; policies, failure rates, and rollback machinery are unpublished. That practice is the truest comparison point for flow's hot auto-merge.
2. Does runtime evolution compound across runs/repos? Live-SWE-agent covers in-run evolution; cross-run compounding (flow's memory + evolve loop) is unresolved in the literature.
3. What empirical failure-mode data exists for agents merging into their own live scaffolding? None published — no incident data, no post-mortems. flow's 200+ self-produced PRs, friction.jsonl history, and revert joins are, as far as this survey found, the only such dataset in existence. Curating it is the core of any publishing move.
4. Is anyone combining deterministic dispatch with TOCTOU/snapshot/lease concurrency guards around LLM-driven self-edits? No measured baseline exists for state-integrity machinery in agent scaffolds.

## Survey caveats

Time-sensitive field; vendor claims rest on marketing pages whose capability assertions are not independently benchmarked. The architecture-taxonomy findings rest on a single-author non-peer-reviewed preprint (N=13, pinned commits). Benchmark numbers (Live-SWE-agent 65.0% vs DGM 53.3% on a 60-instance subset; DGM 20→50%) are self-reported and unreplicated. The novelty assessment is an absence-of-evidence argument and should be framed that way in any public claim.

## Source list

Primary: github.blog (Copilot coding agent 101), developers.openai.com (codex/cloud, agent-approvals-security, sandboxing/auto-review), cognition.ai (Devin 2.2), arXiv 2604.03515 (scaffold taxonomy), arXiv 2511.13646 (Live-SWE-agent), arXiv 2505.22954 + sakana.ai/dgm (Darwin Gödel Machine), arXiv 2504.15228, 2408.08435, 2507.21046, 2602.20867, 2603.07670 (self-improving agents / memory), github.com/nodeglobal/agents, metr.org (reward hacking). Secondary/blog: theregister.com (self-improving AI cheating), emergentmind (Voyager), codesota, augmentcode, developersdigest.
