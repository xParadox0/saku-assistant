"""The only module that reads or writes the database.

Every public method takes tenant_id first and puts it in the WHERE clause. Cross-tenant leaks come from one
query that forgot the filter; keeping all SQL here means there is one place to review.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import psycopg

from .normalise import RawTxn, fingerprints, money, normalise_description

ACCOUNT_KINDS = {"bank", "ewallet", "cash"}

# A pair only counts as an own transfer when at least one side looks like a transfer or top-up.
# Without this, a refund that happens to match a purchase amount on another account would be hidden.
TRANSFER_HINT = re.compile(r"\b(TRSF|TRF|TRANSFER|TOP ?UP|TOPUP|BI-?FAST|PINDAH ?BUKU|ISI SALDO)\b")


@dataclass
class ImportResult:
    batch_id: int
    rows_in: int
    rows_new: int
    rows_dup: int


class Repo:
    def __init__(self, conn: psycopg.Connection):
        self.c = conn

    # ---- tenants and accounts -------------------------------------------------------------------
    def ensure_tenant(self, tenant_id: str, telegram_id: int | None = None, tz: str = "Asia/Jakarta") -> None:
        self.c.execute(
            "INSERT INTO tenant (id, telegram_id, tz) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (tenant_id, telegram_id, tz),
        )

    def add_account(self, tenant_id: str, kind: str, provider: str, label: str,
                    last4: str | None = None, currency: str = "IDR") -> int:
        if kind not in ACCOUNT_KINDS:
            raise ValueError(f"unknown account kind {kind!r}")
        if last4 is not None and not re.fullmatch(r"\d{4}", last4):
            raise ValueError("last4 must be exactly 4 digits; never store a full account number")
        row = self.c.execute(
            "INSERT INTO account (tenant_id, kind, provider, label, last4, currency) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (tenant_id, kind, provider, label, last4, currency),
        ).fetchone()
        return row[0]

    def accounts(self, tenant_id: str) -> list[dict]:
        cur = self.c.execute(
            "SELECT id, kind, provider, label, last4 FROM account WHERE tenant_id = %s ORDER BY id", (tenant_id,)
        )
        return [dict(zip(("id", "kind", "provider", "label", "last4"), r)) for r in cur.fetchall()]

    def _own_account(self, tenant_id: str, account_id: int) -> None:
        row = self.c.execute(
            "SELECT 1 FROM account WHERE id = %s AND tenant_id = %s", (account_id, tenant_id)
        ).fetchone()
        if row is None:
            raise PermissionError("account does not belong to this tenant")

    # ---- import ---------------------------------------------------------------------------------
    def import_rows(self, tenant_id: str, account_id: int, rows: list[RawTxn], source: str,
                    parser: str, file_sha256: str | None = None) -> ImportResult:
        self._own_account(tenant_id, account_id)
        keys = fingerprints(tenant_id, account_id, rows)
        with self.c.transaction():
            batch_id = self.c.execute(
                "INSERT INTO import_batch (tenant_id, account_id, source, file_sha256, parser, status, rows_in) "
                "VALUES (%s, %s, %s, %s, %s, 'running', %s) RETURNING id",
                (tenant_id, account_id, source, file_sha256, parser, len(rows)),
            ).fetchone()[0]
            new = 0
            for r, fp in zip(rows, keys):
                got = self.c.execute(
                    "INSERT INTO txn (tenant_id, account_id, batch_id, booked_on, amount, description, kind, fingerprint) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, fingerprint) DO NOTHING RETURNING id",
                    (tenant_id, account_id, batch_id, r.booked_on, money(r.amount),
                     normalise_description(r.description), r.kind, fp),
                ).fetchone()
                new += got is not None
            dup = len(rows) - new
            self.c.execute(
                "UPDATE import_batch SET status = 'ok', rows_new = %s, rows_dup = %s WHERE id = %s AND tenant_id = %s",
                (new, dup, batch_id, tenant_id),
            )
        return ImportResult(batch_id, len(rows), new, dup)

    # ---- own transfers --------------------------------------------------------------------------
    def detect_own_transfers(self, tenant_id: str, window_days: int = 3) -> int:
        """Pair an outflow and an inflow of the same amount between two of the tenant's accounts within
        window_days, where at least one side looks like a transfer or top-up. Both rows become own_transfer
        and stop counting as income or spending. Returns the number of pairs made."""
        rows = self.c.execute(
            "SELECT id, account_id, booked_on, amount, description FROM txn "
            "WHERE tenant_id = %s AND kind = 'normal' AND transfer_pair IS NULL ORDER BY booked_on, id",
            (tenant_id,),
        ).fetchall()
        outs = [r for r in rows if r[3] < 0]
        ins = [r for r in rows if r[3] > 0]
        used: set[int] = set()
        pairs = []
        for o in outs:
            best = None
            for i in ins:
                if i[0] in used or i[1] == o[1] or i[3] != -o[3]:
                    continue
                gap = abs((i[2] - o[2]).days)
                if gap > window_days:
                    continue
                if not (TRANSFER_HINT.search(o[4]) or TRANSFER_HINT.search(i[4])):
                    continue
                if best is None or gap < best[0]:
                    best = (gap, i)
            if best:
                used.add(best[1][0])
                pairs.append((o[0], best[1][0]))
        with self.c.transaction():
            for out_id, in_id in pairs:
                self.c.execute(
                    "UPDATE txn SET kind = 'own_transfer', transfer_pair = %s WHERE id = %s AND tenant_id = %s",
                    (in_id, out_id, tenant_id),
                )
                self.c.execute(
                    "UPDATE txn SET kind = 'own_transfer', transfer_pair = %s WHERE id = %s AND tenant_id = %s",
                    (out_id, in_id, tenant_id),
                )
        return len(pairs)

    # ---- reporting ------------------------------------------------------------------------------
    def cashflow(self, tenant_id: str, start: date, end: date) -> dict:
        """Income, spending and net for [start, end). Own transfers are excluded; refunds reduce spending."""
        income, gross, refunds, transfers, n = self.c.execute(
            "SELECT "
            " COALESCE(SUM(amount) FILTER (WHERE amount > 0 AND kind = 'normal'), 0),"
            " COALESCE(-SUM(amount) FILTER (WHERE amount < 0 AND kind IN ('normal', 'fee')), 0),"
            " COALESCE(SUM(amount) FILTER (WHERE kind = 'refund'), 0),"
            " COUNT(*) FILTER (WHERE kind = 'own_transfer'),"
            " COUNT(*) "
            "FROM txn WHERE tenant_id = %s AND booked_on >= %s AND booked_on < %s",
            (tenant_id, start, end),
        ).fetchone()
        spending = gross - refunds
        by_account = self.c.execute(
            "SELECT a.label, COUNT(t.id),"
            " COALESCE(SUM(t.amount) FILTER (WHERE t.amount > 0 AND t.kind <> 'own_transfer'), 0),"
            " COALESCE(-SUM(t.amount) FILTER (WHERE t.amount < 0 AND t.kind <> 'own_transfer'), 0) "
            "FROM account a LEFT JOIN txn t ON t.account_id = a.id AND t.tenant_id = a.tenant_id "
            " AND t.booked_on >= %s AND t.booked_on < %s "
            "WHERE a.tenant_id = %s GROUP BY a.id, a.label ORDER BY a.id",
            (start, end, tenant_id),
        ).fetchall()
        return {
            "start": start, "end": end, "transactions": n,
            "income": Decimal(income), "spending": Decimal(spending), "net": Decimal(income) - Decimal(spending),
            "refunds": Decimal(refunds), "own_transfer_rows_excluded": transfers,
            "by_account": [{"label": l, "transactions": c, "money_in": Decimal(i), "money_out": Decimal(o)}
                           for l, c, i, o in by_account],
        }

    def txn_count(self, tenant_id: str) -> int:
        return self.c.execute("SELECT COUNT(*) FROM txn WHERE tenant_id = %s", (tenant_id,)).fetchone()[0]
