# Tap In

**Hand off unfinished AI coding work to another agent, right where it stopped.**

Tap In lets you move a task between Claude Code, Codex, Cursor and other coding agents without starting over. When an agent hits its usage limit, Tap In captures the work in the background and notifies you. The next agent you open in that folder starts with the task history, the plan, the commands that were run and the exact workspace diff.

Tap In never asks the stopped agent to summarize its own work. A rate-limited agent can't respond, so the handoff is built from what is already on disk.

## Why Tap In?

Coding agents are easy to interrupt. You hit a usage limit mid-task, want a second opinion from another model, move from a terminal agent to an IDE, or restart after a crash. Each time, the next agent has to rediscover the work:

- What was the goal?
- Which files changed, and which step was half done?
- What was already tried?
- Which commands failed?
- What was the plan?

Copying a transcript is noisy. Asking the previous agent for a summary is unreliable, and impossible once it's rate-limited. `AGENTS.md` and `CLAUDE.md` describe how the project works, not where this task stands.

Other tools convert a session when you ask, and one hands off from Claude Code automatically. Tap In works in every direction between Claude Code, Codex and Cursor. It detects limits from each agent's own stop events instead of matching text, **warns the agent before the limit** so it can save its own account of the work, and **loads the handoff into whichever agent you open next**, including desktop apps and IDEs. See [How Tap In compares](#how-tap-in-compares).

## How it works

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/andkhong/tapin/main/docs/images/flow-dark.svg">
  <img alt="When Claude Code hits its usage limit, its hook starts tapin capture, which turns the session log, working tree and plan into a handoff in .tapin/. You start Codex, which claims the handoff and picks up mid-step." src="https://raw.githubusercontent.com/andkhong/tapin/main/docs/images/flow-light.svg">
</picture>

1. **An agent stops on its limit.** Its hook starts `tapin capture` in a background process, so the agent isn't held up.
2. **Tap In writes a handoff.** It reads the session log, snapshots git and copies the plan into `.tapin/handoffs/<id>/handoff.md`, marks it as waiting, and shows a desktop notification.
3. **You start the next agent.** Run `tapin to codex`, or just open Codex in the same folder. Its session-start hook finds the waiting handoff, claims it so no other session picks it up, and tells Codex to read it before doing anything else.

## What a handoff contains

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/andkhong/tapin/main/docs/images/handoff-dark.svg">
  <img alt="Each section of handoff.md is filled from something already on disk: the hook payload, Tap In's instructions, checkpoint notes, the plan file, the session log and git." src="https://raw.githubusercontent.com/andkhong/tapin/main/docs/images/handoff-light.svg">
</picture>

A handoff mixes evidence with the previous agent's own account. The git status and diff show the real state of the repository. The last message and the conversation show what the agent believed it had done. The handoff tells the next agent to treat those statements as unverified, and to check the workspace before editing.

The header names the model and reasoning effort behind the stopped session, and the latest checkpoint from that same session says what was done, what was in progress and the next step, who recorded it, and how long before the stop.

The workspace section (the diff plus new untracked files) is capped at 20,000 characters and the session digest at 12,000, and common secret formats are redacted before the file is written.

## Install

```sh
curl -LsSf https://raw.githubusercontent.com/andkhong/tapin/main/install.sh | sh
```

If you already use [uv](https://docs.astral.sh/uv/), install from GitHub until Tap In is on PyPI:

```sh
uv tool install https://github.com/andkhong/tapin/archive/refs/heads/main.tar.gz && tapin install
```

The one-liner needs only `curl`. It installs uv if you don't have it, uv fetches Python 3.11+ if needed, and then `tapin install` sets up the agents it finds and prints what it changed. Set `TAPIN_SKIP_AGENT_SETUP=1` to install just the `tapin` command. Run the one-liner again at any time to update Tap In.

Tap In reads Claude Code and Codex session logs itself, so there is nothing else to install. If you prefer [`continues`](https://github.com/yigitkonur/cli-continues) for session digests, select it with `readers.<agent> = "continues"` in the config; it needs Node.js.

`tapin install` adds Tap In's hooks to Claude Code, Codex and Cursor, and registers the Tap In MCP server with each. The hooks include a `PostToolUse` hook for Claude Code and Codex, and Claude Code also gets Tap In's status line; together they [warn the agent before a usage limit](#warn-before-the-limit). With no `--agents`, it only sets up the agents installed on this machine and tells you which ones it skipped. Hooks call a stable launcher at `~/.tapin/bin/tapin`, so upgrading or reinstalling Tap In doesn't change them.

```sh
tapin install --agents claude,codex   # configure only these agents, even if they aren't detected
tapin install --no-mcp                # skip MCP server registration
tapin install --no-statusline         # leave Claude Code's status line alone (Claude Code then gets no usage warnings)
tapin install --instructions          # also add a short Tap In section to ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md
tapin doctor                          # check the launcher, hooks, status line, Codex hook trust and session reader
```

Every config file Tap In changes is backed up first as `<file>.tapin-backup-<timestamp>`.

**Codex only runs hooks you trust.** Open Codex, run `/hooks`, and check that the Tap In `SessionStart`, `Stop` and `PostToolUse` entries are active. `tapin doctor` reports whether Codex has recorded that trust.

To remove everything: `tapin uninstall`. To remove the `tapin` command as well:

```sh
curl -LsSf https://raw.githubusercontent.com/andkhong/tapin/main/install.sh | sh -s -- --uninstall
```

## Quick start

When an agent hits its limit, you'll get a notification. In that project folder, either run:

```sh
tapin to codex
```

or open Codex there yourself. Either way, Codex starts with the handoff.

You can also hand off at any time, without waiting for a limit:

```sh
tapin to claude              # hand the newest non-Claude session here to Claude Code and launch it
tapin to cursor --from codex # choose which agent's session to hand off
tapin capture                # just write a handoff; the next agent you open picks it up
tapin status                 # show the waiting handoff and recent ones
```

`tapin status` looks like this:

```text
Workspace: /Users/you/projects/pipeline
Pending:   20260912T222249Z-codex from codex (waiting until 2026-09-13T10:22:49Z)
  20260912T222249Z-codex  usage_limit  /Users/you/projects/pipeline/.tapin/handoffs/20260912T222249Z-codex/handoff.md
```

## Common workflows

### Continue after a usage limit

Capture is automatic when:

- **Claude Code** stops with a `rate_limit` error (add others, such as `overloaded`, with `limit_errors`)
- **Codex** ends a turn on a usage-limit error
- **Cursor** stops with an error

Then run `tapin to <agent>`, or open the next agent in the folder.

### Recover after a crash, restart or full context window

These don't trigger automatic capture. `tapin to` reads the session logs directly, so it works anyway:

```sh
tapin to codex --from claude
```

### Switch agents on purpose

Hand a plan to a different model to implement, get a second agent to debug a partial change, or move from the terminal to your IDE:

```sh
tapin to cursor --from claude
```

### Hand off to a desktop app

Tap In can't start Codex Desktop or the Cursor IDE with a prompt, so for those:

```sh
tapin to codex --print
```

This writes the handoff, copies the prompt to your clipboard, and leaves the handoff waiting. Open the app in the project folder and start a new chat; its session-start hook loads the handoff.

If Cursor's terminal agent isn't installed, `tapin to cursor` opens the Cursor IDE the same way.

### Only one session picks it up

The first new session to start in the folder claims the handoff, and later sessions don't see it again. Run `tapin capture` to write a fresh one. A handoff that nobody claims stops being offered after 12 hours.

## Warn before the limit

A capture after the stop can rebuild almost everything from disk, except what the agent was about to do next. So when an agent gets close to a usage limit, Tap In tells it, in its own context, to record a checkpoint while it can still respond:

```text
[tapin] Usage warning: this Codex account has used 97% of its 5-hour limit (resets 14:28). You may be stopped mid-task soon. Before your next step, record a checkpoint: call the Tap In MCP tool `checkpoint` with `session_id` `019d4b2e-5c1f-7a38-9e60-3b7f1c2d8a45` (or run `tapin checkpoint --session 019d4b2e-5c1f-7a38-9e60-3b7f1c2d8a45`) with what is done, what is in progress (file and step), and the exact next step. Then continue the task.
```

The checkpoint is tied to that session and goes into its next handoff. Each warning is given once per limit window and threshold.

- **Claude Code** reports usage only to its status line, and only on a Pro or Max plan. `tapin install` sets Tap In's status line, which saves the numbers for the `PostToolUse` hook to read. If you already have a status line, Tap In's runs yours and shows its output unchanged, and `tapin uninstall` puts yours back.
- **Codex** records usage in its session log. After each tool call, the `PostToolUse` hook reads the end of that log. Trust the hook in `/hooks` first.
- **Cursor** has no usage data, so it gets no warning.

A project's own status line, in `.claude/settings.json` or `.claude/settings.local.json`, overrides Tap In's, so that project gets no usage readings and no warnings (tools such as Graft add one, and `tapin doctor` flags it). Run `tapin statusline --project` in the project to set Tap In's status line in `.claude/settings.local.json`, which runs the project's own and shows its output. `tapin statusline --project --remove` undoes it.

Warnings come at 90% and 97% by default. Set `warn_thresholds` in the config to change them, or to `[]` to turn warnings off.

## Supported agents

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/andkhong/tapin/main/docs/images/agents-dark.svg">
  <img alt="Agents never talk to each other directly. Claude Code, Codex, Cursor and any MCP agent each write a handoff to the project's .tapin/ folder when they stop and load one from it when they start." src="https://raw.githubusercontent.com/andkhong/tapin/main/docs/images/agents-light.svg">
</picture>

Agents never talk to each other directly. Each one writes to and reads from the project's `.tapin/` folder, so supporting another agent means adding one adapter rather than a bridge to every other agent.

- **Claude Code** and **Codex** (CLI and Desktop): Tap In reads their session logs directly, from `~/.claude/projects` and `~/.codex/sessions`.
- **Cursor**: its chats aren't readable from disk, so Tap In's own Cursor hooks record prompts, responses, file edits and shell commands as they happen.

### MCP and other agents

Any agent that supports MCP can use the Tap In MCP server, with or without an adapter:

| Tool | What it does |
|---|---|
| `handoff_status` | Shows whether a handoff is waiting in this workspace |
| `get_handoff` | Reads a handoff without claiming it |
| `claim_handoff` | Claims the waiting handoff so no other session takes it |
| `checkpoint` | Records what is done, what is in progress, decisions and next steps, with the session, model and reasoning effort (filled in from the session log for Claude Code and Codex); the latest one from the same session goes into its next handoff |
| `create_handoff` | Writes a handoff now, for example when an agent knows it is about to stop |

Agents without MCP can record a checkpoint from the shell with `tapin checkpoint --agent <name> --summary "..." --in-progress "..." --next-steps "..."` (plus `--decisions`, `--session`, `--model`, `--effort` and `--workspace` if needed), which writes the same note as the MCP tool, and can use the plain file format described in [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Handoff files

```text
.tapin/
  pending.json                    the handoff waiting to be picked up
  handoffs/
    20260912T222249Z-codex/
      handoff.md                  what the next agent reads
      meta.json                   stop details: agent, session, reason, git branch and HEAD
  checkpoints.jsonl               checkpoints written through MCP or `tapin checkpoint`
  notes.md                        the same checkpoints, readable
  journal/                        Cursor activity recorded by hooks
```

`.tapin/` is added to `.git/info/exclude`, so it never shows up in `git status` or gets committed, and your `.gitignore` is left alone.

## How Tap In compares

As of September 2026:

| Tool | Directions | When it runs | How the next agent gets it |
|---|---|---|---|
| `continues` | From any of the 16 agents whose logs it reads, into another agent | When you run it | It converts the session into the other agent |
| relay | From Claude Code only; it reads only Claude Code session logs (a Codex reader is an open request) | Automatically, when text such as "429" or "rate limit" appears in tool output, or by polling the log | It launches the next agent's CLI with a compact, scored context |
| Codex `/import` (Codex CLI 0.140+) | Claude Code to Codex | When you run it | Codex brings in the Claude Code sessions and settings |
| `/codex:transfer` | Claude Code to Codex | When you run it | It turns the Claude Code session into a Codex thread |
| Tap In | Any direction between Claude Code, Codex and Cursor | Automatically on each agent's stop event (not yet observed live for Codex; see [Limitations](#limitations)), with a warning before the limit for Claude Code and Codex; or when you run `tapin to` | A hook loads the handoff when the next agent starts, including desktop apps and IDEs; MCP tools add checkpoints, and the handoff is plain files any agent can read |

Each of these tools is good at something: [`continues`](https://github.com/yigitkonur/cli-continues) converts between many agents on demand, [relay](https://github.com/Manavarya09/relay) hands off from Claude Code automatically, and Codex `/import` and `/codex:transfer` from [OpenAI's Claude Code plugin](https://github.com/openai/codex-plugin-cc) move a Claude Code session into Codex when you ask.

## Using Tap In with Graft

[Graft](https://github.com/trailhq/Graft) maps the codebase and Tap In carries the task, so they work together.

- **Status line.** In a repo where Graft set a status line, run `tapin statusline --project` so warnings before the limit keep working. Graft's status line still shows.
- **Codex hook trust.** If Codex marks Tap In's hooks untrusted after you install either tool, trust them again in `/hooks`. Codex ties trust to each hook's position in `hooks.json`, and `tapin install` tells you when another tool's hooks come before Tap In's.
- **`tapin doctor`** checks both.

## Privacy and security

Tap In is local-only. Handoffs are written to your repository's `.tapin/` folder, there is no account, and session data is never sent anywhere. By default Tap In makes no network requests. The only exception is the optional `continues` reader, which downloads `continues` from npm through `npx` the first time it runs.

Before writing a handoff, Tap In redacts common secret formats: Anthropic and OpenAI API keys, GitHub tokens, AWS access key IDs, Google API keys, Slack tokens and bearer headers. That is a safety net, not a guarantee.

New untracked files are copied into the handoff, except those whose names look like secrets (`.env`, `*.pem`, `*.key`, `id_rsa*`, `credentials.json`, `.netrc`, `*.tfvars` and similar), which are listed without their contents.

Tap In won't load a handoff from a `.tapin/` folder that is tracked by git, so a cloned repository can't plant instructions for your agent.

A handoff can still contain proprietary code, file paths, command output and anything said in the session. Don't share one without reading it. Handoffs are never deleted automatically; remove `.tapin/handoffs/` when you no longer need them.

## Configuration

`~/.tapin/config.toml` overrides the defaults in [`src/tapin/config.py`](src/tapin/config.py):

```toml
handoff_ttl_hours = 24                          # how long a handoff waits to be claimed (default 12)
limit_errors = ["rate_limit", "overloaded"]     # Claude Code errors that trigger capture

[agents.codex]
command = ["/Applications/ChatGPT.app/Contents/Resources/codex"]
```

| Key | Default | Controls |
|---|---|---|
| `handoff_ttl_hours` | `12` | How long an unclaimed handoff is offered to new sessions, capped at 7 days |
| `limit_errors` | `["rate_limit"]` | Claude Code `StopFailure` errors that trigger capture. Re-run `tapin install` after changing it, since it also sets the hook matcher |
| `limit_message_pattern` | usage/rate limit, quota, 429 | Which Cursor error messages count as a limit |
| `diff_max_chars` / `digest_max_chars` | `20000` / `12000` | Size caps for the workspace section (diff plus new untracked files) and the session digest |
| `brief_max_chars` | `3500` | Size of the note injected when a session starts |
| `checkpoint_max_age_hours` | `12` | How long before the stop a checkpoint recorded without a session id can be and still go into a handoff. A checkpoint from the same session is included however old |
| `warn_thresholds` | `[90, 97]` | Usage percentages at which Claude Code and Codex are told to record a checkpoint before a limit. `[]` turns the warnings off |
| `agents.<name>.command` | `claude`, `codex`, `cursor agent` | How `tapin to` launches each agent |
| `readers.<name>` | `claude-log`, `codex-log`, `journal` for Cursor | How each agent's session is read. `continues` is an optional alternative for Claude Code and Codex that needs Node.js |
| `continues.command` / `continues.timeout_seconds` | `npx -y continues@4.1.1`, `180` | Optional: used only when a reader is set to `continues`. The command it runs and how many seconds to wait for it |

## Limitations

- **Nothing is verified automatically.** The handoff tells the next agent to check the workspace, but Tap In itself doesn't compare the repository against the captured state.
- **Reasoning doesn't transfer.** Claude Code stores little of its thinking and Codex encrypts its reasoning; what transfers is what the agent wrote, ran and edited.
- **Codex limit capture hasn't been observed live.** Detection matches the `usage_limit_exceeded` record Codex writes when a usage limit ends a turn, checked against real Codex 0.154 logs. Whether Codex runs its Stop hook when a limit ends a turn hasn't been observed yet. `tapin to <agent> --from codex` works regardless.
- **Cursor captures on any agent error,** not only usage limits. Its `stop` hook doesn't say why a turn failed. When the `sessionEnd` that follows carries a usage-limit message, the handoff's reason is updated to `rate_limit`.
- **Codex usage warnings read a log format Codex calls unstable.** Codex's hook docs say the session log "isn't a stable interface for hooks", so a Codex update could stop the warnings without an error. They were checked against Codex 0.154 logs.
- **Claude Code warnings need the status line to run.** Claude Code only reports usage to its status line, so warnings work in interactive sessions (not `claude -p`), on Pro or Max plans, and only after the first response in a session.
- **One waiting handoff per workspace.** A new capture replaces the one waiting to be claimed; older handoffs stay in `.tapin/handoffs/`.
- **macOS and Linux.** CI runs the test suite on both. Notifications use `osascript` on macOS and `notify-send` on Linux, and clipboard copy uses `pbcopy`, `wl-copy`, `xclip` or `xsel`, whichever is installed. Windows isn't supported yet, because file locking uses `fcntl`.

## Design principles

- **Evidence over narration.** The git diff and the commands that ran are more reliable than a summary.
- **No model call at capture time.** The agent that stopped can't help, so capture reads files only.
- **Check before editing.** The next agent reconciles the workspace with the handoff before changing anything.
- **Portable.** Handoffs are plain Markdown and JSON that any agent, or person, can read.
- **Local-first.** Your code and session history stay on your machine.
- **Useful without a failure.** `tapin to` works whenever you want to switch, not only after a limit.

## Development

```sh
git clone https://github.com/andkhong/tapin
cd tapin
uv tool install --editable .            # the `tapin` command runs this checkout
tapin install
uv run pytest
sh tests/installer/test_install_sh.sh   # install.sh end to end, in a throwaway HOME
python3 docs/images/make_diagrams.py    # regenerate the diagrams
```

## Contributing

Contributions are especially useful for:

- Adapters for more agents (Gemini CLI, GitHub Copilot CLI, OpenCode and others)
- Reports of Codex running its Stop hook when a usage limit ends a turn, to confirm capture
- A `verify` command that compares the workspace against a handoff
- Windows support
- Tests across real repositories and agent workflows

## License

MIT. See [LICENSE](LICENSE).
