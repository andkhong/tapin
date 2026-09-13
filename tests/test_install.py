import json

from tapin import agents, install

EXE = "/opt/bin/tapin"
ALL = ["claude", "codex", "cursor"]


def _commands(groups):
    return [hook["command"] for group in groups for hook in group["hooks"]]


def test_install_is_idempotent_and_uninstall_restores(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(install, "tapin_executable", lambda: EXE)
    settings = tmp_path / "claude-home" / "settings.json"
    settings.parent.mkdir(parents=True)
    original = {"model": "opus", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}
    settings.write_text(json.dumps(original))

    install.install(ALL, cfg, mcp=False)
    install.install(ALL, cfg, mcp=False)

    claude = json.loads(settings.read_text())
    assert claude["model"] == "opus"
    session_start = _commands(claude["hooks"]["SessionStart"])
    assert session_start.count(f"{EXE} hook claude session-start") == 1
    assert "echo hi" in session_start
    assert claude["hooks"]["StopFailure"][0]["matcher"] == "rate_limit"
    assert list(settings.parent.glob("settings.json.tapin-backup-*"))

    codex = json.loads((tmp_path / "codex-home" / "hooks.json").read_text())
    assert _commands(codex["hooks"]["Stop"]) == [f"{EXE} hook codex stop"]

    cursor = json.loads((tmp_path / "cursor-home" / "hooks.json").read_text())
    assert cursor["version"] == 1
    assert cursor["hooks"]["afterFileEdit"] == [{"command": f"{EXE} hook cursor after-file-edit"}]

    install.uninstall(ALL, cfg, mcp=False)
    assert json.loads(settings.read_text()) == original
    assert "hooks" not in json.loads((tmp_path / "cursor-home" / "hooks.json").read_text())


def test_hook_pattern_matches_every_agent():
    assert all(install.OURS.search(f"{EXE} hook {name} stop") for name in agents.NAMES)
    assert not install.OURS.search(f"{EXE} hook gemini stop")


def test_cursor_mcp_registration(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(install, "tapin_executable", lambda: EXE)
    install.install(["cursor"], cfg, mcp=True)
    servers = json.loads((tmp_path / "cursor-home" / "mcp.json").read_text())["mcpServers"]
    assert servers["tapin"] == {"command": EXE, "args": ["mcp"]}
    install.uninstall(["cursor"], cfg, mcp=True)
    assert "tapin" not in json.loads((tmp_path / "cursor-home" / "mcp.json").read_text())["mcpServers"]


def test_snippet_added_once_and_removed_cleanly(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("# My rules\n\nBe concise.\n")
    assert install.apply_snippet(path, install=True)
    assert not install.apply_snippet(path, install=True)
    assert path.read_text().count(install.SNIPPET_BEGIN) == 1
    assert install.apply_snippet(path, install=False)
    assert path.read_text() == "# My rules\n\nBe concise.\n"
