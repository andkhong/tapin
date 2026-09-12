# Tap In handoff protocol

Any agent can take part by reading and writing these files, whether or not Tap In has an adapter for it. Agents that support MCP can use the `tapin mcp` server instead, which does the same file operations.

## Layout

All state lives in the workspace (the git top level, or the working directory outside git). Tap In adds `.tapin/` to `.git/info/exclude`, so it never shows up in `git status` and is never committed.

```
<workspace>/.tapin/
  pending.json                    the handoff waiting to be picked up (at most one)
  handoffs/<id>/handoff.md        the document the next agent reads
  handoffs/<id>/meta.json         machine-readable details about the stop
  notes.md                        checkpoints agents write at milestones
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

A handoff is **claimable** when `claimed_by` is null and `expires_at` is in the future.

## Claiming

1. Take an exclusive `flock` on `.tapin/.lock`.
2. Re-read `pending.json`. Stop if it isn't claimable, or if `from_session` is your own session id (a session doesn't claim its own handoff).
3. Set `claimed_by` to `{"agent": …, "session_id": …, "at": …}`, write to a temp file, then rename it over `pending.json`.
4. Release the lock and read `handoffs/<id>/handoff.md` in full.

Only one session wins. Others see `claimed_by` and move on.

## handoff.md sections

1. **Header table**: from agent and session, model, stop time, reason, workspace, git branch/HEAD, session log path
2. **How to continue**: fixed instructions (read everything, check the workspace, finish the step in progress, don't redo finished work, treat claims as unverified)
3. **Where it stopped**: the agent's last message and the latest checkpoint note
4. **Plan**: the plan file, when the source agent has one
5. **Session digest**: recent conversation, file changes and tool activity from the session log
6. **Workspace**: `git status`, diff stat, diff against HEAD, and the contents of new untracked files

Common secret formats (API keys, tokens, bearer headers) are redacted before the file is written.

## Writing a handoff yourself

An agent that knows it is about to stop can write a handoff directly: build `handoff.md` with the sections above, write `meta.json` (at least `from_display`, `reason`, `stopped_at`), then write `pending.json` under the lock. The MCP tool `create_handoff` does exactly this.

## Checkpoints

Append to `notes.md` under the lock:

```
## 2026-09-12T21:10:00Z — codex

**Done so far:** …
**Decisions:** …
**Next steps:** …
```

The newest entry is included in every later handoff.
