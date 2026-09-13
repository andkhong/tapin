"""Usage-limit readings, and the warning that asks an agent to record a checkpoint while it can still respond.

The PostToolUse hook runs this after every tool call, so it reads one or two small JSON files (or the tail of a Codex
rollout) and never runs git or parses a whole session log."""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tapin import config
from tapin.store import iso, parse_iso, safe_name, utcnow

SNAPSHOT_MAX_AGE = timedelta(minutes=20)
USAGE_FILE_MAX_AGE = timedelta(days=7)
CLAUDE_WINDOWS = {"five_hour": "5-hour", "seven_day": "weekly", "spend_limit": "spend"}
CODEX_WINDOWS = ("primary", "secondary")
CODEX_LABELS = {300: "5-hour", 10080: "weekly", 43200: "monthly"}
WARNED_MAX = 50
MESSAGE = (
    "[tapin] Usage warning: this {agent} account has used {pct}% of its {label} limit{resets}. You may be stopped "
    "mid-task soon. Before your next step, record a checkpoint: {call} with what is done, what is in progress (file "
    "and step), and the exact next step. Then continue the task."
)
CHECKPOINT_CALL = "call the Tap In MCP tool `checkpoint` (or run `tapin checkpoint`)"
SESSION_CHECKPOINT_CALL = "call the Tap In MCP tool `checkpoint` with `session_id` `{session_id}` (or run `tapin checkpoint --session {quoted}`)"

Windows = dict[str, dict[str, Any]]


def usage_dir() -> Path:
    return config.tapin_home() / "usage"


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _window(used: Any, resets_at: Any, label: str) -> dict[str, Any] | None:
    percent, reset = _number(used), _number(resets_at)
    if percent is None:
        return None
    return {"used_percent": percent, "resets_at": None if reset is None else int(reset), "label": label}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically, through a per-process temp file: status line runs for one session can overlap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def prune(now: datetime | None = None) -> None:
    """Delete usage files older than USAGE_FILE_MAX_AGE, since every session leaves a snapshot or warned file behind.
    Called only when one of those files is first created, so the per-call path stays the same. Never raises."""
    cutoff = (now or utcnow()).timestamp() - USAGE_FILE_MAX_AGE.total_seconds()
    try:
        entries = list(os.scandir(usage_dir()))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            continue


# Claude Code: its status line command receives rate limits, and `tapin statusline` saves them per session.


def snapshot_path(session_id: str | None) -> Path:
    return usage_dir() / f"claude-{safe_name(session_id)}.json"


def claude_payload_windows(payload: dict[str, Any]) -> Windows | None:
    """The windows in a Claude Code status line payload, or None when it has no `rate_limits` (not a Pro or Max
    plan, or before the first API response)."""
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    windows = {}
    for name, label in CLAUDE_WINDOWS.items():
        value = limits.get(name)
        if isinstance(value, dict) and (window := _window(value.get("used_percentage"), value.get("resets_at"), label)):
            windows[name] = window
    return windows


def record_claude(payload: dict[str, Any]) -> None:
    session_id = payload.get("session_id")
    windows = claude_payload_windows(payload)
    if windows is None or not isinstance(session_id, str) or not session_id:
        return
    path = snapshot_path(session_id)
    created = not path.exists()
    _write_json(path, {"agent": "claude", "session_id": session_id, "cwd": payload.get("cwd"), "windows": windows, "updated_at": iso(utcnow())})
    if created:
        prune()


def claude_windows(session_id: str | None, now: datetime | None = None) -> Windows:
    """The windows from the session's latest status line snapshot; none when it is missing or stale."""
    if not session_id:
        return {}
    snapshot = _read_json(snapshot_path(session_id))
    try:
        updated = parse_iso(snapshot["updated_at"])
    except (KeyError, AttributeError, TypeError, ValueError):
        return {}
    windows = snapshot.get("windows")
    if (now or utcnow()) - updated > SNAPSHOT_MAX_AGE or not isinstance(windows, dict):
        return {}
    return windows


# Codex: its rollout records rate limits with each `token_count` event.


def codex_label(window_minutes: Any) -> str:
    minutes = _number(window_minutes)
    if minutes is None:
        return "usage"
    return CODEX_LABELS.get(int(minutes), f"{int(minutes)}-minute")


def codex_windows(transcript_path: str | Path | None) -> Windows:
    """The windows in the latest `token_count` record of a Codex rollout that has any, read from the end of the file
    only. Records whose `rate_limits` is null, or has no primary or secondary window, are skipped."""
    if not transcript_path:
        return {}
    from tapin.readers.base import tail_lines

    for line in reversed(tail_lines(Path(transcript_path))):
        if '"token_count"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict) or payload.get("type") != "token_count" or not isinstance(payload.get("rate_limits"), dict):
            continue
        windows = {}
        for name in CODEX_WINDOWS:
            value = payload["rate_limits"].get(name)
            if isinstance(value, dict):
                window = _window(value.get("used_percent"), value.get("resets_at"), codex_label(value.get("window_minutes")))
                if window:
                    windows[name] = window
        if windows:
            return windows
    return {}


# The warning.


def _reset_time(resets_at: float, now: datetime) -> str:
    """Local HH:MM, with the date when it isn't today."""
    local = datetime.fromtimestamp(resets_at).astimezone()
    if local.date() == now.astimezone().date():
        return f"{local:%H:%M}"
    return f"{local:%b} {local.day} {local:%H:%M}"


def _first_warning(agent: str, session_id: str | None, key: str) -> bool:
    """Record `key` as warned for this session; False if it already was. Locked, because Codex can run the hooks for
    parallel tool calls at the same time."""
    path = usage_dir() / f"warned-{safe_name(agent)}-{safe_name(session_id)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        try:
            warned = json.loads(f.read() or "{}").get("warned")
        except (ValueError, AttributeError):
            warned = None
        warned = warned if isinstance(warned, list) else []
        if key in warned:
            return False
        f.seek(0)
        f.truncate()
        f.write(json.dumps({"warned": [*warned, key][-WARNED_MAX:]}))
    if created:
        prune()
    return True


def warning_for(agent: str, session_id: str | None, windows: Windows, thresholds: list[Any], now: datetime | None = None) -> str | None:
    """The warning for the fullest window once it crosses a threshold: once per window, reset time and threshold.
    Windows whose reset time has passed are ignored, since their percentages are from before the reset."""
    now = now or utcnow()
    levels = [level for level in map(_number, thresholds or []) if level is not None]
    current = {
        name: window
        for name, window in (windows or {}).items()
        if isinstance(window, dict)
        and _number(window.get("used_percent")) is not None
        and not ((reset := _number(window.get("resets_at"))) is not None and reset <= now.timestamp())
    }
    if not levels or not current:
        return None
    name, window = max(current.items(), key=lambda item: float(item[1]["used_percent"]))
    used = float(window["used_percent"])
    crossed = [level for level in levels if used >= level]
    if not crossed:
        return None
    resets_at = _number(window.get("resets_at"))
    if not _first_warning(agent, session_id, f"{name}:{window.get('resets_at')}:{max(crossed):g}"):
        return None

    import shlex

    from tapin import agents

    # With the session id, the checkpoint is tied to this session and can't be mistaken for another one's.
    call = SESSION_CHECKPOINT_CALL.format(session_id=session_id, quoted=shlex.quote(session_id)) if session_id else CHECKPOINT_CALL
    return MESSAGE.format(
        agent=agents.display(agent),
        pct=int(used),
        label=window.get("label") or name,
        resets="" if resets_at is None else f" (resets {_reset_time(resets_at, now)})",
        call=call,
    )
