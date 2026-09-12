"""Snapshot of the working tree the next agent will inherit."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from tapin.store import DIR_NAME

UNTRACKED_FILE_MAX_BYTES = 20_000
EXCLUDE_TAPIN = f":(exclude){DIR_NAME}"


def _git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        return None
    return result.stdout if result.returncode == 0 else None


def find_root(cwd: Path) -> Path:
    top = _git(cwd, "rev-parse", "--show-toplevel")
    return Path(top.strip()) if top else cwd.resolve()


def ensure_excluded(root: Path) -> None:
    """Keep .tapin/ out of git without touching the tracked .gitignore."""
    exclude = _git(root, "rev-parse", "--git-path", "info/exclude")
    if exclude is None:
        return
    path = (root / exclude.strip()).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    if f"{DIR_NAME}/" not in existing.splitlines():
        path.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + f"{DIR_NAME}/\n")


@dataclass
class Snapshot:
    root: Path
    is_git: bool
    branch: str | None = None
    head: str | None = None
    status: str = ""
    diffstat: str = ""
    diff: str = ""
    untracked: dict[str, str] = field(default_factory=dict)
    truncated: bool = False


def snapshot(root: Path, max_chars: int) -> Snapshot:
    if _git(root, "rev-parse", "--is-inside-work-tree") is None:
        return Snapshot(root=root, is_git=False)

    head = (_git(root, "rev-parse", "--short", "HEAD") or "").strip() or None
    snap = Snapshot(
        root=root,
        is_git=True,
        branch=(_git(root, "branch", "--show-current") or "").strip() or None,
        head=head,
        status=_git(root, "status", "--porcelain=v1", "--untracked-files=all", "--", ".", EXCLUDE_TAPIN) or "",
    )
    if head:
        snap.diffstat = _git(root, "diff", "--stat", "HEAD", "--", ".", EXCLUDE_TAPIN) or ""
        diff = _git(root, "diff", "HEAD", "--", ".", EXCLUDE_TAPIN) or ""
    else:
        snap.diffstat = _git(root, "diff", "--stat", "--cached") or ""
        diff = (_git(root, "diff", "--cached") or "") + (_git(root, "diff") or "")

    budget = max_chars
    if len(diff) > budget:
        diff, snap.truncated = diff[:budget], True
    snap.diff = diff
    budget -= len(diff)

    listing = _git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", ".", EXCLUDE_TAPIN) or ""
    for rel in filter(None, listing.split("\0")):
        path = root / rel
        try:
            if path.stat().st_size > UNTRACKED_FILE_MAX_BYTES:
                snap.untracked[rel] = f"(skipped: larger than {UNTRACKED_FILE_MAX_BYTES} bytes)"
                continue
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            snap.untracked[rel] = "(skipped: unreadable or binary)"
            continue
        if len(text) > budget:
            snap.untracked[rel] = "(skipped: diff budget exhausted)"
            snap.truncated = True
            continue
        snap.untracked[rel] = text
        budget -= len(text)
    return snap
