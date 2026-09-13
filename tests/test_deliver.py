import subprocess

import pytest

from tapin import deliver

XCLIP = ["xclip", "-selection", "clipboard"]
XSEL = ["xsel", "--clipboard", "--input"]


@pytest.fixture
def runs(monkeypatch):
    """Records each subprocess.run call; set `fail` to the commands that should fail, mapped to the exception."""
    calls = []
    fail = {}

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] in fail:
            raise fail[argv[0]]
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(deliver.subprocess, "run", fake_run)
    return calls, fail


def _on_path(monkeypatch, *names):
    monkeypatch.setattr(deliver.shutil, "which", lambda name: f"/usr/bin/{name}" if name in names else None)


def test_notify_uses_osascript_on_macos(monkeypatch, runs):
    monkeypatch.delenv("TAPIN_NO_NOTIFY")
    monkeypatch.setattr(deliver.sys, "platform", "darwin")
    deliver.notify("Codex stopped", 'Handoff "ready"')
    [(argv, _)] = runs[0]
    assert argv == ["osascript", "-e", 'display notification "Handoff \\"ready\\"" with title "Codex stopped"']


def test_notify_uses_notify_send_on_linux_when_installed(monkeypatch, runs):
    monkeypatch.delenv("TAPIN_NO_NOTIFY")
    monkeypatch.setattr(deliver.sys, "platform", "linux")
    _on_path(monkeypatch, "notify-send")
    deliver.notify("Codex stopped", "Handoff ready")
    assert [argv for argv, _ in runs[0]] == [["notify-send", "Codex stopped", "Handoff ready"]]

    runs[0].clear()
    _on_path(monkeypatch)
    deliver.notify("Codex stopped", "Handoff ready")
    assert runs[0] == []


def test_notify_respects_opt_out_and_never_raises(monkeypatch, runs):
    calls, fail = runs
    monkeypatch.setattr(deliver.sys, "platform", "linux")
    _on_path(monkeypatch, "notify-send")
    deliver.notify("t", "m")
    assert calls == []

    monkeypatch.delenv("TAPIN_NO_NOTIFY")
    for error in (OSError("no dbus"), subprocess.TimeoutExpired("notify-send", 10)):
        fail["notify-send"] = error
        deliver.notify("t", "m")
    assert len(calls) == 2


def test_clipboard_uses_the_first_tool_that_works(monkeypatch, runs):
    calls, fail = runs
    _on_path(monkeypatch, "pbcopy", "wl-copy", "xclip", "xsel")
    assert deliver.copy_to_clipboard("prompt")
    assert [argv for argv, _ in calls] == [["pbcopy"]]
    assert calls[0][1]["input"] == "prompt" and calls[0][1]["stdout"] is subprocess.DEVNULL

    calls.clear()
    _on_path(monkeypatch, "xclip", "xsel")
    fail["xclip"] = subprocess.CalledProcessError(1, XCLIP)
    assert deliver.copy_to_clipboard("prompt")
    assert [argv for argv, _ in calls] == [XCLIP, XSEL]


def test_clipboard_returns_false_when_nothing_works(monkeypatch, runs):
    calls, fail = runs
    _on_path(monkeypatch)
    assert not deliver.copy_to_clipboard("prompt")
    assert calls == []

    _on_path(monkeypatch, "pbcopy", "wl-copy", "xclip", "xsel")
    fail.update({"pbcopy": OSError(), "wl-copy": subprocess.CalledProcessError(1, ["wl-copy"]), "xclip": subprocess.TimeoutExpired(XCLIP, 5), "xsel": OSError()})
    assert not deliver.copy_to_clipboard("prompt")
    assert [argv for argv, _ in calls] == [["pbcopy"], ["wl-copy"], XCLIP, XSEL]
