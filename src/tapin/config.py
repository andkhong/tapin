"""User configuration loaded from ~/.tapin/config.toml, layered over defaults."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "handoff_ttl_hours": 12,
    "diff_max_chars": 20_000,
    "digest_max_chars": 12_000,
    "brief_max_chars": 3_500,
    # Claude StopFailure `error` values that count as hitting a limit.
    "limit_errors": ["rate_limit"],
    # Matched against free-text error messages (Cursor sessionEnd).
    "limit_message_pattern": r"(usage|rate)[ _-]?limit|quota|too many requests|\b429\b",
    # Usage percentages at which a Claude Code or Codex agent is told to record a checkpoint; [] turns warnings off.
    "warn_thresholds": [90, 97],
    # Which reader summarizes each agent's session. `claude-log` and `codex-log` read Claude Code's and Codex's
    # own session logs and need nothing installed; `journal` renders what Tap In's own hooks recorded (Cursor).
    # `continues` (npm, needs Node.js) is optional and can be chosen for Claude Code or Codex instead.
    "readers": {"claude": "claude-log", "codex": "codex-log", "cursor": "journal"},
    "continues": {"command": ["npx", "-y", "continues@4.1.1"], "timeout_seconds": 180},
    "agents": {
        "claude": {"command": ["claude"]},
        "codex": {"command": ["codex"], "fallback": "/Applications/ChatGPT.app/Contents/Resources/codex"},
        "cursor": {"command": ["cursor", "agent"]},
    },
}


def tapin_home() -> Path:
    return Path(os.environ.get("TAPIN_HOME", "~/.tapin")).expanduser()


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load(path: Path | None = None) -> dict[str, Any]:
    path = path or tapin_home() / "config.toml"
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    import tomllib

    with path.open("rb") as f:
        return _merge(DEFAULTS, tomllib.load(f))
