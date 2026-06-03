# status verb

`/flow status [<ticket>]`. Read-only. Routed from SKILL.md's argument table.
Reports run state, stage progress, the lease, and any drift / attention flags.

1. Run:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/status.py [--ticket <KEY>] \
     --workspace-root .
   ```
   Pass `--ticket <KEY>` when `$ARGUMENTS` had a positional; otherwise run bare (it lists every run in the workspace).
   Add `--json` only when a machine consumer needs the raw payload; default is the human table.

2. Handle the exit:
   - Exit 0 → surface the table verbatim.
   - Exit 1 → workspace not initialized.
     Surface stderr + the `/flow init` hint; stop.
