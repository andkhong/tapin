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

from tapin import agents, config, statusline, workspace
from tapin.store import utcnow

OURS = re.compile(rf"tapin'? hook (?:{'|'.join(re.escape(name) for name in agents.NAMES)}) ")
EPHEMERAL_DIRS = {"archive-v0", "_npx"}
CODEX_TRUST_LABELS = {"SessionStart": "session_start", "Stop": "stop", "PostToolUse": "post_tool_use"}
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
            "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": _command(exe, agent, "post-tool-use")}]}],
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
            "PostToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": _command(exe, agent, "post-tool-use"), "statusMessage": "Checking usage"}]}
            ],
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


def _update_hooks(data: dict[str, Any], agent: str, exe: str | None, cfg: dict[str, Any]) -> bool:
    """Put Tap In's hooks for `agent` into a hooks file's contents, or take them out when `exe` is None. Returns True
    if the hooks changed."""
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
    return hooks != existing


def apply_hooks(agent: str, exe: str | None, cfg: dict[str, Any]) -> bool:
    """Install Tap In's hooks for `agent`, or remove them when `exe` is None. Returns True if the file changed."""
    path = hook_files()[agent]
    if exe is None and not path.exists():
        return False
    data = _load_json(path)
    _update_hooks(data, agent, exe, cfg)
    return _write_json(path, data)


def apply_claude(exe: str | None, cfg: dict[str, Any], status_line: bool = True) -> list[str]:
    """Claude Code's hooks and status line (or with exe=None, their removal), in one write so settings.json is
    backed up once."""
    path = hook_files()["claude"]
    if exe is None and not path.exists():
        return [f"{path}: hooks not present"]
    data = _load_json(path)
    hooks_changed = _update_hooks(data, "claude", exe, cfg)
    line = _update_statusline(data, exe) if status_line else None
    _write_json(path, data)
    if exe is None:
        messages = [f"{path}: hooks {'removed' if hooks_changed else 'not present'}"]
    else:
        messages = [f"{path}: hooks {'installed' if hooks_changed else 'already installed'}"]
    return messages + ([f"{path}: {line}"] if line else [])


def _describe(status_line: Any) -> str:
    command = statusline.command_of(status_line)
    return f"`{command}`" if command else json.dumps(status_line)


def _our_statusline(exe: str, replaced: Any = None) -> dict[str, Any]:
    """Tap In's status line, keeping the replaced one's other settings such as `padding` and `refreshInterval`."""
    kept = {key: value for key, value in replaced.items() if key not in ("type", "command")} if isinstance(replaced, dict) else {}
    return {"type": "command", "command": f"{shlex.quote(exe)} statusline", **kept}


def _update_statusline(data: dict[str, Any], exe: str | None) -> str | None:
    """Point Claude Code's user status line at `tapin statusline`, which runs and shows a status line the user already
    had (recorded in ~/.tapin/statusline.json); with exe=None, put theirs back. Returns what changed."""
    current = data.get("statusLine")
    record = statusline.load_record()
    if exe is None:
        original = record.pop("original", None)
        statusline.save_record(record)
        if not statusline.is_ours(current):
            return None
        if original is None:
            del data["statusLine"]
            return "status line removed"
        data["statusLine"] = original
        return f"status line restored to {_describe(original)}"
    if statusline.is_ours(current):
        wanted = _our_statusline(exe, current)
        if wanted == current:
            return "status line already installed"
        data["statusLine"] = wanted
        return "status line updated"
    if current is None:
        if record.pop("original", None) is not None:
            statusline.save_record(record)
        data["statusLine"] = _our_statusline(exe)
        return "status line installed (it shows usage, and Claude Code's usage warnings need it)"
    record["original"] = current
    statusline.save_record(record)
    data["statusLine"] = _our_statusline(exe, current)
    return f"status line installed; it runs your previous one ({_describe(current)}) and shows its output"


def project_statusline(directory: Path) -> Any:
    """The status line a project's own settings set, if any. Claude Code lets .claude/settings.local.json override
    .claude/settings.json, and either one override the user's settings, where Tap In's is."""
    for name in ("settings.local.json", "settings.json"):
        line = _load_json(directory / ".claude" / name).get("statusLine")
        if line is not None:
            return line
    return None


def wrap_project_statusline(directory: Path) -> str:
    """Set Tap In's status line in the project's settings.local.json, running the project's own, so that usage
    warnings also work in a project whose status line would otherwise override Tap In's."""
    directory = directory.expanduser().resolve()
    local = directory / ".claude" / "settings.local.json"
    current = project_statusline(directory)
    record = statusline.load_record()
    projects = record.get("projects") if isinstance(record.get("projects"), dict) else {}
    if current is None:
        return f"{directory} sets no status line of its own, so none is needed: Tap In's status line runs there."
    if statusline.is_ours(current):
        entry = projects.get(str(directory))
        runs = f" and runs {_describe(entry['original'])}" if isinstance(entry, dict) and "original" in entry else ""
        return f"{local}: the status line is already Tap In's{runs}"
    launcher = launcher_path()
    if not launcher.exists():
        raise RuntimeError(f"Tap In's launcher {launcher} is missing; run `tapin install` first.")
    data = _load_json(local)
    entry: dict[str, Any] = {"original": current}
    if "statusLine" in data:
        entry["previous"] = data["statusLine"]
    if not local.exists():
        entry["created"] = True
    record["projects"] = {**projects, str(directory): entry}
    statusline.save_record(record)
    data["statusLine"] = _our_statusline(str(launcher), current)
    _write_json(local, data)
    if entry.get("created"):
        # Claude Code keeps the settings.local.json it creates out of git; this one holds a path on this machine.
        root = workspace.find_root(directory)
        workspace.ensure_excluded(root, "/" + local.relative_to(root).as_posix())
    return f"{local}: status line set to Tap In's, which runs this project's own ({_describe(current)}) and shows its output"


def unwrap_project_statusline(directory: Path) -> str:
    directory = directory.expanduser().resolve()
    local = directory / ".claude" / "settings.local.json"
    record = statusline.load_record()
    projects = dict(record.get("projects") or {})
    entry = projects.pop(str(directory), None)
    if not isinstance(entry, dict):
        return f"{directory}: Tap In hasn't wrapped this project's status line"
    data = _load_json(local)
    if not statusline.is_ours(data.get("statusLine")):
        message = f"{local}: the status line isn't Tap In's any more, so it was left alone"
    else:
        if "previous" in entry:
            data["statusLine"] = entry["previous"]
            message = f"{local}: status line restored to {_describe(entry['previous'])}"
        else:
            del data["statusLine"]
            message = f"{local}: Tap In's status line removed, so the project's own applies again"
        if not data and entry.get("created"):
            local.unlink()
        else:
            _write_json(local, data)
    if projects:
        record["projects"] = projects
    else:
        record.pop("projects", None)
    statusline.save_record(record)
    return message


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
            "Then trust the three Tap In hooks:",
            "",
            f"    SessionStart  {_command(exe, 'codex', 'session-start')}",
            f"    Stop          {_command(exe, 'codex', 'stop')}",
            f"    PostToolUse   {_command(exe, 'codex', 'post-tool-use')}",
            "",
            "`tapin doctor` shows whether Codex has recorded the trust. PostToolUse warns Codex before a usage limit.",
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


def codex_hooks_behind_others() -> list[str]:
    """Codex events where another tool's hooks (such as Graft's) come before Tap In's in hooks.json. Codex keys hook
    trust to group and handler positions, so a hook added or removed ahead of Tap In's moves it and Codex stops
    trusting it."""
    first: dict[str, tuple[int, int | None]] = {}
    for event, group_index, handler_index, _ in tapin_hooks("codex"):
        first.setdefault(event, (group_index, handler_index))
    return [event for event, (group_index, handler_index) in first.items() if group_index or handler_index]


def install(targets: list[str] | None, cfg: dict[str, Any], mcp: bool = True, instructions: bool = False, status_line: bool = True) -> list[str]:
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
        if agent == "claude":
            messages += apply_claude(exe, cfg, status_line)
        else:
            changed = apply_hooks(agent, exe, cfg)
            messages.append(f"{hook_files()[agent]}: hooks {'installed' if changed else 'already installed'}")
            if agent == "codex":
                messages += [
                    f"`{event}` in {hook_files()[agent]} has another tool's hooks before Tap In's; "
                    "if Codex shows Tap In's hook as untrusted in /hooks, trust it again."
                    for event in codex_hooks_behind_others()
                ]
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
        if agent == "claude":
            messages += apply_claude(None, cfg)
            messages += [unwrap_project_statusline(Path(directory)) for directory in list(statusline.load_record().get("projects") or {})]
        else:
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
        if name == "claude":
            checks += _statusline_checks(cfg) + _project_statusline_checks(Path.cwd())
        if name == "codex":
            checks += _codex_trust_checks()

    log = config.tapin_home() / "hooks.log"
    logged = log.read_text() if log.exists() else ""
    failures = logged.count(" failed\n")
    checks.append((OK if failures == 0 else FAIL, f"hook failures logged in {log}: {failures}"))
    unfinished = logged.count(statusline.LOG_MARKER)
    if unfinished:
        checks.append(
            (WARN, f"your previous status line command timed out or couldn't run {unfinished} time(s); Tap In showed its usage line instead (see {log})")
        )
    return checks


def _statusline_checks(cfg: dict[str, Any]) -> list[tuple[str, str]]:
    path = hook_files()["claude"]
    try:
        current = _load_json(path).get("statusLine")
        original = statusline.load_record().get("original")
        installed = bool(tapin_hooks("claude"))
    except (OSError, ValueError) as exc:
        return [(WARN, f"Claude Code status line: can't read {path} or {statusline.record_path()}: {exc}")]
    checks = []
    if statusline.is_ours(current):
        runs = f" and runs your previous one ({_describe(original)})" if original is not None else ""
        checks.append((OK, f"Claude Code status line in {path} is Tap In's{runs}. Claude Code's usage warnings need it, and a Pro or Max plan"))
    elif installed or detected("claude", cfg):
        what = "isn't set" if current is None else f"is {_describe(current)}, not Tap In's"
        checks.append((WARN, f"Claude Code status line in {path} {what}, so Claude Code gets no usage warnings (they need Tap In's status line); run `tapin install`"))
    if not cfg.get("warn_thresholds"):
        checks.append((OK, "usage warnings are off because warn_thresholds is empty"))
    return checks


def _project_statusline_checks(directory: Path) -> list[tuple[str, str]]:
    """A status line in the project's own settings overrides Tap In's user-level one, which then never runs there."""
    try:
        if (directory / ".claude").resolve() == claude_dir().resolve():
            return []
        current = project_statusline(directory)
    except (OSError, ValueError):
        return []
    if current is None:
        return []
    if statusline.is_ours(current):
        return [(OK, f"this project's status line ({directory / '.claude'}) is Tap In's, so usage warnings work here")]
    return [
        (
            WARN,
            f"Claude Code warnings before the limit are off in this project ({directory}) because its own status line overrides "
            "Tap In's; run `tapin statusline --project`",
        )
    ]
