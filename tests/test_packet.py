from pathlib import Path

from tapin.agents.base import StopEvent
from tapin.md import demote_headings, fence
from tapin.packet import brief, build
from tapin.workspace import snapshot


def _stop(**overrides):
    fields = dict(
        agent="claude",
        cwd=Path("/w"),
        session_id="s1",
        transcript_path="/logs/s1.jsonl",
        reason="rate_limit",
        last_assistant_message="Now writing parse_row()",
    )
    return StopEvent(**{**fields, **overrides})


def test_packet_has_all_sections(repo, cfg):
    digest = "# Session Handoff Context\n\n## Recent Conversation\n\n```sh\n# a shell comment\n```\n"
    built = build(_stop(), "Claude Code", snapshot(repo, 1_000), digest, None, "# Plan\n\n## Step 2", "## note", cfg)
    md = built.markdown
    for heading in ("# Handoff from Claude Code", "## How to continue", "## Where it stopped", "## Plan", "## Session digest", "## Workspace"):
        assert f"\n{heading}" in "\n" + md
    assert "#### Recent Conversation" in md
    assert "\n# a shell comment\n" in md
    assert "Now writing parse_row()" in md
    assert built.meta["reason"] == "rate_limit"


def test_secrets_are_redacted(repo, cfg):
    token = "sk-ant-" + "a" * 40
    built = build(
        _stop(last_assistant_message=f"export KEY={token}"),
        "Claude Code",
        snapshot(repo, 1_000),
        "Authorization: Bearer " + "b" * 30,
        None,
        None,
        None,
        cfg,
    )
    assert token not in built.markdown
    assert token not in built.meta["last_assistant_message"]
    assert "Bearer [REDACTED]" in built.markdown


def test_missing_digest_points_to_log(repo, cfg):
    built = build(_stop(), "Claude Code", snapshot(repo, 1_000), None, "continues exited 1", None, None, cfg)
    assert "continues exited 1" in built.markdown
    assert "/logs/s1.jsonl" in built.markdown


def test_fence_outgrows_inner_backticks():
    assert fence("```inner```").startswith("````\n")


def test_demote_skips_code_blocks():
    assert demote_headings("# A\n```\n# not heading\n```\n## B", 1) == "## A\n```\n# not heading\n```\n### B"


def test_brief_is_bounded_and_points_to_file():
    meta = {
        "from_display": "Claude Code",
        "stopped_at": "2026-09-12T20:00:00Z",
        "reason": "rate_limit",
        "last_assistant_message": "x" * 20_000,
    }
    text = brief(meta, Path("/w/.tapin/handoffs/id/handoff.md"), 3_500)
    assert len(text) <= 3_500
    assert "/w/.tapin/handoffs/id/handoff.md" in text


def test_where_it_stopped_points_to_the_digest_when_the_stop_had_no_message(repo, cfg):
    digest = "# Session Handoff Context\n\n## Last message before the stop\n\n> Writing parse_row()\n"
    built = build(_stop(last_assistant_message=None), "Claude Code", snapshot(repo, 1_000), digest, None, None, None, cfg)
    assert "_The stop event carried no final message. See **Last message before the stop** in the session digest below._" in built.markdown

    bare = build(_stop(last_assistant_message=None), "Claude Code", snapshot(repo, 1_000), None, "no session", None, None, cfg)
    assert "_No final message was captured. The most recent conversation is in the session digest below._" in bare.markdown
