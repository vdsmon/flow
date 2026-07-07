# init verb

`/flow init` (optionally `--reconfigure`, `--resume`). One-time workspace setup. Routed from SKILL.md's argument table.

1. Check whether `.flow/.initialized` already exists in the current workspace.
   If yes AND `--reconfigure` was NOT passed, refuse with the message: "workspace already initialized; re-run with `/flow init --reconfigure` to redo."
   Stop.

2. Collect answers via `AskUserQuestion`:
   - **backend**: `jira` or `beads`.
   - **bundle**: `bare` (no skill handlers), `recommended` (auto-resolved from installed `.flow-bundle.toml` manifests), or `custom` (user supplies per-stage overrides).
   - For `backend=jira`: ask for `cloud_id`, `project_key`, and optional `assignee_account_id`.
   - For `backend=beads`: ask for `prefix` (lowercase slug, default derived from current dir name).

3. Write the answers to a tmp JSON file. Keys are FLAT and mirror init.py's
   CLI flags (dash or underscore both work; `_merge_config_file` maps top-level
   keys onto the matching flag — a nested `"jira": {...}` block would be
   silently ignored and init would fail asking for `--jira-cloud-id`):
   ```bash
   ANSWERS=$(mktemp "${TMPDIR:-/tmp}/flow-init-XXXXXX.json")
   cat > "$ANSWERS" <<EOF
   {
     "backend": "<backend>",
     "bundle": "<bundle>",
     "workspace_root": "$(pwd)",
     "jira_cloud_id": "...",
     "jira_project_key": "...",
     "jira_assignee_account_id": "...",
     "beads_prefix": "..."
   }
   EOF
   ```
   Omit the irrelevant keys (`jira_*` or `beads_prefix`) based on backend.

4. Run init:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/init.py --config "$ANSWERS"
   ```
   Add `--agents-md` when the repo will be run through a non-Claude-Code harness (Cursor, Windsurf, opencode): it writes a marker-guarded `AGENTS.md` entry point so that harness loads the skill (see references/harness.md "Entry point"). Off by default — Claude Code loads via the plugin and needs no AGENTS.md, so a plain init writes no tracked file. Safe to add later via `--reconfigure --agents-md`.
   - Exit 0 → init.py emits result JSON to stdout.
     Surface to user: "Workspace initialized. Backend: <backend>. Namespace: <namespace>. Next step: `/flow do <ticket>`."
   - Non-zero → surface stderr.
     If `.flow/.initializing` marker exists, suggest `/flow init --resume`.
     (Partial state is transactional; init.py handles resume internally.)

5. Clean up:
   ```bash
   rm -f "$ANSWERS"
   ```
