# Troubleshooting: environment + CLI quirks

Machine/tool sharp edges that repeatedly burn fresh sessions. None of these are flow bugs; they are properties of the tools flow drives. Each entry: symptom → cause → remedy.

## gh

- **401 despite `gh auth status` OK (headless/background shells).** gh stores the token in the OS keyring, which a headless session cannot read. Remedy: `export GH_TOKEN=$(gh auth token)` before gh calls.
- **`gh api graphql` does not expand `{owner}` / `{repo}`.** Those placeholders work in REST paths only. Pass explicit owner/repo variables to GraphQL queries.
- **`gh pr list --json commits,files` rejected at scale.** GraphQL node-cost limit (~500k) fails the bulk query. Fetch heavyweight fields per-PR instead of in the list call.
- **A just-pushed PR's CI shows `CANCELLED`.** Same-SHA concurrency cancellation from a rapid re-push, not a failure; re-check after the newer run finishes.

## Bitbucket

- **Comments collection shows `resolution` empty on every comment.** The pull request comments list endpoint omits resolution state, so a thread the bot already resolved still reads as open there. Check the individual comment (`.../comments/<id>`), where `resolution.user` is populated; a `resolve-thread` HTTP 409 on such a thread means it was already resolved, not that the call failed.

## mise

- **`python3` shim suddenly broken ("missing lib directory").** The mise-managed python was rebuilt/moved under the shim. Remedy: reinstall the tool (`mise uninstall python && mise install`), or bridge with the system `python3` — flow's runtime scripts are stdlib-only precisely so the bare system interpreter always works.
- **Changing a pipx/uvx tool's options in mise.toml has no effect.** `uvx_args`/`pipx_args` changes apply only at install: `mise uninstall <tool> && mise install`.
- **Single test file:** `mise exec python -- pytest tests/<file>.py` (plain `pytest tests/<file>.py` may resolve to the wrong interpreter).

## git in sandboxed shells

- **`git push -u` loses the upstream tracking ref.** Sandboxed pushes drop the tracking write. Push explicitly: `git push origin <branch>`.
- **"could not read IPC response" noise.** fsmonitor IPC failing inside the sandbox; cosmetic. Judge the command by its exit status. (fsmonitor is disabled globally on this machine after a daemon leak — do not re-enable it.)
- **`gh pr merge` from a detached HEAD fails** ("could not determine current branch"). Merge from a real branch, e.g. a throwaway off `origin/main`.

## launchd / background jobs

- **Unattended run stalls right after "Bootstrap clean", `tempo=blocked`, no `EnterWorktree` call in the transcript.** Claude Code >= 2.1.206 asks an interactive confirmation before `EnterWorktree` enters any worktree OUTSIDE `<repo>/.claude/worktrees/`, and the confirmation is NOT permission-mediated (the tool is "Permission required: No", so no `permissions.allow` rule, auto-mode vouch, or env var suppresses it). flow >= the pool relocation (flow-gh1u) mints worktrees inside `.claude/worktrees/`, which never confirms. Attended, `claude attach <job>` and approve the prompt.
- **launchd jobs can't find user-installed CLIs.** launchd's minimal PATH omits `~/.local/bin`; export it in the job definition. Test with `launchctl start`, not by running the script in your shell.
- **`claude agents` hangs in a background shell** (blocks on a TTY). Monitor via transcript mtime + `bd`/`gh` state instead.

## zsh

- **`${VAR:+--flag "$VAR"}` expands as ONE word** in zsh (no word-splitting), silently gluing the flag to its value. Use an array: `args=(); [[ -n $VAR ]] && args+=(--flag "$VAR")`.
- **`(eval):1: == not found` from an `echo` separator.** A bare word starting with `=` (the classic is a `===` visual divider between chained commands) is parsed by zsh as the `=cmd` PATH-expansion form, aborting the compound command and silently discarding every command after the separator, so the second half of an investigation never runs (four witnesses across brinta sessions and this seat, 2026-08-05/06). Quote it (`echo "==="`) or use a word separator like `echo ---`.
- **`read-only variable: status` error from a shell recipe.** zsh reserves `status` as a read-only alias for `$?`, plus dozens of other special-parameter names (the full list and its derivation live in `seam_check.py`), so binding one inside a recipe aborts the whole call, not just that line. `seam_check.py`'s `zsh_unsafe_binding_problems` check gates this shape in flow's own prose recipes; rename the local, the way `stage-review_loop.md` renamed its poll variable to `ci_status`. Every unsafe name stays in prose or in a flagless inline span on this line deliberately: an inline span carrying a long option counts as an executable recipe, so demonstrating a real binding here would trip the very check this bullet explains.

## ty / ruff

- **Suppressing a ty diagnostic:** the directive is `# ty: ignore[rule]` — the mypy `# type: ignore` form does not suppress ty.
- **IDE-surfaced ty diagnostics can be false positives** (foreign search path). Trust `mise run lint`, which runs ty with the project's config.
- **`ruff format --check` is a separate CI gate** from `ruff check`; run `mise run lint` before declaring green.
