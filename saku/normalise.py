"""Turn parser output into the stored transaction shape.

Two rules live here:
- descriptions are redacted before storage, so account numbers, phone numbers and emails never reach the
  database or a model;
- every row gets a fingerprint so re-importing the same statement stores nothing twice.
"""
import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

KINDS = {"normal", "own_transfer", "fee", "refund"}
MAX_LEDGER_AMOUNT = Decimal("9999999999999999.99")  # schema.sql: NUMERIC(18,2)


def valid_transaction_amount(value: Decimal) -> bool:
    """Whether an amount is finite and representable without rounding in txn.amount."""
    if not isinstance(value, Decimal) or not value.is_finite():
        return False
    exponent = value.as_tuple().exponent
    return value.copy_abs() <= MAX_LEDGER_AMOUNT and isinstance(exponent, int) and exponent >= -2

_WS = re.compile(r"\s+")
_PHONE = re.compile(r"(?<!\d)(?:\+?62|0)8\d{7,11}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_LONG_NUMBER = re.compile(r"(?<!\d)\d{6,}(?!\d)")  # account and card numbers


@dataclass(frozen=True)
class RawTxn:
    """One row as a parser produced it. Parsers never touch the database."""
    booked_on: date
    amount: Decimal          # negative = money out
    description: str
    kind: str = "normal"     # parsers may tag 'fee' or 'refund' when the statement says so

    def __post_init__(self):
        if not isinstance(self.amount, Decimal):
            raise TypeError("amount must be Decimal, never float")
        if self.kind not in KINDS:
            raise ValueError(f"unknown kind {self.kind!r}")


def redact(text: str) -> str:
    text = _EMAIL.sub("[EMAIL]", text)
    text = _PHONE.sub("[PHONE]", text)
    return _LONG_NUMBER.sub("[NUM]", text)


def normalise_description(text: str) -> str:
    return _WS.sub(" ", redact(text).strip().upper())


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def fingerprints(tenant_id: str, account_id: int, rows: list[RawTxn]) -> list[str]:
    """Stable dedupe keys.

    Two genuinely identical rows on one day (two coffees at the same place) must both survive, so the key
    includes the occurrence number of that exact row within the import. Re-importing the same statement
    produces the same keys, so nothing is stored twice.
    """
    seen: Counter[str] = Counter()
    out = []
    for r in rows:
        key = f"{tenant_id}|{account_id}|{r.booked_on.isoformat()}|{money(r.amount)}|{normalise_description(r.description)}"
        seen[key] += 1
        out.append(hashlib.sha256(f"{key}#{seen[key]}".encode()).hexdigest())
    return out
