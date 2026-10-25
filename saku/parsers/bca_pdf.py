"""BCA e-statement (PDF) parser.

Layout, measured from a real statement (the real file never enters the repository):

    TANGGAL  KETERANGAN                        CBG    MUTASI           SALDO
    02/09    TRSF E-BANKING CR  0209/FTSCY/..         1,500,000.00     1,750,000.00
                                1500000.00                            <- repeat of the amount
                                NAME OF SENDER
    02/09    TRSF E-BANKING DB  ...                   1,500,000.00 DB  <- no SALDO: not last row of the day
    ...
             SALDO AWAL  : 250,000.00
             MUTASI CR   : 1,775,312.50  3
             MUTASI DB   : 1,721,062.50  7
             SALDO AKHIR : 304,250.00

Rules:
- A row starts with a DD/MM date in the left column. Every line until the next date belongs to it.
- The MUTASI amount is credit unless it is followed by "DB". A DB marker, MUTASI amount or SALDO amount on a
  continuation line is rejected rather than ignored, because it means the layout is not what we measured.
- SALDO is printed only on some rows; every printed SALDO must equal the running balance.
- The summary block must be present once and must agree with the rows (sums, counts, closing balance).
  Nothing may follow it. Any disagreement rejects the whole file, so a half-read statement never reaches the ledger.
- The header must name the month (PERIODE), the currency (MATA UANG: IDR) and the account number.

BCA ships the PDF encrypted with an empty user password. That opens without asking the user anything.
A PDF that needs a real password is rejected with a message telling the user to remove it first;
Saku never asks for or stores a statement password.

Error messages never contain statement content: no names, descriptions, amounts or account numbers.
The web route runs this parser in a resource-limited subprocess (saku/parsers/sandbox.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..normalise import RawTxn, valid_transaction_amount
from .base import ParseError

MAX_PAGES = 60
MAX_ROWS = 5000
MAX_CHARS_PER_PAGE = 20_000      # a real BCA page has ~2,000; this stops decompression bombs early

_DATE = re.compile(r"^(\d{2})/(\d{2})$")
_AMOUNT = re.compile(r"^\d{1,3}(,\d{3})*\.\d{2}$")
_BARE_AMOUNT = re.compile(r"^\d+\.\d{2}$")
_YEAR = re.compile(r"^(19|20)\d\d$", re.ASCII)
_COUNT = re.compile(r"^\d{1,5}$", re.ASCII)
_MONTHS = {m: i for i, m in enumerate(
    ["JANUARI", "FEBRUARI", "MARET", "APRIL", "MEI", "JUNI", "JULI", "AGUSTUS",
     "SEPTEMBER", "OKTOBER", "NOVEMBER", "DESEMBER"], start=1)}

# Column boundaries in points. Real columns: date x0~44, description 89..~300, CBG ~308,
# MUTASI right edge 437, DB marker ~442, SALDO right edge 569.
_DATE_MAX_X = 80
_DESC_MAX_X = 300
_MUTASI_MIN_X1 = 380
_MUTASI_MAX_X1 = 440
_DB_MIN_X = 438
_DB_MAX_X = 470
_SALDO_MIN_X1 = 540
_LINE_TOL = 3

# Row kinds BCA labels itself. Only money *out* with these prefixes is a fee; a credit is a refund or normal.
_FEE_PREFIXES = ("BIAYA", "PAJAK")

PASSWORD_MESSAGE = ("this PDF needs a password. Open it once in your PDF viewer, save a copy without a password, "
                    "then upload that copy. Saku never asks for your statement password")


def _money(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


@dataclass(frozen=True)
class BcaStatement:
    period: date               # first day of the statement month
    account_last4: str | None
    opening: Decimal
    closing: Decimal
    rows: list[RawTxn]


@dataclass
class _Line:
    top: float
    words: list[dict]

    def text(self, x_min: float = 0, x_max: float = 10_000) -> str:
        return " ".join(w["text"] for w in self.words if x_min <= w["x0"] < x_max)


def _is_password_error(e: BaseException) -> bool:
    from pdfminer.pdfdocument import PDFPasswordIncorrect
    wrapped = e.args[0] if e.args else None       # pdfplumber wraps pdfminer errors
    return isinstance(e, PDFPasswordIncorrect) or isinstance(wrapped, PDFPasswordIncorrect)


def _open(path: Path):
    import pdfplumber
    try:
        return pdfplumber.open(path, password="")
    except Exception as e:
        if _is_password_error(e):
            raise ParseError(PASSWORD_MESSAGE) from None
        raise ParseError("the file could not be opened as a PDF") from None


def _lines(page) -> list[_Line]:
    if len(page.chars) > MAX_CHARS_PER_PAGE:
        raise ParseError("a page has too much text to be a bank statement")
    words = sorted(page.extract_words(keep_blank_chars=False, use_text_flow=False),
                   key=lambda w: (round(w["top"]), w["x0"]))
    out: list[_Line] = []
    for w in words:
        if out and abs(out[-1].top - w["top"]) <= _LINE_TOL:
            out[-1].words.append(w)
        else:
            out.append(_Line(w["top"], [w]))
    for line in out:
        line.words.sort(key=lambda w: w["x0"])
    return out


def _header_value(lines: list[_Line], label: str) -> str | None:
    for line in lines:
        text = line.text()
        if label in text and ":" in text:
            right = line.text(x_min=400)
            value = right.split(":", 1)[-1].strip() if ":" in right else right.strip()
            if value:
                return value
    return None


def is_bca_pdf(path: Path) -> bool:
    """True for a PDF that opens with an empty password and carries BCA's generator stamp or table header."""
    from pypdf import PdfReader
    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(""):
        return False                   # needs a real password; parsers.needs_password() handles that
    meta = reader.metadata or {}
    stamp = f"{meta.get('/Creator', '')} {meta.get('/Producer', '')}".upper()
    if "BANK CENTRAL ASIA" in stamp:
        return True
    if not reader.pages:
        return False
    first = (reader.pages[0].extract_text() or "")[:MAX_CHARS_PER_PAGE].upper()
    return all(h in first for h in ("TANGGAL", "KETERANGAN", "MUTASI", "SALDO", "NO. REKENING"))


class BcaPdfParser:
    name = "bca-pdf/1"

    def can_parse(self, path: Path) -> bool:
        if path.suffix.lower() != ".pdf":
            return False
        try:
            return is_bca_pdf(path)
        except Exception:
            return False

    def parse(self, path: Path) -> list[RawTxn]:
        return self.parse_statement(path).rows

    def parse_statement(self, path: Path) -> BcaStatement:
        try:
            return self._parse_statement(path)
        except ParseError:
            raise
        except Exception:
            # never let a library message (which may quote page text) reach the user or the log
            raise ParseError("the PDF could not be read as a BCA e-statement. Nothing was imported") from None

    def _parse_statement(self, path: Path) -> BcaStatement:
        with _open(path) as pdf:
            if len(pdf.pages) > MAX_PAGES:
                raise ParseError(f"more than {MAX_PAGES} pages; upload one month at a time")
            pages = [_lines(p) for p in pdf.pages]
        if not pages:
            raise ParseError("the PDF has no pages")

        period = self._period(pages[0])
        last4 = self._last4(pages[0])
        currency = _header_value(pages[0], "MATA UANG")
        if currency is None:
            raise ParseError("MATA UANG (currency) was not found; is this a full BCA e-statement?")
        if currency.upper() != "IDR":
            raise ParseError("only IDR accounts are supported for now")

        raw_rows, opening, summary = self._rows(pages)
        if opening is None:
            raise ParseError("SALDO AWAL (opening balance) was not found; is this a full BCA e-statement?")
        if summary is None:
            raise ParseError("the summary block (SALDO AKHIR) was not found; upload the complete statement")
        if not raw_rows:
            raise ParseError("the statement has no transactions")
        if len(raw_rows) > MAX_ROWS:
            raise ParseError(f"more than {MAX_ROWS} rows; split the file")

        rows: list[RawTxn] = []
        balance = opening
        for n, r in enumerate(raw_rows, start=1):
            if r["month"] != period.month:
                raise ParseError(f"row {n}: date is outside the statement month")
            try:
                booked_on = date(period.year, r["month"], r["day"])
            except ValueError:
                raise ParseError(f"row {n}: invalid date") from None
            amount = r["amount"] if r["credit"] else -r["amount"]
            if not valid_transaction_amount(amount):
                raise ParseError(f"row {n}: amount exceeds the supported numeric range. Nothing was imported")
            balance += amount
            if r["saldo"] is not None and r["saldo"] != balance:
                raise ParseError(f"row {n}: the running balance does not match; the file may be damaged "
                                 "or not a BCA e-statement. Nothing was imported")
            kind = "fee" if amount < 0 and r["description"].upper().startswith(_FEE_PREFIXES) else "normal"
            rows.append(RawTxn(booked_on, amount, r["description"], kind))

        credits = [r for r in rows if r.amount > 0]
        debits = [r for r in rows if r.amount < 0]
        if summary["opening"] != opening:
            raise ParseError("the opening balance in the summary does not match the first page")
        if summary["cr_total"] != sum((r.amount for r in credits), Decimal(0)) \
                or summary["db_total"] != -sum((r.amount for r in debits), Decimal(0)):
            raise ParseError("the CR/DB totals in the summary do not match the rows read. Nothing was imported")
        if (summary["cr_count"] is not None and summary["cr_count"] != len(credits)) or \
                (summary["db_count"] is not None and summary["db_count"] != len(debits)):
            raise ParseError("the transaction count in the summary does not match the rows read. Nothing was imported")
        if summary["closing"] != balance:
            raise ParseError("the closing balance does not match opening balance plus all rows. Nothing was imported")
        return BcaStatement(period, last4, opening, balance, rows)

    # ---- pieces -----------------------------------------------------------------------------------------
    @staticmethod
    def _period(lines: list[_Line]) -> date:
        parts = (_header_value(lines, "PERIODE") or "").upper().split()
        if len(parts) == 2 and parts[0] in _MONTHS and _YEAR.match(parts[1]):
            return date(int(parts[1]), _MONTHS[parts[0]], 1)
        raise ParseError("PERIODE (statement month) was not found or is not a valid month and year")

    @staticmethod
    def _last4(lines: list[_Line]) -> str | None:
        value = _header_value(lines, "NO. REKENING") or ""
        digits = re.sub(r"[^0-9]", "", value)
        return digits[-4:] if len(digits) >= 4 else None

    def _rows(self, pages: list[list[_Line]]):
        rows: list[dict] = []
        opening: Decimal | None = None
        summary: dict | None = None
        current: dict | None = None

        def close():
            nonlocal current
            if current is not None:
                current["description"] = " ".join(p for p in current["parts"] if p)
                rows.append(current)
                current = None

        for lines in pages:
            in_table = False
            i = 0
            while i < len(lines):
                line = lines[i]
                i += 1
                upper = line.text().upper()
                if summary is not None:
                    # nothing but page furniture may follow the summary block
                    if self._has_row_shape(line):
                        raise ParseError("transactions appear after the summary block; upload one statement at a time")
                    continue
                if not in_table:
                    if all(h in upper for h in ("TANGGAL", "KETERANGAN", "MUTASI", "SALDO")):
                        in_table = True
                    continue
                if upper.startswith("BERSAMBUNG"):
                    break
                if self._is_summary(line):
                    close()
                    summary, used = self._summary(lines[i - 1:])
                    i += used - 1
                    continue
                first = line.words[0]
                m = _DATE.match(first["text"]) if first["x0"] < _DATE_MAX_X else None
                if m:
                    close()
                    desc = line.text(x_min=_DATE_MAX_X, x_max=_DESC_MAX_X)
                    if desc.upper().startswith("SALDO AWAL"):
                        if opening is not None:
                            raise ParseError("SALDO AWAL appears twice; upload one statement at a time")
                        saldo = self._saldo(line)
                        if saldo is None:
                            raise ParseError("SALDO AWAL has no amount")
                        opening = saldo
                        continue
                    amount, credit = self._mutasi(line)
                    if amount is None:
                        raise ParseError(f"row {len(rows) + 1}: no MUTASI amount was found")
                    current = {"day": int(m.group(1)), "month": int(m.group(2)), "amount": amount,
                               "credit": credit, "saldo": self._saldo(line), "parts": [desc]}
                elif current is not None:
                    if self._mutasi(line)[0] is not None or self._db_marker(line) or self._saldo(line) is not None:
                        raise ParseError(f"row {len(rows) + 1}: unexpected amount or DB marker on a continuation line")
                    extra = line.text(x_max=_DESC_MAX_X)
                    # the first continuation line usually repeats the amount without separators; drop it
                    tokens = [t for t in extra.split()
                              if not (_BARE_AMOUNT.match(t) and _money(t) == current["amount"])]
                    current["parts"].append(" ".join(tokens))
        close()
        return rows, opening, summary

    @staticmethod
    def _has_row_shape(line: _Line) -> bool:
        first = line.words[0]
        return bool(_DATE.match(first["text"]) and first["x0"] < _DATE_MAX_X) or \
            any(_AMOUNT.match(w["text"]) and _MUTASI_MIN_X1 <= w["x1"] <= _MUTASI_MAX_X1 for w in line.words)

    @staticmethod
    def _db_marker(line: _Line) -> bool:
        return any(w["text"] == "DB" and _DB_MIN_X <= w["x0"] <= _DB_MAX_X for w in line.words)

    @staticmethod
    def _mutasi(line: _Line) -> tuple[Decimal | None, bool]:
        for i, w in enumerate(line.words):
            if _AMOUNT.match(w["text"]) and _MUTASI_MIN_X1 <= w["x1"] <= _MUTASI_MAX_X1:
                nxt = line.words[i + 1] if i + 1 < len(line.words) else None
                debit = nxt is not None and nxt["text"] == "DB" and _DB_MIN_X <= nxt["x0"] <= _DB_MAX_X
                return _money(w["text"]), not debit
        return None, True

    @staticmethod
    def _saldo(line: _Line) -> Decimal | None:
        for w in reversed(line.words):
            if _AMOUNT.match(w["text"]) and w["x1"] >= _SALDO_MIN_X1:
                return _money(w["text"])
        return None

    @staticmethod
    def _is_summary(line: _Line) -> bool:
        text = line.text().upper()
        return text.startswith("SALDO AWAL") and ":" in text and line.words[0]["x0"] > _DATE_MAX_X

    @staticmethod
    def _summary(lines: list[_Line]) -> tuple[dict, int]:
        found: dict = {"cr_count": None, "db_count": None}
        keys = {"SALDO AWAL": "opening", "MUTASI CR": "cr_total", "MUTASI DB": "db_total", "SALDO AKHIR": "closing"}
        used = 0
        for line in lines[:4]:
            text = line.text().upper()
            label = next((lb for lb in keys if text.startswith(lb) and ":" in text), None)
            if label is None:
                break
            key = keys[label]
            if key in found:
                raise ParseError("the summary block repeats a line; upload one statement at a time")
            after = text.split(":", 1)[1].split()
            amounts = [t for t in after if _AMOUNT.match(t)]
            if not amounts:
                raise ParseError(f"summary line {label} has no amount")
            found[key] = _money(amounts[0])
            rest = after[after.index(amounts[0]) + 1:]
            if key in ("cr_total", "db_total") and rest:
                if not _COUNT.match(rest[0]):
                    raise ParseError(f"summary line {label} has an unreadable count")
                found[key.replace("total", "count")] = int(rest[0])
            used += 1
        if any(key not in found for key in keys.values()):
            raise ParseError("the summary block is incomplete; upload the complete statement")
        return found, used
