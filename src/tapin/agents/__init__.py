from tapin.agents.base import Agent, StartEvent, StopEvent
from tapin.agents.claude import Claude
from tapin.agents.codex import Codex
from tapin.agents.cursor import Cursor

REGISTRY: dict[str, Agent] = {agent.name: agent for agent in (Claude(), Codex(), Cursor())}
NAMES = tuple(REGISTRY)


def get(name: str) -> Agent:
    try:
        return REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown agent {name!r}; expected one of: {', '.join(NAMES)}") from None


__all__ = ["NAMES", "REGISTRY", "Agent", "StartEvent", "StopEvent", "get"]
