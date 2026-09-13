from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from tapin.agents.base import Agent, StopEvent
from tapin.store import iso, utcnow

LIMIT_CODES = (
    "usage_limit_exceeded",
    "usage_limit_reached",
    "rate_limit_exceeded",
    "rate_limit_reached",
    "quota_exceeded",
    "usage limit",
)


def _mentions_limit(value: object) -> bool:
    text = (value if isinstance(value, str) else json.dumps(value)).lower()
    return any(code in text for code in LIMIT_CODES)


def rollout_limit_error(path: Path) -> str | None:
    """The usage-limit error that ended the current turn of a Codex rollout, if one did.

    Codex 0.154 ends such a turn with a `task_complete` whose `error` is
    {"codex_error_info": "usage_limit_exceeded", "message": ...}; records can follow it in the same file."""
    # Imported here: every hook loads this module, and journal hooks must not load the readers.
    from tapin.readers.base import tail_lines

    for line in reversed(tail_lines(path)):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") != "event_msg" or not isinstance(record.get("payload"), dict):
            continue
        payload = record["payload"]
        kind = str(payload.get("type", ""))
        if kind == "task_started":
            return None
        if kind == "task_complete":
            error = payload.get("error")
            if isinstance(error, dict):
                info, message = error.get("codex_error_info"), error.get("message")
                if (isinstance(info, str) and info.lower() in LIMIT_CODES) or (isinstance(message, str) and _mentions_limit(message)):
                    return str(message or info)
        elif ("error" in kind or "codex_error_info" in payload) and _mentions_limit(payload):
            return str(payload.get("message") or payload.get("codex_error_info") or kind)
    return None


def rollout_hit_limit(path: Path) -> bool:
    return rollout_limit_error(path) is not None


class Codex(Agent):
    name = "codex"
    display = "Codex"

    def command(self, cfg: dict[str, Any]) -> list[str]:
        spec = cfg["agents"][self.name]
        command = list(spec["command"])
        fallback = spec.get("fallback")
        if shutil.which(command[0]) is None and fallback and Path(fallback).exists():
            command[0] = fallback
        return command

    def limit_stop(self, event: str, payload: dict[str, Any], cfg: dict[str, Any]) -> StopEvent | None:
        path = payload.get("transcript_path")
        error = rollout_limit_error(Path(path)) if event == "stop" and path else None
        if error is None:
            return None
        return StopEvent(
            agent=self.name,
            cwd=Path(payload.get("cwd") or os.getcwd()),
            session_id=payload.get("session_id"),
            transcript_path=path,
            reason="usage_limit",
            details=error,
            last_assistant_message=payload.get("last_assistant_message"),
            model=payload.get("model"),
            stopped_at=iso(utcnow()),
        )
