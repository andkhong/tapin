# Tap In

When one AI coding agent hits its usage limit mid-task, Tap In lets another pick up where it stopped.

- **Capture on the limit.** Hooks notice when Claude Code, Codex or Cursor stops on a limit. Tap In then writes a handoff: the task history, the plan, a digest of the session and the exact workspace diff. You get a macOS notification.
- **Continue anywhere.** Run `tapin to codex` (or `claude`, `cursor`), or just open the other agent in the same folder. Its session-start hook loads the handoff.
- **Any agent.** Agents without an adapter use the MCP server (`checkpoint`, `create_handoff`, `claim_handoff`) or the [file protocol](docs/PROTOCOL.md).

Session logs are read by [`continues`](https://github.com/yigitkonur/cli-continues) (Claude Code, Codex). Cursor's activity is recorded by Tap In's own Cursor hooks.

## How it works

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/flow-dark.svg">
  <img alt="When Claude Code hits its usage limit, its hook starts tapin capture, which turns the session log, working tree and plan into a handoff in .tapin/. You start Codex, which claims the handoff and picks up mid-step." src="docs/images/flow-light.svg">
</picture>

When an agent stops on its limit, a hook starts `tapin capture` in the background. The rate-limited agent can't be asked to summarize its own work, so Tap In builds the handoff only from files already on disk.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/handoff-dark.svg">
  <img alt="Each section of handoff.md is filled from something already on disk: the hook payload, Tap In's instructions, checkpoint notes, the plan file, the session log and git." src="docs/images/handoff-light.svg">
</picture>

Each section of the handoff comes from one source. The agent's last message and the exact workspace diff are what let the next agent finish a half-done step instead of starting over.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/agents-dark.svg">
  <img alt="Agents never talk to each other directly. Claude Code, Codex, Cursor and any MCP agent each write a handoff to the project's .tapin/ folder when they stop and load one from it when they start." src="docs/images/agents-light.svg">
</picture>

Agents never talk to each other directly. Each one writes to and reads from the project's `.tapin/` folder, so supporting another agent means adding one adapter rather than a bridge to every other agent.

## Install

Requires Python 3.13, [uv](https://docs.astral.sh/uv/) and Node (for `npx continues`).

```sh
uv tool install --editable .
tapin install            # hooks + MCP server for claude, codex, cursor
tapin doctor
```

- `tapin install --agents claude,codex` limits which agents are configured.
- `--instructions` also adds a short Tap In section to `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`.
- Every config file Tap In changes is backed up first as `<file>.tapin-backup-<timestamp>`.
- **Codex only runs hooks you have trusted:** open Codex, run `/hooks`, and trust the Tap In entries.

To remove Tap In's configuration: `tapin uninstall`.

## Use

```sh
tapin to codex            # hand the newest non-Codex session here to Codex and launch it
tapin to codex --print    # don't launch: print the command, copy the prompt (for Codex Desktop / Cursor IDE)
tapin capture             # just write a handoff for the newest session here
tapin status              # pending handoff and recent ones
```

If Cursor's terminal agent (`cursor-agent`) isn't installed, `tapin to cursor` opens the Cursor IDE instead and copies the prompt; the handoff loads when you start a new chat.

Handoffs are stored in `<repo>/.tapin/`, which is excluded from git automatically.

## Configure

`~/.tapin/config.toml` overrides the defaults in `src/tapin/config.py`, for example:

```toml
handoff_ttl_hours = 24
limit_errors = ["rate_limit", "overloaded"]

[agents.codex]
command = ["/Applications/ChatGPT.app/Contents/Resources/codex"]
```

## Develop

```sh
uv run pytest
```
# cooper
