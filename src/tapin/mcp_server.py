"""MCP server so any MCP-capable agent can read, claim and contribute to handoffs, adapter or not."""

from __future__ import annotations

import json
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from tapin import capture, config, workspace
from tapin.store import Store

server = MCPServer(
    "tapin",
    instructions=(
        "Tap In passes in-progress work between AI coding agents when one hits a usage limit. "
        "At the start of a session call handoff_status with your workspace path; if a handoff is waiting, "
        "call claim_handoff and continue from it. Call checkpoint at milestones so another agent can pick up "
        "if you stop, and create_handoff if you know you are about to stop."
    ),
)


def _store(workspace_path: str) -> Store:
    return Store(workspace.find_root(Path(workspace_path).expanduser()))


@server.tool()
def handoff_status(workspace_path: str) -> str:
    """Show whether a handoff from another agent is waiting in this workspace, and list recent handoffs."""
    store = _store(workspace_path)
    pending = store.pending()
    return json.dumps(
        {
            "workspace": str(store.workspace),
            "pending": None
            if pending is None
            else {
                "id": pending.id,
                "from_agent": pending.from_agent,
                "claimable": store.claimable() is not None,
                "claimed_by": pending.claimed_by,
                "expires_at": pending.expires_at,
                "file": str(store.handoff_file(pending.id)),
            },
            "recent": [
                {"id": m["id"], "from_agent": m["from_agent"], "reason": m["reason"], "created_at": m["created_at"]}
                for m in store.list_handoffs()[:5]
            ],
        },
        indent=2,
    )


@server.tool()
def get_handoff(workspace_path: str, handoff_id: str | None = None) -> str:
    """Read a handoff document without claiming it: the given id, else the pending one, else the most recent."""
    store = _store(workspace_path)
    pending = store.pending()
    handoff_id = handoff_id or (pending.id if pending else None) or next((m["id"] for m in store.list_handoffs()), None)
    if handoff_id is None:
        return "No handoffs in this workspace."
    return store.read_handoff(handoff_id)


@server.tool()
def claim_handoff(workspace_path: str, agent: str, session_id: str | None = None) -> str:
    """Claim the waiting handoff so no other session also picks it up, and return its full document."""
    store = _store(workspace_path)
    pending = store.claim(agent, session_id)
    if pending is None:
        return "No unclaimed handoff is waiting in this workspace."
    return store.read_handoff(pending.id)


@server.tool()
def checkpoint(workspace_path: str, agent: str, summary: str, next_steps: str, decisions: str = "") -> str:
    """Record progress for whoever continues this work: what is done, key decisions and why, and the next steps."""
    store = _store(workspace_path)
    parts = [f"**Done so far:** {summary}"]
    if decisions:
        parts.append(f"**Decisions:** {decisions}")
    parts.append(f"**Next steps:** {next_steps}")
    store.append_note(agent, "\n\n".join(parts))
    return f"Checkpoint saved to {store.notes_path}"


@server.tool()
def create_handoff(workspace_path: str, agent: str, summary: str, next_steps: str) -> str:
    """Hand off now, e.g. when you are about to hit a usage limit. Packages your summary with the workspace diff."""
    store, handoff_id = capture.capture_summary(Path(workspace_path).expanduser(), agent, summary, next_steps, config.load())
    return f"Handoff written to {store.handoff_file(handoff_id)}. Another agent can continue with `tapin to <agent>` or by opening this folder."


def run() -> None:
    server.run()
