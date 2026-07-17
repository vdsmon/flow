Flow commands

  FLOW
  FLOW <target> [<target> ...] [--unattended] [--together] [--verify express|light|full] [--e2e <recipe>] [--request <additional-intent>] [--route <profile=harness,model,effort>]...
      Start, answer, resume, repair, revise, or show work from its current state.
  FLOW ticket create [--request <problem>]
      Capture a problem, create a tracker ticket, and offer to run it.
  FLOW ticket group (<ticket> ... | --mine) [--state open]
      Propose a coherent ticket group before running or persisting it.
  FLOW ticket split <ticket>
      Plan a parent-ticket decomposition, then create and migrate its children.
  FLOW memory search [<query>] [--ticket <key>]... [--label <facet:value>] [--digest] [--semantic] [--threshold <float>] [--branch <branch>] [--limit <n>]
      Search durable Flow memory with structured and semantic filters.
  FLOW memory prune
      Review and confirm removal of superseded memory.
  FLOW memory rebuild [--full]
      Rebuild the derived memory index.
  FLOW measure <throughput|lead-time|friction|reverts|experiment|trend|memory-health|recall-quality|fix-efficacy> [--since <date>] [--until <date>] [--json]
  FLOW measure throughput --checkpoint <personal|work> [--manifest <path>]
      Measure delivery, quality, experiment, or memory outcomes.
  FLOW workspace setup [--guidance]
      Initialize, resume, repair, or validate the current workspace.
  FLOW workspace inspect [<target>] [--json]
      Inspect workspace or target health without mutation.
  FLOW workspace repair [<target>]
      Diagnose a workspace or run and confirm an applicable repair.
  FLOW workspace sync
      Drain queued tracker mutations.
  FLOW maintain backlog status [--preview]
      Inspect backlog fleet state and the next drain decision.
  FLOW maintain backlog drain [--dry-run]
      Run ready backlog work through the bounded worker pool.
  FLOW maintain evolution audit
      Audit Flow machinery and produce actionable evolution work.
  FLOW maintain evolution propose
      Turn observed friction into evolution proposals.
  FLOW maintain evolution epic
      Produce a weekly evolution epic from accepted proposals.
  FLOW maintain evolution expand <epic>
      Expand an accepted epic into runnable child work.
  FLOW maintain evolution drain [--dry-run] [--include-proposals]
      Run ready evolution work through the bounded worker pool.
  FLOW maintain worktrees clean [--dry-run]
      Remove safe stale Flow worktrees from the invoking workspace.
  FLOW maintain quarantine clean [--dry-run]
      Archive workspace-owned quarantined cognitive capsules.
  FLOW help [ticket|memory|measure|workspace|maintain]
      Show all commands or help for one namespace.
