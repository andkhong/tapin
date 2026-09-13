"""Markdown helpers shared by packet and digest rendering."""

from __future__ import annotations

import re

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^(#{1,6}) ")


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def fence(text: str, lang: str = "") -> str:
    longest = max((len(m.group()) for m in re.finditer(r"`{3,}", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text.rstrip()}\n{ticks}"


def quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines())


def clip_tail(text: str, limit: int) -> str:
    """Keep the end of `text`, where errors and summaries usually are, starting at a line boundary."""
    if len(text) <= limit:
        return text
    kept = text[len(text) - max(0, limit - 1) :]
    newline = kept.find("\n")
    if newline != -1 and kept[newline + 1 :].strip():
        return "…\n" + kept[newline + 1 :]
    return "…" + kept


def one_line(text: str) -> str:
    """Put a multi-line command on one line, marking where its lines broke."""
    return " ↵ ".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def code_span(text: str) -> str:
    """Inline code on one line, delimited by more backticks than any run inside it."""
    text = " ".join(text.split())
    ticks = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def _step(open_fence: str | None, line: str) -> tuple[str | None, bool]:
    match = _FENCE.match(line)
    if not match:
        return open_fence, False
    marker = match.group(1)
    if open_fence is None:
        return marker, True
    if marker[0] == open_fence[0] and len(marker) >= len(open_fence) and line.strip() == marker:
        return None, True
    return open_fence, True


def demote_headings(markdown: str, levels: int = 1) -> str:
    """Push headings down so embedded documents nest under our own sections; code blocks are left alone."""
    out, open_fence = [], None
    for line in markdown.splitlines():
        was_open = open_fence is not None
        open_fence, is_marker = _step(open_fence, line)
        if not was_open and not is_marker and (heading := _HEADING.match(line)):
            line = "#" * min(6, len(heading.group(1)) + levels) + line[len(heading.group(1)) :]
        out.append(line)
    return "\n".join(out)


def close_open_fence(markdown: str) -> str:
    open_fence = None
    for line in markdown.splitlines():
        open_fence, _ = _step(open_fence, line)
    return f"{markdown}\n{open_fence}" if open_fence else markdown
