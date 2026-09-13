"""`tapin` command line.

`tapin hook` runs on every agent hook event, so it is handled before argparse, and each command imports only the
modules it uses."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tapin import config

if TYPE_CHECKING:
    import argparse


def cmd_capture(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    from tapin import capture, workspace

    root = workspace.find_root(args.cwd)
    resolved = capture.resolve_stop(root, cfg, source=args.source, session_id=args.session, reason=args.reason)
    if resolved is None:
        print(f"No agent session found for {root}. Pass --from and --session.", file=sys.stderr)
        return 1
    stop, ref = resolved
    store, handoff_id = capture.capture(stop, cfg, ref=ref)
    print(store.handoff_file(handoff_id))
    return 0


def cmd_to(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    from tapin import agents, capture, deliver, packet, workspace
    from tapin.store import Store

    root = workspace.find_root(args.cwd)
    target = agents.get(args.target)
    resolved = capture.resolve_stop(
        root, cfg, source=args.source, session_id=args.session, exclude=None if args.source else target.name
    )
    if resolved is None:
        print(f"No session from another agent found for {root}. Pass --from.", file=sys.stderr)
        return 1
    stop, ref = resolved

    pending = Store(root).claimable()
    if stop.agent not in agents.REGISTRY and pending:
        # Written through MCP by an agent Tap In has no log reader for; nothing to re-read.
        store, handoff_id = Store(root), pending.id
    else:
        store, handoff_id = capture.capture(stop, cfg, ref=ref)
    handoff_file = store.handoff_file(handoff_id)
    prompt = packet.launch_prompt(store.read_meta(handoff_id), handoff_file)
    argv = target.launch_argv(prompt, cfg)

    if args.print:
        copied = deliver.copy_to_clipboard(prompt)
        print(f"Handoff: {handoff_file}")
        print(f"Launch:  {shlex.join(argv)}")
        print("Prompt copied to clipboard." if copied else f"Prompt: {prompt}")
        print(f"Or open {target.display} in {root}; its session-start hook will load the handoff.")
        return 0

    if prompt in argv:
        store.claim(target.name, session_id=None)
        print(f"Handing off to {target.display}: {handoff_file}", file=sys.stderr)
    else:
        # The target opens without our prompt; leave the handoff unclaimed for its session-start hook.
        deliver.copy_to_clipboard(prompt)
        print(f"Opening {target.display}; the handoff loads when you start a new chat (prompt copied): {handoff_file}", file=sys.stderr)
    try:
        deliver.launch(argv, root)
    except FileNotFoundError:
        print(f"Could not find `{argv[0]}`. Set agents.{target.name}.command in {config.tapin_home() / 'config.toml'}.", file=sys.stderr)
        return 1
    return 0


def cmd_status(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    from tapin import workspace
    from tapin.store import Store

    root = workspace.find_root(args.cwd)
    store = Store(root)
    pending = store.pending()
    print(f"Workspace: {root}")
    if pending is None:
        print("Pending:   none")
    else:
        if pending.claimed_by:
            state = f"claimed by {pending.claimed_by['agent']} at {pending.claimed_by['at']}"
        elif pending.expired():
            state = "expired"
        else:
            state = f"waiting until {pending.expires_at}"
        print(f"Pending:   {pending.id} from {pending.from_agent} ({state})")
    for meta in store.list_handoffs()[:5]:
        print(f"  {meta['id']}  {meta['reason']}  {store.handoff_file(meta['id'])}")
    return 0


def run_hook(args: list[str]) -> int:
    """`tapin hook <agent> <event>` with the payload on stdin. A hook must never break the agent that runs it, so
    any error is logged, the event's fallback output is printed, and the exit status is always 0."""
    from tapin import hooks

    agent = args[0] if args else "unknown"
    event = args[1] if len(args) > 1 else "unknown"
    try:
        if len(args) != 2:
            raise ValueError(f"expected `tapin hook <agent> <event>`, got {args}")
        raw = sys.stdin.read()
        output = hooks.handle(agent, event, json.loads(raw) if raw.strip() else {}, config.load())
    except Exception:
        hooks.log_exception(agent, event)
        output = hooks.fallback_output(event)
    if output:
        print(json.dumps(output))
    return 0


def run_statusline() -> int:
    """`tapin statusline`, Claude Code's status line command, with the payload on stdin. Like a hook it must never
    break Claude Code, so on any error it prints nothing, logs, and exits 0."""
    try:
        from tapin import statusline

        stdin = getattr(sys.stdin, "buffer", None)
        output = statusline.render(stdin.read() if stdin is not None else sys.stdin.read().encode())
    except Exception:
        from tapin import hooks

        hooks.log_exception("claude", "statusline")
        return 0
    stdout = getattr(sys.stdout, "buffer", None)
    if stdout is None:
        sys.stdout.write(output.decode(errors="replace"))
    else:
        sys.stdout.flush()
        stdout.write(output)
        stdout.flush()
    return 0


def cmd_statusline(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    from tapin import install

    if not args.project:
        print("`tapin statusline` reads a Claude Code status line payload on stdin. To wrap a project's own status line, run `tapin statusline --project [dir]`.", file=sys.stderr)
        return 2
    try:
        message = install.unwrap_project_statusline(args.dir) if args.remove else install.wrap_project_statusline(args.dir)
    except (RuntimeError, OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(message)
    return 0


def cmd_checkpoint(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    from tapin import mcp_server

    saved = mcp_server.checkpoint(
        str(args.workspace),
        args.agent,
        args.summary,
        args.next_steps,
        decisions=args.decisions,
        in_progress=args.in_progress,
        session_id=args.session,
        model=args.model,
        effort=args.effort,
    )
    print(saved)
    return 0


def cmd_capture_event(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    from tapin import hooks
    from tapin.agents.base import StopEvent

    stop = None
    try:
        stop = StopEvent.from_json(sys.stdin.read())
        hooks.run_capture(stop, cfg)
    except Exception:
        hooks.log_exception(stop.agent if stop else "unknown", "capture-event")
        return 1
    return 0


def _targets(value: str) -> list[str]:
    from tapin import agents

    names = [name.strip() for name in value.split(",") if name.strip()]
    for name in names:
        agents.get(name)
    return names


def cmd_install(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    from tapin import install

    try:
        messages = install.install(args.agents, cfg, mcp=not args.no_mcp, instructions=args.instructions, status_line=not args.no_statusline)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    for message in messages:
        print(message)
    return 0


def cmd_uninstall(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    from tapin import install

    for message in install.uninstall(args.agents, cfg, mcp=not args.no_mcp):
        print(message)
    return 0


def cmd_doctor(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    from tapin import install

    checks = install.doctor(cfg)
    for status, message in checks:
        print(f"{status:<4} {message}")
    return 1 if any(status == install.FAIL for status, _ in checks) else 0


def cmd_mcp(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    from tapin import mcp_server

    mcp_server.run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    import argparse

    from tapin import agents

    parser = argparse.ArgumentParser(prog="tapin", description="Hand off in-progress work between AI coding agents.")
    sub = parser.add_subparsers(dest="command", required=True)
    all_agents = ",".join(agents.NAMES)

    p = sub.add_parser("capture", help="write a handoff for the newest (or given) agent session in this workspace")
    p.add_argument("--from", dest="source", choices=agents.NAMES)
    p.add_argument("--session", help="session id to hand off")
    p.add_argument("--reason", default="manual")
    p.add_argument("--cwd", type=Path, default=Path.cwd())
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("to", help="hand off to another agent and launch it here")
    p.add_argument("target", choices=agents.NAMES)
    p.add_argument("--from", dest="source", choices=agents.NAMES)
    p.add_argument("--session", help="session id to hand off")
    p.add_argument("--print", action="store_true", help="don't launch; print the command and copy the prompt (for desktop apps)")
    p.add_argument("--cwd", type=Path, default=Path.cwd())
    p.set_defaults(func=cmd_to)

    p = sub.add_parser("status", help="show the pending handoff and recent handoffs")
    p.add_argument("--cwd", type=Path, default=Path.cwd())
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("install", help="install hooks (and MCP server) into agent configs")
    p.add_argument("--agents", type=_targets, help=f"comma-separated, from {all_agents} (default: the agents found on this machine)")
    p.add_argument("--no-mcp", action="store_true", help="skip MCP server registration")
    p.add_argument("--instructions", action="store_true", help="also add a Tap In snippet to ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md")
    p.add_argument("--no-statusline", action="store_true", help="don't set Tap In's Claude Code status line (Claude Code then gets no usage warnings)")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="remove Tap In hooks, status line, MCP registration, snippets and (for all agents) the launcher")
    p.add_argument("--agents", type=_targets, help=f"comma-separated (default: {all_agents})")
    p.add_argument("--no-mcp", action="store_true", help="leave MCP registrations alone")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("doctor", help="check the launcher, hooks, status line, Codex hook trust and session readers")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("checkpoint", help="record progress for whoever continues this work (the MCP checkpoint tool, from the shell)")
    p.add_argument("--agent", required=True, help="the agent recording it, e.g. claude or codex")
    p.add_argument("--summary", required=True, help="what is done so far")
    p.add_argument("--in-progress", default="", help="the file and step being worked on")
    p.add_argument("--next-steps", required=True, help="the exact next step")
    p.add_argument("--decisions", default="", help="key decisions and why")
    p.add_argument("--session", help="the session recording it (default for claude and codex: the only one active in the workspace, if just one is)")
    p.add_argument("--model", help="the model (default for claude and codex: read from the session log)")
    p.add_argument("--effort", help="the reasoning effort (default for claude and codex: read from the session log)")
    p.add_argument("--workspace", type=Path, default=Path.cwd())
    p.set_defaults(func=cmd_checkpoint)

    p = sub.add_parser("statusline", help="Claude Code's status line command (payload on stdin); --project wraps a project's own status line")
    p.add_argument("--project", action="store_true", help="wrap the status line set in DIR/.claude/settings(.local).json, which overrides Tap In's")
    p.add_argument("--remove", action="store_true", help="with --project, put the project's status line back")
    p.add_argument("dir", nargs="?", type=Path, default=Path.cwd(), help="the project directory (default: the current directory)")
    p.set_defaults(func=cmd_statusline)

    p = sub.add_parser("mcp", help="run the Tap In MCP server over stdio")
    p.set_defaults(func=cmd_mcp)

    sub.add_parser("hook", help="entry point for agent hooks: tapin hook <agent> <event>, payload on stdin")

    p = sub.add_parser("capture-event", help="internal: capture a StopEvent read from stdin")
    p.set_defaults(func=cmd_capture_event)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:
        return run_hook(argv[1:])
    if argv == ["statusline"]:
        return run_statusline()
    args = build_parser().parse_args(argv)
    return args.func(args, config.load())


if __name__ == "__main__":
    raise SystemExit(main())
