"""Session digests read straight from Codex's own rollout logs, with nothing else to install."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tapin.md import clip, clip_tail, close_open_fence, code_span, demote_headings, fence, one_line, quote
from tapin.readers import sections
from tapin.readers.base import ReaderError, SessionRef, last_record, read_jsonl, within
from tapin.store import iso

META_SCAN_LINES = 200
SCAN_MAX = 500
PLAN_MAX = 3_000
LAST_TEXT_MAX = 2_000
NOTICE_MAX = 1_000
OUTPUT_MAX = 800
COMMAND_MAX = 500
ACTIVITY_ARG_MAX = 200
ACTIVITY_FILES = 3
FAILED_MAX = 5
ACTIVITY_MAX = 25
_ROLLOUT_ID = re.compile(r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)$")


def sessions_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass
class _Meta:
    id: str | None = None
    cwd: str | None = None
    subagent: bool = False


def rollout_meta(path: Path) -> _Meta:
    """The rollout's own `session_meta` (the first one), with a `turn_context` cwd if it names none."""
    meta, seen = _Meta(), False
    try:
        for record in read_jsonl(path, META_SCAN_LINES):
            payload = record.get("payload") if record else None
            if not isinstance(payload, dict):
                continue
            if record.get("type") == "session_meta" and not seen:
                seen = True
                meta.id, meta.cwd = _str(payload.get("id")), meta.cwd or _str(payload.get("cwd"))
                meta.subagent = payload.get("thread_source") == "subagent" or bool(payload.get("parent_thread_id"))
            elif record.get("type") == "turn_context" and not meta.cwd:
                meta.cwd = _str(payload.get("cwd"))
            if seen and meta.cwd:
                break
    except OSError:
        pass
    return meta


def _rollouts() -> list[Path]:
    root = sessions_dir()
    if not root.is_dir():
        return []
    return sorted((p for p in root.rglob("rollout-*.jsonl") if p.is_file()), key=_mtime, reverse=True)


def _ref(agent: str, path: Path, meta: _Meta | None = None) -> SessionRef:
    meta = meta or rollout_meta(path)
    named = _ROLLOUT_ID.match(path.stem)
    session_id = meta.id or (named.group(1) if named else path.stem)
    updated = iso(datetime.fromtimestamp(_mtime(path), timezone.utc))
    return SessionRef(agent, session_id, path=str(path), cwd=meta.cwd, updated_at=updated)


def _workspace_rollouts(workspace: Path, paths: list[Path], since: float | None = None) -> Iterator[tuple[Path, _Meta]]:
    """The rollouts among `paths` (newest first) of sessions that ran in `workspace`, stopping at the first older than
    `since`. Each check opens a rollout, so only the newest SCAN_MAX are considered."""
    for path in paths[:SCAN_MAX]:
        if since is not None and _mtime(path) < since:
            return
        meta = rollout_meta(path)
        # A subagent's rollout shares the workspace but isn't the session the user was working in.
        if not meta.subagent and within(meta.cwd, workspace):
            yield path, meta


def _find(agent: str, workspace: Path, session_id: str | None) -> SessionRef | None:
    paths = _rollouts()
    if session_id:
        match = next((p for p in paths if p.stem.endswith(f"-{session_id}")), None)
        match = match or next((p for p in paths[:SCAN_MAX] if rollout_meta(p).id == session_id), None)
        return _ref(agent, match) if match else None
    found = next(_workspace_rollouts(workspace, paths), None)
    return _ref(agent, *found) if found else None


def recent_sessions(agent: str, workspace: Path, window: timedelta) -> list[SessionRef]:
    """Sessions that ran in `workspace` whose rollout was written within `window` of now, newest first."""
    since = time.time() - window.total_seconds()
    return [_ref(agent, path, meta) for path, meta in _workspace_rollouts(workspace, _rollouts(), since)]


def _identity(record: dict[str, Any]) -> tuple[str, str | None] | None:
    payload = record.get("payload") if record.get("type") == "turn_context" else None
    if isinstance(payload, dict) and _str(payload.get("model")):
        return payload["model"], _str(payload.get("effort"))
    return None


def session_identity(path: Path) -> tuple[str | None, str | None]:
    """The model and reasoning effort of the latest `turn_context` that names a model, searched from the end of a
    rollout. (`session_meta` names only the provider.)"""
    return last_record(path, '"turn_context"', _identity) or (None, None)


def _text(content: Any) -> str:
    """The text parts of an item or message (`Text`, `input_text`, `output_text`), joined."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n".join(part["text"].strip() for part in content if isinstance(part, dict) and _str(part.get("text"))).strip()


@dataclass
class _Messages:
    user: list[str] = field(default_factory=list)
    final_answer: str | None = None
    latest: str | None = None
    turn_latest: str | None = None

    def agent(self, text: str, phase: Any) -> None:
        if text:
            self.latest = self.turn_latest = text
            if phase == "final_answer":
                self.final_answer = text


@dataclass
class _Action:
    line: str
    failed: bool = False
    detail: str | None = None
    files: list[str] = field(default_factory=list)


def _command(item: dict[str, Any]) -> _Action:
    argv = item.get("command")
    command = argv[-1] if isinstance(argv, list) and argv and isinstance(argv[-1], str) else str(argv or "")
    code = item.get("exit_code")
    failed = isinstance(code, int) and code != 0
    line = code_span("$ " + clip(one_line(command), ACTIVITY_ARG_MAX)) + (f" (exit {code})" if failed else "")
    if not failed:
        return _Action(line)
    output = clip_tail(str(item.get("aggregated_output") or "").strip(), OUTPUT_MAX)
    detail = f"{code_span('$ ' + clip(one_line(command), COMMAND_MAX))} exited with code {code}" + (f":\n\n{fence(output)}" if output else ".")
    return _Action(line, failed=True, detail=detail)


def _file_change(item: dict[str, Any]) -> _Action:
    changes = item.get("changes") if isinstance(item.get("changes"), dict) else {}
    failed = item.get("status") == "failed"
    files, shown = [], []
    for path, change in changes.items():
        change = change if isinstance(change, dict) else {}
        files += [path] + ([change["move_path"]] if _str(change.get("move_path")) else [])
        shown.append(f"{code_span(path)} ({_str(change.get('type')) or 'update'})")
    more = f" and {len(shown) - ACTIVITY_FILES} more" if len(shown) > ACTIVITY_FILES else ""
    line = "Edited " + (", ".join(shown[:ACTIVITY_FILES]) or "files") + more + (" (failed)" if failed else "")
    return _Action(line, failed=failed, files=[] if failed else files)


def _mcp_call(item: dict[str, Any]) -> _Action:
    name = ".".join(filter(None, (_str(item.get("server")), _str(item.get("tool"))))) or "tool"
    failed = item.get("status") == "failed"
    return _Action(f"MCP {code_span(name)}" + (" (failed)" if failed else ""), failed=failed)


def _extension(item: dict[str, Any]) -> _Action:
    query = _str(item.get("query"))
    return _Action((_str(item.get("kind")) or "extension") + (f" {code_span(clip(query, ACTIVITY_ARG_MAX))}" if query else ""))


ACTIONS = {"CommandExecution": _command, "FileChange": _file_change, "McpToolCall": _mcp_call, "Extension": _extension}


@dataclass
class _Rollout:
    records: int = 0
    skipped: int = 0
    origin: str | None = None
    has_items: bool = False
    items: _Messages = field(default_factory=_Messages)
    responses: _Messages = field(default_factory=_Messages)
    completed_message: str | None = None
    error: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    plan: str | None = None
    actions: list[_Action] = field(default_factory=list)

    def add(self, record: dict[str, Any] | None) -> None:
        if record is None:
            self.skipped += 1
            return
        self.records += 1
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        kind, event = record.get("type"), payload.get("type")
        if kind == "session_meta" and self.origin is None:
            self.origin = " ".join(filter(None, (_str(payload.get("originator")), _str(payload.get("cli_version")))))
        elif kind == "event_msg" and event == "item_completed" and isinstance(payload.get("item"), dict):
            self.has_items = True
            self._item(payload["item"])
        elif kind == "event_msg" and event == "task_started":
            self.items.turn_latest = self.responses.turn_latest = None
        elif kind == "event_msg" and event == "task_complete":
            self.error = payload.get("error") if isinstance(payload.get("error"), dict) else None
            self.completed_message = _str(payload.get("last_agent_message")) or self.completed_message
        elif kind == "event_msg" and event == "token_count":
            limits = payload.get("rate_limits")
            primary = limits.get("primary") if isinstance(limits, dict) else None
            if isinstance(primary, dict) and primary.get("used_percent") is not None:
                self.usage = primary
        elif kind == "response_item" and event == "message":
            self._response(payload)

    def _item(self, item: dict[str, Any]) -> None:
        kind = item.get("type")
        if kind == "UserMessage":
            if text := _text(item.get("content")):
                self.items.user.append(text)
        elif kind == "AgentMessage":
            self.items.agent(_text(item.get("content")), item.get("phase"))
        elif kind == "Plan" and _str(item.get("text")):
            self.plan = item["text"]
        elif kind in ACTIONS:
            self.actions.append(ACTIONS[kind](item))

    def _response(self, payload: dict[str, Any]) -> None:
        """Fallback for rollouts without `item_completed` records; injected context parts start with `<`."""
        content = payload.get("content")
        parts = [p for p in content if isinstance(p, dict) and not (_str(p.get("text")) or "").lstrip().startswith("<")] if isinstance(content, list) else []
        text = _text(parts)
        if payload.get("role") == "user" and text:
            self.responses.user.append(text)
        elif payload.get("role") == "assistant":
            self.responses.agent(text, payload.get("phase"))


def _usage(primary: dict[str, Any]) -> str:
    used, window = primary.get("used_percent"), primary.get("window_minutes")
    share = f"{used:g}%" if isinstance(used, (int, float)) else f"{used}%"
    return f"Last reported usage: {share} of the {window}-minute window." if window else f"Last reported usage: {share}."


def _render(r: _Rollout, path: str) -> str:
    view = r.items if r.has_items else r.responses
    blocks = sections.header("Codex", path, r.records, r.skipped, f", written by {r.origin}" if r.origin else "")
    blocks += sections.section("Task", sections.task(view.user))
    if r.plan:
        blocks += sections.section("Plan", [demote_headings(close_open_fence(clip(r.plan.strip(), PLAN_MAX)), 2)])
    # A turn cut off by a limit never gets a final answer, so its own latest message beats an older turn's answer.
    last = view.turn_latest or view.final_answer or view.latest or r.completed_message
    blocks += sections.section("Last message before the stop", [quote(clip(last, LAST_TEXT_MAX))] if last else [])
    if r.error:
        message, code = _str(r.error.get("message")), r.error.get("codex_error_info")
        intro = "The last turn ended with an error" + (f", {code_span(str(code))}" if code else "")
        stop = [intro + ":", quote(clip(message, NOTICE_MAX))] if message else [intro + "."]
        blocks += sections.section("Stop", stop + ([_usage(r.usage)] if r.usage else []))
    edited = list(dict.fromkeys(file for action in reversed(r.actions) for file in action.files))
    blocks += sections.section("Files changed by the agent", sections.files(edited))
    failed = [action.detail for action in r.actions if action.detail]
    blocks += sections.section("Failed commands", failed[-FAILED_MAX:])
    blocks += sections.section("Recent activity", sections.activity([action.line for action in r.actions], ACTIVITY_MAX))
    return sections.join(blocks)


class CodexLogReader:
    def find_session(self, agent: str, workspace: Path, session_id: str | None = None) -> SessionRef | None:
        return _find(agent, workspace, session_id)

    def digest(self, ref: SessionRef) -> str:
        if not ref.path:
            raise ReaderError(f"no Codex session log path for session {ref.session_id}")
        rollout = _Rollout()
        try:
            for record in read_jsonl(Path(ref.path)):
                rollout.add(record)
        except OSError as exc:
            raise ReaderError(f"Codex session log unreadable: {exc}") from exc
        return _render(rollout, ref.path)
