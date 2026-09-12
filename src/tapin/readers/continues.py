"""Session digests from `continues` (npm), which parses Claude Code, Codex and other native logs."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from tapin.readers.base import ReaderError, SessionRef, within
from tapin.store import iso, parse_iso

LIST_LIMIT = 500


class ContinuesReader:
    def __init__(self, command: list[str], timeout: float):
        self.command = command
        self.timeout = timeout

    def _run(self, *args: str) -> str:
        try:
            result = subprocess.run(
                [*self.command, *args],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReaderError(f"continues {args[0]} failed: {exc}") from exc
        if result.returncode != 0:
            raise ReaderError(f"continues {args[0]} exited {result.returncode}: {result.stderr.strip()[-500:]}")
        return result.stdout

    def find_session(self, agent: str, workspace: Path, session_id: str | None = None) -> SessionRef | None:
        # continues caches its session index for 5 minutes and `inspect` looks sessions up in that index,
        # so rebuild it here or a session started in the last few minutes can't be found.
        try:
            items = json.loads(self._run("list", "--source", agent, "--json", "--limit", str(LIST_LIMIT), "--rebuild"))
        except json.JSONDecodeError as exc:
            raise ReaderError(f"continues list returned invalid JSON: {exc}") from exc
        if session_id:
            matches = [item for item in items if item.get("id") == session_id]
        else:
            matches = [item for item in items if within(item.get("cwd"), workspace)]
        if not matches:
            return None
        item = max(matches, key=lambda i: i.get("updatedAt") or "")
        updated = item.get("updatedAt")
        return SessionRef(
            agent=agent,
            session_id=item["id"],
            path=item.get("originalPath"),
            cwd=item.get("cwd"),
            updated_at=iso(parse_iso(updated)) if updated else None,
        )

    def digest(self, ref: SessionRef) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "handoff.md"
            self._run("inspect", ref.session_id, "--write-md", str(out))
            if not out.exists():
                raise ReaderError(f"continues wrote no digest for session {ref.session_id}")
            return out.read_text()
