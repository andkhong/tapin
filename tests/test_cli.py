from tapin import cli, deliver
from tapin.store import Store


def test_to_with_print_leaves_handoff_for_session_start(repo, fake_reader, monkeypatch, capsys):
    monkeypatch.setattr(deliver, "copy_to_clipboard", lambda text: True)
    assert cli.main(["to", "codex", "--from", "claude", "--cwd", str(repo), "--print"]) == 0
    assert "handoff.md" in capsys.readouterr().out
    assert Store(repo).claimable() is not None


def test_to_launches_target_and_claims(repo, fake_reader, monkeypatch):
    launched = {}
    monkeypatch.setattr(deliver, "launch", lambda argv, cwd: launched.update(argv=argv, cwd=cwd))
    assert cli.main(["to", "claude", "--from", "codex", "--cwd", str(repo)]) == 0
    assert launched["argv"][0] == "claude"
    assert "handoff.md" in launched["argv"][-1]
    assert Store(repo).claimable() is None


def test_to_cursor_without_terminal_agent_opens_ide(repo, fake_reader, monkeypatch):
    launched = {}
    monkeypatch.setattr("tapin.agents.cursor.shutil.which", lambda name: None)
    monkeypatch.setattr(deliver, "copy_to_clipboard", lambda text: True)
    monkeypatch.setattr(deliver, "launch", lambda argv, cwd: launched.update(argv=argv))
    assert cli.main(["to", "cursor", "--from", "claude", "--cwd", str(repo)]) == 0
    assert launched["argv"] == ["cursor", "."]
    assert Store(repo).claimable() is not None


def test_to_reuses_hook_capture_details(repo, cfg, fake_reader, monkeypatch):
    from tapin import hooks

    payload = {"session_id": "s9", "cwd": str(repo), "error": "rate_limit", "last_assistant_message": "mid-way through the schema"}
    hooks.handle("claude", "stop-failure", payload, cfg, background=False)
    monkeypatch.setattr(deliver, "launch", lambda argv, cwd: None)
    assert cli.main(["to", "codex", "--cwd", str(repo)]) == 0

    store = Store(repo)
    latest = store.list_handoffs()[0]
    assert latest["session_id"] == "s9"
    assert "mid-way through the schema" in store.read_handoff(latest["id"])


def test_hook_command_never_fails(repo, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("not json"))
    assert cli.main(["hook", "cursor", "before-submit-prompt"]) == 0
    assert '"continue": true' in capsys.readouterr().out
