"""Wire Tap In into each agent: a stable launcher, hooks, MCP registration, and an optional instruction snippet."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from tapin import agents, config
from tapin.store import utcnow

OURS = re.compile(rf"tapin'? hook (?:{'|'.join(re.escape(name) for name in agents.NAMES)}) ")
EPHEMERAL_DIRS = {"archive-v0", "_npx"}
CODEX_TRUST_LABELS = {"SessionStart": "session_start", "Stop": "stop"}
OK, WARN, FAIL = "ok", "warn", "FAIL"
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


def agent_dirs() -> dict[str, Path]:
    return {"claude": claude_dir(), "codex": codex_dir(), "cursor": cursor_dir()}


def hook_files() -> dict[str, Path]:
    return {
        "claude": claude_dir() / "settings.json",
        "codex": codex_dir() / "hooks.json",
        "cursor": cursor_dir() / "hooks.json",
    }


def detected(name: str, cfg: dict[str, Any]) -> bool:
    return agent_dirs()[name].is_dir() or shutil.which(agents.get(name).command(cfg)[0]) is not None


def launcher_path() -> Path:
    return config.tapin_home() / "bin" / "tapin"


def tapin_executable() -> str:
    sibling = Path(sys.executable).parent / "tapin"
    if sibling.exists():
        return str(sibling)
    found = shutil.which("tapin")
    if found:
        return found
    raise RuntimeError("Cannot find the `tapin` executable. Install it with `uv tool install tapin`.")


def _shim(target: Path) -> str:
    return f'#!/bin/sh\nexec {shlex.quote(str(target))} "$@"\n'


def ensure_launcher() -> str:
    """Point the launcher at the running install. Hooks call the launcher, so their command text (which Codex
    keys trust to) stays the same across upgrades and reinstalls."""
    target = Path(tapin_executable()).resolve()
    if EPHEMERAL_DIRS.intersection(target.parts):
        raise RuntimeError(
            f"Tap In is running from a temporary cache ({target}) that can be deleted at any time, so hooks can't use it. "
            "Install it with `uv tool install tapin`, then run `tapin install` again."
        )
    link = launcher_path()
    if link.is_symlink() and os.readlink(link) == str(target):
        return f"{link}: launcher unchanged, runs {target}"
    if not link.is_symlink() and link.is_file() and link.read_text() == _shim(target):
        return f"{link}: launcher unchanged, runs {target}"
    existed = link.is_symlink() or link.exists()
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.tmp")
    tmp.unlink(missing_ok=True)
    try:
        tmp.symlink_to(target)
    except OSError:
        tmp.write_text(_shim(target))
        tmp.chmod(0o755)
    os.replace(tmp, link)
    return f"{link}: launcher {'repointed' if existed else 'created'}, runs {target}"


def launcher_target() -> Path | None:
    link = launcher_path()
    if link.is_symlink():
        return link.resolve()
    if link.is_file():
        for line in link.read_text().splitlines():
            if line.startswith("exec "):
                return Path(shlex.split(line)[1])
    return None


def remove_launcher() -> str | None:
    link = launcher_path()
    if not (link.is_symlink() or link.exists()):
        return None
    link.unlink()
    try:
        link.parent.rmdir()
    except OSError:
        pass
    return f"{link}: launcher removed"


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


def _is_ours(handler: dict[str, Any]) -> bool:
    return bool(OURS.search(handler.get("command", "")))


def _place(entries: list[dict[str, Any]], wanted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put Tap In's entries where its previous ones were, appending only when there were none, so the group and
    handler indexes Codex keys hook trust to survive a reinstall. An empty `wanted` removes them.

    Entries are groups with a `hooks` list (Claude Code, Codex) or bare handlers (Cursor)."""
    placed = not wanted
    result: list[dict[str, Any]] = []
    for entry in entries:
        if "hooks" not in entry:
            if not _is_ours(entry):
                result.append(entry)
            elif not placed:
                result.extend(wanted)
                placed = True
            continue
        handlers = entry["hooks"]
        if not any(_is_ours(handler) for handler in handlers):
            result.append(entry)
        elif all(_is_ours(handler) for handler in handlers):
            if not placed:
                result.extend(wanted)
                placed = True
        else:
            kept = []
            for handler in handlers:
                if not _is_ours(handler):
                    kept.append(handler)
                elif not placed:
                    kept.extend(new for group in wanted for new in group["hooks"])
                    placed = True
            result.append({**entry, "hooks": kept})
    if not placed:
        result.extend(wanted)
    return result


def apply_hooks(agent: str, exe: str | None, cfg: dict[str, Any]) -> bool:
    """Install Tap In's hooks for `agent`, or remove them when `exe` is None. Returns True if the file changed."""
    path = hook_files()[agent]
    if exe is None and not path.exists():
        return False
    data = _load_json(path)
    existing = data.get("hooks", {})
    wanted = desired_hooks(agent, exe, cfg) if exe is not None else {}
    hooks = {}
    for event in {**existing, **wanted}:
        entries = _place(existing.get(event, []), wanted.get(event, []))
        if entries:
            hooks[event] = entries
    if exe is not None and agent == "cursor":
        data.setdefault("version", 1)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    return _write_json(path, data)


def tapin_hooks(agent: str) -> list[tuple[str, int, int | None, str]]:
    """(event, group index, handler index, command) for each Tap In hook in the agent's hooks file.
    Cursor's hooks aren't grouped, so its handler index is None."""
    found: list[tuple[str, int, int | None, str]] = []
    for event, entries in _load_json(hook_files()[agent]).get("hooks", {}).items():
        for group_index, entry in enumerate(entries):
            if "hooks" in entry:
                found += [(event, group_index, i, h["command"]) for i, h in enumerate(entry["hooks"]) if _is_ours(h)]
            elif _is_ours(entry):
                found.append((event, group_index, None, entry["command"]))
    return found


def codex_hook_hash(event_label: str, group: dict[str, Any], handler: dict[str, Any]) -> str:
    """The trusted_hash Codex records for a command hook: sha256 of the hook's canonical JSON identity
    (hook_hash in codex-rs/hooks/src/engine/discovery.rs)."""
    timeout = handler.get("timeout")
    normalized = {
        "type": "command",
        "command": handler.get("command"),
        "timeout": 600 if timeout is None else max(1, timeout),
        "async": handler.get("async") or False,
    }
    if handler.get("statusMessage") is not None:
        normalized["statusMessage"] = handler["statusMessage"]
    identity = {"event_name": event_label, "hooks": [normalized]}
    if group.get("matcher") is not None:
        identity["matcher"] = group["matcher"]
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def codex_trust() -> dict[str, str]:
    """Codex's trust state for each installed Tap In hook: `trusted`, `changed` (trusted, but the hook has changed
    since, so Codex won't run it), `missing` or `disabled`.

    Codex records it in config.toml as hooks.state."<hooks.json>:<event label>:<group>:<handler>".trusted_hash."""
    path = hook_files()["codex"]
    config_file = codex_dir() / "config.toml"
    state: dict[str, Any] = {}
    if config_file.exists():
        with config_file.open("rb") as f:
            state = tomllib.load(f).get("hooks", {}).get("state", {})
    hooks = _load_json(path).get("hooks", {})
    result = {}
    for event, group_index, handler_index, _ in tapin_hooks("codex"):
        if event not in CODEX_TRUST_LABELS or event in result:
            continue
        label = CODEX_TRUST_LABELS[event]
        suffix = f":{label}:{group_index}:{handler_index}"
        entry = next((state[key] for key in (f"{path}{suffix}", f"{path.resolve()}{suffix}") if key in state), {})
        group = hooks[event][group_index]
        if entry.get("enabled") is False:
            result[event] = "disabled"
        elif "trusted_hash" not in entry:
            result[event] = "missing"
        elif entry["trusted_hash"] != codex_hook_hash(label, group, group["hooks"][handler_index]):
            result[event] = "changed"
        else:
            result[event] = "trusted"
    return result


def codex_trust_steps(exe: str) -> str:
    return "\n".join(
        [
            "Codex runs a hook only after you trust it. Open Codex and run:",
            "",
            "    /hooks",
            "",
            "Then trust both Tap In hooks:",
            "",
            f"    SessionStart  {_command(exe, 'codex', 'session-start')}",
            f"    Stop          {_command(exe, 'codex', 'stop')}",
            "",
            "`tapin doctor` shows whether Codex has recorded the trust.",
        ]
    )


def apply_mcp(agent: str, exe: str | None, cfg: dict[str, Any]) -> str:
    """Register (or with exe=None, unregister) the Tap In MCP server with `agent`."""
    if agent == "cursor":
        path = cursor_dir() / "mcp.json"
        if exe is None and not path.exists():
            return f"{path}: not present"
        data = _load_json(path)
        servers = data.setdefault("mcpServers", {})
        if exe is None:
            servers.pop("tapin", None)
        else:
            servers["tapin"] = {"command": exe, "args": ["mcp"]}
        return f"{path}: {'updated' if _write_json(path, data) else 'unchanged'}"

    cli = agents.get(agent).command(cfg)[:1]
    scope = ["--scope", "user"] if agent == "claude" else []
    try:
        subprocess.run([*cli, "mcp", "remove", *scope, "tapin"], capture_output=True, text=True)
        if exe is None:
            return f"{agent}: MCP server removed"
        result = subprocess.run([*cli, "mcp", "add", *scope, "tapin", "--", exe, "mcp"], capture_output=True, text=True)
    except OSError as exc:
        return f"{agent}: MCP {'registration' if exe else 'removal'} skipped, could not run `{cli[0]}`: {exc.strerror or exc}"
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


def _codex_fully_trusted() -> bool:
    try:
        states = codex_trust()
    except (OSError, ValueError):
        return False
    return bool(states) and all(state == "trusted" for state in states.values())


def install(targets: list[str] | None, cfg: dict[str, Any], mcp: bool = True, instructions: bool = False) -> list[str]:
    """Set up `targets`, or with None every agent detected on this machine."""
    messages = [ensure_launcher()]
    exe = str(launcher_path())
    if targets is None:
        targets = [name for name in agents.NAMES if detected(name, cfg)]
        for name in agents.NAMES:
            if name not in targets:
                agent = agents.get(name)
                messages.append(
                    f"{agent.display}: skipped, not detected (no {agent_dirs()[name]} and no `{agent.command(cfg)[0]}` on PATH). "
                    f"Run `tapin install --agents {name}` to set it up anyway."
                )
    for agent in targets:
        changed = apply_hooks(agent, exe, cfg)
        messages.append(f"{hook_files()[agent]}: hooks {'installed' if changed else 'already installed'}")
        if mcp:
            messages.append(apply_mcp(agent, exe, cfg))
    if instructions:
        for path in instruction_files():
            messages.append(f"{path}: {'snippet added' if apply_snippet(path, install=True) else 'snippet already present'}")
    if "codex" in targets and not _codex_fully_trusted():
        messages.append(codex_trust_steps(exe))
    return messages


def uninstall(targets: list[str] | None, cfg: dict[str, Any], mcp: bool = True) -> list[str]:
    targets = targets or list(agents.NAMES)
    messages = []
    for agent in targets:
        changed = apply_hooks(agent, None, cfg)
        messages.append(f"{hook_files()[agent]}: hooks {'removed' if changed else 'not present'}")
        if mcp:
            messages.append(apply_mcp(agent, None, cfg))
    for path in instruction_files():
        if path.exists() and apply_snippet(path, install=False):
            messages.append(f"{path}: snippet removed")
    if set(agents.NAMES) <= set(targets) and (removed := remove_launcher()):
        messages.append(removed)
    return messages


def _hook_checks(name: str, cfg: dict[str, Any], launcher: Path) -> list[tuple[str, str]]:
    agent, path = agents.get(name), hook_files()[name]
    try:
        commands = [command for *_, command in tapin_hooks(name)]
        executables = sorted({shlex.split(command)[0] for command in commands} - {str(launcher)})
    except (OSError, ValueError) as exc:
        return [(FAIL, f"{agent.display} hooks: can't read {path}: {exc}")]
    if not commands:
        if detected(name, cfg):
            return [(WARN, f"{agent.display} hooks: not installed in {path}; run `tapin install`")]
        return [(OK, f"{agent.display} hooks: not installed ({agent.display} not detected)")]
    missing = [exe for exe in executables if not Path(exe).exists()]
    if missing:
        return [(FAIL, f"{agent.display} hooks in {path} call {', '.join(missing)}, which doesn't exist; run `tapin install`")]
    if executables:
        return [(WARN, f"{agent.display} hooks in {path} still call {', '.join(executables)}; run `tapin install` to move them to the launcher")]
    return [(OK, f"{agent.display} hooks in {path} call the launcher")]


def _codex_trust_checks() -> list[tuple[str, str]]:
    try:
        states = codex_trust()
    except (OSError, ValueError) as exc:
        return [(WARN, f"Codex hook trust: can't read {codex_dir() / 'config.toml'}: {exc}")]
    messages = {
        "trusted": (OK, "trust recorded"),
        "changed": (WARN, "changed since you trusted it; run /hooks in Codex to trust it again"),
        "disabled": (WARN, "disabled; run /hooks in Codex to enable it"),
        "missing": (WARN, "not trusted yet; run /hooks in Codex"),
    }
    return [(messages[state][0], f"Codex {event} hook: {messages[state][1]}") for event, state in states.items()]


def doctor(cfg: dict[str, Any]) -> list[tuple[str, str]]:
    """Read-only checks, each (status, message) with status ok, warn or FAIL."""
    checks: list[tuple[str, str]] = []
    try:
        checks.append((OK, f"tapin executable: {tapin_executable()}"))
    except RuntimeError as exc:
        checks.append((FAIL, str(exc)))

    launcher, target = launcher_path(), launcher_target()
    if target is None:
        checks.append((FAIL, f"launcher {launcher} is missing; run `tapin install`"))
    elif not (target.is_file() and os.access(target, os.X_OK)):
        checks.append((FAIL, f"launcher {launcher} points at {target}, which isn't an executable; run `tapin install`"))
    else:
        checks.append((OK, f"launcher {launcher} runs {target}"))

    digest_agents = [agents.get(name).display for name, reader in cfg["readers"].items() if reader == "continues" and name in agents.REGISTRY]
    if digest_agents:
        reader_command = cfg["continues"]["command"][0]
        names = " and ".join(digest_agents)
        if shutil.which(reader_command):
            checks.append((OK, f"session reader `{reader_command}` on PATH (session digests for {names})"))
        else:
            checks.append(
                (
                    WARN,
                    f"`{reader_command}` not found, so {names} handoffs have no session digest. They still work, with git state, "
                    "the last message, checkpoints and the plan. Install Node.js 22 or newer to add the digest.",
                )
            )

    for name, agent in agents.REGISTRY.items():
        command = agent.command(cfg)[0]
        if shutil.which(command):
            checks.append((OK, f"{agent.display} command `{command}`"))
        else:
            checks.append((WARN, f"{agent.display} command `{command}` not found; `tapin to {name}` needs it"))
        checks += _hook_checks(name, cfg, launcher)
        if name == "codex":
            checks += _codex_trust_checks()

    log = config.tapin_home() / "hooks.log"
    failures = log.read_text().count(" failed\n") if log.exists() else 0
    checks.append((OK if failures == 0 else FAIL, f"hook failures logged in {log}: {failures}"))
    return checks
