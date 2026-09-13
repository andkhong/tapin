"""Markdown sections shared by the native session-log readers."""

from __future__ import annotations

from tapin.md import clip, code_span, quote

FIRST_PROMPT_MAX = 1_500
PROMPT_MAX = 600
LATEST_PROMPTS = 3
FILES_MAX = 40


def header(source: str, path: str, records: int, skipped: int, extra: str = "") -> list[str]:
    counts = f"{records} records"
    if skipped:
        counts += f", {skipped} unreadable line{'' if skipped == 1 else 's'} skipped"
    return ["# Session Handoff Context", f"Read from the {source} session log {code_span(path)} ({counts}{extra})."]


def section(title: str, blocks: list[str]) -> list[str]:
    return [f"## {title}", *blocks] if blocks else []


def task(prompts: list[str]) -> list[str]:
    """The first request, then the latest few after it."""
    if not prompts:
        return []
    blocks = ["First request:", quote(clip(prompts[0], FIRST_PROMPT_MAX))]
    latest = prompts[1:][-LATEST_PROMPTS:]
    if latest:
        blocks += ["Latest requests:", *(quote(clip(prompt, PROMPT_MAX)) for prompt in latest)]
    return blocks


def files(paths: list[str]) -> list[str]:
    if not paths:
        return []
    lines = [f"- {code_span(path)}" for path in paths[:FILES_MAX]]
    if len(paths) > FILES_MAX:
        lines.append(f"- _…and {len(paths) - FILES_MAX} more_")
    return ["\n".join(lines)]


def activity(lines: list[str], limit: int) -> list[str]:
    """Runs of identical lines merged into one ending in ` (×N)`, then the last `limit` of those."""
    runs: list[tuple[str, int]] = []
    for line in lines:
        if runs and runs[-1][0] == line:
            runs[-1] = (line, runs[-1][1] + 1)
        else:
            runs.append((line, 1))
    rendered = [f"- {line}" + (f" (×{count})" if count > 1 else "") for line, count in runs[-limit:]]
    return ["\n".join(rendered)] if rendered else []


def join(blocks: list[str]) -> str:
    return "\n\n".join(blocks) + "\n"
