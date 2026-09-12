"""Getting the handoff in front of the user and the next agent."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, message: str) -> None:
    if sys.platform != "darwin" or os.environ.get("TAPIN_NO_NOTIFY"):
        return
    script = f"display notification {_applescript_string(message)} with title {_applescript_string(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True, check=False)


def copy_to_clipboard(text: str) -> bool:
    try:
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def launch(argv: list[str], cwd: Path) -> None:
    os.chdir(cwd)
    os.execvp(argv[0], argv)
