# Cooper

**Verified handoffs for AI coding work.**

Cooper lets you switch between Claude Code, Codex, Cursor, and other coding agents without losing the state of an unfinished task.

When an agent stops—because it reaches a limit, loses context, crashes, or you simply want a different model—Cooper creates a **verified checkpoint** of the work. The next agent receives a compact handoff with the task, workspace state, decisions, validation results, and next steps, then verifies that the repository still matches before editing.

Cooper does not just summarize a chat.

It preserves the evidence another agent needs to safely continue:

- The task objective and acceptance criteria
- Git branch, commit, worktree status, and workspace diff
- Changed, staged, and untracked files
- Commands run, including exit codes and relevant output
- Tests, lint, build, and validation results
- Decisions, open questions, blocked work, and recommended next actions
- A compact prompt tailored for the receiving agent

```text
Claude Code stopped halfway through a task.
        ↓
Cooper creates a checkpoint from the session and workspace.
        ↓
You start Codex, Cursor, or another agent.
        ↓
The new agent verifies the checkpoint and continues.
```

## Why Cooper?

Coding agents are powerful, but their work is easy to interrupt.

You may hit a usage limit, switch models for a second opinion, move from a terminal agent to an IDE, restart after a crash, or hand a task to a teammate. In each case, the next agent usually has to rediscover the work:

- What was the original goal?
- Which files changed?
- What was already tried?
- Which tests passed or failed?
- What decisions were made?
- What should happen next?
- Is the repository still in the state the previous agent described?

Copying a transcript is noisy. Asking the previous agent for a summary is unreliable. A generic `AGENTS.md` or `CLAUDE.md` explains project conventions, but not the exact state of the task you were working on.

Cooper captures the current work as a structured checkpoint so the next agent can start from evidence instead of guesswork.

## What Cooper captures

A Cooper checkpoint separates **verified facts** from agent-generated interpretation.

| Checkpoint data | Source | Examples |
|---|---|---|
| Workspace state | Git and filesystem | Branch, commit, diff, staged files, untracked files |
| Validation results | Command execution | Test, lint, type-check, build, and command exit status |
| Session context | Agent history | Goal, plan, decisions, failed approaches, open questions |
| Next actions | Agent session and checkpoint analysis | Suggested files to inspect, commands to run, remaining work |
| Safety information | Cooper checks | Redacted secrets, stale state, conflicts, missing tools |

This distinction matters: a prior agent’s summary may be wrong, stale, or incomplete. Git state and command results are evidence. Cooper gives the receiving agent both, so it can verify the handoff before continuing.

## Install

Cooper requires Python 3.13+, [uv](https://docs.astral.sh/uv/), and Node.js for `npx continues`.

```sh
uv tool install --editable .
cooper install
cooper doctor
```

`cooper install` configures supported agents and installs the Cooper MCP server.

To configure only selected agents:

```sh
cooper install --agents claude,codex
```

To also add short Cooper usage instructions to Claude Code and Codex configuration files:

```sh
cooper install --instructions
```

Every configuration file Cooper changes is backed up first:

```text
<file>.cooper-backup-<timestamp>
```

Codex only runs hooks you explicitly trust. Open Codex, run `/hooks`, and trust the Cooper entries.

To remove Cooper’s configuration:

```sh
cooper uninstall
```

## Quick start

Create a checkpoint when you want to switch agents, recover from an interruption, or preserve the current state of a task.

```sh
# Capture the most recent agent session and current workspace state.
cooper checkpoint

# Start Codex with the newest checkpoint.
cooper resume codex

# Start Claude Code with the newest checkpoint.
cooper resume claude

# Start Cursor with the newest checkpoint.
cooper resume cursor
```

Before the next agent edits code, verify that the current repository still matches the checkpoint:

```sh
cooper verify
```

A typical result looks like this:

```text
Checkpoint: valid with 1 warning

✓ Repository matches checkpoint
✓ Branch and commit match
✓ Workspace diff matches
✓ Expected changed files exist
✓ Latest failing test reproduced
! Docker is unavailable; service-state checks were skipped

Task:
  Fix retry behavior for failed payment webhooks

Next suggested action:
  Inspect retry-state serialization in src/webhooks/events.py
```

## Common workflows

### Recover after an agent stops

If Claude Code, Codex, or Cursor stops unexpectedly, Cooper can capture the latest session and workspace state.

```sh
cooper capture
cooper resume codex
```

This is useful after:

- Usage or rate limits
- Context-window exhaustion
- Provider overloads
- Terminal or IDE crashes
- A laptop restart
- An interrupted long-running task

### Intentionally switch models

You do not need to wait for a failure.

Use Cooper when you want to:

- Ask another model to implement an existing plan
- Move from planning to execution
- Switch to a cheaper or faster model for straightforward changes
- Get a second agent to debug or review a partial implementation
- Move between terminal and IDE workflows

```sh
cooper checkpoint
cooper resume cursor
```

### Continue without launching an agent

Print the receiving-agent command and copy the prepared handoff prompt manually:

```sh
cooper resume codex --print
```

This is useful for Codex Desktop, Cursor IDE, remote development environments, or any workflow where Cooper cannot directly launch the target agent.

### Inspect recent work

```sh
cooper status
```

This shows the current pending checkpoint and recent handoffs.

## How verification works

A handoff should not be trusted blindly.

Between the time one agent stops and another starts, the repository may change. You may switch branches, edit files manually, pull remote changes, run a formatter, or have another agent working in the same directory.

`cooper verify` compares the current workspace against the checkpoint before the receiving agent continues.

It can verify:

- Repository root, Git branch, and current commit
- Staged, unstaged, and untracked changes
- File paths and expected hashes
- Patch and diff compatibility
- Whether required files still exist
- Relevant test, lint, build, or command outcomes
- Whether the checkpoint is stale or superseded
- Conflicts caused by another agent or developer changing the workspace

When the workspace no longer matches, Cooper reports the mismatch instead of silently presenting stale context as current.

## Handoff artifacts

Cooper stores checkpoints in the repository’s `.cooper/` directory.

```text
.cooper/
  handoffs/
    2026-09-12T154500Z-<id>/
      HANDOFF.md
      manifest.json
      git-state.json
      diff.patch
      transcript-summary.md
      commands.jsonl
      validation.json
      files.json
```

`.cooper/` is excluded from Git automatically.

Each handoff includes:

- `HANDOFF.md` — A concise, human- and agent-readable task brief
- `manifest.json` — Checkpoint metadata and schema version
- `git-state.json` — Branch, commit, worktree, and file-state evidence
- `diff.patch` — The captured workspace patch
- `transcript-summary.md` — Relevant session context and decisions
- `commands.jsonl` — Commands run, timestamps, output references, and exit codes
- `validation.json` — Test, lint, build, and verification results
- `files.json` — Relevant files and integrity metadata

The files are designed to be inspectable, portable, and usable without a specific AI vendor.

## Supported agents

Cooper currently supports:

- Claude Code
- OpenAI Codex
- Cursor

Cooper reads session logs using [`continues`](https://github.com/yigitkonur/cli-continues) for Claude Code and Codex. Cursor activity is captured through Cooper’s Cursor hooks.

Agents without a dedicated adapter can use either:

- Cooper’s MCP server
- The portable file-based handoff protocol

## MCP and custom agents

Cooper exposes a local MCP server for agents and tools that can work with structured handoffs.

Available operations include:

- `checkpoint`
- `create_handoff`
- `claim_handoff`
- `verify_handoff`
- `list_handoffs`

This allows custom agents to create, inspect, validate, and claim Cooper checkpoints without relying on a vendor-specific adapter.

For non-MCP integrations, see the file protocol:

```text
docs/PROTOCOL.md
```

## Privacy and security

Cooper is local-first.

By default, checkpoints are stored in your repository under `.cooper/`. Cooper does not require a hosted account or send session data to a remote service.

Because coding-agent transcripts and terminal output can contain sensitive information, review your checkpoints before sharing them outside your machine or repository.

Cooper is designed to support:

- Local-only storage
- Configurable checkpoint retention
- Redaction of sensitive values
- Explicit artifact inspection
- Repository-scoped handoffs
- Portable files you can audit and delete

Do not treat a checkpoint as safe to share automatically. It can contain proprietary code, file paths, command output, implementation details, or session-derived context.

## Configuration

User-level configuration lives in:

```text
~/.cooper/config.toml
```

It overrides defaults in `src/cooper/config.py`.

Example:

```toml
handoff_ttl_hours = 24
limit_errors = ["rate_limit", "overloaded"]

[agents.codex]
command = ["/Applications/ChatGPT.app/Contents/Resources/codex"]
```

You can configure:

- Checkpoint retention and expiration
- Recognized interruption and limit errors
- Agent executable paths
- Enabled agent adapters
- Redaction and capture behavior
- Default validation commands

## Design principles

Cooper is built around a few principles:

- **Evidence over narration.** Git state and command output are more reliable than a chat summary.
- **Facts separate from claims.** The next agent should know what was verified, inferred, attempted, and still unknown.
- **Verify before editing.** A receiving agent should reconcile the current workspace with the handoff before making more changes.
- **Portable by default.** Handoffs should not depend on one vendor, model, or chat format.
- **Local-first.** Your repository state and agent history should remain under your control.
- **Useful without failure.** Cooper supports intentional model switching, recovery, review, and human-to-agent handoff—not only usage-limit interruptions.

## Development

```sh
uv run pytest
```

## Contributing

Cooper is intended to be a portable continuity layer for coding agents.

Contributions are especially useful for:

- Agent adapters
- MCP integrations
- Checkpoint schema improvements
- Workspace and validation checks
- Secret-redaction support
- Documentation and examples
- Tests across real repositories and agent workflows