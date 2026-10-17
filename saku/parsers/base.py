from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..normalise import RawTxn


class ParseError(Exception):
    """Raised with a message that is safe to show the user. Never include row contents in it."""


class StatementParser(Protocol):
    name: str

    def can_parse(self, path: Path) -> bool: ...

    def parse(self, path: Path) -> list[RawTxn]: ...
