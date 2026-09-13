from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

TAIL_BYTES = 256_000
T = TypeVar("T")


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


def tail_lines(path: Path) -> list[str]:
    """The lines in the last TAIL_BYTES of a file, the first possibly cut off; none if it can't be read."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TAIL_BYTES))
            data = f.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()


def last_record(path: Path, marker: str, pick: Callable[[dict[str, Any]], T | None]) -> T | None:
    """What `pick` returns for the last record of a JSONL log that it accepts, parsing only lines that contain `marker`.

    The end of the file is tried first. A long turn can write more than TAIL_BYTES after the record (a real 4.4 MB Codex
    rollout's last `turn_context` was 728 KB from the end), so the whole file is read when the end has none. None if
    there is no such record or the file can't be read."""

    def parse(line: str) -> T | None:
        if marker not in line:
            return None
        try:
            record = json.loads(line)
        except ValueError:
            return None
        return pick(record) if isinstance(record, dict) else None

    for line in reversed(tail_lines(path)):
        if (value := parse(line)) is not None:
            return value
    found = None
    try:
        if path.stat().st_size <= TAIL_BYTES:
            return None
        with path.open("rb") as f:
            for raw in f:
                if (value := parse(raw.decode("utf-8", "replace"))) is not None:
                    found = value
    except OSError:
        return None
    return found
