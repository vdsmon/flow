# Codex-first friction fixes

Status: approved for implementation on 2026-07-21.

## Motivation

An ordinary Codex ticket run hit four avoidable adapter failures before delivery could
begin: a natural Jira ticket URL was rejected, the workspace facade reused Python 3.11
despite Flow requiring Python 3.12 or newer, the ticket-stage instructions assumed a
Claude/MCP-shaped write path, and the `skill_root` contract was easy to bind one directory
too high. These are boundary defects, not reasons to add another orchestration layer.

The goal is to make the existing Codex path direct and unsurprising while keeping Flow's
current runtime and stage model intact.

## Design

1. **Jira browse URLs.** The public router recognizes an HTTP(S) URL whose path ends in
   `/browse/<key>`. It extracts `<key>` and accepts it only when the key matches the active
   workspace's configured tracker grammar. No Jira hostname or new workspace setting is
   introduced. Static namespaces and forge URL precedence remain unchanged.

2. **Python runtime selection.** Launcher setup verifies that the generated facade can run
   Flow with Python 3.12 or newer. The facade uses a compatible current or PATH interpreter;
   when none is available it may run Flow through `uv`. If neither route is usable, setup
   fails before replacing the facade and gives a Codex-specific remediation. Individual
   Flow modules are not backported to Python 3.11.

3. **Codex ticket writes.** The ticket-stage contract tells Codex to fetch the complete
   tracker payload through the workspace facade, retain the exact JSON returned, and use
   the host's rooted exact writer for `ticket.json` and `ticket.out`. MCP remains available
   when the host exposes it. Shell redirection is retained only for generic/headless use.
   A summarized ticket payload is not valid stage evidence.

4. **Skill-root preflight.** The entry contract includes a Codex cache-shaped example whose
   value ends at `.../skills/flow`. Before launcher invocation, the driver verifies that
   both `<skill_root>/SKILL.md` and `<skill_root>/scripts/flow_launcher.py` exist.

## Verification

- Router tests cover a valid Jira browse URL, a browse URL with a key outside the workspace
  grammar, and preservation of forge/static routing.
- Launcher tests invoke the generated facade under Python 3.11 and prove that it selects a
  supported runtime; setup failure is tested when no compatible interpreter or `uv` exists.
- Documentation contract checks cover the Codex ticket writer and skill-root preflight.
- Focused router, launcher, and seam checks are sufficient; this change does not require a
  full ticket-to-PR pipeline run.

## Out of scope

- Adding Jira site-host configuration.
- Lowering Flow's declared Python requirement.
- Adding migration state, compatibility aliases, or another runtime abstraction.
- Changing tracker payload shape, lifecycle behavior, or stage ownership.
