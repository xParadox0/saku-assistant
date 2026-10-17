"""Statement parsers. Parsers read a file and return RawTxn rows; they never touch the database."""
from __future__ import annotations

from pathlib import Path

from .base import ParseError, StatementParser
from .saku_csv import SakuCsvParser

PARSERS: list[StatementParser] = [SakuCsvParser()]


def pick(path: Path, provider: str) -> StatementParser | None:
    """First parser that recognises the file. Provider-specific parsers are added in Stage 1."""
    for p in PARSERS:
        if p.can_parse(path):
            return p
    return None


__all__ = ["ParseError", "StatementParser", "PARSERS", "pick"]
