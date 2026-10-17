"""Saku's own CSV format, so any account can be imported before its bank-specific parser exists.

    date,amount,description[,kind]
    2026-07-01,15000000,SALARY
    01/07/2026,-45000,LUNCH

date      YYYY-MM-DD or DD/MM/YYYY
amount    plain number, negative for money out, up to 2 decimals, no thousands separators
kind      optional: normal, fee or refund
"""
from __future__ import annotations

import csv
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ..normalise import RawTxn
from .base import ParseError

HEADER = ["date", "amount", "description"]
MAX_ROWS = 5000
_AMOUNT = re.compile(r"^-?\d{1,15}(\.\d{1,2})?$")
_ALLOWED_KINDS = {"normal", "fee", "refund"}  # own_transfer is detected, never imported


def _date(text: str) -> date:
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError


class SakuCsvParser:
    name = "saku-csv/1"

    def can_parse(self, path: Path) -> bool:
        if path.suffix.lower() != ".csv":
            return False
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                first = next(csv.reader(f), [])
        except (UnicodeDecodeError, OSError):
            return False
        return [c.strip().lower() for c in first[:3]] == HEADER

    def parse(self, path: Path) -> list[RawTxn]:
        rows: list[RawTxn] = []
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                reader.fieldnames = [h.strip().lower() for h in (reader.fieldnames or [])]
                for line, rec in enumerate(reader, start=2):
                    if len(rows) >= MAX_ROWS:
                        raise ParseError(f"more than {MAX_ROWS} rows; split the file")
                    try:
                        booked_on = _date(rec.get("date") or "")
                    except ValueError:
                        raise ParseError(f"line {line}: date must be YYYY-MM-DD or DD/MM/YYYY") from None
                    amount = (rec.get("amount") or "").strip()
                    if not _AMOUNT.match(amount):
                        raise ParseError(f"line {line}: amount must be a plain number such as -45000 or 1500000.50")
                    description = (rec.get("description") or "").strip()
                    if not description:
                        raise ParseError(f"line {line}: description is empty")
                    kind = (rec.get("kind") or "normal").strip().lower() or "normal"
                    if kind not in _ALLOWED_KINDS:
                        raise ParseError(f"line {line}: kind must be normal, fee or refund")
                    rows.append(RawTxn(booked_on, Decimal(amount), description, kind))
        except UnicodeDecodeError:
            raise ParseError("file is not UTF-8 text") from None
        if not rows:
            raise ParseError("the file has a header but no rows")
        return rows
