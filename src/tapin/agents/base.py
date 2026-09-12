"""What Tap In needs to know about each agent: hook payloads, limit detection, and how to launch it."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class StopEvent:
    agent: str
    cwd: Path
    session_id: str | None = None
    transcript_path: str | None = None
    reason: str = "manual"
    details: str | None = None
    last_assistant_message: str | None = None
    model: str | None = None
    stopped_at: str | None = None

    def to_json(self) -> str:
        return json.dumps({**asdict(self), "cwd": str(self.cwd)})

    @classmethod
    def from_json(cls, raw: str) -> StopEvent:
        data = json.loads(raw)
        return cls(**{**data, "cwd": Path(data["cwd"])})


@dataclass
class StartEvent:
    agent: str
    cwd: Path
    session_id: str | None = None
    source: str | None = None


class Agent:
    name = ""
    display = ""

    def command(self, cfg: dict[str, Any]) -> list[str]:
        return list(cfg["agents"][self.name]["command"])

    def launch_argv(self, prompt: str, cfg: dict[str, Any]) -> list[str]:
        return [*self.command(cfg), prompt]

    def parse_start(self, payload: dict[str, Any]) -> StartEvent:
        return StartEvent(
            agent=self.name,
            cwd=Path(payload.get("cwd") or os.getcwd()),
            session_id=payload.get("session_id"),
            source=payload.get("source"),
        )

    def start_output(self, brief: str) -> dict[str, Any]:
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": brief}}

    def limit_stop(self, event: str, payload: dict[str, Any], cfg: dict[str, Any]) -> StopEvent | None:
        """Return a StopEvent if this hook event means the agent stopped on a limit or error."""
        return None

    def plan_text(self, stop: StopEvent) -> str | None:
        return None
