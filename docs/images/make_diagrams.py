"""Generate the README diagrams in light and dark variants.

    python3 docs/images/make_diagrams.py
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).parent
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"

# GitHub's own light and dark palettes, so the images sit flush on the README page.
THEMES = {
    "light": {
        "bg": "#ffffff", "fg": "#1f2328", "muted": "#59636e", "border": "#d1d9e0", "surface": "#f6f8fa",
        "accent": "#0969da", "accent_surface": "#ddf4ff", "warn": "#9a6700", "warn_surface": "#fff8c5", "warn_border": "#d4a72c",
    },
    "dark": {
        "bg": "#0d1117", "fg": "#f0f6fc", "muted": "#9198a1", "border": "#3d444d", "surface": "#151b23",
        "accent": "#4493f8", "accent_surface": "#0f213a", "warn": "#d29922", "warn_surface": "#272115", "warn_border": "#9e6a03",
    },
}


class Canvas:
    def __init__(self, width: int, height: int, label: str, theme: str):
        self.width, self.height, self.label = width, height, label
        self.colors = THEMES[theme]
        self.parts: list[str] = []

    def box(self, x, y, w, h, fill="surface", stroke="border", radius=10, stroke_width=1.5):
        stroke_attr = f'stroke="{self.colors[stroke]}" stroke-width="{stroke_width}"' if stroke else 'stroke="none"'
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{self.colors[fill]}" {stroke_attr}/>')

    def line(self, x1, y1, x2, y2, color="border"):
        self.parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{self.colors[color]}" stroke-width="1.5"/>')

    def text(self, x, y, content, size=15, color="fg", weight=400, mono=False, anchor="start"):
        family = MONO if mono else SANS
        self.parts.append(
            f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" '
            f'fill="{self.colors[color]}" text-anchor="{anchor}">{escape(content)}</text>'
        )

    def pill(self, x, y, content):
        width = 16 + len(content) * 7.2
        self.box(x, y, width, 28, fill="warn_surface", stroke="warn_border", radius=14, stroke_width=1.25)
        self.text(x + width / 2, y + 19, content, size=13, color="warn", weight=600, anchor="middle")

    def arrow(self, x1, y1, x2, y2, label=None, mono=False, below=False):
        self.parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{self.colors["muted"]}" '
            f'stroke-width="1.75" marker-end="url(#arrow)"/>'
        )
        if not label:
            return
        if x1 == x2:
            self.text(x1 + 12, (y1 + y2) / 2 + 5, label, size=12.5, color="muted", mono=mono)
        else:
            self.text((x1 + x2) / 2, y1 + (22 if below else -9), label, size=12.5, color="muted", mono=mono, anchor="middle")

    def render(self) -> str:
        label = escape(self.label, {'"': "&quot;"})
        marker = (
            '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{self.colors["muted"]}"/></marker>'
        )
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.width} {self.height}" '
            f'width="{self.width}" height="{self.height}" role="img" aria-label="{label}">'
            f"<title>{escape(self.label)}</title><defs>{marker}</defs>"
            f'<rect width="{self.width}" height="{self.height}" fill="{self.colors["bg"]}"/>'
            + "".join(self.parts)
            + "</svg>\n"
        )


def flow(theme: str) -> str:
    c = Canvas(
        1040,
        580,
        "When Claude Code hits its usage limit, its hook starts tapin capture, which turns the session log, "
        "working tree and plan into a handoff in .tapin/. You start Codex, which claims the handoff and picks up mid-step.",
        theme,
    )
    for x, number, title in ((24, "1", "Agent hits its limit"), (404, "2", "Tap In captures"), (764, "3", "Next agent continues")):
        c.text(x, 52, number, size=14, weight=700, color="accent")
        c.text(x + 18, 52, title, size=14, weight=600, color="muted")

    # 1. What the stopped agent leaves on disk.
    c.box(24, 86, 270, 140)
    c.text(44, 120, "Claude Code", size=17, weight=700)
    c.text(44, 144, "working on your task", size=14, color="muted")
    c.pill(44, 170, "hits its usage limit")
    for y, title, detail in (
        (252, "Session log", "~/.claude/projects/…"),
        (360, "Working tree", "git status · git diff"),
        (468, "Plan file", "~/.claude/plans/…"),
    ):
        c.box(24, y, 270, 82)
        c.text(44, y + 34, title, size=15, weight=600)
        c.text(44, y + 58, detail, size=13, color="muted", mono=True)

    # 2. Capture: one row per input.
    c.box(404, 86, 280, 464, fill="accent_surface", stroke="accent")
    c.text(424, 120, "tapin capture", size=17, weight=700, mono=True)
    c.text(424, 143, "reads files only, no model call", size=13.5, color="muted")
    c.text(424, 195, "Starts when the hook fires", size=15)
    for y, title, detail in (
        (293, "Session digest", "conversation, commands, edits"),
        (401, "Workspace snapshot", "diff + untracked files"),
        (509, "Plan", "what it was following"),
    ):
        c.text(424, y - 4, title, size=15, weight=600)
        c.text(424, y + 18, detail, size=14, color="muted")
    for y, label, mono in ((190, "rate_limit", True), (293, "parsed", False), (401, "git diff", True), (509, "copied", False)):
        c.arrow(294, y, 404, y, label=label, mono=mono)

    # 3. Delivery to the next agent.
    c.box(764, 86, 252, 102, fill="accent_surface", stroke="accent")
    c.text(784, 116, ".tapin/", size=16, weight=700, mono=True)
    c.text(784, 144, "handoffs/<id>/handoff.md", size=13, mono=True)
    c.text(784, 168, "pending.json", size=13, mono=True)
    c.arrow(684, 137, 764, 137, label="writes")
    c.arrow(890, 188, 890, 236, label="notifies you")

    c.box(764, 236, 252, 150)
    c.text(784, 268, "Start the next agent", size=16, weight=700)
    c.text(784, 298, "tapin to codex", size=14, mono=True)
    c.text(784, 318, "launches it with the prompt", size=13, color="muted")
    c.text(784, 350, "or open Codex in the folder", size=14)
    c.text(784, 370, "its SessionStart hook loads it", size=13, color="muted")
    c.arrow(890, 386, 890, 430, label="claims it")

    c.box(764, 430, 252, 120)
    c.text(784, 462, "Codex", size=17, weight=700)
    for i, line in enumerate(("reads handoff.md", "checks the diff", "picks up mid-step")):
        c.text(784, 488 + i * 21, line, size=14)
    return c.render()


def anatomy(theme: str) -> str:
    c = Canvas(
        1040,
        600,
        "Each section of handoff.md is filled from something already on disk: the hook payload, Tap In's instructions, "
        "checkpoint notes, the plan file, the session log and git.",
        theme,
    )
    c.text(24, 52, "Already on disk", size=14, weight=600, color="muted")
    c.text(414, 52, "fills", size=14, weight=600, color="muted", anchor="middle")
    c.text(474, 52, "What the next agent reads", size=14, weight=600, color="muted")

    c.box(474, 70, 542, 510, fill="bg", stroke="accent", radius=12)
    c.text(494, 97, "handoff.md", size=14, weight=700, mono=True)
    c.text(590, 97, ".tapin/handoffs/<id>/", size=12.5, color="muted", mono=True)
    c.line(474, 110, 1016, 110)

    rows = (
        ("Hook payload", "StopFailure · Stop · sessionEnd", True,
         "# Handoff from Claude Code", "agent, session, stop reason and time, git branch and HEAD"),
        ("Tap In", "the same instructions every time", False,
         "## How to continue", "read it all, verify the diff, don't redo finished steps"),
        ("Last message + checkpoints", "hook payload · .tapin/notes.md", True,
         "## Where it stopped", "the agent's final message and its latest checkpoint note"),
        ("Plan file", "~/.claude/plans/<slug>.md", True,
         "## Plan", "the plan it was following, when there is one"),
        ("Session log", "read by continues · Cursor: hook journal", False,
         "## Session digest", "recent conversation, commands with output, file edits"),
        ("Git", "git status · git diff HEAD", True,
         "## Workspace", "status, full diff, and the contents of new untracked files"),
    )
    for i, (source, detail, detail_mono, heading, description) in enumerate(rows):
        y = 118 + i * 76
        c.box(24, y, 330, 66)
        c.text(44, y + 27, source, size=15, weight=600)
        c.text(44, y + 49, detail, size=13, color="muted", mono=detail_mono)
        c.box(490, y, 510, 66, stroke=None, radius=8)
        c.text(510, y + 27, heading, size=14, weight=600, mono=True)
        c.text(510, y + 49, description, size=14, color="muted")
        c.arrow(354, y + 33, 490, y + 33)
    return c.render()


def hub(theme: str) -> str:
    c = Canvas(
        1040,
        430,
        "Agents never talk to each other directly. Claude Code, Codex, Cursor and any MCP agent each write a handoff "
        "to the project's .tapin/ folder when they stop and load one from it when they start.",
        theme,
    )
    agents = (
        (24, 30, "Claude Code", (("on limit", "StopFailure hook", False), ("session", "continues", False), ("on start", "SessionStart hook", False))),
        (726, 30, "Codex", (("on limit", "Stop hook", False), ("session", "continues", False), ("on start", "SessionStart hook", False))),
        (24, 230, "Cursor", (("on limit", "stop, sessionEnd", False), ("session", "Tap In hook journal", False), ("on start", "sessionStart hook", False))),
        (726, 230, "Any MCP agent", (("on limit", "create_handoff", True), ("session", "checkpoint", True), ("on start", "claim_handoff", True))),
    )
    for x, y, name, rows in agents:
        c.box(x, y, 290, 170)
        c.text(x + 20, y + 36, name, size=17, weight=700)
        for i, (label, value, mono) in enumerate(rows):
            c.text(x + 20, y + 76 + i * 32, label, size=13, color="muted")
            c.text(x + 104, y + 76 + i * 32, value, size=14, mono=mono)
        mid = y + 85
        if x < 404:
            c.arrow(314, mid - 12, 404, mid - 12, label="writes")
            c.arrow(404, mid + 14, 314, mid + 14, label="loads", below=True)
        else:
            c.arrow(726, mid - 12, 636, mid - 12, label="writes")
            c.arrow(636, mid + 14, 726, mid + 14, label="loads", below=True)

    c.box(404, 30, 232, 370, fill="accent_surface", stroke="accent")
    c.text(520, 72, ".tapin/", size=18, weight=700, mono=True, anchor="middle")
    c.text(520, 96, "in your project", size=13, color="muted", anchor="middle")
    for i, (name, note) in enumerate(
        (("pending.json", "the waiting handoff"), ("handoffs/", "one per stop"), ("notes.md", "MCP checkpoints"), ("journal/", "Cursor activity"))
    ):
        c.text(428, 150 + i * 64, name, size=14, mono=True)
        c.text(428, 170 + i * 64, note, size=13, color="muted")
    return c.render()


DIAGRAMS = {"flow": flow, "handoff": anatomy, "agents": hub}


def main() -> None:
    for name, draw in DIAGRAMS.items():
        for theme in THEMES:
            (OUT / f"{name}-{theme}.svg").write_text(draw(theme))


if __name__ == "__main__":
    main()
