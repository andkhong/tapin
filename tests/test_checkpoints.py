from datetime import timedelta

from tapin import capture, mcp_server
from tapin.agents.base import StopEvent
from tapin.store import Store, iso, parse_iso, select_checkpoint, utcnow

STOP = parse_iso("2026-09-12T20:00:00Z")
CAP = timedelta(hours=12)


def _record(minutes_before, agent="claude", session_id=None, done="work"):
    at = iso(STOP - timedelta(minutes=minutes_before))
    return {"at": at, "agent": agent, "model": None, "effort": None, "session_id": session_id, "done": done, "in_progress": "", "decisions": "", "next_steps": ""}


def test_same_session_checkpoint_wins_over_newer_ones_whatever_its_age():
    ours = _record(3 * 24 * 60, session_id="s1", done="ours")
    newer = _record(5, session_id="s2", done="another session")
    loose = _record(1, done="no session")
    pick = select_checkpoint([ours, newer, loose], "claude", "s1", STOP, CAP)
    assert (pick.record, pick.by_session, pick.others) == (ours, True, 1)


def test_another_sessions_checkpoint_is_never_picked():
    records = [_record(5, session_id="s2"), _record(10, agent="codex", session_id="x1")]
    for session_id in ("s1", None):
        pick = select_checkpoint(records, "claude", session_id, STOP, CAP)
        assert (pick.record, pick.by_session, pick.others) == (None, False, 2)


def test_checkpoint_without_a_session_is_matched_by_agent_within_the_age_cap():
    recent, stale, edge = _record(11 * 60, done="recent"), _record(12 * 60 + 1, done="stale"), _record(12 * 60, done="edge")
    pick = select_checkpoint([recent, stale, _record(1, agent="codex")], "claude", "s1", STOP, CAP)
    assert (pick.record, pick.by_session, pick.others) == (recent, False, 1)
    assert select_checkpoint([stale], "claude", None, STOP, CAP).record is None
    assert select_checkpoint([stale], "claude", None, STOP, timedelta(hours=13)).record == stale
    assert select_checkpoint([edge], "claude", None, STOP, CAP).record == edge

    first, second = _record(30, done="first"), _record(30, done="second")
    assert select_checkpoint([_record(10, done="newest"), first, second], "claude", None, STOP, CAP).record["done"] == "newest"
    assert select_checkpoint([first, second, _record(60)], "claude", None, STOP, CAP).record is second


def test_others_counts_recent_checkpoints_from_other_sessions_and_agents():
    records = [
        _record(10, session_id="s1"),
        _record(20, session_id="s1"),
        _record(30, session_id="s2"),
        _record(-30, session_id="s3"),
        _record(40, agent="codex"),
        _record(50),
        _record(13 * 60, session_id="s4"),
    ]
    assert select_checkpoint(records, "claude", "s1", STOP, CAP).others == 3
    assert select_checkpoint(records, "claude", None, STOP, CAP).others == 5


def test_capture_includes_the_stopped_sessions_checkpoint_only(repo, cfg, fake_reader):
    ws = str(repo)
    Store(repo).append_note("claude", "A legacy note with no record")
    mcp_server.checkpoint(ws, "claude", "Parser done", "Row tests", in_progress="csv.py: parse_row()", session_id="s1")
    mcp_server.checkpoint(ws, "claude", "Unrelated work", "Unrelated step", session_id="other-session")

    store, handoff_id = capture.capture(StopEvent(agent="claude", cwd=repo, session_id="s1", stopped_at=iso(utcnow())), cfg)
    handoff = store.read_handoff(handoff_id)
    assert (
        "### Latest checkpoint\n\nRecorded by Claude Code in session `s1`, just before the stop.\n\n"
        "**Done so far:** Parser done\n\n**In progress:** csv.py: parse_row()\n\n**Next steps:** Row tests\n\n"
        "_1 other recent checkpoint in .tapin/notes.md is from another session and isn't included._\n"
    ) in handoff
    assert "Unrelated" not in handoff and "legacy note" not in handoff

    other, other_id = capture.capture(StopEvent(agent="claude", cwd=repo, session_id="s9", stopped_at=iso(utcnow())), cfg)
    assert "### Latest checkpoint" not in other.read_handoff(other_id)
    assert "_2 recent checkpoints in .tapin/notes.md are from other sessions and aren't included._" in other.read_handoff(other_id)


def test_capture_falls_back_to_a_recent_checkpoint_without_a_session(repo, cfg, fake_reader):
    mcp_server.checkpoint(str(repo), "cursor", "Form layout done", "Validation")
    stop = StopEvent(agent="cursor", cwd=repo, session_id="c1", stopped_at=iso(utcnow() + timedelta(hours=2)))
    store, handoff_id = capture.capture(stop, cfg)
    assert "Recorded by Cursor, 2 hours before the stop. It isn't tied to a session; it was matched by agent and time." in store.read_handoff(handoff_id)

    late = StopEvent(agent="cursor", cwd=repo, session_id="c1", stopped_at=iso(utcnow() + timedelta(hours=13)))
    store, handoff_id = capture.capture(late, cfg)
    assert "### Latest checkpoint" not in store.read_handoff(handoff_id)
    store, handoff_id = capture.capture(late, {**cfg, "checkpoint_max_age_hours": 14})
    assert "Form layout done" in store.read_handoff(handoff_id)


def test_create_handoff_follows_the_same_rules(repo):
    ws = str(repo)
    mcp_server.checkpoint(ws, "gemini", "Schema drafted", "Write migrations")
    mcp_server.checkpoint(ws, "gemini", "Other window", "Other step", session_id="g2")
    mcp_server.create_handoff(ws, "gemini", "Halfway through migrations", "Finish them", session_id="g1")
    handoff = mcp_server.get_handoff(ws)
    assert "Recorded by gemini, just before the stop. It isn't tied to a session; it was matched by agent and time." in handoff
    assert "Schema drafted" in handoff and "Other window" not in handoff
    assert "_1 other recent checkpoint in .tapin/notes.md is from another session and isn't included._" in handoff

    mcp_server.checkpoint(ws, "gemini", "Migrations written", "Run them", session_id="g1")
    mcp_server.create_handoff(ws, "gemini", "Running migrations", "Check the output", session_id="g1", model="gemini-4-pro", effort="high")
    handoff = mcp_server.get_handoff(ws)
    assert "| From | gemini (session `g1`) |" in handoff and "| Model | gemini-4-pro (effort high) |" in handoff
    assert "Recorded by gemini in session `g1`, just before the stop.\n\n**Done so far:** Migrations written" in handoff

    old = {"at": iso(utcnow() - timedelta(hours=13)), "agent": "cline", "model": None, "effort": None, "session_id": None}
    Store(repo).append_checkpoint({**old, "done": "Stale work", "in_progress": "", "decisions": "", "next_steps": "n"})
    mcp_server.create_handoff(ws, "cline", "Stopping", "Resume")
    assert "Stale work" not in mcp_server.get_handoff(ws)
