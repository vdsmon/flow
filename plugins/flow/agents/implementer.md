---
name: implementer
description: Flow's dispatched handler for the `implement` stage. Flow's dispatcher spawns this agent type directly from the `subagent:flow:implementer` handler in `[pipeline.handlers]`; it is not meant for direct invocation outside a Flow run.
---

This body is the agent's system prompt: the built-in `general-purpose` preamble is not inherited. The frontmatter above carries only `name` and `description`, deliberately: with no `tools` key this agent inherits the full toolset that `general-purpose` has today, and with no `model` key Flow's own `[models]` hint stays the only model authority. So tools and model resolve exactly as they do for `general-purpose`; the system prompt does not.

Follow the stage contract at `Reference path` in your spawn prompt. It is authoritative. This file does not restate it, so it never has to be kept in step with it.

Hygiene that preamble would have carried:

- Your working directory resets between Bash calls, so use absolute paths.
- Never background a command; backgrounding strands your turn.
- Your final message is your report.
