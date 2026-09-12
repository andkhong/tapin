"""Per-workspace handoff state under <workspace>/.tapin/."""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DIR_NAME = ".tapin"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def safe_name(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value or "unknown")


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


@dataclass
class Pending:
    id: str
    from_agent: str
    from_session: str | None
    created_at: str
    expires_at: str
    claimed_by: dict[str, Any] | None = None

    def expired(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= parse_iso(self.expires_at)


class Store:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.root = workspace / DIR_NAME

    @property
    def handoffs_dir(self) -> Path:
        return self.root / "handoffs"

    @property
    def journal_dir(self) -> Path:
        return self.root / "journal"

    @property
    def pending_path(self) -> Path:
        return self.root / "pending.json"

    @property
    def notes_path(self) -> Path:
        return self.root / "notes.md"

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def create_handoff(self, from_agent: str, markdown: str, meta: dict[str, Any], ttl_hours: float) -> str:
        now = utcnow()
        base = f"{now:%Y%m%dT%H%M%SZ}-{from_agent}"
        with self.lock():
            handoff_id, n = base, 1
            while (self.handoffs_dir / handoff_id).exists():
                n += 1
                handoff_id = f"{base}-{n}"
            directory = self.handoffs_dir / handoff_id
            directory.mkdir(parents=True)
            (directory / "handoff.md").write_text(markdown)
            (directory / "meta.json").write_text(
                json.dumps({**meta, "id": handoff_id, "from_agent": from_agent, "created_at": iso(now)}, indent=2)
            )
            pending = Pending(
                id=handoff_id,
                from_agent=from_agent,
                from_session=meta.get("session_id"),
                created_at=iso(now),
                expires_at=iso(now + timedelta(hours=ttl_hours)),
            )
            _write_atomic(self.pending_path, json.dumps(asdict(pending), indent=2))
        return handoff_id

    def pending(self) -> Pending | None:
        if not self.pending_path.exists():
            return None
        return Pending(**json.loads(self.pending_path.read_text()))

    def claimable(self) -> Pending | None:
        pending = self.pending()
        if pending is None or pending.claimed_by or pending.expired():
            return None
        return pending

    def claim(self, agent: str, session_id: str | None) -> Pending | None:
        """Claim the pending handoff for a starting session; at most one session wins."""
        if not self.pending_path.exists():
            return None
        with self.lock():
            pending = self.claimable()
            if pending is None or (session_id and session_id == pending.from_session):
                return None
            pending.claimed_by = {"agent": agent, "session_id": session_id, "at": iso(utcnow())}
            _write_atomic(self.pending_path, json.dumps(asdict(pending), indent=2))
            return pending

    def begin_capture(self, agent: str, session_id: str | None, window: timedelta) -> bool:
        """Return False if the same session was already captured within `window` (hooks can fire twice)."""
        marker = self.root / f".capture-{safe_name(agent)}-{safe_name(session_id)}"
        with self.lock():
            if marker.exists():
                age = utcnow() - datetime.fromtimestamp(marker.stat().st_mtime, timezone.utc)
                if age < window:
                    return False
            marker.write_text(iso(utcnow()))
            return True

    def handoff_file(self, handoff_id: str) -> Path:
        return self.handoffs_dir / handoff_id / "handoff.md"

    def read_handoff(self, handoff_id: str) -> str:
        return self.handoff_file(handoff_id).read_text()

    def read_meta(self, handoff_id: str) -> dict[str, Any]:
        return json.loads((self.handoffs_dir / handoff_id / "meta.json").read_text())

    def list_handoffs(self) -> list[dict[str, Any]]:
        if not self.handoffs_dir.exists():
            return []
        metas = [json.loads(p.read_text()) for p in self.handoffs_dir.glob("*/meta.json")]
        return sorted(metas, key=lambda m: m["created_at"], reverse=True)

    def journal_file(self, agent: str, session_id: str | None) -> Path:
        return self.journal_dir / f"{safe_name(agent)}-{safe_name(session_id)}.jsonl"

    def append_journal(self, agent: str, session_id: str | None, entry: dict[str, Any]) -> None:
        with self.lock():
            self.journal_dir.mkdir(exist_ok=True)
            with self.journal_file(agent, session_id).open("a") as f:
                f.write(json.dumps(entry) + "\n")

    def append_note(self, agent: str, text: str) -> None:
        with self.lock():
            with self.notes_path.open("a") as f:
                f.write(f"\n## {iso(utcnow())} — {agent}\n\n{text.strip()}\n")

    def latest_note(self) -> str | None:
        if not self.notes_path.exists():
            return None
        entries = self.notes_path.read_text().split("\n## ")
        return ("## " + entries[-1]).strip() if len(entries) > 1 else None
