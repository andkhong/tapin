from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from tapin.agents.base import Agent, StartEvent, StopEvent
from tapin.store import iso, utcnow


def cursor_cwd(payload: dict[str, Any]) -> Path:
    roots = payload.get("workspace_roots") or []
    return Path(roots[0] if roots else payload.get("cwd") or os.getcwd())


def cursor_session(payload: dict[str, Any]) -> str | None:
    return payload.get("conversation_id") or payload.get("session_id")


class Cursor(Agent):
    name = "cursor"
    display = "Cursor"

    def parse_start(self, payload: dict[str, Any]) -> StartEvent:
        return StartEvent(self.name, cursor_cwd(payload), cursor_session(payload), payload.get("composer_mode"))

    def start_output(self, brief: str) -> dict[str, Any]:
        return {"additional_context": brief}

    def launch_argv(self, prompt: str, cfg: dict[str, Any]) -> list[str]:
        # `cursor agent` silently installs cursor-agent when it's missing; open the IDE instead,
        # where the sessionStart hook injects the handoff.
        if shutil.which("cursor-agent") is None:
            return ["cursor", "."]
        return [*self.command(cfg), prompt]

    def limit_stop(self, event: str, payload: dict[str, Any], cfg: dict[str, Any]) -> StopEvent | None:
        # `stop` only reports status=error; `sessionEnd` carries the message. Either may fire for one failure.
        message = payload.get("error_message") or ""
        if event == "stop":
            if payload.get("status") != "error":
                return None
        elif event != "session-end" or not message:
            return None
        limited = re.search(cfg["limit_message_pattern"], message, re.IGNORECASE)
        return StopEvent(
            agent=self.name,
            cwd=cursor_cwd(payload),
            session_id=cursor_session(payload),
            transcript_path=payload.get("transcript_path"),
            reason="rate_limit" if limited else "error",
            details=message or None,
            model=payload.get("model"),
            stopped_at=iso(utcnow()),
        )
