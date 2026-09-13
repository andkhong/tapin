from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


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


def read_jsonl(path: Path, max_lines: int | None = None) -> Iterator[dict[str, Any] | None]:
    """Each line of a JSONL log as a dict, or None for a line that isn't a JSON object. Raises OSError."""
    with path.open("rb") as f:
        for number, line in enumerate(f):
            if max_lines is not None and number >= max_lines:
                return
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            yield record if isinstance(record, dict) else None
