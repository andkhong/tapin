import io
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta

from tapin import cli, config, hooks, install, statusline, usage, workspace
from tapin.store import Store, iso, utcnow


def _limits(**windows):
    reset = int(time.time()) + 3600
    return {name: {"used_percentage": pct, "resets_at": reset} for name, pct in windows.items()}


def _statusline(monkeypatch, capsys, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)))
    assert cli.main(["statusline"]) == 0
    return capsys.readouterr().out


# Status line


def test_statusline_records_the_windows_present(monkeypatch, capsys):
    reset = int(time.time()) + 3600
    limits = {
        "five_hour": {"used_percentage": 23.4, "resets_at": reset},
        "seven_day": None,
        "spend_limit": {"used_percentage": 112, "resets_at": reset + 60},
        "hourly": {"used_percentage": 5, "resets_at": reset},
    }
    out = _statusline(monkeypatch, capsys, {"session_id": "s/1", "cwd": "/tmp/x", "rate_limits": limits})

    assert out == "tapin · 5h 23% · spend 112%\n"
    snapshot = json.loads((usage.usage_dir() / "claude-s_1.json").read_text())
    assert set(snapshot) == {"agent", "session_id", "cwd", "windows", "updated_at"}
    assert (snapshot["agent"], snapshot["session_id"], snapshot["cwd"]) == ("claude", "s/1", "/tmp/x")
    assert snapshot["windows"] == {
        "five_hour": {"used_percent": 23.4, "resets_at": reset, "label": "5-hour"},
        "spend_limit": {"used_percent": 112.0, "resets_at": reset + 60, "label": "spend"},
    }
    assert snapshot["updated_at"].endswith("Z")
    assert usage.claude_windows("s/1") == snapshot["windows"]


def test_statusline_compact_line(monkeypatch, capsys):
    payload = {"session_id": "s1", "rate_limits": _limits(five_hour=23, seven_day=41.4)}
    assert _statusline(monkeypatch, capsys, payload) == "tapin · 5h 23% · week 41%\n"


def test_statusline_without_rate_limits_or_session_writes_nothing(monkeypatch, capsys):
    assert _statusline(monkeypatch, capsys, {"session_id": "s1", "cwd": "/tmp/x"}) == ""
    assert _statusline(monkeypatch, capsys, {"cwd": "/tmp/x", "rate_limits": _limits(five_hour=50)}) == "tapin · 5h 50%\n"
    assert not usage.usage_dir().exists()


def test_statusline_runs_the_wrapped_status_line_with_the_same_input(tmp_path, monkeypatch, capsys):
    seen = tmp_path / "seen.json"
    statusline.save_record({"original": {"type": "command", "command": f"cat > {shlex.quote(str(seen))}; printf 'MY LINE\\n'", "padding": 1}})
    raw = json.dumps({"session_id": "s1", "cwd": str(tmp_path), "rate_limits": _limits(five_hour=91.2)})

    assert _statusline(monkeypatch, capsys, raw) == "MY LINE\n"
    assert seen.read_text() == raw
    assert usage.claude_windows("s1")["five_hour"]["used_percent"] == 91.2


def test_statusline_chains_to_the_project_status_line_where_the_session_runs(tmp_path, monkeypatch, capsys):
    project = tmp_path / "graft-repo"
    (project / "src").mkdir(parents=True)
    statusline.save_record(
        {
            "original": {"type": "command", "command": "printf USER_LINE"},
            "projects": {str(project.resolve()): {"original": {"type": "command", "command": "printf GRAFT_LINE"}}},
        }
    )
    assert _statusline(monkeypatch, capsys, {"session_id": "s1", "cwd": str(project / "src")}) == "GRAFT_LINE"
    assert _statusline(monkeypatch, capsys, {"session_id": "s1", "workspace": {"current_dir": str(project)}}) == "GRAFT_LINE"
    assert _statusline(monkeypatch, capsys, {"session_id": "s1", "cwd": str(tmp_path / "graft-repo-2")}) == "USER_LINE"


def test_wrapped_status_line_that_times_out_shows_the_usage_line_and_is_not_a_failure(cfg, monkeypatch, capsys):
    monkeypatch.setattr(statusline, "TIMEOUT_SECONDS", 0.2)
    statusline.save_record({"original": {"type": "command", "command": "sleep 3; echo failed"}})

    assert _statusline(monkeypatch, capsys, {"session_id": "s1", "rate_limits": _limits(five_hour=42)}) == "tapin · 5h 42%\n"
    assert _statusline(monkeypatch, capsys, {"session_id": "s1"}) == ""

    log = config.tapin_home() / "hooks.log"
    lines = log.read_text().splitlines()
    assert len(lines) == 2 and all(line.endswith(" statusline: wrapped command timed out after 0.2s: `sleep 3; echo failed`") for line in lines)
    checks = install.doctor(cfg)
    assert (install.OK, f"hook failures logged in {log}: 0") in checks
    unfinished = f"your previous status line command timed out or couldn't run 2 time(s); Tap In showed its usage line instead (see {log})"
    assert (install.WARN, unfinished) in checks


def test_wrapped_status_line_exit_status_and_bad_input(monkeypatch, capsys):
    statusline.save_record({"original": {"type": "command", "command": "printf PARTIAL; exit 3"}})
    assert _statusline(monkeypatch, capsys, {"session_id": "s1"}) == "PARTIAL"
    assert _statusline(monkeypatch, capsys, "not json") == ""
    assert (config.tapin_home() / "hooks.log").read_text().count("hook claude statusline failed\n") == 1


def test_statusline_loads_no_argparse_config_parser_hooks_or_install():
    code = (
        "import json, sys\n"
        "from tapin import cli\n"
        "cli.main(['statusline'])\n"
        "heavy = ['argparse', 'tomllib', 'tapin.hooks', 'tapin.install', 'tapin.agents', 'tapin.readers', 'tapin.capture']\n"
        "print(json.dumps([name for name in heavy if name in sys.modules]), file=sys.stderr)\n"
    )
    payload = json.dumps({"session_id": "s1", "rate_limits": _limits(five_hour=12)})
    result = subprocess.run([sys.executable, "-c", code], input=payload, capture_output=True, text=True, check=True)
    assert result.stdout == "tapin · 5h 12%\n"
    assert json.loads(result.stderr.strip().splitlines()[-1]) == []


# Warnings


def _windows(pct, resets_at, name="five_hour", label="5-hour"):
    return {name: {"used_percent": pct, "resets_at": resets_at, "label": label}}


def test_warning_message_names_the_fullest_window_and_local_reset_time():
    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    reset = int((now + timedelta(minutes=90)).timestamp())
    windows = {**_windows(50, reset), **_windows(91.2, reset + 60, "seven_day", "weekly")}

    assert usage.warning_for("claude", "s1", windows, [90, 97], now=now) == (
        "[tapin] Usage warning: this Claude Code account has used 91% of its weekly limit (resets 13:31). You may be "
        "stopped mid-task soon. Before your next step, record a checkpoint: call the Tap In MCP tool `checkpoint` (or run "
        "`tapin checkpoint`) with what is done, what is in progress (file and step), and the exact next step. Then "
        "continue the task."
    )
    tomorrow = datetime.fromtimestamp((now + timedelta(days=1)).timestamp()).astimezone()
    assert usage._reset_time(tomorrow.timestamp(), now) == f"{tomorrow:%b} {tomorrow.day} {tomorrow:%H:%M}"


def test_warning_fires_once_per_threshold_and_window_reset():
    now = utcnow()
    reset = int(now.timestamp()) + 3600

    def warn(pct, resets_at=reset, session="s1", thresholds=(90, 97)):
        return usage.warning_for("codex", session, _windows(pct, resets_at), list(thresholds), now=now)

    assert warn(89.9) is None
    assert "this Codex account has used 90% of its 5-hour limit" in warn(90)
    assert warn(96) is None
    assert "has used 97%" in warn(97.5)
    assert warn(99) is None
    assert "has used 92%" in warn(92, resets_at=reset + 18_000)
    assert warn(92, resets_at=reset + 18_000) is None
    assert "has used 95%" in warn(95, session="s2")
    assert warn(98, resets_at=int(now.timestamp()) - 1, session="s3") is None
    assert warn(99, session="s4", thresholds=()) is None


def _post_tool_use(session_id="s1"):
    return {"session_id": session_id, "cwd": "/tmp/x", "hook_event_name": "PostToolUse", "tool_name": "Bash"}


def test_claude_post_tool_use_warns_once_without_workspace_lookup_or_git(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("PostToolUse must not look up the workspace or run git")

    usage.record_claude({"session_id": "s1", "cwd": "/tmp/x", "rate_limits": _limits(five_hour=91.2)})
    for target, name in ((workspace, "find_root"), (workspace, "_git"), (subprocess, "run"), (subprocess, "Popen")):
        monkeypatch.setattr(target, name, forbidden)

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_post_tool_use())))
    assert cli.main(["hook", "claude", "post-tool-use"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert output["hookSpecificOutput"]["additionalContext"].startswith(
        "[tapin] Usage warning: this Claude Code account has used 91% of its 5-hour limit (resets "
    )

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_post_tool_use())))
    assert cli.main(["hook", "claude", "post-tool-use"]) == 0
    assert capsys.readouterr().out == ""
    assert not (config.tapin_home() / "hooks.log").exists()


def test_claude_post_tool_use_stays_quiet_below_threshold_stale_or_disabled(cfg):
    usage.record_claude({"session_id": "low", "rate_limits": _limits(five_hour=89, seven_day=40)})
    assert hooks.handle("claude", "post-tool-use", _post_tool_use("low"), cfg) == {}

    usage.record_claude({"session_id": "stale", "rate_limits": _limits(five_hour=95)})
    path = usage.snapshot_path("stale")
    path.write_text(json.dumps({**json.loads(path.read_text()), "updated_at": iso(utcnow() - timedelta(minutes=21))}))
    assert hooks.handle("claude", "post-tool-use", _post_tool_use("stale"), cfg) == {}

    usage.record_claude({"session_id": "off", "rate_limits": _limits(five_hour=95)})
    assert hooks.handle("claude", "post-tool-use", _post_tool_use("off"), {**cfg, "warn_thresholds": []}) == {}
    assert hooks.handle("claude", "post-tool-use", _post_tool_use("off"), cfg) != {}

    assert hooks.handle("claude", "post-tool-use", _post_tool_use("no-snapshot"), cfg) == {}
    assert hooks.handle("cursor", "post-tool-use", {"conversation_id": "c1"}, cfg) == {}


def _token_count(rate_limits):
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {}}, "rate_limits": rate_limits}}


def test_codex_usage_comes_from_the_latest_token_count_with_rate_limits(tmp_path, cfg):
    reset = int(time.time()) + 3600
    records = [
        _token_count({"primary": {"used_percent": 50.0, "window_minutes": 300, "resets_at": reset}, "secondary": None}),
        _token_count(
            {
                "limit_id": "codex",
                "primary": {"used_percent": 97.0, "window_minutes": 300, "resets_at": reset},
                "secondary": {"used_percent": 15.0, "window_minutes": 43200, "resets_at": reset + 60},
            }
        ),
        # Codex 0.154 wrote this right after the limit ended a turn: another limit id, with no windows.
        _token_count({"limit_id": "premium", "primary": None, "secondary": None, "credits": {"has_credits": False}}),
        _token_count(None),
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "rg token_count"}},
    ]
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("".join(json.dumps(r) + "\n" for r in records) + '{"type":"event_msg","payload":{"type":"token_co')

    assert usage.codex_windows(str(rollout)) == {
        "primary": {"used_percent": 97.0, "resets_at": reset, "label": "5-hour"},
        "secondary": {"used_percent": 15.0, "resets_at": reset + 60, "label": "monthly"},
    }
    assert [usage.codex_label(m) for m in (300, 10080, 43200, 60, None)] == ["5-hour", "weekly", "monthly", "60-minute", "usage"]
    assert usage.codex_windows(tmp_path / "missing.jsonl") == {} and usage.codex_windows(None) == {}

    payload = {"session_id": "x1", "turn_id": "t1", "cwd": str(tmp_path), "transcript_path": str(rollout), "tool_name": "Bash"}
    context = hooks.handle("codex", "post-tool-use", payload, cfg)["hookSpecificOutput"]["additionalContext"]
    assert "this Codex account has used 97% of its 5-hour limit" in context
    assert hooks.handle("codex", "post-tool-use", payload, cfg) == {}
    assert hooks.handle("codex", "post-tool-use", {**payload, "session_id": "x2", "transcript_path": None}, cfg) == {}


def test_first_usage_writes_prune_files_older_than_a_week(monkeypatch):
    directory = usage.usage_dir()
    directory.mkdir(parents=True)

    def backdated(name, days):
        path = directory / name
        path.write_text("{}")
        then = time.time() - days * 86400
        os.utime(path, (then, then))
        return path

    old_snapshot, old_warned, recent = backdated("claude-old.json", 8), backdated("warned-codex-old.json", 7.1), backdated("claude-recent.json", 6)
    usage.record_claude({"session_id": "s1", "rate_limits": _limits(five_hour=10)})
    assert not old_snapshot.exists() and not old_warned.exists()
    assert recent.exists() and usage.snapshot_path("s1").exists()

    stale = backdated("claude-stale.json", 8)
    usage.record_claude({"session_id": "s1", "rate_limits": _limits(five_hour=20)})
    assert stale.exists()

    now = utcnow()
    reset = int(now.timestamp()) + 3600
    assert usage.warning_for("codex", "x1", _windows(95, reset), [90, 97], now=now) is not None
    assert not stale.exists()
    stale = backdated("claude-stale-2.json", 8)
    assert usage.warning_for("codex", "x1", _windows(98, reset), [90, 97], now=now) is not None
    assert stale.exists()

    def refuse(path, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(os, "unlink", refuse)
    usage.record_claude({"session_id": "s2", "rate_limits": _limits(five_hour=10)})
    assert stale.exists() and usage.snapshot_path("s2").exists()
