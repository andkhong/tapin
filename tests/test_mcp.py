import json

from tapin import cli, deliver, mcp_server


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
