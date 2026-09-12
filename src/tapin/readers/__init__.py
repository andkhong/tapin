from typing import Any

from tapin.readers.base import Reader, ReaderError, SessionRef
from tapin.readers.continues import ContinuesReader
from tapin.readers.journal import JournalReader


def get(name: str, cfg: dict[str, Any]) -> Reader:
    if name == "continues":
        spec = cfg["continues"]
        return ContinuesReader(spec["command"], spec["timeout_seconds"])
    if name == "journal":
        return JournalReader()
    raise ValueError(f"unknown reader {name!r}")


__all__ = ["Reader", "ReaderError", "SessionRef", "get"]
