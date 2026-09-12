"""Turn a stopped agent session into a stored handoff."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tapin import agents, packet, readers, workspace
from tapin.agents.base import StopEvent
from tapin.readers.base import Reader, ReaderError, SessionRef
from tapin.store import Store, parse_iso, safe_name

SELF_WRITTEN_DIGEST = "_This handoff was written by the agent itself through Tap In's MCP server; no session log was read._"


def _reader(agent: str, cfg: dict[str, Any]) -> Reader:
    return readers.get(cfg["readers"][agent], cfg)


def capture(stop: StopEvent, cfg: dict[str, Any], reader: Reader | None = None) -> tuple[Store, str]:
    root = workspace.find_root(stop.cwd)
    store = Store(root)
    agent = agents.get(stop.agent)
    reader = reader or _reader(stop.agent, cfg)

    digest = digest_error = None
    try:
        ref = reader.find_session(stop.agent, root, stop.session_id)
        if ref is None and stop.session_id:
            ref = SessionRef(stop.agent, stop.session_id, path=stop.transcript_path)
        if ref is not None:
            stop.session_id = stop.session_id or ref.session_id
            stop.transcript_path = stop.transcript_path or ref.path
            stop.stopped_at = stop.stopped_at or ref.updated_at
            digest = reader.digest(ref)
    except ReaderError as exc:
        digest_error = str(exc)

    built = packet.build(
        stop,
        agent.display,
        workspace.snapshot(root, cfg["diff_max_chars"]),
        digest,
        digest_error,
        agent.plan_text(stop),
        store.latest_note(),
        cfg,
    )
    workspace.ensure_excluded(root)
    handoff_id = store.create_handoff(stop.agent, built.markdown, built.meta, cfg["handoff_ttl_hours"])
    return store, handoff_id


def capture_summary(cwd: Path, agent_label: str, summary: str, next_steps: str, cfg: dict[str, Any]) -> tuple[Store, str]:
    """A handoff the agent writes itself (via MCP); works for any agent, with or without an adapter."""
    root = workspace.find_root(cwd)
    store = Store(root)
    label = safe_name(agent_label)
    display = agents.REGISTRY[label].display if label in agents.REGISTRY else agent_label
    stop = StopEvent(
        agent=label,
        cwd=root,
        reason="agent_handoff",
        last_assistant_message=f"{summary.strip()}\n\nNext steps: {next_steps.strip()}",
    )
    built = packet.build(
        stop,
        display,
        workspace.snapshot(root, cfg["diff_max_chars"]),
        SELF_WRITTEN_DIGEST,
        None,
        None,
        store.latest_note(),
        cfg,
    )
    workspace.ensure_excluded(root)
    return store, store.create_handoff(label, built.markdown, built.meta, cfg["handoff_ttl_hours"])


def resolve_stop(
    root: Path,
    cfg: dict[str, Any],
    source: str | None = None,
    session_id: str | None = None,
    exclude: str | None = None,
    reason: str = "manual",
) -> StopEvent | None:
    """Work out which session to hand off: an unclaimed hook capture first, else the newest session here."""
    pending = Store(root).claimable()
    if (
        pending
        and source in (None, pending.from_agent)
        and session_id in (None, pending.from_session)
        and pending.from_agent != exclude
    ):
        meta = Store(root).read_meta(pending.id)
        return StopEvent(
            agent=pending.from_agent,
            cwd=root,
            session_id=meta.get("session_id"),
            transcript_path=meta.get("transcript_path"),
            reason=meta.get("reason") or reason,
            details=meta.get("details"),
            last_assistant_message=meta.get("last_assistant_message"),
            model=meta.get("model"),
            stopped_at=meta.get("stopped_at"),
        )

    names = [source] if source else [name for name in agents.NAMES if name != exclude]
    refs: list[SessionRef] = []
    for name in names:
        try:
            ref = _reader(name, cfg).find_session(name, root, session_id)
        except ReaderError:
            continue
        if ref:
            refs.append(ref)

    if not refs:
        if source and session_id:
            return StopEvent(agent=source, cwd=root, session_id=session_id, reason=reason)
        return None
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    ref = max(refs, key=lambda r: parse_iso(r.updated_at) if r.updated_at else oldest)
    return StopEvent(
        agent=ref.agent,
        cwd=root,
        session_id=ref.session_id,
        transcript_path=ref.path,
        reason=reason,
        stopped_at=ref.updated_at,
    )
