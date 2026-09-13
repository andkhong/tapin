from pathlib import Path

from tapin.agents.base import StopEvent
from tapin.md import demote_headings, fence
from tapin.packet import age, brief, build
from tapin.store import CheckpointPick
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
    built = build(_stop(), "Claude Code", snapshot(repo, 1_000), digest, None, "# Plan\n\n## Step 2", None, cfg)
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


def _markdown(repo, cfg, checkpoint=None, **stop):
    return build(_stop(stopped_at="2026-09-12T20:00:00Z", **stop), "Claude Code", snapshot(repo, 1_000), None, "no session", None, checkpoint, cfg)


def test_model_row_shows_the_effort(repo, cfg):
    def row(**stop):
        return next((line for line in _markdown(repo, cfg, **stop).markdown.splitlines() if line.startswith("| Model |")), None)

    assert row(model="claude-opus-5", effort="xhigh") == "| Model | claude-opus-5 (effort xhigh) |"
    assert row(model="claude-opus-5") == "| Model | claude-opus-5 |"
    assert row(effort="xhigh") == "| Model | effort xhigh |"
    assert row() is None
    assert _markdown(repo, cfg, effort="xhigh").meta["effort"] == "xhigh"


def _record(**overrides):
    fields = dict(
        at="2026-09-12T19:46:00Z",
        agent="claude",
        model="claude-opus-5",
        effort="xhigh",
        session_id="s1",
        done="Parser done",
        in_progress="csv.py: parse_row()",
        decisions="",
        next_steps="Row tests\n# Edge cases",
    )
    return {**fields, **overrides}


def test_checkpoint_block_says_who_recorded_it_and_when(repo, cfg):
    md = _markdown(repo, cfg, CheckpointPick(_record(), by_session=True)).markdown
    assert (
        "### Latest checkpoint\n\n"
        "Recorded by Claude Code (claude-opus-5, effort xhigh) in session `s1`, 14 minutes before the stop.\n\n"
        "**Done so far:** Parser done\n\n**In progress:** csv.py: parse_row()\n\n**Next steps:** Row tests\n#### Edge cases\n\n## Session digest"
    ) in md
    assert "Decisions" not in md and "isn't tied to a session" not in md and "other recent" not in md

    def sentence(record, by_session=False, others=0):
        md = _markdown(repo, cfg, CheckpointPick(record, by_session, others)).markdown
        return next(line for line in md.splitlines() if line.startswith("Recorded by"))

    loose = _record(agent="gemini", model=None, effort=None, session_id=None, at="2026-09-12T18:00:00Z")
    assert sentence(loose) == "Recorded by gemini, 2 hours before the stop. It isn't tied to a session; it was matched by agent and time."
    assert sentence(_record(model=None, at="2026-09-12T20:00:30Z"), True) == "Recorded by Claude Code (effort xhigh) in session `s1`, just after the stop."
    assert sentence(_record(effort=None, at="2026-09-15T21:00:00Z"), True) == "Recorded by Claude Code (claude-opus-5) in session `s1`, 3 days after the stop."
    assert [age(s) for s in (0, 59, 60, 14 * 60 + 59, 7_200, 86_399, 3 * 86_400, -120)] == [
        "just now", "just now", "1 minute", "14 minutes", "2 hours", "23 hours", "3 days", "2 minutes"
    ]


def test_checkpoint_block_points_to_recent_checkpoints_it_left_out(repo, cfg):
    md = _markdown(repo, cfg, CheckpointPick(_record(), True, others=2)).markdown
    assert "\n_2 other recent checkpoints in .tapin/notes.md are from other sessions and aren't included._\n" in md
    one = _markdown(repo, cfg, CheckpointPick(_record(), True, others=1)).markdown
    assert "\n_1 other recent checkpoint in .tapin/notes.md is from another session and isn't included._\n" in one

    none_picked = _markdown(repo, cfg, CheckpointPick(None, others=1)).markdown
    assert "### Latest checkpoint" not in none_picked
    assert "\n_1 recent checkpoint in .tapin/notes.md is from another session and isn't included._\n" in none_picked
    nothing = _markdown(repo, cfg, CheckpointPick(None)).markdown
    assert "### Latest checkpoint" not in nothing and "recent checkpoint" not in nothing


def test_brief_names_the_model_and_effort():
    meta = {"from_display": "Claude Code", "stopped_at": "2026-09-12T20:00:00Z", "reason": "rate_limit", "model": "claude-opus-5", "effort": "xhigh"}
    handoff = Path("/w/.tapin/handoffs/id/handoff.md")
    assert brief(meta, handoff, 3_500).startswith(
        "[tapin] You are taking over in-progress work from Claude Code (claude-opus-5, effort xhigh), "
        "which stopped at 2026-09-12T20:00:00Z (reason: rate_limit).\n"
    )
    assert "from Claude Code (effort xhigh), which" in brief({**meta, "model": None}, handoff, 3_500)
    old = {key: value for key, value in meta.items() if key not in ("model", "effort")}
    assert brief(old, handoff, 3_500).startswith("[tapin] You are taking over in-progress work from Claude Code, which stopped at ")
