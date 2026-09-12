"""Wire Tap In into each agent: hooks, MCP registration, and an optional instruction snippet."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from tapin import agents, config
from tapin.store import utcnow

OURS = re.compile(r"tapin'? hook (?:claude|codex|cursor) ")
SNIPPET_BEGIN = "<!-- tapin:begin -->"
SNIPPET_END = "<!-- tapin:end -->"
SNIPPET = f"""{SNIPPET_BEGIN}
## Tap In handoffs
- If `.tapin/pending.json` in the workspace names an unclaimed handoff, another agent stopped mid-task. Read `.tapin/handoffs/<id>/handoff.md` (or call the `claim_handoff` MCP tool) before starting, and continue from where it stopped.
- At milestones (a design decision, a finished step, before a long operation), call the `checkpoint` MCP tool with what is done, key decisions and the next steps, so another agent can continue if you hit a usage limit.
{SNIPPET_END}
"""
CURSOR_EVENTS = {
    "sessionStart": "session-start",
    "stop": "stop",
    "sessionEnd": "session-end",
    "beforeSubmitPrompt": "before-submit-prompt",
    "afterAgentResponse": "after-agent-response",
    "afterFileEdit": "after-file-edit",
    "afterShellExecution": "after-shell-execution",
}


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()


def codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def cursor_dir() -> Path:
    return Path(os.environ.get("TAPIN_CURSOR_DIR", "~/.cursor")).expanduser()


def hook_files() -> dict[str, Path]:
    return {
        "claude": claude_dir() / "settings.json",
        "codex": codex_dir() / "hooks.json",
        "cursor": cursor_dir() / "hooks.json",
    }


def tapin_executable() -> str:
    sibling = Path(sys.executable).parent / "tapin"
    if sibling.exists():
        return str(sibling)
    found = shutil.which("tapin")
    if found:
        return found
    raise RuntimeError("Cannot find the `tapin` executable. Install it with `uv tool install --editable .`.")


def _command(exe: str, agent: str, event: str) -> str:
    return f"{shlex.quote(exe)} hook {agent} {event}"


def desired_hooks(agent: str, exe: str, cfg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    if agent == "claude":
        return {
            "StopFailure": [
                {"matcher": "|".join(cfg["limit_errors"]), "hooks": [{"type": "command", "command": _command(exe, agent, "stop-failure")}]}
            ],
            "SessionStart": [
                {"matcher": "startup|resume|clear", "hooks": [{"type": "command", "command": _command(exe, agent, "session-start")}]}
            ],
        }
    if agent == "codex":
        return {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear",
                    "hooks": [{"type": "command", "command": _command(exe, agent, "session-start"), "statusMessage": "Checking for a Tap In handoff"}],
                }
            ],
            "Stop": [{"hooks": [{"type": "command", "command": _command(exe, agent, "stop")}]}],
        }
    if agent == "cursor":
        return {event: [{"command": _command(exe, agent, ours)}] for event, ours in CURSOR_EVENTS.items()}
    raise ValueError(f"unknown agent {agent!r}")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.read_text().strip():
        return {}
    return json.loads(path.read_text())


def _write_text(path: Path, text: str) -> bool:
    old = path.read_text() if path.exists() else None
    if old == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if old is not None:
        shutil.copy2(path, path.with_name(f"{path.name}.tapin-backup-{utcnow():%Y%m%dT%H%M%SZ}"))
    path.write_text(text)
    return True


def _write_json(path: Path, data: dict[str, Any]) -> bool:
    return _write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _strip_ours(hooks: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Remove Tap In's entries from both grouped (Claude/Codex) and flat (Cursor) hook lists."""
    cleaned: dict[str, list[dict[str, Any]]] = {}
    for event, entries in hooks.items():
        kept = []
        for entry in entries:
            if "hooks" in entry:
                inner = [h for h in entry["hooks"] if not OURS.search(h.get("command", ""))]
                if inner:
                    kept.append({**entry, "hooks": inner})
            elif not OURS.search(entry.get("command", "")):
                kept.append(entry)
        if kept:
            cleaned[event] = kept
    return cleaned


def apply_hooks(agent: str, exe: str | None, cfg: dict[str, Any]) -> bool:
    """Install Tap In's hooks for `agent`, or remove them when `exe` is None. Returns True if the file changed."""
    path = hook_files()[agent]
    data = _load_json(path)
    hooks = _strip_ours(data.get("hooks", {}))
    if exe is not None:
        for event, entries in desired_hooks(agent, exe, cfg).items():
            hooks.setdefault(event, []).extend(entries)
        if agent == "cursor":
            data.setdefault("version", 1)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    return _write_json(path, data)


def apply_mcp(agent: str, exe: str | None, cfg: dict[str, Any]) -> str:
    """Register (or with exe=None, unregister) the Tap In MCP server with `agent`."""
    if agent == "cursor":
        path = cursor_dir() / "mcp.json"
        data = _load_json(path)
        servers = data.setdefault("mcpServers", {})
        if exe is None:
            servers.pop("tapin", None)
        else:
            servers["tapin"] = {"command": exe, "args": ["mcp"]}
        return f"{path}: {'updated' if _write_json(path, data) else 'unchanged'}"

    cli = agents.get(agent).command(cfg)[:1]
    scope = ["--scope", "user"] if agent == "claude" else []
    subprocess.run([*cli, "mcp", "remove", *scope, "tapin"], capture_output=True, text=True)
    if exe is None:
        return f"{agent}: MCP server removed"
    result = subprocess.run([*cli, "mcp", "add", *scope, "tapin", "--", exe, "mcp"], capture_output=True, text=True)
    if result.returncode != 0:
        return f"{agent}: MCP registration failed: {(result.stderr or result.stdout).strip()[-300:]}"
    return f"{agent}: MCP server registered"


def instruction_files() -> list[Path]:
    return [claude_dir() / "CLAUDE.md", codex_dir() / "AGENTS.md"]


def apply_snippet(path: Path, install: bool) -> bool:
    text = path.read_text() if path.exists() else ""
    pattern = re.compile(re.escape(SNIPPET_BEGIN) + r".*?" + re.escape(SNIPPET_END) + r"\n?", re.DOTALL)
    stripped = pattern.sub("", text).rstrip("\n")
    if install:
        new = (stripped + "\n\n" if stripped else "") + SNIPPET
    else:
        new = stripped + "\n" if stripped else ""
    if not path.exists() and not new:
        return False
    return _write_text(path, new)


def install(targets: list[str], cfg: dict[str, Any], mcp: bool = True, instructions: bool = False) -> list[str]:
    exe = tapin_executable()
    messages = []
    for agent in targets:
        changed = apply_hooks(agent, exe, cfg)
        messages.append(f"{hook_files()[agent]}: hooks {'installed' if changed else 'already installed'}")
        if mcp:
            messages.append(apply_mcp(agent, exe, cfg))
    if instructions:
        for path in instruction_files():
            messages.append(f"{path}: {'snippet added' if apply_snippet(path, install=True) else 'snippet already present'}")
    if "codex" in targets:
        messages.append("Codex: run /hooks in Codex once and trust the Tap In hooks, or they will not run.")
    return messages


def uninstall(targets: list[str], cfg: dict[str, Any], mcp: bool = True) -> list[str]:
    messages = []
    for agent in targets:
        changed = apply_hooks(agent, None, cfg)
        messages.append(f"{hook_files()[agent]}: hooks {'removed' if changed else 'not present'}")
        if mcp:
            messages.append(apply_mcp(agent, None, cfg))
    for path in instruction_files():
        if path.exists() and apply_snippet(path, install=False):
            messages.append(f"{path}: snippet removed")
    return messages


def doctor(cfg: dict[str, Any]) -> list[tuple[bool, str]]:
    checks: list[tuple[bool, str]] = []
    try:
        checks.append((True, f"tapin executable: {tapin_executable()}"))
    except RuntimeError as exc:
        checks.append((False, str(exc)))

    reader_command = cfg["continues"]["command"][0]
    checks.append((shutil.which(reader_command) is not None, f"session reader `{reader_command}` on PATH"))

    for name, agent in agents.REGISTRY.items():
        launcher = agent.command(cfg)[0]
        found = shutil.which(launcher) is not None or Path(launcher).exists()
        checks.append((found, f"{agent.display} launcher `{launcher}`"))
        path = hook_files()[name]
        installed = path.exists() and bool(OURS.search(path.read_text()))
        checks.append((installed, f"{agent.display} hooks in {path}"))

    log = config.tapin_home() / "hooks.log"
    failures = log.read_text().count(" failed\n") if log.exists() else 0
    checks.append((failures == 0, f"hook failures logged in {log}: {failures}"))
    return checks
