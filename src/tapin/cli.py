"""`tapin` command line."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

from tapin import agents, capture, config, deliver, hooks, install, packet, workspace
from tapin.agents.base import StopEvent
from tapin.store import Store


def cmd_capture(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    root = workspace.find_root(args.cwd)
    stop = capture.resolve_stop(root, cfg, source=args.source, session_id=args.session, reason=args.reason)
    if stop is None:
        print(f"No agent session found for {root}. Pass --from and --session.", file=sys.stderr)
        return 1
    store, handoff_id = capture.capture(stop, cfg)
    print(store.handoff_file(handoff_id))
    return 0


def cmd_to(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    root = workspace.find_root(args.cwd)
    target = agents.get(args.target)
    stop = capture.resolve_stop(
        root, cfg, source=args.source, session_id=args.session, exclude=None if args.source else target.name
    )
    if stop is None:
        print(f"No session from another agent found for {root}. Pass --from.", file=sys.stderr)
        return 1

    pending = Store(root).claimable()
    if stop.agent not in agents.REGISTRY and pending:
        # Written through MCP by an agent Tap In has no log reader for; nothing to re-read.
        store, handoff_id = Store(root), pending.id
    else:
        store, handoff_id = capture.capture(stop, cfg)
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


def cmd_hook(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    try:
        raw = sys.stdin.read()
        output = hooks.handle(args.agent, args.event, json.loads(raw) if raw.strip() else {}, cfg)
    except Exception:
        hooks.log_exception(args.agent, args.event)
        output = hooks.fallback_output(args.event)
    if output:
        print(json.dumps(output))
    return 0


def cmd_capture_event(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    hooks.run_capture(StopEvent.from_json(sys.stdin.read()), cfg)
    return 0


def _targets(value: str) -> list[str]:
    names = [name.strip() for name in value.split(",") if name.strip()]
    for name in names:
        agents.get(name)
    return names


def cmd_install(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    for message in install.install(args.agents, cfg, mcp=not args.no_mcp, instructions=args.instructions):
        print(message)
    return 0


def cmd_uninstall(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    for message in install.uninstall(args.agents, cfg, mcp=not args.no_mcp):
        print(message)
    return 0


def cmd_doctor(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    checks = install.doctor(cfg)
    for ok, message in checks:
        print(f"{'ok  ' if ok else 'FAIL'} {message}")
    return 0 if all(ok for ok, _ in checks) else 1


def cmd_mcp(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    from tapin import mcp_server

    mcp_server.run()
    return 0


def build_parser() -> argparse.ArgumentParser:
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
    p.add_argument("--agents", type=_targets, default=list(agents.NAMES), help=f"comma-separated (default: {all_agents})")
    p.add_argument("--no-mcp", action="store_true", help="skip MCP server registration")
    p.add_argument("--instructions", action="store_true", help="also add a Tap In snippet to ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="remove Tap In hooks, MCP registration and snippets")
    p.add_argument("--agents", type=_targets, default=list(agents.NAMES), help=f"comma-separated (default: {all_agents})")
    p.add_argument("--no-mcp", action="store_true", help="leave MCP registrations alone")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("doctor", help="check that readers, launchers and hooks are in place")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("mcp", help="run the Tap In MCP server over stdio")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("hook", help="entry point for agent hooks (reads the hook payload on stdin)")
    p.add_argument("agent", choices=agents.NAMES)
    p.add_argument("event")
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("capture-event", help="internal: capture a StopEvent read from stdin")
    p.set_defaults(func=cmd_capture_event)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args, config.load())


if __name__ == "__main__":
    raise SystemExit(main())
