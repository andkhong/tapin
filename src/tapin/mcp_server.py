"""MCP server so any MCP-capable agent can read, claim and contribute to handoffs, adapter or not.

Implements MCP 2025-11-25 over stdio: one JSON-RPC 2.0 message per line on stdin and stdout, logs on stderr.
"""

from __future__ import annotations

import inspect
import json
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tapin import __version__, capture, config, workspace
from tapin.store import Store, iso, utcnow

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Tap In passes in-progress work between AI coding agents when one hits a usage limit. "
    "At the start of a session call handoff_status with your workspace path; if a handoff is waiting, "
    "call claim_handoff and continue from it. Call checkpoint at milestones, with what is done, what is in progress "
    "and your session id, so another agent can pick up if you stop, and create_handoff if you know you are about to stop."
)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _store(workspace_path: str) -> Store:
    return Store(workspace.find_root(Path(workspace_path).expanduser()))


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


def get_handoff(workspace_path: str, handoff_id: str | None = None) -> str:
    """Read a handoff document without claiming it: the given id, else the pending one, else the most recent."""
    store = _store(workspace_path)
    pending = store.pending()
    handoff_id = handoff_id or (pending.id if pending else None) or next((m["id"] for m in store.list_handoffs()), None)
    if handoff_id is None:
        return "No handoffs in this workspace."
    return store.read_handoff(handoff_id)


def claim_handoff(workspace_path: str, agent: str, session_id: str | None = None) -> str:
    """Claim the waiting handoff so no other session also picks it up, and return its full document."""
    store = _store(workspace_path)
    pending = store.claim(agent, session_id)
    if pending is None:
        return "No unclaimed handoff is waiting in this workspace."
    return store.read_handoff(pending.id)


def checkpoint(
    workspace_path: str,
    agent: str,
    summary: str,
    next_steps: str,
    decisions: str = "",
    in_progress: str = "",
    session_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> str:
    """Record progress for whoever continues this work if you stop.

    agent: your agent name, e.g. claude, codex or cursor. summary: what is done so far. in_progress: the file and step
    you are working on now. next_steps: the exact next step. decisions: key decisions and why. Pass session_id whenever
    you know it, so the next handoff from your session includes this checkpoint. Pass model and effort (your reasoning
    effort) if you know them; otherwise Tap In fills them in from your session log where it can (Claude Code and Codex)."""
    root = workspace.find_root(Path(workspace_path).expanduser())
    store = Store(root)
    agent = agent.strip()
    session_id, model, effort = capture.identify(agent, root, session_id or None, model or None, effort or None)
    store.append_checkpoint(
        {
            "at": iso(utcnow()),
            "agent": agent,
            "model": model,
            "effort": effort,
            "session_id": session_id,
            "done": summary.strip(),
            "in_progress": in_progress.strip(),
            "decisions": decisions.strip(),
            "next_steps": next_steps.strip(),
        }
    )
    if session_id:
        return f"Checkpoint saved to {store.notes_path} (session {session_id})"
    return f"Checkpoint saved to {store.notes_path} (no session id: pass session_id so the next handoff from this session includes it)"


def create_handoff(
    workspace_path: str,
    agent: str,
    summary: str,
    next_steps: str,
    session_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> str:
    """Hand off now, e.g. when you are about to hit a usage limit. Packages your summary with the workspace diff.
    Pass session_id, model and effort if you know them; Tap In fills them in for Claude Code and Codex otherwise."""
    store, handoff_id = capture.capture_summary(
        Path(workspace_path).expanduser(), agent, summary, next_steps, config.load(), session_id or None, model or None, effort or None
    )
    return f"Handoff written to {store.handoff_file(handoff_id)}. Another agent can continue with `tapin to <agent>` or by opening this folder."


TOOLS: dict[str, Callable[..., str]] = {
    tool.__name__: tool for tool in (handoff_status, get_handoff, claim_handoff, checkpoint, create_handoff)
}


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class ArgumentError(ValueError):
    """Arguments that don't fit a tool's schema; reported to the model so it can retry."""


def input_schema(tool: Callable[..., str]) -> dict[str, Any]:
    """Every tool parameter is a string; one that defaults to None also accepts null."""
    properties: dict[str, Any] = {}
    required = []
    for name, param in inspect.signature(tool).parameters.items():
        if param.default is inspect.Parameter.empty:
            properties[name] = {"type": "string"}
            required.append(name)
        elif param.default is None:
            properties[name] = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
        else:
            properties[name] = {"type": "string", "default": param.default}
    return {"type": "object", "properties": properties, "required": required}


def tool_definitions() -> list[dict[str, Any]]:
    return [{"name": name, "description": inspect.getdoc(tool), "inputSchema": input_schema(tool)} for name, tool in TOOLS.items()]


def _bind(tool: Callable[..., str], arguments: dict[str, Any]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    missing, not_strings = [], []
    for name, param in inspect.signature(tool).parameters.items():
        if name not in arguments:
            if param.default is inspect.Parameter.empty:
                missing.append(name)
        elif isinstance(arguments[name], str) or (arguments[name] is None and param.default is None):
            kwargs[name] = arguments[name]
        else:
            not_strings.append(name)
    problems = []
    if missing:
        problems.append(f"missing required argument(s): {', '.join(missing)}")
    if not_strings:
        problems.append(f"must be strings: {', '.join(not_strings)}")
    if problems:
        raise ArgumentError("; ".join(problems))
    return kwargs


def _initialize(params: dict[str, Any]) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    return {
        "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "tapin", "version": __version__},
        "instructions": INSTRUCTIONS,
    }


def _call_tool(params: dict[str, Any]) -> dict[str, Any]:
    name, arguments = params.get("name"), params.get("arguments") or {}
    if not isinstance(name, str) or not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, "tools/call needs a tool name and an arguments object")
    tool = TOOLS.get(name)
    if tool is None:
        raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
    try:
        text, is_error = tool(**_bind(tool, arguments)), False
    except ArgumentError as exc:
        text, is_error = f"Invalid arguments for {name}: {exc}", True
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        text, is_error = f"{name} failed: {type(exc).__name__}: {exc}", True
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


METHODS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "initialize": _initialize,
    "ping": lambda params: {},
    "tools/list": lambda params: {"tools": tool_definitions()},
    "tools/call": _call_tool,
}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(message: Any) -> dict[str, Any] | None:
    """Answer one JSON-RPC message. Notifications, and responses to requests we never send, get no reply."""
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "Invalid Request: expected a single JSON-RPC message object")
    if "method" not in message or "id" not in message:
        return None
    msg_id, method, params = message["id"], message["method"], message.get("params") or {}
    if message.get("jsonrpc") != "2.0" or not isinstance(method, str) or not isinstance(params, dict):
        return _error(msg_id, INVALID_REQUEST, "Invalid Request")
    handler = METHODS.get(method)
    if handler is None:
        return _error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")
    try:
        return {"jsonrpc": "2.0", "id": msg_id, "result": handler(params)}
    except RpcError as exc:
        return _error(msg_id, exc.code, str(exc))
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        return _error(msg_id, INTERNAL_ERROR, f"Internal error: {type(exc).__name__}: {exc}")


def run() -> None:
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # stray prints must not corrupt the protocol stream
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        try:
            reply = handle(json.loads(line))
        except ValueError:
            reply = _error(None, PARSE_ERROR, "Parse error")
        if reply is None:
            continue
        try:
            out.write(json.dumps(reply).encode() + b"\n")
            out.flush()
        except BrokenPipeError:
            return
