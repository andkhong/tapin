"""Turn a stopped agent session into a stored handoff."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

from tapin import agents, packet, readers, workspace
from tapin.agents.base import StopEvent
from tapin.readers import claude_log, codex_log
from tapin.readers.base import Reader, ReaderError, SessionRef
from tapin.store import CheckpointPick, Store, parse_iso, parse_utc, safe_name, select_checkpoint, utcnow

SELF_WRITTEN_DIGEST = "_This handoff was written by the agent itself through Tap In's MCP server; no session log was read._"
# An agent calling `checkpoint` or `create_handoff` is mid-session, so its own log was written this recently.
ACTIVE_WINDOW = timedelta(minutes=2)


class NativeLog(NamedTuple):
    """How to read an agent's own session logs, which record the model and reasoning effort."""

    reader_name: str
    reader: Callable[[], Reader]
    session_identity: Callable[[Path], tuple[str | None, str | None]]
    recent_sessions: Callable[[str, Path, timedelta], list[SessionRef]]


NATIVE_LOGS = {
    "claude": NativeLog("claude-log", claude_log.ClaudeLogReader, claude_log.session_identity, claude_log.recent_sessions),
    "codex": NativeLog("codex-log", codex_log.CodexLogReader, codex_log.session_identity, codex_log.recent_sessions),
}


def _reader(agent: str, cfg: dict[str, Any]) -> Reader:
    return readers.get(cfg["readers"][agent], cfg)


def identify(
    agent: str, root: Path, session_id: str | None = None, model: str | None = None, effort: str | None = None
) -> tuple[str | None, str | None, str | None]:
    """Fill in a missing session id, model and reasoning effort for Claude Code or Codex from the agent's own session
    logs. Values passed in always win, and other agents get back what they passed. Never raises, since recording the
    work matters more than knowing who did it.

    With a session id, the model and effort come from that session's log. Without one, only the agent's sessions in the
    workspace written within ACTIVE_WINDOW count: the session id is filled in only if there is exactly one, and the model
    and effort only if they all agree on them. A wrong guess would tie a checkpoint to another session, or record a
    handoff as coming from a session that then can't claim it."""
    native = NATIVE_LOGS.get(agent)
    if native is None or (session_id and model and effort):
        return session_id, model, effort
    try:
        if session_id:
            refs = [ref] if (ref := native.reader().find_session(agent, root, session_id)) else []
        else:
            refs = native.recent_sessions(agent, root, ACTIVE_WINDOW)
        found = [native.session_identity(Path(ref.path)) if ref.path else (None, None) for ref in refs]
    except Exception:
        return session_id, model, effort
    if not session_id and len(refs) == 1:
        session_id = refs[0].session_id
    models, efforts = {found_model for found_model, _ in found}, {found_effort for _, found_effort in found}
    if len(models) == 1:
        model = model or models.pop()
        effort = effort or (efforts.pop() if len(efforts) == 1 else None)
    return session_id, model, effort


def latest_checkpoint(store: Store, stop: StopEvent, cfg: dict[str, Any]) -> CheckpointPick:
    """The checkpoint that goes into this stop's handoff; see `select_checkpoint`."""
    try:
        stopped_at = parse_utc(stop.stopped_at) if stop.stopped_at else utcnow()
    except (TypeError, ValueError):
        stopped_at = utcnow()
    max_age = timedelta(hours=cfg["checkpoint_max_age_hours"])
    return select_checkpoint(store.checkpoints(), stop.agent, stop.session_id, stopped_at, max_age)


def _fill_identity(stop: StopEvent, ref: SessionRef | None, cfg: dict[str, Any]) -> None:
    """Claude Code's hooks never carry the model, so take a missing model or effort from the session log, when the
    agent's own log reader is the one that found it."""
    native = NATIVE_LOGS.get(stop.agent)
    if (stop.model and stop.effort) or native is None or cfg["readers"].get(stop.agent) != native.reader_name or not (ref and ref.path):
        return
    model, effort = native.session_identity(Path(ref.path))
    stop.model, stop.effort = stop.model or model, stop.effort or effort


def capture(
    stop: StopEvent, cfg: dict[str, Any], reader: Reader | None = None, ref: SessionRef | None = None
) -> tuple[Store, str]:
    root = workspace.find_root(stop.cwd)
    store = Store(root)
    agent = agents.get(stop.agent)
    reader = reader or _reader(stop.agent, cfg)

    digest = digest_error = None
    try:
        ref = ref or reader.find_session(stop.agent, root, stop.session_id)
        if ref is None and stop.session_id:
            ref = SessionRef(stop.agent, stop.session_id, path=stop.transcript_path)
        if ref is not None:
            stop.session_id = stop.session_id or ref.session_id
            stop.transcript_path = stop.transcript_path or ref.path
            stop.stopped_at = stop.stopped_at or ref.updated_at
            digest = reader.digest(ref)
    except ReaderError as exc:
        digest_error = str(exc)
    _fill_identity(stop, ref, cfg)

    built = packet.build(
        stop,
        agent.display,
        workspace.snapshot(root, cfg["diff_max_chars"]),
        digest,
        digest_error,
        agent.plan_text(stop),
        latest_checkpoint(store, stop, cfg),
        cfg,
    )
    workspace.ensure_excluded(root)
    handoff_id = store.create_handoff(stop.agent, built.markdown, built.meta, cfg["handoff_ttl_hours"])
    return store, handoff_id


def refine_reason(store: Store, stop: StopEvent, wait: timedelta = timedelta(0)) -> bool:
    """Fill in what a duplicate stop event knows and the handoff already written doesn't: Cursor's `stop`
    says only that the turn failed, and the `session-end` that follows says why. `wait` gives a capture
    still running in the background time to write that handoff first."""
    if not stop.details:
        return False
    deadline = time.monotonic() + wait.total_seconds()
    while True:
        with store.lock():
            pending = store.pending()
            if pending and pending.from_agent == stop.agent and pending.from_session == stop.session_id:
                meta = store.read_meta(pending.id)
                if meta.get("details"):
                    return False
                store.write_meta(pending.id, {**meta, "reason": stop.reason, "details": stop.details})
                path = store.handoff_file(pending.id)
                path.write_text(packet.replace_reason_row(path.read_text(), stop.reason, stop.details))
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def capture_summary(
    cwd: Path,
    agent_label: str,
    summary: str,
    next_steps: str,
    cfg: dict[str, Any],
    session_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> tuple[Store, str]:
    """A handoff the agent writes itself (via MCP); works for any agent, with or without an adapter."""
    root = workspace.find_root(cwd)
    store = Store(root)
    label = safe_name(agent_label)
    display = agents.REGISTRY[label].display if label in agents.REGISTRY else agent_label
    session_id, model, effort = identify(label, root, session_id, model, effort)
    stop = StopEvent(
        agent=label,
        cwd=root,
        session_id=session_id,
        reason="agent_handoff",
        last_assistant_message=f"{summary.strip()}\n\nNext steps: {next_steps.strip()}",
        model=model,
        effort=effort,
    )
    built = packet.build(
        stop,
        display,
        workspace.snapshot(root, cfg["diff_max_chars"]),
        SELF_WRITTEN_DIGEST,
        None,
        None,
        latest_checkpoint(store, stop, cfg),
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
) -> tuple[StopEvent, SessionRef | None] | None:
    """Work out which session to hand off: an unclaimed hook capture first, else the newest session here.

    Returns the session it found with it, so the caller's capture doesn't look the same session up again."""
    pending = Store(root).claimable()
    if (
        pending
        and source in (None, pending.from_agent)
        and session_id in (None, pending.from_session)
        and pending.from_agent != exclude
    ):
        meta = Store(root).read_meta(pending.id)
        stop = StopEvent(
            agent=pending.from_agent,
            cwd=root,
            session_id=meta.get("session_id"),
            transcript_path=meta.get("transcript_path"),
            reason=meta.get("reason") or reason,
            details=meta.get("details"),
            last_assistant_message=meta.get("last_assistant_message"),
            model=meta.get("model"),
            stopped_at=meta.get("stopped_at"),
            effort=meta.get("effort"),
        )
        return stop, None

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
            return StopEvent(agent=source, cwd=root, session_id=session_id, reason=reason), None
        return None
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    ref = max(refs, key=lambda r: parse_iso(r.updated_at) if r.updated_at else oldest)
    stop = StopEvent(
        agent=ref.agent,
        cwd=root,
        session_id=ref.session_id,
        transcript_path=ref.path,
        reason=reason,
        stopped_at=ref.updated_at,
    )
    return stop, ref
