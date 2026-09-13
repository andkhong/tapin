# Tap In handoff protocol

Any agent can take part by reading and writing these files, whether or not Tap In has an adapter for it. Agents that support MCP can use the `tapin mcp` server instead, which does the same file operations.

## Layout

All state lives in the workspace (the git top level, or the working directory outside git). Tap In adds `.tapin/` to `.git/info/exclude`, so it never shows up in `git status` and is never committed.

```
<workspace>/.tapin/
  pending.json                    the handoff waiting to be picked up (at most one)
  handoffs/<id>/handoff.md        the document the next agent reads
  handoffs/<id>/meta.json         machine-readable details about the stop
  checkpoints.jsonl               checkpoints agents write at milestones, one JSON record per line
  notes.md                        the same checkpoints, for people to read
  journal/<agent>-<session>.jsonl activity recorded by hooks (for agents without a log reader)
  .lock                           flock() taken for every write
```

`<id>` is `<UTC timestamp>-<agent>`, for example `20260912T212400Z-claude`.

## pending.json

```json
{
  "id": "20260912T212400Z-claude",
  "from_agent": "claude",
  "from_session": "3f30c910-…",
  "created_at": "2026-09-12T21:24:00Z",
  "expires_at": "2026-09-13T09:24:00Z",
  "claimed_by": null
}
```

A handoff is **claimable** when `claimed_by` is null, `created_at` is not in the future, and now is before both `expires_at` and `created_at` plus `MAX_TTL_HOURS` (7 days), so a file that asks for a longer life is capped at that.

## Claiming

1. Take an exclusive `flock` on `.tapin/.lock`.
2. Re-read `pending.json`. Stop if it isn't claimable, or if `from_session` is your own session id (a session doesn't claim its own handoff).
3. Set `claimed_by` to `{"agent": …, "session_id": …, "at": …}`, write to a temp file, then rename it over `pending.json`.
4. Release the lock and read `handoffs/<id>/handoff.md` in full.

Only one session wins. Others see `claimed_by` and move on.

Nothing is claimed when `.tapin/` is tracked by git, since a cloned repository would otherwise hand you instructions committed by a stranger.

## handoff.md sections

1. **Header table**: from agent and session, model and reasoning effort, stop time, reason, workspace, git branch/HEAD, session log path
2. **How to continue**: fixed instructions (read everything, check the workspace, finish the step in progress, don't redo finished work, treat claims as unverified)
3. **Where it stopped**: the agent's last message and the checkpoint chosen for this stop (see [Checkpoints](#checkpoints))
4. **Plan**: the plan file, when the source agent has one
5. **Session digest**: recent conversation, file changes and tool activity from the session log
6. **Workspace**: `git status`, diff stat, diff against HEAD, and the contents of new untracked files

Common secret formats (API keys, tokens, bearer headers) are redacted before the file is written.

## Writing a handoff yourself

An agent that knows it is about to stop can write a handoff directly: build `handoff.md` with the sections above, write `meta.json` (at least `from_display`, `reason`, `stopped_at`), then write `pending.json` under the lock. The MCP tool `create_handoff` does exactly this.

## Checkpoints

Under the lock, append one JSON object per line to `checkpoints.jsonl`:

```json
{"at": "2026-09-12T21:10:00Z", "agent": "claude", "model": "claude-opus-5", "effort": "xhigh", "session_id": "3f30c910-…", "done": "…", "in_progress": "…", "decisions": "…", "next_steps": "…"}
```

`at` is UTC. `model`, `effort` and `session_id` are null when unknown, and empty text fields are `""`. Readers skip lines that aren't a JSON object with a valid `at`. When the `checkpoint` tool or `tapin checkpoint` is called for `claude` or `codex` without them, Tap In reads the agent's session logs: the latest assistant record's `message.model` and `effort` for Claude Code, the latest `turn_context` for Codex. With a session id, the model and effort come from that session. Without one, only the agent's sessions in the workspace whose log was written in the last 2 minutes count: the session id is filled in only if there is exactly one, and the model and effort only if they all agree, so a checkpoint is never tied to the wrong session.

Then append the same checkpoint to `notes.md`, leaving out unknown and empty parts:

```
## 2026-09-12T21:10:00Z — Claude Code · claude-opus-5 (effort xhigh) · session `3f30c910-…`

**Done so far:** …

**In progress:** …

**Decisions:** …

**Next steps:** …
```

A handoff includes at most one checkpoint from `checkpoints.jsonl`:

1. The newest record with the handoff's `session_id`, however old.
2. Otherwise (the handoff has no session id, or no record has it), the newest record with a null `session_id`, the same `agent`, and `at` no more than `checkpoint_max_age_hours` (default 12) before the stop. The handoff says it was matched by agent and time.

A record from another session is never included. The handoff counts the records from other sessions or agents recorded since the stop minus `checkpoint_max_age_hours`, and points to `notes.md` for them. Sections of `notes.md` with no record in `checkpoints.jsonl`, such as those written by older versions, are never included.
