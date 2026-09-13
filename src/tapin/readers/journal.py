"""Digests built from activity Tap In's own hooks journaled, for agents whose logs no reader can parse."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tapin.md import clip, fence
from tapin.readers.base import ReaderError, SessionRef
from tapin.store import Store, iso, safe_name

TEXT_MAX = 4_000
OUTPUT_MAX = 1_500
DIGEST_MAX = 50_000


def _render(entry: dict[str, Any]) -> str:
    kind, ts = entry.get("kind"), entry.get("ts", "")
    if kind == "prompt":
        return f"### User ({ts})\n\n{clip(entry.get('text', ''), TEXT_MAX)}"
    if kind == "response":
        return f"### Assistant ({ts})\n\n{clip(entry.get('text', ''), TEXT_MAX)}"
    if kind == "edit":
        return f"- Edited `{entry.get('file_path')}` ({entry.get('edits', 0)} change(s))"
    if kind == "shell":
        output = clip(entry.get("output") or "", OUTPUT_MAX)
        return f"- `$ {entry.get('command', '')}`" + (f"\n\n{fence(output)}" if output.strip() else "")
    return f"- {json.dumps(entry)}"


class JournalReader:
    def find_session(self, agent: str, workspace: Path, session_id: str | None = None) -> SessionRef | None:
        directory = Store(workspace).journal_dir
        if not directory.exists():
            return None
        pattern = f"{safe_name(agent)}-{safe_name(session_id)}.jsonl" if session_id else f"{safe_name(agent)}-*.jsonl"
        files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return None
        newest = files[0]
        return SessionRef(
            agent=agent,
            session_id=session_id or newest.stem.removeprefix(f"{safe_name(agent)}-"),
            path=str(newest),
            cwd=str(workspace),
            updated_at=iso(datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc)),
        )

    def digest(self, ref: SessionRef) -> str:
        if not ref.path:
            raise ReaderError(f"no journal recorded for {ref.agent} session {ref.session_id}")
        try:
            lines = Path(ref.path).read_text().splitlines()
        except OSError as exc:
            raise ReaderError(f"journal unreadable: {exc}") from exc
        entries, skipped = [], 0
        for line in lines:
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                skipped += 1

        blocks: list[str] = []
        used = 0
        for entry in reversed(entries):
            block = _render(entry)
            if used + len(block) > DIGEST_MAX:
                blocks.append("_[Earlier activity omitted.]_")
                break
            blocks.append(block)
            used += len(block)
        blocks.reverse()

        counts = f"{len(entries)} events" + (f", {skipped} unreadable line(s) skipped" if skipped else "")
        header = [
            "# Session Handoff Context",
            "",
            f"Recorded by Tap In hooks for {ref.agent} session `{ref.session_id}` ({counts}).",
            "",
            "## Recent Activity",
            "",
        ]
        return "\n".join(header) + "\n\n".join(blocks) + "\n"
