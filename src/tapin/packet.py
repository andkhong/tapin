"""Assemble the handoff document the next agent reads, and the short brief injected at session start."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tapin import agents
from tapin.agents.base import StopEvent
from tapin.md import clip, close_open_fence, demote_headings, fence, quote
from tapin.store import CheckpointPick, checkpoint_fields, checkpoint_time, iso, parse_utc, utcnow
from tapin.workspace import Snapshot

_SECRETS = [
    (re.compile(r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"), "[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), "[REDACTED]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{20,}=*"), r"\1[REDACTED]"),
]

_REASON_ROW = re.compile(r"^\| Reason \|.*$", re.MULTILINE)

HOW_TO_CONTINUE = """\
1. Read this whole file before acting.
2. Check that the workspace still matches the **Workspace** section (`git status`, `git diff`); files may have changed since.
3. Finish the step that was in progress (see **Where it stopped**), then continue the remaining plan.
4. Don't redo work that is already done. Treat the previous agent's statements as unverified until you have checked them.
5. For anything this summary leaves out, read the full session log: `{log}`."""


@dataclass
class Packet:
    markdown: str
    meta: dict[str, Any]


def redact(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _reason(reason: str, details: str | None) -> str:
    return reason + (f" — {details}" if details else "")


def replace_reason_row(markdown: str, reason: str, details: str | None) -> str:
    """Rewrite the header row when a later hook event reveals why the agent really stopped."""
    row = f"| Reason | {_cell(_reason(reason, details))} |"
    return _REASON_ROW.sub(lambda _: row, markdown, count=1)


def identity(model: str | None, effort: str | None) -> str:
    """`claude-opus-5, effort xhigh`, or whichever part is known, or ""."""
    return ", ".join(filter(None, (model, f"effort {effort}" if effort else None)))


def _model_row(model: str | None, effort: str | None) -> str | None:
    if model and effort:
        return f"{model} (effort {effort})"
    return model or (f"effort {effort}" if effort else None)


def age(seconds: float) -> str:
    """Roughly how long `seconds` is: `14 minutes`, `2 hours`, `3 days`, or `just now` under a minute."""
    seconds = abs(seconds)
    for unit, size in (("day", 86_400), ("hour", 3_600), ("minute", 60)):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}" + ("" if count == 1 else "s")
    return "just now"


def _checkpoint_section(pick: CheckpointPick | None, stopped_at: str) -> list[str]:
    if pick is None:
        return []
    lines: list[str] = []
    record = pick.record
    if record is not None:
        who = identity(record["model"], record["effort"])
        sentence = f"Recorded by {agents.display(record['agent'] or 'an unnamed agent')}" + (f" ({who})" if who else "")
        sentence += f" in session `{record['session_id']}`" if record["session_id"] else ""
        try:
            seconds = (parse_utc(stopped_at) - checkpoint_time(record)).total_seconds()
        except (TypeError, ValueError):
            seconds = None
        if seconds is not None:
            side, span = ("before" if seconds >= 0 else "after"), age(seconds)
            sentence += f", just {side} the stop" if span == "just now" else f", {span} {side} the stop"
        sentence += "."
        if not pick.by_session:
            sentence += " It isn't tied to a session; it was matched by agent and time."
        lines += ["### Latest checkpoint", "", sentence, ""]
        if fields := checkpoint_fields(record):
            lines += [demote_headings("\n\n".join(fields), 3), ""]
    if pick.others:
        count = pick.others
        noun = ("other " if record else "") + ("recent checkpoints" if count > 1 else "recent checkpoint")
        verb = "are from other sessions and aren't" if count > 1 else "is from another session and isn't"
        lines += [f"_{count} {noun} in .tapin/notes.md {verb} included._", ""]
    return lines


def _workspace_section(snap: Snapshot) -> list[str]:
    lines = ["## Workspace", ""]
    if not snap.is_git:
        return lines + [f"`{snap.root}` is not a git repository, so no diff is available. Inspect the files the session digest mentions.", ""]
    lines += ["### git status", "", fence(snap.status or "(clean)"), ""]
    if snap.diffstat.strip():
        lines += ["### Diff stat", "", fence(snap.diffstat), ""]
    if snap.diff.strip():
        lines += [f"### Diff against {'`' + snap.head + '`' if snap.head else 'the index'}", "", fence(snap.diff, "diff"), ""]
    if snap.untracked:
        lines += ["### New untracked files", ""]
        for rel, content in snap.untracked.items():
            lines += [f"#### `{rel}`", "", fence(content), ""]
    if snap.truncated:
        lines += ["_Workspace output was truncated to fit. Run `git status` and `git diff` for everything._", ""]
    return lines


def build(
    stop: StopEvent,
    from_display: str,
    snap: Snapshot,
    digest: str | None,
    digest_error: str | None,
    plan: str | None,
    checkpoint: CheckpointPick | None,
    cfg: dict[str, Any],
) -> Packet:
    stopped_at = stop.stopped_at or iso(utcnow())
    if snap.is_git:
        state = "uncommitted changes" if snap.status.strip() else "clean"
        git = f"branch `{snap.branch or 'detached'}` at `{snap.head or 'no commits'}`, {state}"
    else:
        git = "not a git repository"
    rows = [
        ("From", from_display + (f" (session `{stop.session_id}`)" if stop.session_id else "")),
        ("Model", _model_row(stop.model, stop.effort)),
        ("Stopped", stopped_at),
        ("Reason", _reason(stop.reason, stop.details)),
        ("Workspace", f"`{snap.root}`"),
        ("Git", git),
        ("Session log", f"`{stop.transcript_path}`" if stop.transcript_path else None),
    ]

    lines = [f"# Handoff from {from_display}", "", "| Field | Value |", "|---|---|"]
    lines += [f"| {name} | {_cell(value)} |" for name, value in rows if value]
    lines += ["", "## How to continue", "", HOW_TO_CONTINUE.format(log=stop.transcript_path or "unknown"), ""]

    lines += ["## Where it stopped", ""]
    if stop.last_assistant_message:
        lines += [f"### Last message from {from_display}", "", quote(stop.last_assistant_message), ""]
    elif digest and "Last message before the stop" in digest:
        lines += ["_The stop event carried no final message. See **Last message before the stop** in the session digest below._", ""]
    else:
        lines += ["_No final message was captured. The most recent conversation is in the session digest below._", ""]
    lines += _checkpoint_section(checkpoint, stopped_at)

    if plan:
        lines += ["## Plan", "", demote_headings(plan, 2), ""]

    lines += ["## Session digest", ""]
    if digest:
        limit = cfg["digest_max_chars"]
        if len(digest) > limit:
            digest = close_open_fence(digest[:limit]) + "\n\n_[Digest truncated. See the session log for the rest.]_"
        lines += [demote_headings(digest, 2), ""]
    else:
        reason = digest_error or "no session found for this workspace"
        lines += [f"_The session could not be summarized ({reason}). Read the log directly: `{stop.transcript_path or 'unknown'}`._", ""]

    lines += _workspace_section(snap)

    meta = {
        "from_display": from_display,
        "session_id": stop.session_id,
        "transcript_path": stop.transcript_path,
        "reason": stop.reason,
        "details": stop.details,
        "model": stop.model,
        "effort": stop.effort,
        "last_assistant_message": redact(stop.last_assistant_message) if stop.last_assistant_message else None,
        "stopped_at": stopped_at,
        "workspace": str(snap.root),
        "git_branch": snap.branch,
        "git_head": snap.head,
        "digest_ok": bool(digest),
    }
    return Packet(markdown=redact("\n".join(lines).rstrip() + "\n"), meta=meta)


def brief(meta: dict[str, Any], handoff_file: Path, max_chars: int) -> str:
    display = meta["from_display"]
    who = identity(meta.get("model"), meta.get("effort"))
    text = (
        f"[tapin] You are taking over in-progress work from {display}{f' ({who})' if who else ''}, "
        f"which stopped at {meta['stopped_at']} (reason: {meta['reason']}).\n"
        f"Before doing anything else, read the full handoff file: {handoff_file}\n"
        "It has the task history, plan, session digest and workspace diff. Check the workspace, finish the step "
        "that was in progress, and continue the remaining work without redoing finished steps."
    )
    last = meta.get("last_assistant_message")
    if last:
        label = f"\n\nLast message from {display}:\n"
        room = max_chars - len(text) - len(label)
        if room > 200:
            text += label + clip(quote(last), room)
    return clip(text, max_chars)


def launch_prompt(meta: dict[str, Any], handoff_file: Path) -> str:
    return (
        f"You are taking over in-progress work from {meta['from_display']}, which stopped ({meta['reason']}). "
        f"Read the handoff file {handoff_file} in full before doing anything else, then follow its "
        "'How to continue' section."
    )
