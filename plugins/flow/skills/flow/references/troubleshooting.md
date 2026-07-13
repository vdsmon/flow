# Troubleshooting: environment + CLI quirks

Machine/tool sharp edges that repeatedly burn fresh sessions. None of these are flow bugs; they are properties of the tools flow drives. Each entry: symptom → cause → remedy.

## gh

- **401 despite `gh auth status` OK (headless/background shells).** gh stores the token in the OS keyring, which a headless session cannot read. Remedy: `export GH_TOKEN=$(gh auth token)` before gh calls.
- **`gh api graphql` does not expand `{owner}` / `{repo}`.** Those placeholders work in REST paths only. Pass explicit owner/repo variables to GraphQL queries.
- **`gh pr list --json commits,files` rejected at scale.** GraphQL node-cost limit (~500k) fails the bulk query. Fetch heavyweight fields per-PR instead of in the list call.
- **A just-pushed PR's CI shows `CANCELLED`.** Same-SHA concurrency cancellation from a rapid re-push, not a failure; re-check after the newer run finishes.
- **Unattended self-merge denied by the permission classifier.** "Merge Without Review … does not specifically authorize merging this PR" at the merge stage is the Claude Code auto-mode classifier, by design — not a flow gate failure. The green PR is a complete deliverable. Remedy: approve per-PR when asked, or add a specific Bash allow-rule for the merge command to enable unattended self-merge.

## mise

- **`python3` shim suddenly broken ("missing lib directory").** The mise-managed python was rebuilt/moved under the shim. Remedy: reinstall the tool (`mise uninstall python && mise install`), or bridge with the system `python3` — flow's runtime scripts are stdlib-only precisely so the bare system interpreter always works.
- **Changing a pipx/uvx tool's options in mise.toml has no effect.** `uvx_args`/`pipx_args` changes apply only at install: `mise uninstall <tool> && mise install`.
- **Single test file:** `mise exec python -- pytest tests/<file>.py` (plain `pytest tests/<file>.py` may resolve to the wrong interpreter).

## git in sandboxed shells

- **`git push -u` loses the upstream tracking ref.** Sandboxed pushes drop the tracking write. Push explicitly: `git push origin <branch>`.
- **"could not read IPC response" noise.** fsmonitor IPC failing inside the sandbox; cosmetic. Judge the command by its exit status. (fsmonitor is disabled globally on this machine after a daemon leak — do not re-enable it.)
- **`gh pr merge` from a detached HEAD fails** ("could not determine current branch"). Merge from a real branch, e.g. a throwaway off `origin/main`.

## launchd / background jobs

- **Unattended run stalls right after "Bootstrap clean", `tempo=blocked`, no `EnterWorktree` call in the transcript.** Claude Code >= 2.1.206 asks an interactive confirmation before `EnterWorktree` enters any worktree OUTSIDE `<repo>/.claude/worktrees/`, and the confirmation is NOT permission-mediated (the tool is "Permission required: No", so no `permissions.allow` rule, auto-mode vouch, or env var suppresses it). flow >= the pool relocation (flow-gh1u) mints worktrees inside `.claude/worktrees/`, which never confirms. On an older flow, the only unattended remedy is upgrading; attended, `claude attach <job>` and approve the prompt.
- **launchd jobs can't find user-installed CLIs.** launchd's minimal PATH omits `~/.local/bin`; export it in the job definition. Test with `launchctl start`, not by running the script in your shell.
- **`claude agents` hangs in a background shell** (blocks on a TTY). Monitor via transcript mtime + `bd`/`gh` state instead.

## zsh

- **`${VAR:+--flag "$VAR"}` expands as ONE word** in zsh (no word-splitting), silently gluing the flag to its value. Use an array: `args=(); [[ -n $VAR ]] && args+=(--flag "$VAR")`.

## ty / ruff

- **Suppressing a ty diagnostic:** the directive is `# ty: ignore[rule]` — the mypy `# type: ignore` form does not suppress ty.
- **IDE-surfaced ty diagnostics can be false positives** (foreign search path). Trust `mise run lint`, which runs ty with the project's config.
- **`ruff format --check` is a separate CI gate** from `ruff check`; run `mise run lint` before declaring green.
