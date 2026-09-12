from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from tapin.agents.base import Agent, StopEvent
from tapin.store import iso, utcnow

LIMIT_CODES = ("usage_limit_reached", "rate_limit_exceeded", "rate_limit_reached", "quota_exceeded", "usage limit")
TAIL_BYTES = 256_000


def _tail_lines(path: Path) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TAIL_BYTES))
            data = f.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()


def rollout_hit_limit(path: Path) -> bool:
    """Scan the current turn of a Codex rollout for a usage/rate-limit error event."""
    for line in reversed(_tail_lines(path)):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = record.get("payload")
        if record.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        if payload.get("type") == "task_started":
            return False
        if "error" in str(payload.get("type", "")) or "codex_error_info" in payload:
            blob = json.dumps(payload).lower()
            if any(code in blob for code in LIMIT_CODES):
                return True
    return False


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
        if event != "stop" or not path or not rollout_hit_limit(Path(path)):
            return None
        return StopEvent(
            agent=self.name,
            cwd=Path(payload.get("cwd") or os.getcwd()),
            session_id=payload.get("session_id"),
            transcript_path=path,
            reason="usage_limit",
            last_assistant_message=payload.get("last_assistant_message"),
            model=payload.get("model"),
            stopped_at=iso(utcnow()),
        )
