import io

from tapin import capture, cli, config, deliver, install
from tapin.agents.base import StopEvent
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


def test_to_resolves_the_session_only_once(repo, fake_reader, monkeypatch):
    calls = []
    find_session = fake_reader.find_session
    monkeypatch.setattr(fake_reader, "find_session", lambda *a, **kw: calls.append(a) or find_session(*a, **kw))
    monkeypatch.setattr(deliver, "launch", lambda argv, cwd: None)
    assert cli.main(["to", "codex", "--from", "claude", "--cwd", str(repo)]) == 0
    assert len(calls) == 1


def test_capture_event_failure_is_logged_and_reported_by_doctor(repo, cfg, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("reader exploded")

    monkeypatch.setattr(capture, "capture", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(StopEvent(agent="claude", cwd=repo).to_json()))
    assert cli.main(["capture-event"]) == 1

    log = config.tapin_home() / "hooks.log"
    assert " failed" in log.read_text()
    assert (False, f"hook failures logged in {log}: 1") in install.doctor(cfg)


def test_hook_command_never_fails(repo, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("not json"))
    assert cli.main(["hook", "cursor", "before-submit-prompt"]) == 0
    assert '"continue": true' in capsys.readouterr().out
