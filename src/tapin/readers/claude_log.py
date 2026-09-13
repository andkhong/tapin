"""Session digests read straight from Claude Code's own session logs, with nothing else to install."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tapin.md import clip, clip_tail, code_span, fence, one_line, quote
from tapin.readers import sections
from tapin.readers.base import ReaderError, SessionRef, last_record, read_jsonl, within
from tapin.store import iso

CWD_SCAN_LINES = 200
LAST_TEXT_MAX = 2_000
NOTICE_MAX = 1_000
TODO_MAX = 300
OUTPUT_MAX = 800
COMMAND_MAX = 500
ACTIVITY_ARG_MAX = 200
FAILED_MAX = 5
ACTIVITY_MAX = 25
RECAP_SUFFIX = " (disable recaps in /config)"
EDIT_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")
ARG_KEYS = ("file_path", "notebook_path", "path", "url", "pattern")
_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")


def projects_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "projects"


def project_dir_name(cwd: Path | str) -> str:
    """Claude Code names each project folder after the session's cwd, every non-alphanumeric character as `-`."""
    return _NOT_ALNUM.sub("-", str(cwd))


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def session_cwd(path: Path) -> str | None:
    """The cwd of the first record near the top of a session log that has one."""
    try:
        for record in read_jsonl(path, CWD_SCAN_LINES):
            if record and isinstance(record.get("cwd"), str):
                return record["cwd"]
    except OSError:
        pass
    return None


def _subdirs(root: Path) -> list[Path]:
    try:
        return [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return []


def _newest_first(directories: list[Path]) -> list[Path]:
    """Top-level session logs only: subagent transcripts live in `<session>/subagents/`."""
    return sorted((p for d in directories for p in d.glob("*.jsonl") if p.is_file()), key=_mtime, reverse=True)


def _folder_cwd(directory: Path) -> str | None:
    return next(filter(None, map(session_cwd, _newest_first([directory]))), None)


def _ref(agent: str, path: Path) -> SessionRef:
    updated = iso(datetime.fromtimestamp(_mtime(path), timezone.utc))
    return SessionRef(agent, path.stem, path=str(path), cwd=session_cwd(path), updated_at=updated)


def _recorded_in(paths: list[Path], workspace: Path, since: float | None) -> Iterator[Path]:
    """The logs among `paths` (newest first) whose session ran in `workspace`, stopping at the first older than `since`."""
    for path in paths:
        if since is not None and _mtime(path) < since:
            return
        if within(session_cwd(path), workspace):
            yield path


def _workspace_logs(workspace: Path, since: float | None = None) -> Iterator[Path]:
    """Top-level logs of sessions that ran in `workspace`, in the order find_session prefers them: newest first in the
    folders named after it, then newest first in other folders. With `since`, only logs written at or after it."""
    dirs = _subdirs(projects_dir())
    prefixes = {project_dir_name(workspace), project_dir_name(workspace.resolve())}
    named = [d for d in dirs if any(d.name == p or d.name.startswith(p + "-") for p in prefixes)]
    yield from _recorded_in(_newest_first(named), workspace, since)
    # Folder names for paths with dots or underscores are unverified, so also match other folders by recorded cwd.
    others = [d for d in dirs if d not in named and (since is None or _written_since(d, since)) and within(_folder_cwd(d), workspace)]
    yield from _recorded_in(_newest_first(others), workspace, since)


def _written_since(directory: Path, since: float) -> bool:
    return any(_mtime(path) >= since for path in directory.glob("*.jsonl"))


def _find(agent: str, workspace: Path, session_id: str | None) -> SessionRef | None:
    if session_id:
        name = f"{session_id}.jsonl"
        if Path(name).name != name:
            return None
        paths = [d / name for d in _subdirs(projects_dir()) if (d / name).is_file()]
        return _ref(agent, max(paths, key=_mtime)) if paths else None
    path = next(_workspace_logs(workspace), None)
    return _ref(agent, path) if path else None


def recent_sessions(agent: str, workspace: Path, window: timedelta) -> list[SessionRef]:
    """Sessions that ran in `workspace` whose log was written within `window` of now, newest first."""
    since = time.time() - window.total_seconds()
    return [_ref(agent, path) for path in sorted(_workspace_logs(workspace, since), key=_mtime, reverse=True)]


def _identity(record: dict[str, Any]) -> tuple[str, str | None] | None:
    if record.get("type") != "assistant" or record.get("isSidechain"):
        return None
    message = record.get("message")
    model = message.get("model") if isinstance(message, dict) else None
    if isinstance(model, str) and model and model != "<synthetic>":
        return model, _str(record.get("effort"))
    return None


def session_identity(path: Path) -> tuple[str | None, str | None]:
    """The model and reasoning effort of the latest assistant response in a session log, searched from the end.

    Each assistant record has `message.model` and a top-level `effort`. Records with the model `<synthetic>` weren't
    written by a model, and sidechain records can come from a subagent on another model, so both are skipped."""
    return last_record(path, '"assistant"', _identity) or (None, None)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _text(content: Any) -> str:
    """A string as is, or the text blocks of a content list joined."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))


def _is_prompt(text: str) -> bool:
    """Something the user typed, not a wrapper like `<command-name>` or an interruption marker."""
    return bool(text) and not text.startswith("<") and "[Request interrupted by user" not in text


@dataclass
class _Call:
    name: str
    command: str | None = None
    arg: str | None = None
    edited: str | None = None
    failed: bool = False
    output: str = ""


@dataclass
class _Session:
    records: int = 0
    skipped: int = 0
    title: str | None = None
    last_prompt: str | None = None
    prompts: list[str] = field(default_factory=list)
    recap: str | None = None
    last_text: str | None = None
    stop: tuple[str, str] | None = None
    stop_limit: str | None = None
    quota: Any = None
    todos: list[dict[str, Any]] | None = None
    calls: list[_Call] = field(default_factory=list)
    by_id: dict[str, _Call] = field(default_factory=dict)

    def add(self, record: dict[str, Any] | None) -> None:
        if record is None:
            self.skipped += 1
            return
        self.records += 1
        previous_quota, self.quota = self.quota, record.get("quotaLimits")
        if record.get("isMeta") or record.get("isSidechain"):
            return
        kind = record.get("type")
        if kind == "user":
            self._user(record)
        elif kind == "assistant":
            self._assistant(record, previous_quota)
        elif kind == "system" and record.get("subtype") == "away_summary" and _str(record.get("content")):
            self.recap = record["content"].strip().removesuffix(RECAP_SUFFIX).strip() or self.recap
        elif kind == "ai-title" and _str(record.get("aiTitle")):
            self.title = record["aiTitle"].strip() or self.title
        elif kind == "last-prompt" and _str(record.get("lastPrompt")):
            self.last_prompt = record["lastPrompt"].strip() or self.last_prompt

    def _user(self, record: dict[str, Any]) -> None:
        texts = []
        for block in _blocks(record):
            if block.get("type") == "text":
                text = _text([block]).strip()
                if _is_prompt(text):
                    texts.append(text)
            elif block.get("type") == "tool_result" and block.get("is_error"):
                call = self.by_id.get(str(block.get("tool_use_id")))
                if call:
                    call.failed, call.output = True, clip_tail(_text(block.get("content")).strip(), OUTPUT_MAX)
        if texts:
            self.prompts.append("\n\n".join(texts))

    def _assistant(self, record: dict[str, Any], previous_quota: Any) -> None:
        blocks = _blocks(record)
        if record.get("isApiErrorMessage"):
            self.stop = (str(record.get("error") or "unknown"), _text(blocks).strip())
            self.stop_limit = _limit_line(record.get("quotaLimits")) or _limit_line(previous_quota)
            return
        self.stop, self.stop_limit = None, None
        for block in blocks:
            if block.get("type") == "text" and _text([block]).strip():
                self.last_text = _text([block]).strip()
            elif block.get("type") == "tool_use":
                self._tool_use(block)

    def _tool_use(self, block: dict[str, Any]) -> None:
        call_id, name = _str(block.get("id")), str(block.get("name") or "tool")
        if call_id and call_id in self.by_id:
            return
        args = block.get("input") if isinstance(block.get("input"), dict) else {}
        call = _Call(
            name,
            command=_str(args.get("command")) if name == "Bash" else None,
            arg=next(filter(None, (_str(args.get(key)) for key in ARG_KEYS)), None),
            edited=(_str(args.get("file_path")) or _str(args.get("notebook_path"))) if name in EDIT_TOOLS else None,
        )
        self.calls.append(call)
        if call_id:
            self.by_id[call_id] = call
        if name == "TodoWrite" and isinstance(args.get("todos"), list):
            self.todos = [todo for todo in args["todos"] if isinstance(todo, dict)]


def _label(call: _Call) -> str:
    if call.command is not None:
        return f"$ {clip(call.command, COMMAND_MAX)}"
    return f"{call.name} {clip(call.arg, COMMAND_MAX)}" if call.arg else call.name


def _activity(call: _Call) -> str:
    if call.command is not None:
        text = code_span("$ " + clip(one_line(call.command), ACTIVITY_ARG_MAX))
    elif call.arg:
        text = f"{call.name} {code_span(clip(call.arg, ACTIVITY_ARG_MAX))}"
    else:
        text = call.name
    return text + (" (failed)" if call.failed else "")


def _todo(todo: dict[str, Any]) -> str:
    return f"- [{todo.get('status') or 'unknown'}] {clip(' '.join(str(todo.get('content') or '').split()), TODO_MAX)}"


def _limit_line(quota: Any) -> str | None:
    """Claude Code (2.1) stores the rejected request's `quotaLimits` with its API error: which limit and when it resets,
    but no usage percentages."""
    resets_at = quota.get("resetsAt") if isinstance(quota, dict) else None
    if not isinstance(resets_at, (int, float)) or isinstance(resets_at, bool):
        return None
    try:
        reset = iso(datetime.fromtimestamp(resets_at, timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None
    return f"Limit: {_str(quota.get('rateLimitType')) or 'usage'}, resets {reset}."


def _render(s: _Session, path: str) -> str:
    blocks = sections.header("Claude Code", path, s.records, s.skipped)
    prompts = s.prompts or ([s.last_prompt] if s.last_prompt else [])
    blocks += sections.section("Task", ([f"**{s.title}**"] if s.title else []) + sections.task(prompts))
    blocks += sections.section("Claude Code's latest recap", [s.recap] if s.recap else [])
    blocks += sections.section("Last message before the stop", [quote(clip(s.last_text, LAST_TEXT_MAX))] if s.last_text else [])
    if s.stop:
        error, notice = s.stop
        intro = f"The session's last response was an API error, {code_span(error)}"
        lines = [intro + ":", quote(clip(notice, NOTICE_MAX))] if notice else [intro + "."]
        blocks += sections.section("Stop", lines + ([s.stop_limit] if s.stop_limit else []))
    blocks += sections.section("Task list", ["\n".join(map(_todo, s.todos))] if s.todos else [])
    edited = list(dict.fromkeys(call.edited for call in reversed(s.calls) if call.edited and not call.failed))
    blocks += sections.section("Files changed by the agent", sections.files(edited))
    failed = [call for call in s.calls if call.failed][-FAILED_MAX:]
    blocks += sections.section("Failed commands", [fence(f"{_label(call)}\n{call.output}") for call in failed])
    blocks += sections.section("Recent activity", sections.activity([_activity(call) for call in s.calls], ACTIVITY_MAX))
    return sections.join(blocks)


class ClaudeLogReader:
    def find_session(self, agent: str, workspace: Path, session_id: str | None = None) -> SessionRef | None:
        return _find(agent, workspace, session_id)

    def digest(self, ref: SessionRef) -> str:
        if not ref.path:
            raise ReaderError(f"no Claude Code session log path for session {ref.session_id}")
        session = _Session()
        try:
            for record in read_jsonl(Path(ref.path)):
                session.add(record)
        except OSError as exc:
            raise ReaderError(f"Claude Code session log unreadable: {exc}") from exc
        return _render(session, ref.path)
