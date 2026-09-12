from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ReaderError(Exception):
    """A session log could not be located or summarized."""


@dataclass
class SessionRef:
    agent: str
    session_id: str
    path: str | None = None
    cwd: str | None = None
    updated_at: str | None = None


class Reader(Protocol):
    def find_session(self, agent: str, workspace: Path, session_id: str | None = None) -> SessionRef | None: ...

    def digest(self, ref: SessionRef) -> str: ...


def within(path: str | None, workspace: Path) -> bool:
    if not path:
        return False
    try:
        return Path(path).resolve().is_relative_to(workspace.resolve())
    except OSError:
        return False
