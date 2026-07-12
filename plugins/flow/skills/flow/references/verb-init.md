# init verb

`/flow init` (optionally `--reconfigure`, `--resume`). One-time workspace setup. Routed from SKILL.md's argument table.

1. Bind the initialized checkout as the logical `<absolute task_root>` and use it as
   the explicit workdir for every call in this procedure. Check whether
   `<absolute task_root>/.flow/.initialized` already exists.
   If yes AND `--reconfigure` was NOT passed, refuse with the message: "workspace already initialized; re-run with `/flow init --reconfigure` to redo."
   Stop.

2. Collect answers through the adapter's user-input capability:
   - **backend**: `jira` or `beads`.
   - **bundle**: `bare` (no skill handlers), `recommended` (auto-resolved from installed `.flow-bundle.toml` manifests), or `custom` (user supplies per-stage overrides).
   - For `backend=jira`: ask for `cloud_id`, `project_key`, and optional `assignee_account_id`.
   - For `backend=beads`: ask for `prefix` (lowercase slug, default derived from current dir name).

3. Create a tmp JSON file in one call:
   ```bash
   mktemp "${TMPDIR:-/tmp}/flow-init-XXXXXX.json"
   ```
   Capture the printed absolute path as the logical `answers_path`. This value lives in
   orchestration context, not only in a shell variable, so a later host call can use it
   after shell state resets. Write the JSON below to that absolute path with the
   adapter's exact file-write primitive. Keys are FLAT and mirror init.py's
   CLI flags (dash or underscore both work; `_merge_config_file` maps top-level
   keys onto the matching flag — a nested `"jira": {...}` block would be
   silently ignored and init would fail asking for `--jira-cloud-id`):
   ```json
   {
     "backend": "<backend>",
     "bundle": "<bundle>",
     "workspace_root": "<absolute task_root>",
     "jira_cloud_id": "...",
     "jira_project_key": "...",
     "jira_assignee_account_id": "...",
     "beads_prefix": "..."
   }
   ```
   Omit the irrelevant keys (`jira_*` or `beads_prefix`) based on backend.

4. Run init:
   ```bash
   FLOW_HARNESS="<codex|claude-code|generic>" \
     python3 "<skill-root>/scripts/init.py" --config "<absolute answers_path>"
   ```
   Codex uses `FLOW_HARNESS=codex` on this exact call. Claude Code may use
   `claude-code` or its compatibility default. A generic adapter uses `generic`; it
   discovers bundles only from an explicit `FLOW_BUNDLE_SEARCH_ROOTS`. Never rely on a
   prior export. Pass `--reconfigure` / `--resume` from adapter-supplied arguments on
   this same invocation when requested.
   Add `--agents-md` for durable repository guidance or a generic harness that cannot
   discover the native plugin. Codex and Claude Code normally load their respective
   plugin manifests, so the tracked block is opt-in rather than their primary loader.
   Safe to add later via `--reconfigure --agents-md`; once present, ordinary
   reconfigure upgrades the managed block without requiring the flag again.
   - Exit 0 → init.py emits result JSON to stdout.
     It also installs the gitignored `.flow/skill_dir` and executable `.flow/flow`
     facade, atomically replacing each file; init, resume, and reconfigure all
     converge them. Surface the backend, namespace, and the host-appropriate Flow
     invocation for the next ticket.
   - Non-zero → surface stderr.
     If `.flow/.initializing` marker exists, suggest `/flow init --resume`.
     (Partial state is transactional; init.py handles resume internally.)

5. Clean up:
   ```bash
   rm -f "<absolute answers_path>"
   ```
