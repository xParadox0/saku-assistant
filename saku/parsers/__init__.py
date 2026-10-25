"""Statement parsers. Parsers read a file and return RawTxn rows; they never touch the database."""
from __future__ import annotations

from pathlib import Path

from .base import ParseError, StatementParser
from .bca_pdf import PASSWORD_MESSAGE, BcaPdfParser
from .saku_csv import SakuCsvParser

PARSERS: list[StatementParser] = [SakuCsvParser(), BcaPdfParser()]


def needs_password(path: Path) -> bool:
    """True for a PDF that cannot be opened with an empty password. Independent of any bank."""
    if path.suffix.lower() != ".pdf":
        return False
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        return bool(reader.is_encrypted and not reader.decrypt(""))
    except Exception:
        return False


def pick(path: Path, provider: str) -> StatementParser | None:
    """First parser that recognises the file."""
    for p in PARSERS:
        if p.can_parse(path):
            return p
    return None


def parse_for_account(parser: StatementParser, path: Path, account: dict):
    """Parse and refuse a bank statement that belongs to another bank or another account number."""
    if isinstance(parser, BcaPdfParser):
        if account.get("provider") != "bca":
            raise ParseError("this is a BCA e-statement; upload it to a BCA account")
        stored = account.get("last4")
        if not stored:
            raise ParseError("save this BCA account's last 4 digits before importing a statement. Nothing was imported")
        statement = parser.parse_statement(path)
        if not statement.account_last4:
            raise ParseError("the account number could not be read from this statement, so it cannot be "
                             "matched to this account. Nothing was imported")
        if stored != statement.account_last4:
            raise ParseError("the last 4 digits of this statement do not match this account. Nothing was imported")
        return statement.rows
    return parser.parse(path)


__all__ = ["ParseError", "StatementParser", "PARSERS", "PASSWORD_MESSAGE", "needs_password", "pick",
           "parse_for_account"]
