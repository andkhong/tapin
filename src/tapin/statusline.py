"""`tapin statusline`, Claude Code's status line command. It saves the session's rate limits for the PostToolUse usage
warning, then shows the status line Tap In wrapped (the user's own, or a project's), or else a compact usage line.

Claude Code runs it on every status update, so the CLI handles it before argparse and it imports little."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from tapin import config, usage
from tapin.md import clip
from tapin.store import iso, utcnow

OURS = re.compile(r"tapin'? statusline\s*$")
TIMEOUT_SECONDS = 5
SHORT_NAMES = {"five_hour": "5h", "seven_day": "week", "spend_limit": "spend"}
# Marks hooks.log lines about a wrapped status line that didn't finish. That isn't a Tap In failure, so doctor warns
# about these instead of counting them with the lines that end in " failed".
LOG_MARKER = " statusline: wrapped command "
LOG_COMMAND_MAX = 120


def record_path() -> Path:
    """What Tap In replaced: {"original": <user status line>, "projects": {<dir>: {"original": ..., ...}}}."""
    return config.tapin_home() / "statusline.json"


def load_record() -> dict[str, Any]:
    path = record_path()
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return data if isinstance(data, dict) else {}


def save_record(record: dict[str, Any]) -> None:
    path = record_path()
    if not record:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")


def is_ours(status_line: Any) -> bool:
    return isinstance(status_line, dict) and isinstance(status_line.get("command"), str) and bool(OURS.search(status_line["command"]))


def command_of(status_line: Any) -> str | None:
    if not isinstance(status_line, dict) or status_line.get("type", "command") != "command" or is_ours(status_line):
        return None
    command = status_line.get("command")
    return command if isinstance(command, str) and command.strip() else None


def _places(payload: dict[str, Any]) -> list[Path]:
    """Where the session runs. Project settings come from the directory Claude Code started in, so that goes first."""
    workspace = payload.get("workspace") if isinstance(payload.get("workspace"), dict) else {}
    values = (workspace.get("project_dir"), payload.get("cwd"), workspace.get("current_dir"))
    return [Path(value).resolve() for value in values if isinstance(value, str) and value]


def wrapped(payload: dict[str, Any], record: dict[str, Any]) -> Any:
    """The status line to chain to: the one wrapped in the project the session runs in, else the user's."""
    projects = record.get("projects") if isinstance(record.get("projects"), dict) else {}
    if projects:
        for place in _places(payload):
            inside = [directory for directory in projects if place.is_relative_to(directory)]
            if inside:
                entry = projects[max(inside, key=len)]
                return entry.get("original") if isinstance(entry, dict) else None
    return record.get("original")


def compact(windows: usage.Windows) -> str:
    parts = [f"{SHORT_NAMES.get(name, name)} {float(window['used_percent']):.0f}%" for name, window in windows.items()]
    return " · ".join(["tapin", *parts]) if parts else ""


def _log_unfinished(problem: str, command: str) -> None:
    """Note in hooks.log that the wrapped command didn't finish. The command goes in backticks, so the line never ends
    in " failed" and doctor doesn't count it as a hook failure. Never raises."""
    try:
        home = config.tapin_home()
        home.mkdir(parents=True, exist_ok=True)
        with open(home / "hooks.log", "a") as log:
            log.write(f"{iso(utcnow())}{LOG_MARKER}{problem}: `{clip(' '.join(command.split()), LOG_COMMAND_MAX)}`\n")
    except OSError:
        pass


def render(raw: bytes) -> bytes:
    """The status line output for a payload. Recording usage never stops the wrapped status line from showing, and a
    wrapped command that times out or can't start is replaced by the compact usage line. One that exits non-zero
    still shows its output."""
    payload = json.loads(raw) if raw.strip() else {}
    payload = payload if isinstance(payload, dict) else {}
    try:
        usage.record_claude(payload)
    except Exception:
        from tapin import hooks

        hooks.log_exception("claude", "statusline")
    command = command_of(wrapped(payload, load_record()))
    if command:
        try:
            return subprocess.run(command, shell=True, input=raw, stdout=subprocess.PIPE, timeout=TIMEOUT_SECONDS).stdout
        except subprocess.TimeoutExpired:
            _log_unfinished(f"timed out after {TIMEOUT_SECONDS:g}s", command)
        except OSError as exc:
            _log_unfinished(f"couldn't run ({exc.strerror or exc})", command)
    line = compact(usage.claude_payload_windows(payload) or {})
    return f"{line}\n".encode() if line else b""
