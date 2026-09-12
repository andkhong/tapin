"""Assemble the handoff document the next agent reads, and the short brief injected at session start."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tapin.agents.base import StopEvent
from tapin.md import clip, close_open_fence, demote_headings, fence, quote
from tapin.store import iso, utcnow
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
    note: str | None,
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
        ("Model", stop.model),
        ("Stopped", stopped_at),
        ("Reason", stop.reason + (f" — {stop.details}" if stop.details else "")),
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
    else:
        lines += ["_No final message was captured. The most recent conversation is in the session digest below._", ""]
    if note:
        lines += ["### Latest checkpoint note", "", demote_headings(note, 2), ""]

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
    text = (
        f"[tapin] You are taking over in-progress work from {display} "
        f"(stopped {meta['stopped_at']}, reason: {meta['reason']}).\n"
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
