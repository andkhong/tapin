import asyncio
import json
import os
import select
import subprocess
import sys
import time

import pytest

from tapin import __version__, cli, deliver, mcp_server
from tapin.readers.claude_log import project_dir_name
from tapin.store import Store

LATEST = mcp_server.PROTOCOL_VERSIONS[0]
CLIENT_INFO = {"capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
REQUIRED = {
    "handoff_status": ["workspace_path"],
    "get_handoff": ["workspace_path"],
    "claim_handoff": ["workspace_path", "agent"],
    "checkpoint": ["workspace_path", "agent", "summary", "next_steps"],
    "create_handoff": ["workspace_path", "agent", "summary", "next_steps"],
}


def test_agent_without_adapter_can_hand_off_and_another_can_claim(repo):
    workspace = str(repo)
    mcp_server.checkpoint(workspace, "gemini", "CSV parser done", "wire up retries", "streaming over batch loads")
    mcp_server.create_handoff(workspace, "gemini", "Halfway through retry backoff in fetch.py", "finish backoff, add tests")

    status = json.loads(mcp_server.handoff_status(workspace))
    assert status["pending"]["from_agent"] == "gemini"
    assert status["pending"]["claimable"]

    document = mcp_server.claim_handoff(workspace, "codex")
    assert "Halfway through retry backoff" in document
    assert "streaming over batch loads" in document
    assert "No unclaimed" in mcp_server.claim_handoff(workspace, "cursor")
    assert "Halfway through retry backoff" in mcp_server.get_handoff(workspace)


def test_tapin_to_uses_mcp_handoff_from_unknown_agent(repo, fake_reader, monkeypatch):
    mcp_server.create_handoff(str(repo), "gemini", "Designing the schema", "write migrations")
    launched = {}
    monkeypatch.setattr(deliver, "launch", lambda argv, cwd: launched.update(argv=argv))
    assert cli.main(["to", "claude", "--cwd", str(repo)]) == 0
    assert launched["argv"][0] == "claude"
    assert "gemini" in launched["argv"][-1]


def test_checkpoint_with_only_the_required_fields_still_works(repo):
    assert str(repo / ".tapin" / "notes.md") in mcp_server.checkpoint(str(repo), "claude", " Parser done ", "Row tests")
    [record] = Store(repo).checkpoints()
    empty = {"model": None, "effort": None, "session_id": None, "in_progress": "", "decisions": ""}
    assert record == {"at": record["at"], "agent": "claude", "done": "Parser done", "next_steps": "Row tests", **empty}

    schema = mcp_server.input_schema(mcp_server.checkpoint)
    assert schema["required"] == ["workspace_path", "agent", "summary", "next_steps"]
    assert schema["properties"]["in_progress"] == {"type": "string", "default": ""}
    for name in ("session_id", "model", "effort"):
        assert schema["properties"][name] == {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
    assert "in_progress: the file and step" in mcp_server.tool_definitions()[3]["description"]


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_checkpoint_and_create_handoff_fill_in_who_from_the_session_logs(repo, tmp_path):
    ws = str(repo)
    reply = {"type": "assistant", "cwd": ws, "effort": "xhigh", "message": {"role": "assistant", "model": "claude-opus-5", "content": []}}
    synthetic = {"type": "assistant", "cwd": ws, "message": {"role": "assistant", "model": "<synthetic>", "content": []}}
    _write_jsonl(tmp_path / "claude-home" / "projects" / project_dir_name(repo) / "c-123.jsonl", [{"type": "user", "cwd": ws}, reply, synthetic])
    rollout = tmp_path / "codex-home" / "sessions" / "2026" / "09" / "13" / "rollout-2026-09-13T10-00-00-x-456.jsonl"
    turn = {"type": "turn_context", "payload": {"cwd": ws, "model": "gpt-6-astra", "effort": "high"}}
    _write_jsonl(rollout, [{"type": "session_meta", "payload": {"id": "x-456", "cwd": ws, "model_provider": "openai"}}, turn])

    mcp_server.checkpoint(ws, "claude", "Parser done", "Row tests")
    mcp_server.checkpoint(ws, "codex", "Retries done", "Backoff test")
    mcp_server.checkpoint(ws, "claude", "Explicit", "Next", session_id="mine", model="claude-sonnet-5", effort="low")
    mcp_server.checkpoint(ws, "claude", "Explicit session", "Next", session_id="c-123", effort="max")
    mcp_server.checkpoint(ws, "claude", "Unknown session", "Next", session_id="gone", model="")
    mcp_server.checkpoint(ws, "cursor", "Cursor work", "Next")
    assert [(r["agent"], r["session_id"], r["model"], r["effort"]) for r in Store(repo).checkpoints()] == [
        ("claude", "c-123", "claude-opus-5", "xhigh"),
        ("codex", "x-456", "gpt-6-astra", "high"),
        ("claude", "mine", "claude-sonnet-5", "low"),
        ("claude", "c-123", "claude-opus-5", "max"),
        ("claude", "gone", None, None),
        ("cursor", None, None, None),
    ]

    mcp_server.create_handoff(ws, "claude", "Stopping mid-parser", "Finish parse_row()")
    handoff = mcp_server.get_handoff(ws)
    assert "| From | Claude Code (session `c-123`) |" in handoff
    assert "| Model | claude-opus-5 (effort xhigh) |" in handoff
    assert "Recorded by Claude Code (claude-opus-5, effort max) in session `c-123`, just before the stop.\n\n**Done so far:** Explicit session" in handoff
    assert "_4 other recent checkpoints in .tapin/notes.md are from other sessions and aren't included._" in handoff


NO_SESSION = "(no session id: pass session_id so the next handoff from this session includes it)"


def _claude_session(tmp_path, repo, session_id, model, effort, seconds_ago):
    reply = {"type": "assistant", "cwd": str(repo), "effort": effort, "message": {"role": "assistant", "model": model, "content": []}}
    path = tmp_path / "claude-home" / "projects" / project_dir_name(repo) / f"{session_id}.jsonl"
    _write_jsonl(path, [{"type": "user", "cwd": str(repo)}, reply])
    then = time.time() - seconds_ago
    os.utime(path, (then, then))
    return path


def _last_identity(repo):
    record = Store(repo).checkpoints()[-1]
    return record["session_id"], record["model"], record["effort"]


def test_checkpoint_infers_the_session_only_when_exactly_one_is_active(repo, tmp_path):
    ws = str(repo)
    _claude_session(tmp_path, repo, "old", "claude-sonnet-5", "low", seconds_ago=600)
    active = _claude_session(tmp_path, repo, "active", "claude-opus-5", "xhigh", seconds_ago=5)
    saved = mcp_server.checkpoint(ws, "claude", "Parser done", "Row tests")
    assert saved == f"Checkpoint saved to {repo / '.tapin' / 'notes.md'} (session active)"
    assert _last_identity(repo) == ("active", "claude-opus-5", "xhigh")

    # A second active session: no session id, and the model and effort only where both sessions agree.
    _claude_session(tmp_path, repo, "second", "claude-opus-5", "high", seconds_ago=30)
    assert mcp_server.checkpoint(ws, "claude", "Parser done", "Row tests") == f"Checkpoint saved to {repo / '.tapin' / 'notes.md'} {NO_SESSION}"
    assert _last_identity(repo) == (None, "claude-opus-5", None)
    _claude_session(tmp_path, repo, "second", "claude-opus-5", "xhigh", seconds_ago=30)
    mcp_server.checkpoint(ws, "claude", "Parser done", "Row tests")
    assert _last_identity(repo) == (None, "claude-opus-5", "xhigh")
    second = _claude_session(tmp_path, repo, "second", "claude-sonnet-5", "xhigh", seconds_ago=30)
    mcp_server.checkpoint(ws, "claude", "Parser done", "Row tests")
    assert _last_identity(repo) == (None, None, None)
    mcp_server.checkpoint(ws, "claude", "Parser done", "Row tests", model="claude-opus-5")
    assert _last_identity(repo) == (None, "claude-opus-5", None)

    # No session written within the window.
    for path in (active, second):
        os.utime(path, (time.time() - 600, time.time() - 600))
    assert mcp_server.checkpoint(ws, "claude", "Parser done", "Row tests").endswith(NO_SESSION)
    assert _last_identity(repo) == (None, None, None)


def test_explicit_session_id_is_looked_up_however_old_its_log(repo, tmp_path):
    _claude_session(tmp_path, repo, "old", "claude-sonnet-5", "low", seconds_ago=3_600)
    _claude_session(tmp_path, repo, "busy", "claude-opus-5", "xhigh", seconds_ago=5)
    assert mcp_server.checkpoint(str(repo), "claude", "Parser done", "Row tests", session_id="old").endswith("notes.md (session old)")
    assert _last_identity(repo) == ("old", "claude-sonnet-5", "low")


def test_create_handoff_with_two_active_sessions_records_no_session_so_either_can_claim(repo, tmp_path):
    ws = str(repo)
    _claude_session(tmp_path, repo, "c-1", "claude-opus-5", "xhigh", seconds_ago=10)
    _claude_session(tmp_path, repo, "c-2", "claude-opus-5", "xhigh", seconds_ago=20)
    store = Store(repo)
    for claimant in ("c-1", "c-2"):
        mcp_server.create_handoff(ws, "claude", "Stopping mid-parser", "Finish parse_row()")
        pending = store.pending()
        assert pending.from_session is None and store.read_meta(pending.id)["session_id"] is None
        handoff = store.read_handoff(pending.id)
        assert "| From | Claude Code |" in handoff and "| Model | claude-opus-5 (effort xhigh) |" in handoff
        assert "# Handoff from Claude Code" in mcp_server.claim_handoff(ws, "claude", claimant)


class StdioServer:
    """`tapin mcp` in a subprocess, driven one JSON line at a time."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "tapin", "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def send(self, message):
        self.proc.stdin.write((message if isinstance(message, str) else json.dumps(message)) + "\n")
        self.proc.stdin.flush()

    def receive(self):
        ready, _, _ = select.select([self.proc.stdout], [], [], 10)
        assert ready, "no reply within 10 seconds"
        return json.loads(self.proc.stdout.readline())

    def request(self, msg_id, method, params=None):
        self.send({"jsonrpc": "2.0", "id": msg_id, "method": method, **({} if params is None else {"params": params})})
        reply = self.receive()
        assert reply["id"] == msg_id
        return reply

    def call(self, msg_id, name, **arguments):
        return self.request(msg_id, "tools/call", {"name": name, "arguments": arguments})

    def close(self):
        self.proc.stdin.close()
        code = self.proc.wait(timeout=10)
        leftover = self.proc.stdout.read()
        self.proc.stdout.close()
        self.proc.stderr.close()
        return code, leftover


@pytest.fixture
def server():
    server = StdioServer()
    yield server
    if server.proc.poll() is None:
        server.proc.kill()
        server.proc.wait()


def test_stdio_handshake_ping_and_protocol_errors(server):
    init = server.request(1, "initialize", {"protocolVersion": "2025-11-25", **CLIENT_INFO})["result"]
    assert init == {
        "protocolVersion": "2025-11-25",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "tapin", "version": __version__},
        "instructions": mcp_server.INSTRUCTIONS,
    }

    server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert server.request(2, "ping") == {"jsonrpc": "2.0", "id": 2, "result": {}}

    assert server.request(3, "resources/list")["error"]["code"] == -32601
    server.send("{not json")
    assert server.receive() == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}

    older = server.request(4, "initialize", {"protocolVersion": "2025-03-26", **CLIENT_INFO})
    assert older["result"]["protocolVersion"] == "2025-03-26"
    unsupported = server.request(5, "initialize", {"protocolVersion": "1999-01-01", **CLIENT_INFO})
    assert unsupported["result"]["protocolVersion"] == LATEST

    assert server.close() == (0, "")


def test_stdio_tools_list_and_call(server, repo):
    server.request(1, "initialize", {"protocolVersion": LATEST, **CLIENT_INFO})
    tools = {tool["name"]: tool for tool in server.request(2, "tools/list")["result"]["tools"]}
    assert {name: tool["inputSchema"]["required"] for name, tool in tools.items()} == REQUIRED
    assert tools["get_handoff"]["inputSchema"] == {
        "type": "object",
        "properties": {
            "workspace_path": {"type": "string"},
            "handoff_id": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        },
        "required": ["workspace_path"],
    }
    assert tools["claim_handoff"]["description"] == "Claim the waiting handoff so no other session also picks it up, and return its full document."

    saved = server.call(3, "checkpoint", workspace_path=str(repo), agent="gemini", summary="parser done", next_steps="retries")
    assert saved["result"]["isError"] is False
    assert str(repo / ".tapin" / "notes.md") in saved["result"]["content"][0]["text"]

    assert server.call(4, "no_such_tool")["error"] == {"code": -32602, "message": "Unknown tool: no_such_tool"}

    missing = server.call(5, "checkpoint", workspace_path=str(repo))["result"]
    assert missing["isError"] is True
    assert "missing required argument(s): agent, summary, next_steps" in missing["content"][0]["text"]
    assert server.call(6, "handoff_status", workspace_path=7)["result"]["isError"] is True

    crashed = server.call(7, "get_handoff", workspace_path=str(repo), handoff_id="no-such-handoff")["result"]
    assert crashed["isError"] is True
    assert "FileNotFoundError" in crashed["content"][0]["text"]
    assert server.request(8, "ping")["result"] == {}

    assert server.close() == (0, "")


def test_mcp_sdk_client_conformance(repo):
    pytest.importorskip("mcp")
    from mcp import ClientSession, MCPError, StdioServerParameters, stdio_client

    workspace = str(repo)

    async def session():
        params = StdioServerParameters(command=sys.executable, args=["-m", "tapin", "mcp"], env=dict(os.environ))
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            init = await client.initialize()
            assert init.protocol_version == LATEST
            assert init.server_info.name == "tapin"
            assert init.instructions == mcp_server.INSTRUCTIONS

            listed = await client.list_tools()
            assert {tool.name: tool.input_schema["required"] for tool in listed.tools} == REQUIRED

            async def call(name, **arguments):
                result = await client.call_tool(name, arguments)
                return result.is_error, result.content[0].text

            assert (await call("handoff_status", workspace_path=workspace))[0] is False
            assert (await call("checkpoint", workspace_path=workspace, agent="gemini", summary="did x", next_steps="do y"))[0] is False
            assert "Handoff written to" in (await call("create_handoff", workspace_path=workspace, agent="gemini", summary="s", next_steps="n"))[1]
            assert "did x" in (await call("get_handoff", workspace_path=workspace))[1]
            assert "# Handoff from gemini" in (await call("claim_handoff", workspace_path=workspace, agent="codex"))[1]
            assert "No unclaimed" in (await call("claim_handoff", workspace_path=workspace, agent="codex"))[1]

            is_error, text = await call("checkpoint", workspace_path=workspace)
            assert is_error and "missing required argument(s)" in text
            with pytest.raises(MCPError) as unknown:
                await client.call_tool("no_such_tool", {})
            assert unknown.value.code == -32602
            await client.send_ping()

    asyncio.run(asyncio.wait_for(session(), timeout=60))
