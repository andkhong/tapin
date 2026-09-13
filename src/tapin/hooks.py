"""Entry points invoked by each agent's hook system. A hook must never break the agent that runs it."""

from __future__ import annotations

import subprocess
import sys
import traceback
from datetime import timedelta
from typing import Any

from tapin import agents, capture, config, deliver, packet, workspace
from tapin.agents.base import Agent, StopEvent
from tapin.md import clip
from tapin.store import Store, iso, utcnow

DUPLICATE_WINDOW = timedelta(minutes=2)
REFINE_WAIT = timedelta(seconds=2)
JOURNAL_EVENTS = ("before-submit-prompt", "after-agent-response", "after-file-edit", "after-shell-execution")
JOURNAL_OUTPUT_MAX = 4_000


def handle(agent_name: str, event: str, payload: dict[str, Any], cfg: dict[str, Any], background: bool = True) -> dict[str, Any]:
    agent = agents.get(agent_name)
    if event == "session-start":
        return session_start(agent, payload, cfg)
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
    brief = packet.brief(store.read_meta(pending.id), store.handoff_file(pending.id), cfg["brief_max_chars"])
    return agent.start_output(brief)


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
    store, handoff_id = capture.capture(stop, cfg)
    display = agents.get(stop.agent).display
    deliver.notify(
        f"{display} stopped: {stop.reason}",
        f"Handoff ready in {store.workspace.name}. Run `tapin to <agent>` there, or open another agent in that folder.",
    )
    return store, handoff_id


def spawn_capture(stop: StopEvent) -> None:
    """Capture in a detached process so the hook returns immediately; reading logs can take seconds."""
    home = config.tapin_home()
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "hooks.log", "a") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "tapin", "capture-event"],
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
