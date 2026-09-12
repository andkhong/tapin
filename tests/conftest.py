import subprocess
from pathlib import Path

import pytest

from tapin import config
from tapin.readers.base import ReaderError, SessionRef


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TAPIN_HOME", str(tmp_path / "tapin-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("TAPIN_CURSOR_DIR", str(tmp_path / "cursor-home"))
    monkeypatch.setenv("TAPIN_NO_NOTIFY", "1")


@pytest.fixture
def cfg():
    return config.load()


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    (root / "app.py").write_text("def total(items):\n    return sum(items)\n")
    git("add", ".")
    git("commit", "-qm", "init")
    return root


class FakeReader:
    def __init__(self, digest="# Session Handoff Context\n\n## Recent Conversation\n\nWorking on total()", error=None):
        self.digest_text = digest
        self.error = error

    def find_session(self, agent, workspace, session_id=None):
        if self.error:
            raise ReaderError(self.error)
        return SessionRef(agent, session_id or "sess-1", path="/logs/sess-1.jsonl", cwd=str(workspace), updated_at="2026-09-12T20:00:00Z")

    def digest(self, ref):
        return self.digest_text


@pytest.fixture
def fake_reader(monkeypatch):
    from tapin import readers

    reader = FakeReader()
    monkeypatch.setattr(readers, "get", lambda name, cfg: reader)
    return reader
