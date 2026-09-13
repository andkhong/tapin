import json
import os
import subprocess
from pathlib import Path

import pytest

from tapin import agents, cli, install, statusline

ALL = ["claude", "codex", "cursor"]
OLD_EXE = "/Users/you/.local/share/uv/tools/tapin/bin/tapin"


def _commands(groups):
    return [hook["command"] for group in groups for hook in group["hooks"]]


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\necho "$@"\n')
    path.chmod(0o755)
    return path


def _trust(tmp_path, entries):
    lines = []
    for key, trusted_hash in entries.items():
        lines += [f'[hooks.state."{key}"]', f'trusted_hash = "{trusted_hash}"', "enabled = true", ""]
    (tmp_path / "codex-home" / "config.toml").write_text("\n".join(lines))


def _current_trust(tmp_path):
    """The hooks.state entries Codex would record after the user trusts Tap In's hooks as they are now."""
    hooks_json = tmp_path / "codex-home" / "hooks.json"
    hooks = json.loads(hooks_json.read_text())["hooks"]
    entries = {}
    for event, group, handler, _ in install.tapin_hooks("codex"):
        label = install.CODEX_TRUST_LABELS[event]
        entries[f"{hooks_json}:{label}:{group}:{handler}"] = install.codex_hook_hash(label, hooks[event][group], hooks[event][group]["hooks"][handler])
    return entries


@pytest.fixture
def exe(tmp_path, monkeypatch):
    path = _executable(tmp_path / "tools" / "tapin" / "bin" / "tapin")
    monkeypatch.setattr(install, "tapin_executable", lambda: str(path))
    return path


@pytest.fixture
def launcher():
    return str(install.launcher_path())


def test_install_is_idempotent_and_uninstall_restores(tmp_path, cfg, exe, launcher):
    settings = tmp_path / "claude-home" / "settings.json"
    settings.parent.mkdir(parents=True)
    original = {"model": "opus", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}
    settings.write_text(json.dumps(original))

    install.install(ALL, cfg, mcp=False)
    install.install(ALL, cfg, mcp=False)

    claude = json.loads(settings.read_text())
    assert claude["model"] == "opus"
    session_start = _commands(claude["hooks"]["SessionStart"])
    assert session_start == ["echo hi", f"{launcher} hook claude session-start"]
    assert claude["hooks"]["StopFailure"][0]["matcher"] == "rate_limit"
    assert list(settings.parent.glob("settings.json.tapin-backup-*"))

    codex = json.loads((tmp_path / "codex-home" / "hooks.json").read_text())
    assert _commands(codex["hooks"]["Stop"]) == [f"{launcher} hook codex stop"]

    cursor = json.loads((tmp_path / "cursor-home" / "hooks.json").read_text())
    assert cursor["version"] == 1
    assert cursor["hooks"]["afterFileEdit"] == [{"command": f"{launcher} hook cursor after-file-edit"}]

    install.uninstall(ALL, cfg, mcp=False)
    assert json.loads(settings.read_text()) == original
    assert "hooks" not in json.loads((tmp_path / "cursor-home" / "hooks.json").read_text())
    assert not install.launcher_path().is_symlink()


def test_hook_pattern_matches_launcher_and_old_absolute_paths(launcher):
    for exe in (launcher, OLD_EXE, "'/Users/you/My Tools/tapin'"):
        assert all(install.OURS.search(f"{exe} hook {name} stop") for name in agents.NAMES)
    assert not install.OURS.search(f"{launcher} hook gemini stop")


def test_reinstall_replaces_old_hooks_in_place(tmp_path, cfg, exe, launcher):
    before, after = {"type": "command", "command": "echo before"}, {"type": "command", "command": "echo after"}
    codex_file = tmp_path / "codex-home" / "hooks.json"
    codex_file.parent.mkdir(parents=True)
    codex_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [before]},
                        {"matcher": "startup|resume|clear", "hooks": [{"type": "command", "command": f"{OLD_EXE} hook codex session-start"}]},
                        {"hooks": [after]},
                    ],
                    "Stop": [{"hooks": [{"type": "command", "command": f"{OLD_EXE} hook codex stop"}, after]}],
                }
            }
        )
    )
    cursor_file = tmp_path / "cursor-home" / "hooks.json"
    cursor_file.parent.mkdir(parents=True)
    cursor_file.write_text(json.dumps({"version": 1, "hooks": {"stop": [{"command": f"{OLD_EXE} hook cursor stop"}, {"command": "afplay done.aiff"}]}}))

    install.install(["codex", "cursor"], cfg, mcp=False)

    codex = json.loads(codex_file.read_text())["hooks"]
    assert _commands(codex["SessionStart"]) == ["echo before", f"{launcher} hook codex session-start", "echo after"]
    assert _commands(codex["Stop"]) == [f"{launcher} hook codex stop", "echo after"]
    positions = [(event, group, handler) for event, group, handler, _ in install.tapin_hooks("codex")]
    assert positions == [("SessionStart", 1, 0), ("Stop", 0, 0), ("PostToolUse", 0, 0)]
    cursor = json.loads(cursor_file.read_text())["hooks"]
    assert cursor["stop"] == [{"command": f"{launcher} hook cursor stop"}, {"command": "afplay done.aiff"}]

    text = codex_file.read_text()
    assert not install.apply_hooks("codex", launcher, cfg)
    assert codex_file.read_text() == text


def test_launcher_is_a_symlink_that_follows_the_install(tmp_path, monkeypatch, exe):
    link = install.launcher_path()
    assert "created" in install.ensure_launcher()
    assert os.readlink(link) == str(exe.resolve())
    assert "unchanged" in install.ensure_launcher()

    moved = _executable(tmp_path / "pipx" / "venvs" / "tapin" / "bin" / "tapin")
    monkeypatch.setattr(install, "tapin_executable", lambda: str(moved))
    assert "repointed" in install.ensure_launcher()
    assert os.readlink(link) == str(moved.resolve())
    assert install.launcher_target() == moved.resolve()


def test_launcher_falls_back_to_a_shell_shim(monkeypatch, exe):
    def no_symlinks(self, target):
        raise OSError("symlinks not supported")

    monkeypatch.setattr(Path, "symlink_to", no_symlinks)
    install.ensure_launcher()
    link = install.launcher_path()
    assert not link.is_symlink()
    assert install.launcher_target() == exe.resolve()
    result = subprocess.run([str(link), "hook", "claude", "stop"], capture_output=True, text=True)
    assert result.stdout == "hook claude stop\n"
    assert "unchanged" in install.ensure_launcher()


@pytest.mark.parametrize("cache", ["uv/archive-v0/Xy12/bin/tapin", "npm/_npx/3f9a/node_modules/.bin/tapin"])
def test_launcher_refuses_temporary_caches(tmp_path, monkeypatch, capsys, cache):
    path = _executable(tmp_path / "cache" / cache)
    monkeypatch.setattr(install, "tapin_executable", lambda: str(path))
    with pytest.raises(RuntimeError, match="uv tool install tapin"):
        install.ensure_launcher()
    assert cli.main(["install", "--no-mcp"]) == 1
    assert "temporary cache" in capsys.readouterr().err
    assert not install.launcher_path().exists()


def test_install_without_agents_sets_up_detected_agents_only(tmp_path, cfg, exe, monkeypatch):
    (tmp_path / "codex-home").mkdir()
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/local/bin/cursor" if name == "cursor" else None)

    messages = install.install(None, cfg, mcp=False)

    assert (tmp_path / "codex-home" / "hooks.json").exists()
    assert (tmp_path / "cursor-home" / "hooks.json").exists()
    assert not (tmp_path / "claude-home" / "settings.json").exists()
    assert any(message.startswith("Claude Code: skipped, not detected") for message in messages)
    assert not any(message.startswith(("Codex: skipped", "Cursor: skipped")) for message in messages)

    install.install(["claude"], cfg, mcp=False)
    assert (tmp_path / "claude-home" / "settings.json").exists()


def test_launcher_is_removed_only_with_all_agents(cfg, exe):
    install.install(ALL, cfg, mcp=False)
    install.uninstall(["claude", "codex"], cfg, mcp=False)
    assert install.launcher_path().is_symlink()
    assert any("launcher removed" in message for message in install.uninstall(None, cfg, mcp=False))
    assert not install.launcher_path().is_symlink()


def test_uninstall_creates_no_config_for_agents_never_set_up(tmp_path, cfg, monkeypatch):
    def missing_cli(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(install.subprocess, "run", missing_cli)
    messages = install.uninstall(None, cfg, mcp=True)
    assert not any((tmp_path / name).exists() for name in ("claude-home", "codex-home", "cursor-home"))
    assert f"{tmp_path / 'cursor-home' / 'hooks.json'}: hooks not present" in messages


def test_mcp_registration_uses_the_launcher(tmp_path, cfg, exe, launcher, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        if argv[0] != "claude":
            raise FileNotFoundError(2, "No such file or directory")
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    messages = install.install(ALL, cfg, mcp=True)

    assert ["claude", "mcp", "add", "--scope", "user", "tapin", "--", launcher, "mcp"] in calls
    assert any(message.startswith("codex: MCP registration skipped, could not run") for message in messages)
    servers = json.loads((tmp_path / "cursor-home" / "mcp.json").read_text())["mcpServers"]
    assert servers["tapin"] == {"command": launcher, "args": ["mcp"]}
    install.uninstall(["cursor"], cfg, mcp=True)
    assert "tapin" not in json.loads((tmp_path / "cursor-home" / "mcp.json").read_text())["mcpServers"]


def test_install_prints_codex_trust_steps_until_trust_is_recorded(tmp_path, cfg, exe, launcher):
    steps = install.install(["codex"], cfg, mcp=False)[-1]
    assert steps.startswith("Codex runs a hook only after you trust it.")
    assert "\n    /hooks\n" in steps
    assert f"SessionStart  {launcher} hook codex session-start" in steps
    assert f"Stop          {launcher} hook codex stop" in steps
    assert f"PostToolUse   {launcher} hook codex post-tool-use" in steps

    _trust(tmp_path, _current_trust(tmp_path))
    assert not any(message.startswith("Codex runs a hook") for message in install.install(["codex"], cfg, mcp=False))


def test_codex_hook_hash_matches_codex(cfg):
    hooks = install.desired_hooks("codex", "/home/test/.tapin/bin/tapin", cfg)
    session_start, stop = hooks["SessionStart"][0], hooks["Stop"][0]
    assert (
        install.codex_hook_hash("session_start", session_start, session_start["hooks"][0])
        == "sha256:c8ce7e434d4bb8995b7dd17f7ee284b5d6a845ec704a03192f13bf0d77074427"
    )
    assert install.codex_hook_hash("stop", stop, stop["hooks"][0]) == "sha256:91536584df1ef6e77f0f8e143ed1f6bab0752e2c2e8c87c99b95150cc0ff90b2"


def test_moving_hooks_to_the_launcher_makes_codex_trust_stale(tmp_path, cfg, exe):
    hooks_json = tmp_path / "codex-home" / "hooks.json"
    hooks_json.parent.mkdir(parents=True)
    hooks_json.write_text(json.dumps({"hooks": install.desired_hooks("codex", OLD_EXE, cfg)}))
    old_trust = _current_trust(tmp_path)
    _trust(tmp_path, old_trust)
    assert install.codex_trust() == {"SessionStart": "trusted", "Stop": "trusted", "PostToolUse": "trusted"}

    messages = install.install(["codex"], cfg, mcp=False)

    new_trust = _current_trust(tmp_path)
    assert new_trust.keys() == old_trust.keys() and new_trust != old_trust
    assert install.codex_trust() == {"SessionStart": "changed", "Stop": "changed", "PostToolUse": "changed"}
    assert (install.WARN, "Codex Stop hook: changed since you trusted it; run /hooks in Codex to trust it again") in install.doctor(cfg)
    assert messages[-1].startswith("Codex runs a hook only after you trust it.")


def test_doctor_reads_codex_trust_at_tapin_hook_positions(tmp_path, cfg, exe):
    hooks_json = tmp_path / "codex-home" / "hooks.json"
    hooks_json.parent.mkdir(parents=True)
    hooks_json.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo start"}]}]}}))
    install.install(["codex"], cfg, mcp=False)
    trusted = _current_trust(tmp_path)
    session_start, stop = f"{hooks_json}:session_start:1:0", f"{hooks_json}:stop:0:0"
    _trust(tmp_path, {session_start: trusted[session_start], f"{hooks_json}:stop:1:0": trusted[stop]})

    checks = install.doctor(cfg)

    assert (install.OK, "Codex SessionStart hook: trust recorded") in checks
    assert (install.WARN, "Codex Stop hook: not trusted yet; run /hooks in Codex") in checks


def test_doctor_checks_launcher_and_hook_paths(tmp_path, cfg, exe, launcher):
    assert (install.FAIL, f"launcher {launcher} is missing; run `tapin install`") in install.doctor(cfg)

    install.install(["claude", "cursor"], cfg, mcp=False)
    checks = install.doctor(cfg)
    assert (install.OK, f"launcher {launcher} runs {exe.resolve()}") in checks
    assert (install.OK, f"Claude Code hooks in {tmp_path / 'claude-home' / 'settings.json'} call the launcher") in checks

    old = _executable(tmp_path / "old" / "bin" / "tapin")
    install.apply_hooks("cursor", str(old), cfg)
    cursor_hooks = tmp_path / "cursor-home" / "hooks.json"
    moved = f"Cursor hooks in {cursor_hooks} still call {old}; run `tapin install` to move them to the launcher"
    assert (install.WARN, moved) in install.doctor(cfg)
    old.unlink()
    assert (install.FAIL, f"Cursor hooks in {cursor_hooks} call {old}, which doesn't exist; run `tapin install`") in install.doctor(cfg)


def test_doctor_checks_node_only_for_continues_readers(cfg, monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    assert not any("npx" in message for _, message in install.doctor(cfg))

    cfg["readers"]["claude"] = "continues"
    node = [(status, message) for status, message in install.doctor(cfg) if "`npx`" in message]
    assert len(node) == 1 and node[0][0] == install.WARN
    assert "no session digest" in node[0][1] and "still work" in node[0][1]
    assert "Claude Code handoffs" in node[0][1]


def test_snippet_added_once_and_removed_cleanly(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("# My rules\n\nBe concise.\n")
    assert install.apply_snippet(path, install=True)
    assert not install.apply_snippet(path, install=True)
    assert path.read_text().count(install.SNIPPET_BEGIN) == 1
    assert install.apply_snippet(path, install=False)
    assert path.read_text() == "# My rules\n\nBe concise.\n"


def test_adding_post_tool_use_keeps_codex_trust_for_existing_hooks(tmp_path, cfg, exe, launcher):
    hooks_json = tmp_path / "codex-home" / "hooks.json"
    hooks_json.parent.mkdir(parents=True)
    before = install.desired_hooks("codex", launcher, cfg)
    mine = {"type": "command", "command": "echo mine"}
    existing = {"SessionStart": [{"hooks": [mine]}, *before["SessionStart"]], "Stop": before["Stop"], "PostToolUse": [{"matcher": "apply_patch", "hooks": [mine]}]}
    hooks_json.write_text(json.dumps({"hooks": existing}))
    trusted = _current_trust(tmp_path)
    _trust(tmp_path, trusted)
    assert install.codex_trust() == {"SessionStart": "trusted", "Stop": "trusted"}

    install.install(["codex"], cfg, mcp=False)

    positions = [(event, group, handler) for event, group, handler, _ in install.tapin_hooks("codex")]
    assert positions == [("SessionStart", 1, 0), ("Stop", 0, 0), ("PostToolUse", 1, 0)]
    assert install.CODEX_TRUST_LABELS["PostToolUse"] == "post_tool_use"
    after = _current_trust(tmp_path)
    assert {key: value for key, value in after.items() if ":post_tool_use:" not in key} == trusted
    assert f"{hooks_json}:post_tool_use:1:0" in after
    assert install.codex_trust() == {"SessionStart": "trusted", "Stop": "trusted", "PostToolUse": "missing"}
    assert (install.WARN, "Codex PostToolUse hook: not trusted yet; run /hooks in Codex") in install.doctor(cfg)


def test_install_sets_wraps_and_restores_the_claude_status_line(tmp_path, cfg, exe, launcher):
    settings = tmp_path / "claude-home" / "settings.json"
    settings.parent.mkdir(parents=True)
    ours = f"{launcher} statusline"

    install.install(["claude"], cfg, mcp=False)
    assert json.loads(settings.read_text())["statusLine"] == {"type": "command", "command": ours}
    assert not statusline.record_path().exists()
    assert f"{settings}: status line removed" in install.uninstall(["claude"], cfg, mcp=False)
    assert "statusLine" not in json.loads(settings.read_text())

    theirs = {"type": "command", "command": "~/.claude/statusline.sh", "padding": 2, "refreshInterval": 10}
    settings.write_text(json.dumps({"statusLine": theirs, "model": "opus"}))
    for backup in settings.parent.glob("settings.json.tapin-backup-*"):
        backup.unlink()
    messages = install.install(["claude"], cfg, mcp=False)

    assert f"{settings}: status line installed; it runs your previous one (`~/.claude/statusline.sh`) and shows its output" in messages
    assert json.loads(settings.read_text())["statusLine"] == {"type": "command", "command": ours, "padding": 2, "refreshInterval": 10}
    assert statusline.load_record() == {"original": theirs}
    backups = list(settings.parent.glob("settings.json.tapin-backup-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == {"statusLine": theirs, "model": "opus"}
    doctor_ok = f"Claude Code status line in {settings} is Tap In's and runs your previous one (`~/.claude/statusline.sh`)."
    assert any(status == install.OK and message.startswith(doctor_ok) for status, message in install.doctor(cfg))

    assert f"{settings}: status line already installed" in install.install(["claude"], cfg, mcp=False)
    assert statusline.load_record() == {"original": theirs}

    assert f"{settings}: status line restored to `~/.claude/statusline.sh`" in install.uninstall(["claude"], cfg, mcp=False)
    assert json.loads(settings.read_text()) == {"statusLine": theirs, "model": "opus"}
    assert not statusline.record_path().exists()


def test_install_no_statusline_leaves_the_status_line_alone(tmp_path, cfg, exe, launcher):
    settings = tmp_path / "claude-home" / "settings.json"
    settings.parent.mkdir(parents=True)
    theirs = {"type": "command", "command": "echo mine"}
    settings.write_text(json.dumps({"statusLine": theirs}))

    assert cli.main(["install", "--agents", "claude", "--no-mcp", "--no-statusline"]) == 0

    data = json.loads(settings.read_text())
    assert data["statusLine"] == theirs
    assert data["hooks"]["PostToolUse"] == [{"matcher": "*", "hooks": [{"type": "command", "command": f"{launcher} hook claude post-tool-use"}]}]
    assert not statusline.record_path().exists()
    warning = (
        f"Claude Code status line in {settings} is `echo mine`, not Tap In's, so Claude Code gets no usage warnings "
        "(they need Tap In's status line); run `tapin install`"
    )
    assert (install.WARN, warning) in install.doctor(cfg)


def _graft(project):
    (project / ".claude").mkdir(parents=True, exist_ok=True)
    graft = {"type": "command", "command": "node .claude/helpers/graft-statusline.cjs"}
    (project / ".claude" / "settings.json").write_text(json.dumps({"statusLine": graft}))
    return graft


def test_project_status_line_is_wrapped_and_restored(tmp_path, cfg, exe, launcher, repo, monkeypatch, capsys):
    graft = _graft(repo)
    install.install(["claude"], cfg, mcp=False)
    monkeypatch.chdir(repo)
    warning = (
        install.WARN,
        f"Claude Code warnings before the limit are off in this project ({Path.cwd()}) because its own status line overrides "
        "Tap In's; run `tapin statusline --project`",
    )
    assert warning in install.doctor(cfg)

    assert cli.main(["statusline", "--project"]) == 0
    assert "runs this project's own (`node .claude/helpers/graft-statusline.cjs`)" in capsys.readouterr().out
    local = repo / ".claude" / "settings.local.json"
    assert json.loads(local.read_text()) == {"statusLine": {"type": "command", "command": f"{launcher} statusline"}}
    assert statusline.load_record()["projects"] == {str(Path.cwd()): {"original": graft, "created": True}}
    assert "/.claude/settings.local.json" in (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert warning not in install.doctor(cfg)
    assert cli.main(["statusline", "--project", str(repo)]) == 0
    assert "already Tap In's and runs `node .claude/helpers/graft-statusline.cjs`" in capsys.readouterr().out

    plain = tmp_path / "plain"
    plain.mkdir()
    assert cli.main(["statusline", "--project", str(plain)]) == 0
    assert "sets no status line of its own, so none is needed" in capsys.readouterr().out

    assert cli.main(["statusline", "--project", "--remove"]) == 0
    assert "status line removed" in capsys.readouterr().out
    assert not local.exists()
    assert not statusline.record_path().exists()
    assert warning in install.doctor(cfg)


def test_uninstall_restores_every_wrapped_project(tmp_path, cfg, exe, launcher):
    install.install(["claude"], cfg, mcp=False)
    shared = tmp_path / "shared"
    _graft(shared)
    local_only = tmp_path / "local-only"
    (local_only / ".claude").mkdir(parents=True)
    local_file = local_only / ".claude" / "settings.local.json"
    mine = {"type": "command", "command": "echo mine", "padding": 1}
    local_file.write_text(json.dumps({"model": "opus", "statusLine": mine}))

    install.wrap_project_statusline(shared)
    install.wrap_project_statusline(local_only)
    assert json.loads(local_file.read_text()) == {"model": "opus", "statusLine": {"type": "command", "command": f"{launcher} statusline", "padding": 1}}
    assert statusline.load_record()["projects"][str(local_only.resolve())] == {"original": mine, "previous": mine}

    messages = install.uninstall(["claude"], cfg, mcp=False)

    assert not (shared / ".claude" / "settings.local.json").exists()
    assert json.loads(local_file.read_text()) == {"model": "opus", "statusLine": mine}
    assert f"{local_only.resolve() / '.claude' / 'settings.local.json'}: status line restored to `echo mine`" in messages
    assert list(local_file.parent.glob("settings.local.json.tapin-backup-*"))
    assert not statusline.record_path().exists()


GRAFT_CODEX = {"matcher": "Write|Edit|MultiEdit", "hooks": [{"type": "command", "command": "node ~/.codex/hooks/graft/graft-hooks.cjs post-edit"}]}


def _positions():
    return [(event, group, handler) for event, group, handler, _ in install.tapin_hooks("codex")]


def _behind_hints(messages):
    return [message for message in messages if "another tool's hooks before Tap In's" in message]


def test_reinstall_keeps_graft_codex_group_before_tapin_and_trust(tmp_path, cfg, exe, launcher):
    hooks_json = tmp_path / "codex-home" / "hooks.json"
    hooks_json.parent.mkdir(parents=True)
    ours = install.desired_hooks("codex", launcher, cfg)
    fixture = {"hooks": {**ours, "PostToolUse": [GRAFT_CODEX, *ours["PostToolUse"]]}}
    hooks_json.write_text(json.dumps(fixture))
    _trust(tmp_path, _current_trust(tmp_path))
    assert _positions() == [("SessionStart", 0, 0), ("Stop", 0, 0), ("PostToolUse", 1, 0)]

    messages = install.install(["codex"], cfg, mcp=False)

    assert json.loads(hooks_json.read_text()) == fixture
    assert _positions() == [("SessionStart", 0, 0), ("Stop", 0, 0), ("PostToolUse", 1, 0)]
    assert install.codex_trust() == {"SessionStart": "trusted", "Stop": "trusted", "PostToolUse": "trusted"}
    assert _behind_hints(messages) == [
        f"`PostToolUse` in {hooks_json} has another tool's hooks before Tap In's; if Codex shows Tap In's hook as untrusted in /hooks, trust it again."
    ]


def test_reinstall_keeps_graft_codex_group_appended_after_tapin(tmp_path, cfg, exe, launcher):
    hooks_json = tmp_path / "codex-home" / "hooks.json"
    install.install(["codex"], cfg, mcp=False)
    data = json.loads(hooks_json.read_text())
    data["hooks"]["PostToolUse"].append(GRAFT_CODEX)
    hooks_json.write_text(json.dumps(data))

    messages = install.install(["codex"], cfg, mcp=False)

    after = json.loads(hooks_json.read_text())
    assert after == data and after["hooks"]["PostToolUse"][1] == GRAFT_CODEX
    assert _positions() == [("SessionStart", 0, 0), ("Stop", 0, 0), ("PostToolUse", 0, 0)]
    assert _behind_hints(messages) == []


def test_claude_install_and_uninstall_keep_graft_hooks(tmp_path, cfg, exe, launcher):
    def graft(event, **extra):
        return {**extra, "hooks": [{"type": "command", "command": f"node .claude/helpers/graft-hooks.cjs {event}"}]}

    foreign = {"SessionStart": [graft("session-start")], "UserPromptSubmit": [graft("prompt")], "PostToolUse": [graft("post-edit", matcher="Write|Edit|MultiEdit")]}
    settings = tmp_path / "claude-home" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": foreign}))
    project = tmp_path / "graft-project"
    _graft(project)
    project_settings = project / ".claude" / "settings.json"
    project_settings.write_text(json.dumps({**json.loads(project_settings.read_text()), "hooks": foreign}))
    project_before = project_settings.read_text()

    install.install(["claude"], cfg, mcp=False)
    install.wrap_project_statusline(project)

    hooks = json.loads(settings.read_text())["hooks"]
    for event, groups in foreign.items():
        assert hooks[event][: len(groups)] == groups
    assert _commands(hooks["SessionStart"])[-1] == f"{launcher} hook claude session-start"
    assert _commands(hooks["PostToolUse"])[-1] == f"{launcher} hook claude post-tool-use"
    assert hooks["UserPromptSubmit"] == foreign["UserPromptSubmit"]
    assert project_settings.read_text() == project_before

    install.uninstall(["claude"], cfg, mcp=False)

    assert json.loads(settings.read_text()) == {"hooks": foreign}
    assert project_settings.read_text() == project_before
