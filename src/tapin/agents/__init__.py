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


def display(name: str) -> str:
    """The agent's product name, or the name as given for an agent Tap In has no adapter for."""
    return REGISTRY[name].display if name in REGISTRY else name


__all__ = ["NAMES", "REGISTRY", "Agent", "StartEvent", "StopEvent", "display", "get"]
