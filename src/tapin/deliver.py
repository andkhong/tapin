"""Getting the handoff in front of the user and the next agent."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

NOTIFY_TIMEOUT = 10
CLIPBOARD_TIMEOUT = 5
# Tried in order; the first one on PATH that succeeds wins: macOS, Wayland, then X11.
CLIPBOARD_COMMANDS = (
    ["pbcopy"],
    ["wl-copy"],
    ["xclip", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--input"],
)


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, message: str) -> None:
    """A desktop notification: `osascript` on macOS, `notify-send` on Linux when it's installed, otherwise nothing.
    Never raises, since it runs at the end of a capture."""
    if os.environ.get("TAPIN_NO_NOTIFY"):
        return
    if sys.platform == "darwin":
        argv = ["osascript", "-e", f"display notification {_applescript_string(message)} with title {_applescript_string(title)}"]
    elif sys.platform.startswith("linux") and shutil.which("notify-send"):
        argv = ["notify-send", title, message]
    else:
        return
    try:
        subprocess.run(argv, capture_output=True, check=False, timeout=NOTIFY_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        pass


def copy_to_clipboard(text: str) -> bool:
    """Copy with the first clipboard tool that is installed and works. Returns False if none did.

    Output goes to /dev/null rather than a pipe: xclip and wl-copy leave a process running to serve the clipboard, and
    it would hold a pipe open."""
    for argv in CLIPBOARD_COMMANDS:
        if not shutil.which(argv[0]):
            continue
        try:
            subprocess.run(argv, input=text, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=CLIPBOARD_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            continue
        return True
    return False


def launch(argv: list[str], cwd: Path) -> None:
    os.chdir(cwd)
    os.execvp(argv[0], argv)
