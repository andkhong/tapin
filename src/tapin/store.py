"""Per-workspace handoff state under <workspace>/.tapin/."""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tapin.workspace import DIR_NAME, tapin_tracked

MAX_TTL_HOURS = 24 * 7


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_utc(value: str) -> datetime:
    """Like parse_iso, but a time without an offset is taken as UTC, so it can be compared with ours."""
    dt = parse_iso(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def safe_name(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value or "unknown")


# A checkpoint record, one JSON object per line of checkpoints.jsonl: `at` plus these fields.
CHECKPOINT_IDENTITY = ("agent", "model", "effort", "session_id")
CHECKPOINT_TEXT = {"done": "Done so far", "in_progress": "In progress", "decisions": "Decisions", "next_steps": "Next steps"}


def checkpoint_fields(record: dict[str, Any]) -> list[str]:
    """`**Done so far:** …` and the rest, leaving out empty ones."""
    return [f"**{label}:** {record[key].strip()}" for key, label in CHECKPOINT_TEXT.items() if (record.get(key) or "").strip()]


def checkpoint_note(record: dict[str, Any]) -> str:
    """A checkpoint as a notes.md section: `## <at> — <agent> · <model> (effort <effort>) · session `<id>``."""
    from tapin import agents  # tapin.agents imports this module

    heading = f"## {record['at']} — {agents.display(record.get('agent') or 'unknown')}"
    heading += f" · {record['model']}" if record.get("model") else ""
    heading += f" (effort {record['effort']})" if record.get("effort") else ""
    heading += f" · session `{record['session_id']}`" if record.get("session_id") else ""
    return "\n\n".join([heading, *checkpoint_fields(record)])


def checkpoint_time(record: dict[str, Any]) -> datetime:
    return parse_utc(record["at"])


def _checkpoint(line: str) -> dict[str, Any] | None:
    """A record with unknown identity values as None and missing text as "", or None for a line that isn't one."""
    try:
        record = json.loads(line)
        checkpoint_time(record)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    identity = {key: value if isinstance(value := record.get(key), str) and value else None for key in CHECKPOINT_IDENTITY}
    text = {key: value if isinstance(value := record.get(key), str) else "" for key in CHECKPOINT_TEXT}
    return {"at": record["at"], **identity, **text}


@dataclass
class CheckpointPick:
    """The checkpoint chosen for a handoff, and how many recent ones from other sessions or agents were left out."""

    record: dict[str, Any] | None
    by_session: bool = False
    others: int = 0


def _newest(matches: list[tuple[int, dict[str, Any]]]) -> dict[str, Any] | None:
    """Latest `at` first, then latest in the file."""
    return max(matches, key=lambda item: (checkpoint_time(item[1]), item[0]))[1] if matches else None


def select_checkpoint(
    records: list[dict[str, Any]], agent: str, session_id: str | None, stopped_at: datetime, max_age: timedelta
) -> CheckpointPick:
    """The checkpoint for a handoff from `agent`'s session `session_id`, stopped at `stopped_at`.

    The newest from the same session, whatever its age. Otherwise (no session id, or none from it) the newest by the same
    agent with no session id, recorded no more than `max_age` before the stop. A checkpoint from another session is never
    chosen. `others` counts the checkpoints since `stopped_at - max_age` from other sessions or other agents."""
    cutoff = stopped_at - max_age
    same = [(n, r) for n, r in enumerate(records) if session_id and r["session_id"] == session_id]
    loose = [(n, r) for n, r in enumerate(records) if r["session_id"] is None and safe_name(r["agent"]) == safe_name(agent)]
    record = _newest(same)
    by_session = record is not None
    if record is None:
        record = _newest([(n, r) for n, r in loose if checkpoint_time(r) >= cutoff])
    ours = {n for n, _ in same + loose}
    others = sum(1 for n, r in enumerate(records) if n not in ours and checkpoint_time(r) >= cutoff)
    return CheckpointPick(record, by_session, others)


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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Pending:
        """Other agents write this file too (see docs/PROTOCOL.md), so ignore fields we don't know."""
        known = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    def expires(self) -> datetime:
        """`expires_at` is only as trustworthy as whoever wrote the file, so cap it at MAX_TTL_HOURS."""
        return min(parse_iso(self.expires_at), parse_iso(self.created_at) + timedelta(hours=MAX_TTL_HOURS))

    def expired(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= self.expires()

    def in_window(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return parse_iso(self.created_at) <= now < self.expires()


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

    @property
    def checkpoints_path(self) -> Path:
        return self.root / "checkpoints.jsonl"

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
        return Pending.from_dict(json.loads(self.pending_path.read_text()))

    def claimable(self) -> Pending | None:
        pending = self.pending()
        if pending is None or pending.claimed_by or not pending.in_window():
            return None
        if tapin_tracked(self.workspace):
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

    def write_meta(self, handoff_id: str, meta: dict[str, Any]) -> None:
        _write_atomic(self.handoffs_dir / handoff_id / "meta.json", json.dumps(meta, indent=2))

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

    def append_checkpoint(self, record: dict[str, Any]) -> None:
        """The record for handoffs to choose from, and the same checkpoint as a notes.md section for people."""
        with self.lock():
            with self.checkpoints_path.open("a") as f:
                f.write(json.dumps(record) + "\n")
            with self.notes_path.open("a") as f:
                f.write(f"\n{checkpoint_note(record)}\n")

    def checkpoints(self) -> list[dict[str, Any]]:
        """Every checkpoint record in file order, skipping lines that aren't a JSON object with a valid `at`."""
        try:
            text = self.checkpoints_path.read_bytes().decode("utf-8", "replace")
        except OSError:
            return []
        return [record for record in map(_checkpoint, text.splitlines()) if record is not None]
