import io
import json
import subprocess
import sys

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
    assert (install.FAIL, f"hook failures logged in {log}: 1") in install.doctor(cfg)


def test_hook_command_never_fails(repo, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert cli.main(["hook", "cursor", "before-submit-prompt"]) == 0
    assert '"continue": true' in capsys.readouterr().out


def test_hook_with_unknown_agent_event_or_arguments_is_logged_not_raised(monkeypatch, capsys):
    for argv in (["hook", "gemini", "stop"], ["hook", "cursor", "before-submit-promt"], ["hook"]):
        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
        assert cli.main(argv) == 0
    assert capsys.readouterr().out == ""
    assert (config.tapin_home() / "hooks.log").read_text().count(" failed\n") == 3


def test_journal_hook_loads_no_argparse_config_parser_or_capture_modules(repo):
    code = (
        "import json, sys\n"
        "from tapin import cli\n"
        "status = cli.main(['hook', 'cursor', 'before-submit-prompt'])\n"
        "heavy = ['argparse', 'tomllib', 'tapin.install', 'tapin.packet', 'tapin.capture', 'tapin.readers']\n"
        "print(json.dumps({'status': status, 'loaded': [name for name in heavy if name in sys.modules]}), file=sys.stderr)\n"
    )
    payload = json.dumps({"conversation_id": "c1", "workspace_roots": [str(repo)], "prompt": "build the parser"})
    result = subprocess.run([sys.executable, "-c", code], input=payload, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"continue": True}
    assert json.loads(result.stderr.strip().splitlines()[-1]) == {"status": 0, "loaded": []}
    assert Store(repo).journal_file("cursor", "c1").exists()


def test_checkpoint_command_writes_the_note_the_mcp_tool_would(repo, capsys):
    argv = ["checkpoint", "--agent", "codex", "--summary", "Parser done; retry loop half written in fetch.py"]
    argv += ["--next-steps", "Add the backoff test", "--decisions", "Keep it synchronous", "--workspace", str(repo)]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("Checkpoint saved to ") and out.endswith("/.tapin/notes.md")

    note = Store(repo).latest_note()
    assert note.splitlines()[0].startswith("## ") and note.splitlines()[0].endswith(" — codex")
    assert note.endswith(
        "**Done so far:** Parser done; retry loop half written in fetch.py\n\n**Decisions:** Keep it synchronous\n\n**Next steps:** Add the backoff test"
    )
