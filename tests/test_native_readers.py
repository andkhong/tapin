import json
import os
import time
from datetime import timedelta

from tapin.agents.claude import Claude
from tapin.agents.codex import Codex, rollout_hit_limit, rollout_limit_error
from tapin.md import clip_tail
from tapin.readers import base as readers_base
from tapin.readers import claude_log, codex_log
from tapin.readers.claude_log import ClaudeLogReader, project_dir_name
from tapin.readers.codex_log import CodexLogReader


def _jsonl(path, records, extra=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + extra)
    return path


def _age(path, seconds):
    then = time.time() - seconds
    os.utime(path, (then, then))


def _section(digest, title):
    start = digest.index(f"\n## {title}\n")
    end = digest.find("\n## ", start + 1)
    return digest[start : end if end != -1 else None]


# Claude Code


def _claude_log(tmp_path, cwd, session_id, records, folder=None, extra=""):
    folder = folder or project_dir_name(cwd)
    return _jsonl(tmp_path / "claude-home" / "projects" / folder / f"{session_id}.jsonl", records, extra)


def _user(cwd, content, **extra):
    return {"type": "user", "cwd": str(cwd), "message": {"role": "user", "content": content}, **extra}


def _assistant(cwd, message_id, blocks, **extra):
    return {"type": "assistant", "cwd": str(cwd), "message": {"id": message_id, "role": "assistant", "content": blocks}, **extra}


def _result(cwd, tool_use_id, content, is_error=False):
    return _user(cwd, [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content, "is_error": is_error}])


def test_claude_find_session_picks_newest_in_workspace(tmp_path):
    ws = tmp_path / "repo"
    old = _claude_log(tmp_path, ws, "old", [{"type": "mode"}, _user(ws, "first")])
    new = _claude_log(tmp_path, ws / "sub", "new", [_user(ws / "sub", "second")])
    subagent = _jsonl(new.parent / "new" / "subagents" / "agent-1.jsonl", [_user(ws, "subagent work")])
    other = _claude_log(tmp_path, tmp_path / "repo-other", "other", [_user(tmp_path / "repo-other", "elsewhere")])
    _age(old, 300)
    _age(new, 200)
    assert subagent.exists() and other.exists()

    reader = ClaudeLogReader()
    ref = reader.find_session("claude", ws)
    assert (ref.session_id, ref.path, ref.cwd) == ("new", str(new), str(ws / "sub"))
    assert ref.updated_at.endswith("Z")
    assert reader.find_session("claude", tmp_path / "nowhere", "old").path == str(old)
    assert reader.find_session("claude", ws, "agent-1") is None
    assert reader.find_session("claude", tmp_path / "nowhere") is None


def test_claude_find_session_falls_back_to_recorded_cwd(tmp_path):
    ws = tmp_path / "my.repo_v2"
    path = _claude_log(tmp_path, ws, "dotted", [_user(ws, "hello")], folder="named-some-other-way")
    assert ClaudeLogReader().find_session("claude", ws).path == str(path)


def test_claude_recent_sessions_are_workspace_logs_written_within_the_window(tmp_path):
    ws = tmp_path / "repo"
    older = _claude_log(tmp_path, ws, "older", [_user(ws, "a")])
    newer = _claude_log(tmp_path, ws / "sub", "newer", [_user(ws / "sub", "b")])
    stale = _claude_log(tmp_path, ws, "stale", [_user(ws, "c")])
    dotted = _claude_log(tmp_path, ws, "dotted", [_user(ws, "d")], folder="named-some-other-way")
    elsewhere = _claude_log(tmp_path, tmp_path / "repo-other", "elsewhere", [_user(tmp_path / "repo-other", "e")])
    subagent = _jsonl(newer.parent / "newer" / "subagents" / "agent-1.jsonl", [_user(ws, "f")])
    for path, seconds in ((older, 60), (newer, 10), (stale, 600), (dotted, 30), (elsewhere, 5), (subagent, 1)):
        _age(path, seconds)

    refs = claude_log.recent_sessions("claude", ws, timedelta(minutes=2))
    assert [(ref.session_id, ref.path) for ref in refs] == [("newer", str(newer)), ("dotted", str(dotted)), ("older", str(older))]
    assert [ref.session_id for ref in claude_log.recent_sessions("claude", ws, timedelta(seconds=45))] == ["newer", "dotted"]
    assert claude_log.recent_sessions("claude", tmp_path / "nowhere", timedelta(minutes=2)) == []
    assert ClaudeLogReader().find_session("claude", ws).session_id == "newer"


def test_claude_digest_keeps_what_the_next_agent_needs(tmp_path):
    ws = tmp_path / "repo"
    records = [
        {"type": "permission-mode", "permissionMode": "default"},
        _user(ws, "<command-name>/plan</command-name>"),
        _user(ws, "Build the CSV importer"),
        _user(ws, "Base directory for this skill", isMeta=True),
        {"type": "ai-title", "aiTitle": "CSV importer"},
        _assistant(ws, "m1", [{"type": "thinking", "thinking": "hidden reasoning"}]),
        _assistant(ws, "m1", [{"type": "text", "text": "Writing parse_row now."}]),
        _assistant(ws, "m1", [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "/w/ingest.py"}}]),
        _result(ws, "t1", "ok"),
        _assistant(ws, "m2", [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q"}}]),
        _result(ws, "t2", [{"type": "text", "text": "Exit code 1\n1 failed: test_parse_row"}], is_error=True),
        _assistant(ws, "m3", [{"type": "tool_use", "id": "t3", "name": "TodoWrite", "input": {"todos": [{"content": "Fix parse_row", "status": "in_progress"}]}}]),
        _result(ws, "t3", "ok"),
        _user(ws, [{"type": "text", "text": "[Request interrupted by user]"}]),
        _user(ws, "Also handle quoted commas"),
        _user(ws, "sidechain prompt", isSidechain=True),
        {"type": "system", "subtype": "away_summary", "content": "Tests fail on parse_row. (disable recaps in /config)"},
        {"type": "last-prompt", "lastPrompt": "Also handle quoted commas"},
        _assistant(ws, "e1", [{"type": "text", "text": "You've hit your usage limit. Resets 5pm."}], isApiErrorMessage=True, error="rate_limit"),
    ]
    path = _claude_log(tmp_path, ws, "s1", records, extra='{"type":"user","mess\n')
    reader = ClaudeLogReader()
    digest = reader.digest(reader.find_session("claude", ws))

    assert f"Read from the Claude Code session log `{path}` ({len(records)} records, 1 unreadable line skipped)." in digest
    task = _section(digest, "Task")
    assert "**CSV importer**" in task and "> Build the CSV importer" in task and "> Also handle quoted commas" in task
    for hidden in ("command-name", "Base directory", "sidechain prompt", "Request interrupted", "hidden reasoning", "disable recaps"):
        assert hidden not in digest
    assert "Tests fail on parse_row." in _section(digest, "Claude Code's latest recap")
    last = _section(digest, "Last message before the stop")
    assert "Writing parse_row now." in last and "usage limit" not in last
    stop = _section(digest, "Stop")
    assert "`rate_limit`" in stop and "You've hit your usage limit. Resets 5pm." in stop
    assert "- [in_progress] Fix parse_row" in _section(digest, "Task list")
    assert "`/w/ingest.py`" in _section(digest, "Files changed by the agent")
    failed = _section(digest, "Failed commands")
    assert "$ pytest -q" in failed and "1 failed: test_parse_row" in failed
    activity = _section(digest, "Recent activity")
    assert "- Edit `/w/ingest.py`" in activity and "- `$ pytest -q` (failed)" in activity

    _jsonl(path, [*records, _assistant(ws, "m4", [{"type": "text", "text": "Back after the reset."}])])
    assert "\n## Stop\n" not in reader.digest(reader.find_session("claude", ws))


def test_claude_limit_notice_moves_into_details(cfg):
    payload = {
        "session_id": "s1",
        "cwd": "/w",
        "error": "rate_limit",
        "error_details": "429 Too Many Requests",
        "last_assistant_message": "You've hit your usage limit.",
    }
    stop = Claude().limit_stop("stop-failure", payload, cfg)
    assert stop.last_assistant_message is None
    assert stop.details == "429 Too Many Requests — You've hit your usage limit."

    long = Claude().limit_stop("stop-failure", {**payload, "error_details": None, "last_assistant_message": "x" * 900}, cfg)
    assert len(long.details) == 500 and " — " not in long.details
    assert stop.effort is None
    assert Claude().limit_stop("stop-failure", {**payload, "effort": {"level": "xhigh"}}, cfg).effort == "xhigh"


def test_claude_session_identity_skips_synthetic_and_sidechain_records(tmp_path, monkeypatch):
    ws = tmp_path / "repo"

    def reply(model, **extra):
        return {"type": "assistant", "cwd": str(ws), "message": {"role": "assistant", "model": model, "content": []}, **extra}

    records = [
        reply("claude-sonnet-5", effort="high"),
        reply("claude-opus-5", effort="xhigh"),
        _user(ws, "Keep going"),
        reply("claude-haiku-5", effort="low", isSidechain=True),
        reply("<synthetic>", effort="xhigh"),
    ]
    path = _claude_log(tmp_path, ws, "s1", records, extra='{"type":"assistant","message":{"model":"claude-')
    assert claude_log.session_identity(path) == ("claude-opus-5", "xhigh")
    assert claude_log.session_identity(_claude_log(tmp_path, ws, "s2", [reply("claude-opus-5", effort={"level": "high"})])) == ("claude-opus-5", None)
    assert claude_log.session_identity(_claude_log(tmp_path, ws, "s3", [reply("<synthetic>"), reply(""), _user(ws, "hi")])) == (None, None)
    assert claude_log.session_identity(tmp_path / "missing.jsonl") == (None, None)
    assert claude_log.session_identity(tmp_path) == (None, None)

    # A turn can write more than the tail after its last assistant record.
    monkeypatch.setattr(readers_base, "TAIL_BYTES", 2_000)
    long_turn = [reply("claude-opus-5", effort="max"), *[_user(ws, "x" * 500) for _ in range(10)]]
    assert claude_log.session_identity(_claude_log(tmp_path, ws, "s4", long_turn)) == ("claude-opus-5", "max")


def test_claude_stop_names_the_limit_and_when_it_resets(tmp_path):
    """Claude Code 2.1.268 stores `quotaLimits` on the assistant record of a rejected request."""
    ws = tmp_path / "repo"
    quota = {"status": "rejected", "resetsAt": 1789179000, "rateLimitType": "five_hour", "overageStatus": "rejected", "isUsingOverage": False}
    notice = [{"type": "text", "text": "You've hit your limit · resets 7:10pm"}]
    reader = ClaudeLogReader()

    def stop_section(records):
        _claude_log(tmp_path, ws, "s1", records)
        return _section(reader.digest(reader.find_session("claude", ws)), "Stop")

    stop = stop_section([_user(ws, "Build it"), _assistant(ws, "e1", notice, isApiErrorMessage=True, error="rate_limit", quotaLimits=quota)])
    assert stop.rstrip().endswith("> You've hit your limit · resets 7:10pm\n\nLimit: five_hour, resets 2026-09-12T02:10:00Z.")

    before = stop_section([_user(ws, "Build it", quotaLimits={"resetsAt": 1789179000}), _assistant(ws, "e1", notice, isApiErrorMessage=True, error="rate_limit")])
    assert "Limit: usage, resets 2026-09-12T02:10:00Z." in before

    older = stop_section([_user(ws, "Build it", quotaLimits=quota), _user(ws, "Retry"), _assistant(ws, "e1", notice, isApiErrorMessage=True, error="rate_limit")])
    assert "Limit:" not in older


# Codex


def _event(event_type, **payload):
    return {"type": "event_msg", "payload": {"type": event_type, **payload}}


def _item(item_type, **item):
    return _event("item_completed", item={"type": item_type, **item})


LIMIT_ERROR = {"codex_error_info": "usage_limit_exceeded", "message": "You've hit your usage limit. Try again at 4:48 AM."}
LIMIT_TURN = [
    _event("task_started"),
    _event("token_count", rate_limits={"primary": {"used_percent": 97.0, "window_minutes": 300, "resets_at": 1789300116}}),
    _event("token_count", rate_limits=None),
    _event("task_complete", last_agent_message=None, error=LIMIT_ERROR),
    _event("thread_settings_applied"),
]


def test_rollout_limit_error_matches_real_usage_limit_turn(tmp_path, cfg):
    limit = _jsonl(tmp_path / "limit.jsonl", LIMIT_TURN, extra='[]\n{"type":"event_msg","pay\n')
    assert rollout_limit_error(limit) == LIMIT_ERROR["message"]
    assert rollout_hit_limit(limit)
    stop = Codex().limit_stop("stop", {"session_id": "x1", "cwd": str(tmp_path), "transcript_path": str(limit)}, cfg)
    assert stop.reason == "usage_limit" and stop.details == LIMIT_ERROR["message"]

    clean = _jsonl(tmp_path / "clean.jsonl", [_event("task_started"), _event("task_complete", last_agent_message="Done.")])
    assert rollout_limit_error(clean) is None and not rollout_hit_limit(clean)

    earlier = _jsonl(tmp_path / "earlier.jsonl", [*LIMIT_TURN, _event("task_started"), _event("task_complete", last_agent_message="Done.")])
    assert not rollout_hit_limit(earlier)
    assert Codex().limit_stop("stop", {"cwd": str(tmp_path), "transcript_path": str(earlier)}, cfg) is None


def _rollout(tmp_path, stamp, session_id, cwd, records, **meta):
    path = tmp_path / "codex-home" / "sessions" / "2026" / "09" / "12" / f"rollout-2026-09-12T{stamp}-{session_id}.jsonl"
    head = {"type": "session_meta", "payload": {"id": session_id, "cwd": str(cwd), "originator": "codex-tui", "cli_version": "0.154.0", **meta}}
    return _jsonl(path, [head, *records])


def test_codex_session_identity_comes_from_the_latest_turn_context(tmp_path, monkeypatch):
    ws = tmp_path / "repo"
    records = [
        {"type": "turn_context", "payload": {"cwd": str(ws), "model": "gpt-6", "effort": "medium"}},
        _event("task_started"),
        {"type": "turn_context", "payload": {"cwd": str(ws), "model": "gpt-6-astra", "effort": "high"}},
        {"type": "turn_context", "payload": {"cwd": str(ws)}},
        _event("task_complete", last_agent_message="Done."),
    ]
    path = _rollout(tmp_path, "10-00-00", "x1", ws, records, model_provider="openai")
    assert codex_log.session_identity(path) == ("gpt-6-astra", "high")
    no_effort = _jsonl(tmp_path / "no-effort.jsonl", [{"type": "turn_context", "payload": {"model": "gpt-6-astra", "effort": None}}])
    assert codex_log.session_identity(no_effort) == ("gpt-6-astra", None)
    assert codex_log.session_identity(_rollout(tmp_path, "11-00-00", "x2", ws, [], model_provider="openai")) == (None, None)
    assert codex_log.session_identity(tmp_path / "missing.jsonl") == (None, None)
    assert codex_log.session_identity(tmp_path) == (None, None)

    # A real 4.4 MB rollout's last turn_context was 728 KB before the end, well outside the tail.
    monkeypatch.setattr(readers_base, "TAIL_BYTES", 2_000)
    long_turn = [{"type": "turn_context", "payload": {"model": "gpt-6-astra", "effort": "xhigh"}}]
    long_turn += [_event("agent_message", message="y" * 500) for _ in range(10)]
    assert codex_log.session_identity(_jsonl(tmp_path / "long-turn.jsonl", long_turn)) == ("gpt-6-astra", "xhigh")


def test_codex_find_session_by_cwd_and_id(tmp_path):
    ws = tmp_path / "repo"
    older = _rollout(tmp_path, "10-00-00", "aaa-1", ws, [])
    newer = _rollout(tmp_path, "11-00-00", "bbb-2", ws / "pkg", [])
    child = _rollout(tmp_path, "12-00-00", "ccc-3", ws, [], thread_source="subagent", parent_thread_id="bbb-2")
    _rollout(tmp_path, "13-00-00", "ddd-4", tmp_path / "other", [])
    renamed = _jsonl(
        tmp_path / "codex-home" / "sessions" / "2026" / "09" / "11" / "rollout-2026-09-11T08-00-00-renamed.jsonl",
        [{"type": "session_meta", "payload": {"id": "eee-5"}}, {"type": "turn_context", "payload": {"cwd": str(tmp_path / "late")}}],
    )
    for age, path in enumerate((renamed, older, newer, child)):
        _age(path, 500 - age * 100)

    reader = CodexLogReader()
    ref = reader.find_session("codex", ws)
    assert (ref.session_id, ref.path, ref.cwd) == ("bbb-2", str(newer), str(ws / "pkg"))
    assert reader.find_session("codex", tmp_path / "nowhere", "aaa-1").path == str(older)
    assert reader.find_session("codex", tmp_path / "nowhere", "ccc-3").path == str(child)
    assert reader.find_session("codex", tmp_path / "nowhere", "eee-5").path == str(renamed)
    assert reader.find_session("codex", tmp_path / "late").session_id == "eee-5"
    assert reader.find_session("codex", tmp_path / "nowhere") is None


def test_codex_recent_sessions_skip_subagents_other_workspaces_and_old_rollouts(tmp_path, monkeypatch):
    ws = tmp_path / "repo"
    first = _rollout(tmp_path, "10-00-00", "aaa-1", ws, [])
    second = _rollout(tmp_path, "11-00-00", "bbb-2", ws / "pkg", [])
    child = _rollout(tmp_path, "12-00-00", "ccc-3", ws, [], thread_source="subagent", parent_thread_id="bbb-2")
    other = _rollout(tmp_path, "13-00-00", "ddd-4", tmp_path / "other", [])
    old = _rollout(tmp_path, "09-00-00", "eee-5", ws, [])
    for path, seconds in ((first, 50), (second, 20), (child, 5), (other, 1), (old, 600)):
        _age(path, seconds)

    refs = codex_log.recent_sessions("codex", ws, timedelta(minutes=2))
    assert [(ref.session_id, ref.path) for ref in refs] == [("bbb-2", str(second)), ("aaa-1", str(first))]
    monkeypatch.setattr(codex_log, "SCAN_MAX", 3)
    assert [ref.session_id for ref in codex_log.recent_sessions("codex", ws, timedelta(minutes=2))] == ["bbb-2"]


def test_codex_digest_keeps_plan_answer_failures_and_stop(tmp_path):
    ws = tmp_path / "repo"
    records = [
        _event("task_started"),
        {"type": "turn_context", "payload": {"cwd": str(ws), "model": "gpt-5"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}},
        _item("UserMessage", content=[{"type": "text", "text": "Plan a revive item"}]),
        _item("Plan", text="# Revive plan\n\n1. Add the catalog entry"),
        _item("AgentMessage", phase="final_answer", content=[{"type": "Text", "text": "The plan is ready."}]),
        _event("task_complete", last_agent_message="The plan is ready."),
        _event("task_started"),
        _item("UserMessage", content=[{"type": "text", "text": "Implement the plan."}]),
        _item("AgentMessage", phase="commentary", content=[{"type": "Text", "text": "Wiring the revive into the shop."}]),
        _item("FileChange", status="completed", changes={"/w/shop.ts": {"type": "update", "unified_diff": "@@"}, "/w/revive.ts": {"type": "add", "content": "x"}}),
        _item("CommandExecution", command=["/bin/zsh", "-lc", "npm test"], exit_code=1, status="failed", aggregated_output="1 failed: stocks every item"),
        _item("CommandExecution", command=["/bin/zsh", "-lc", "git status"], exit_code=0, status="completed", aggregated_output="clean"),
        _item("McpToolCall", server="browser", tool="open", status="failed"),
        _item("Extension", kind="web.search", query="revive item design"),
        *LIMIT_TURN[1:],
    ]
    path = _rollout(tmp_path, "15-01-44", "01a097a4", ws, records)
    reader = CodexLogReader()
    digest = reader.digest(reader.find_session("codex", ws))

    assert f"Read from the Codex session log `{path}` ({len(records) + 1} records, written by codex-tui 0.154.0)." in digest
    task = _section(digest, "Task")
    assert "> Plan a revive item" in task and "> Implement the plan." in task and "environment_context" not in digest
    plan = _section(digest, "Plan")
    assert "### Revive plan" in plan and "1. Add the catalog entry" in plan
    last = _section(digest, "Last message before the stop")
    assert "Wiring the revive into the shop." in last and "The plan is ready." not in last
    stop = _section(digest, "Stop")
    assert "`usage_limit_exceeded`" in stop and LIMIT_ERROR["message"] in stop and "97% of the 300-minute window" in stop
    files = _section(digest, "Files changed by the agent")
    assert "`/w/shop.ts`" in files and "`/w/revive.ts`" in files
    failed = _section(digest, "Failed commands")
    assert "`$ npm test` exited with code 1" in failed and "1 failed: stocks every item" in failed and "git status" not in failed
    activity = _section(digest, "Recent activity")
    for line in (
        "- Edited `/w/shop.ts` (update), `/w/revive.ts` (add)",
        "- `$ npm test` (exit 1)",
        "- `$ git status`",
        "- MCP `browser.open` (failed)",
        "- web.search `revive item design`",
    ):
        assert line in activity


def test_codex_digest_falls_back_to_response_items(tmp_path):
    ws = tmp_path / "repo"

    def message(role, text, **extra):
        part = "output_text" if role == "assistant" else "input_text"
        return {"type": "response_item", "payload": {"type": "message", "role": role, "content": [{"type": part, "text": text}], **extra}}

    records = [
        _event("task_started"),
        message("developer", "sandbox rules"),
        message("user", "<environment_context>cwd</environment_context>"),
        message("user", "Summarize the repo"),
        message("assistant", "It is a CLI.", phase="final_answer"),
        _event("task_complete", last_agent_message="It is a CLI."),
    ]
    _rollout(tmp_path, "15-01-44", "fallback", ws, records)
    reader = CodexLogReader()
    digest = reader.digest(reader.find_session("codex", ws))
    assert "> Summarize the repo" in _section(digest, "Task")
    assert "> It is a CLI." in _section(digest, "Last message before the stop")
    assert "environment_context" not in digest and "sandbox rules" not in digest and "\n## Stop\n" not in digest


def test_recent_activity_collapses_repeats(tmp_path):
    ws = tmp_path / "repo"
    records = []

    def call(name, args, is_error=False):
        n = len(records)
        records.extend([_assistant(ws, f"m{n}", [{"type": "tool_use", "id": f"t{n}", "name": name, "input": args}]), _result(ws, f"t{n}", "out", is_error)])

    call("Read", {"file_path": "/w/early.py"})
    for _ in range(30):
        call("mcp__claude-in-chrome__browser_batch", {})
    call("Bash", {"command": "npm test"}, is_error=True)
    call("Bash", {"command": "npm test"})
    for step in range(21):
        call("Bash", {"command": f"step {step}"})
    _claude_log(tmp_path, ws, "s1", records)
    claude = ClaudeLogReader()
    activity = _section(claude.digest(claude.find_session("claude", ws)), "Recent activity")
    assert len([line for line in activity.splitlines() if line.startswith("- ")]) == 25
    assert "- Read `/w/early.py`\n- mcp__claude-in-chrome__browser_batch (×30)\n- `$ npm test` (failed)\n- `$ npm test`\n" in activity

    git = _item("CommandExecution", command=["/bin/zsh", "-lc", "git status"], exit_code=0)
    npm = ["/bin/zsh", "-lc", "npm test"]
    _rollout(tmp_path, "15-01-44", "c1", ws, [git, git, git, _item("CommandExecution", command=npm, exit_code=1), _item("CommandExecution", command=npm, exit_code=0)])
    codex = CodexLogReader()
    activity = _section(codex.digest(codex.find_session("codex", ws)), "Recent activity")
    assert "- `$ git status` (×3)\n- `$ npm test` (exit 1)\n- `$ npm test`" in activity


def test_failed_output_keeps_its_end(tmp_path):
    output = "FIRST LINE\n" + "".join(f"progress line {n}\n" for n in range(200)) + "LAST LINE: 1 failed"
    clipped = clip_tail(output, 800)
    assert len(clipped) <= 800 and clipped.endswith("LAST LINE: 1 failed") and "FIRST LINE" not in clipped
    assert clipped.startswith("…\nprogress line ")
    assert clip_tail("short", 800) == "short"

    ws = tmp_path / "repo"
    bash = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest"}}
    _claude_log(tmp_path, ws, "s1", [_assistant(ws, "m1", [bash]), _result(ws, "t1", output, is_error=True)])
    _rollout(tmp_path, "15-01-44", "c1", ws, [_item("CommandExecution", command=["/bin/zsh", "-lc", "pytest"], exit_code=1, aggregated_output=output)])
    for reader, agent in ((ClaudeLogReader(), "claude"), (CodexLogReader(), "codex")):
        failed = _section(reader.digest(reader.find_session(agent, ws)), "Failed commands")
        assert "LAST LINE: 1 failed" in failed and "FIRST LINE" not in failed


def test_codex_scan_is_bounded(tmp_path, monkeypatch):
    ws, other = tmp_path / "repo", tmp_path / "other"
    renamed = _jsonl(
        tmp_path / "codex-home" / "sessions" / "2026" / "09" / "11" / "rollout-2026-09-11T08-00-00-renamed.jsonl",
        [{"type": "session_meta", "payload": {"id": "meta-2", "cwd": str(other)}}],
    )
    mine = _rollout(tmp_path, "09-00-00", "mine-1", ws, [])
    newer = [_rollout(tmp_path, f"1{n}-00-00", f"other-{n}", other, []) for n in range(2)]
    for age, path in zip((600, 500, 200, 100), (renamed, mine, *newer)):
        _age(path, age)

    reader = CodexLogReader()
    assert reader.find_session("codex", ws).path == str(mine)
    assert reader.find_session("codex", tmp_path / "nowhere", "meta-2").path == str(renamed)

    monkeypatch.setattr(codex_log, "SCAN_MAX", 2)
    assert reader.find_session("codex", ws) is None
    assert reader.find_session("codex", tmp_path / "nowhere", "mine-1").path == str(mine)
    assert reader.find_session("codex", tmp_path / "nowhere", "meta-2") is None
