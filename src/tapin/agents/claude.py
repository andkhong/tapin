from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from tapin.agents.base import Agent, StopEvent
from tapin.store import iso, utcnow

_SLUG = re.compile(rb'"slug":"([^"]+)"')


def plans_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "plans"


def last_slug(transcript: Path) -> str | None:
    try:
        matches = _SLUG.findall(transcript.read_bytes())
    except OSError:
        return None
    return matches[-1].decode() if matches else None


class Claude(Agent):
    name = "claude"
    display = "Claude Code"

    def limit_stop(self, event: str, payload: dict[str, Any], cfg: dict[str, Any]) -> StopEvent | None:
        error = payload.get("error")
        if event != "stop-failure" or error not in cfg["limit_errors"]:
            return None
        return StopEvent(
            agent=self.name,
            cwd=Path(payload.get("cwd") or os.getcwd()),
            session_id=payload.get("session_id"),
            transcript_path=payload.get("transcript_path"),
            reason=error,
            details=payload.get("error_details"),
            last_assistant_message=payload.get("last_assistant_message"),
            stopped_at=iso(utcnow()),
        )

    def plan_text(self, stop: StopEvent) -> str | None:
        """Plan-mode plans live in ~/.claude/plans/<slug>.md, keyed by the session's slug."""
        if not stop.transcript_path:
            return None
        slug = last_slug(Path(stop.transcript_path))
        if not slug:
            return None
        try:
            return (plans_dir() / f"{slug}.md").read_text()
        except OSError:
            return None
