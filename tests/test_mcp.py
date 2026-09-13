import asyncio
import json
import os
import select
import subprocess
import sys

import pytest

from tapin import __version__, cli, deliver, mcp_server

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
