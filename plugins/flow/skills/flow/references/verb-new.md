# new verb

`/flow new`. The PROBLEM-capture front door: collect the problem, no solution or plan (planning is the spec stage's job). Routed from SKILL.md's argument table.

1. Collect PROBLEM-only inputs via `AskUserQuestion`: a one-line **summary**, a **short context** blurb, and the **type**. These are problem-only — no solution, no plan.

2. Create the ticket:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . create \
     --summary "<summary>" --description "<short context>" --type "<type>"
   ```

3. Handle the exit. `create` writes JSON `{"key": "<newkey>"}` to stdout; parse `.key` from it.
   - Exit 0 → surface the new key.
   - Non-zero → surface stderr; stop.

4. Offer `/flow <newkey>` to spec the freshly created ticket. No planning happens in `new` — `spec` owns that.
