"""Entry points invoked by each agent's hook system. A hook must never break the agent that runs it.

Journal events fire on every prompt, edit and shell command, so modules only a stop or a pending handoff needs
are imported inside the functions that use them."""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from datetime import timedelta
from typing import Any

from tapin import agents, config, workspace
from tapin.agents.base import Agent, StopEvent
from tapin.md import clip
from tapin.store import Store, iso, utcnow

DUPLICATE_WINDOW = timedelta(minutes=2)
REFINE_WAIT = timedelta(seconds=2)
JOURNAL_EVENTS = ("before-submit-prompt", "after-agent-response", "after-file-edit", "after-shell-execution")
EVENTS = ("session-start", "stop", "stop-failure", "session-end", "post-tool-use", *JOURNAL_EVENTS)
JOURNAL_OUTPUT_MAX = 4_000


def handle(agent_name: str, event: str, payload: dict[str, Any], cfg: dict[str, Any], background: bool = True) -> dict[str, Any]:
    agent = agents.get(agent_name)
    if event not in EVENTS:
        raise ValueError(f"unknown hook event {event!r}; expected one of: {', '.join(EVENTS)}")
    if event == "session-start":
        return session_start(agent, payload, cfg)
    if event == "post-tool-use":
        return post_tool_use(agent, payload, cfg)
    if event in JOURNAL_EVENTS:
        record_journal(agent, event, payload)
        return fallback_output(event)

    stop = agent.limit_stop(event, payload, cfg)
    if stop is None:
        return {}
    store = Store(workspace.find_root(stop.cwd))
    if store.begin_capture(stop.agent, stop.session_id, DUPLICATE_WINDOW):
        if background:
            spawn_capture(stop)
        else:
            run_capture(stop, cfg)
    else:
        from tapin import capture

        capture.refine_reason(store, stop, REFINE_WAIT if background else timedelta(0))
    return {}


def fallback_output(event: str) -> dict[str, Any]:
    return {"continue": True} if event == "before-submit-prompt" else {}


def session_start(agent: Agent, payload: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    start = agent.parse_start(payload)
    store = Store(workspace.find_root(start.cwd))
    pending = store.claim(agent.name, start.session_id)
    if pending is None:
        return {}
    from tapin import packet

    brief = packet.brief(store.read_meta(pending.id), store.handoff_file(pending.id), cfg["brief_max_chars"])
    return agent.start_output(brief)


def post_tool_use(agent: Agent, payload: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Tell the agent to record a checkpoint when its account nears a usage limit. This runs after every tool call, so
    it reads the status line's snapshot (Claude Code) or the end of the rollout (Codex) and never looks for the
    workspace. Cursor reports no usage."""
    from tapin import usage

    thresholds = cfg.get("warn_thresholds") or []
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
    if not thresholds:
        return {}
    if agent.name == "claude":
        windows = usage.claude_windows(session_id)
    elif agent.name == "codex":
        windows = usage.codex_windows(payload.get("transcript_path"))
    else:
        return {}
    message = usage.warning_for(agent.name, session_id, windows, thresholds)
    if message is None:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": message}}


def record_journal(agent: Agent, event: str, payload: dict[str, Any]) -> None:
    start = agent.parse_start(payload)
    entry: dict[str, Any] = {"ts": iso(utcnow())}
    if event == "before-submit-prompt":
        entry |= {"kind": "prompt", "text": payload.get("prompt", "")}
    elif event == "after-agent-response":
        entry |= {"kind": "response", "text": payload.get("text", "")}
    elif event == "after-file-edit":
        entry |= {"kind": "edit", "file_path": payload.get("file_path"), "edits": len(payload.get("edits") or [])}
    else:
        entry |= {
            "kind": "shell",
            "command": payload.get("command", ""),
            "output": clip(payload.get("output") or "", JOURNAL_OUTPUT_MAX),
        }
    root = workspace.find_root(start.cwd)
    store = Store(root)
    first_entry = not store.journal_file(agent.name, start.session_id).exists()
    store.append_journal(agent.name, start.session_id, entry)
    if first_entry:
        workspace.ensure_excluded(root)


def run_capture(stop: StopEvent, cfg: dict[str, Any]) -> tuple[Store, str]:
    from tapin import capture, deliver

    store, handoff_id = capture.capture(stop, cfg)
    display = agents.get(stop.agent).display
    deliver.notify(
        f"{display} stopped: {stop.reason}",
        f"Handoff ready in {store.workspace.name}. Run `tapin to <agent>` there, or open another agent in that folder.",
    )
    return store, handoff_id


def capture_argv() -> list[str]:
    from tapin.install import launcher_path

    launcher = launcher_path()
    if launcher.exists() and os.access(launcher, os.X_OK):
        return [str(launcher), "capture-event"]
    return [sys.executable, "-m", "tapin", "capture-event"]


def spawn_capture(stop: StopEvent) -> None:
    """Capture in a detached process so the hook returns immediately; reading logs can take seconds."""
    home = config.tapin_home()
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "hooks.log", "a") as log:
        proc = subprocess.Popen(
            capture_argv(),
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
            start_new_session=True,
            text=True,
        )
    assert proc.stdin is not None
    proc.stdin.write(stop.to_json())
    proc.stdin.close()


def log_exception(agent: str, event: str) -> None:
    home = config.tapin_home()
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "hooks.log", "a") as log:
        log.write(f"{iso(utcnow())} hook {agent} {event} failed\n{traceback.format_exc()}\n")
