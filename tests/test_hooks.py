import json

import pytest

from tapin import hooks
from tapin.store import Store


def _claude_failure(repo, error="rate_limit", **extra):
    return {
        "hook_event_name": "StopFailure",
        "session_id": "s1",
        "transcript_path": str(repo / "transcript.jsonl"),
        "cwd": str(repo),
        "error": error,
        "last_assistant_message": "Halfway through total(); next is the tax rule.",
        **extra,
    }


def test_claude_rate_limit_captures_handoff(repo, cfg, fake_reader):
    assert hooks.handle("claude", "stop-failure", _claude_failure(repo), cfg, background=False) == {}
    store = Store(repo)
    pending = store.pending()
    assert pending.from_agent == "claude" and pending.from_session == "s1"
    handoff = store.read_handoff(pending.id)
    assert "Halfway through total()" in handoff
    assert "Working on total()" in handoff


def test_claude_other_errors_are_ignored(repo, cfg, fake_reader):
    hooks.handle("claude", "stop-failure", _claude_failure(repo, error="authentication_failed"), cfg, background=False)
    assert Store(repo).pending() is None


def test_claude_plan_file_is_included(repo, cfg, fake_reader, tmp_path):
    (repo / "transcript.jsonl").write_text('{"type":"user","slug":"brave-plan"}\n')
    plans = tmp_path / "claude-home" / "plans"
    plans.mkdir(parents=True)
    (plans / "brave-plan.md").write_text("# Ingestion plan\n\n## Step 2: streaming parser\n")
    hooks.handle("claude", "stop-failure", _claude_failure(repo), cfg, background=False)
    store = Store(repo)
    assert "Step 2: streaming parser" in store.read_handoff(store.pending().id)


@pytest.mark.parametrize(
    "agent,payload_key,output_key",
    [("claude", "cwd", "hookSpecificOutput"), ("codex", "cwd", "hookSpecificOutput"), ("cursor", "workspace_roots", "additional_context")],
)
def test_session_start_injects_once(repo, cfg, agent, payload_key, output_key):
    store = Store(repo)
    meta = {"session_id": "s1", "from_display": "Claude Code", "stopped_at": "2026-09-12T20:00:00Z", "reason": "rate_limit"}
    handoff_id = store.create_handoff("claude", "# handoff", meta, ttl_hours=1)
    location = [str(repo)] if payload_key == "workspace_roots" else str(repo)
    payload = {"session_id": "new", payload_key: location, "source": "startup"}

    output = hooks.handle(agent, "session-start", payload, cfg)
    assert output_key in output
    assert handoff_id in json.dumps(output)
    assert hooks.handle(agent, "session-start", {**payload, "session_id": "another"}, cfg) == {}


def test_cursor_journal_becomes_digest(repo, cfg):
    base = {"conversation_id": "c1", "workspace_roots": [str(repo)]}
    assert hooks.handle("cursor", "before-submit-prompt", {**base, "prompt": "build the CSV parser"}, cfg) == {"continue": True}
    hooks.handle("cursor", "after-agent-response", {**base, "text": "Writing parse_row now"}, cfg)
    hooks.handle("cursor", "after-file-edit", {**base, "file_path": "ingest.py", "edits": [{}]}, cfg)
    hooks.handle("cursor", "after-shell-execution", {**base, "command": "pytest -q", "output": "1 failed"}, cfg)
    hooks.handle("cursor", "stop", {**base, "status": "error"}, cfg, background=False)
    hooks.handle("cursor", "session-end", {**base, "error_message": "You've hit your usage limit"}, cfg, background=False)

    store = Store(repo)
    assert len(store.list_handoffs()) == 1
    handoff = store.read_handoff(store.pending().id)
    for expected in ("build the CSV parser", "Writing parse_row now", "ingest.py", "$ pytest -q", "1 failed"):
        assert expected in handoff


def _rollout(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def test_codex_stop_after_usage_limit_captures(repo, cfg, fake_reader):
    rollout = _rollout(
        repo / "rollout.jsonl",
        [
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "error", "message": "You've hit your usage limit.", "codex_error_info": "usage_limit_reached"}},
        ],
    )
    payload = {"session_id": "x1", "cwd": str(repo), "transcript_path": str(rollout), "last_assistant_message": None}
    hooks.handle("codex", "stop", payload, cfg, background=False)
    assert Store(repo).pending().from_agent == "codex"


def test_codex_error_from_earlier_turn_is_ignored(repo, cfg, fake_reader):
    rollout = _rollout(
        repo / "rollout.jsonl",
        [
            {"type": "event_msg", "payload": {"type": "error", "codex_error_info": "usage_limit_reached"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
        ],
    )
    payload = {"session_id": "x1", "cwd": str(repo), "transcript_path": str(rollout), "last_assistant_message": "done"}
    hooks.handle("codex", "stop", payload, cfg, background=False)
    assert Store(repo).pending() is None
