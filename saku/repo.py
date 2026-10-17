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

    def ping(self) -> None:
        self.c.execute("SELECT 1").fetchone()

    # ---- identity ---------------------------------------------------------------------------------
    # Cross-tenant by design: this is how a verified email finds its tenant. Nothing else may skip the filter.
    def member(self, email: str) -> dict | None:
        row = self.c.execute("SELECT tenant_id, role FROM member WHERE email = %s", (email.strip().lower(),)).fetchone()
        return {"tenant_id": row[0], "role": row[1]} if row else None

    def add_member(self, email: str, tenant_id: str, role: str = "user") -> None:
        if role not in {"user", "admin"}:
            raise ValueError("role must be user or admin")
        self.ensure_tenant(tenant_id)
        self.c.execute(
            "INSERT INTO member (email, tenant_id, role) VALUES (%s, %s, %s) "
            "ON CONFLICT (email) DO UPDATE SET tenant_id = EXCLUDED.tenant_id, role = EXCLUDED.role",
            (email.strip().lower(), tenant_id, role),
        )

    def tenant_ids(self) -> list[str]:
        return [r[0] for r in self.c.execute("SELECT id FROM tenant ORDER BY id").fetchall()]

    # ---- account management -----------------------------------------------------------------------
    def rename_account(self, tenant_id: str, account_id: int, label: str) -> bool:
        label = label.strip()
        if not 1 <= len(label) <= 60:
            raise ValueError("label must be 1 to 60 characters")
        cur = self.c.execute("UPDATE account SET label = %s WHERE id = %s AND tenant_id = %s", (label, account_id, tenant_id))
        return cur.rowcount == 1

    def delete_account(self, tenant_id: str, account_id: int) -> str:
        """'deleted', 'has_transactions' or 'not_found'. An account with history is never deleted silently."""
        row = self.c.execute(
            "SELECT (SELECT COUNT(*) FROM txn t WHERE t.account_id = a.id AND t.tenant_id = a.tenant_id) "
            "FROM account a WHERE a.id = %s AND a.tenant_id = %s", (account_id, tenant_id),
        ).fetchone()
        if row is None:
            return "not_found"
        if row[0] > 0:
            return "has_transactions"
        with self.c.transaction():
            self.c.execute("DELETE FROM import_batch WHERE account_id = %s AND tenant_id = %s", (account_id, tenant_id))
            self.c.execute("DELETE FROM account WHERE id = %s AND tenant_id = %s", (account_id, tenant_id))
        return "deleted"

    def account_stats(self, tenant_id: str) -> list[dict]:
        cur = self.c.execute(
            "SELECT a.id, a.kind, a.provider, a.label, a.last4, COUNT(t.id), MAX(t.booked_on),"
            " (SELECT b.status FROM import_batch b WHERE b.account_id = a.id AND b.tenant_id = a.tenant_id ORDER BY b.id DESC LIMIT 1),"
            " (SELECT b.created_at FROM import_batch b WHERE b.account_id = a.id AND b.tenant_id = a.tenant_id ORDER BY b.id DESC LIMIT 1) "
            "FROM account a LEFT JOIN txn t ON t.account_id = a.id AND t.tenant_id = a.tenant_id "
            "WHERE a.tenant_id = %s GROUP BY a.id ORDER BY a.id", (tenant_id,),
        )
        keys = ("id", "kind", "provider", "label", "last4", "transactions", "last_booked", "last_import_status", "last_import_at")
        return [dict(zip(keys, r)) for r in cur.fetchall()]

    def record_failed_import(self, tenant_id: str, account_id: int, source: str, parser: str | None,
                             file_sha256: str | None, error: str) -> int:
        self._own_account(tenant_id, account_id)
        return self.c.execute(
            "INSERT INTO import_batch (tenant_id, account_id, source, file_sha256, parser, status, rows_in, rows_new, rows_dup, error) "
            "VALUES (%s, %s, %s, %s, %s, 'failed', 0, 0, 0, %s) RETURNING id",
            (tenant_id, account_id, source, file_sha256, parser, error[:500]),
        ).fetchone()[0]

    # ---- categories and insights --------------------------------------------------------------------
    def apply_categories(self, tenant_id: str) -> int:
        """Label uncategorised rows. Kind first, then income, tenant rules, built-in rules, else 'other'.
        Rows a user labelled by hand (cat_source = 'user') are never touched."""
        from .categorise import BUILTIN_RULES
        n = 0
        n += self.c.execute(
            "UPDATE txn SET category = CASE kind WHEN 'own_transfer' THEN 'transfer' WHEN 'fee' THEN 'fees' "
            " WHEN 'refund' THEN 'refund' END, cat_source = 'builtin' "
            "WHERE tenant_id = %s AND kind <> 'normal' AND (cat_source IS DISTINCT FROM 'user') "
            " AND category IS DISTINCT FROM CASE kind WHEN 'own_transfer' THEN 'transfer' WHEN 'fee' THEN 'fees' "
            " WHEN 'refund' THEN 'refund' END", (tenant_id,)).rowcount
        n += self.c.execute(
            "UPDATE txn SET category = 'income', cat_source = 'builtin' WHERE tenant_id = %s AND kind = 'normal' "
            "AND amount > 0 AND category IS NULL", (tenant_id,)).rowcount
        rules = self.c.execute("SELECT pattern, category FROM category_rule WHERE tenant_id = %s ORDER BY id",
                               (tenant_id,)).fetchall()
        for pattern, cat in rules:
            n += self.c.execute(
                "UPDATE txn SET category = %s, cat_source = 'rule' WHERE tenant_id = %s AND kind = 'normal' "
                "AND amount < 0 AND category IS NULL AND description ~* %s", (cat, tenant_id, pattern)).rowcount
        for cat, pattern in BUILTIN_RULES:
            n += self.c.execute(
                "UPDATE txn SET category = %s, cat_source = 'builtin' WHERE tenant_id = %s AND kind = 'normal' "
                "AND amount < 0 AND category IS NULL AND description ~* %s", (cat, tenant_id, pattern)).rowcount
        n += self.c.execute(
            "UPDATE txn SET category = 'other', cat_source = 'builtin' WHERE tenant_id = %s AND kind = 'normal' "
            "AND amount < 0 AND category IS NULL", (tenant_id,)).rowcount
        return n

    def spending_by_category(self, tenant_id: str, start: date, end: date) -> list[dict]:
        """Spending per category for [start, end). Fees count as spending; refunds reduce their own line."""
        cur = self.c.execute(
            "SELECT COALESCE(category, 'other'), -SUM(amount), COUNT(*) FROM txn "
            "WHERE tenant_id = %s AND booked_on >= %s AND booked_on < %s AND amount < 0 "
            " AND kind IN ('normal', 'fee') GROUP BY 1 ORDER BY 2 DESC", (tenant_id, start, end))
        return [{"category": c, "amount": Decimal(a), "count": n} for c, a, n in cur.fetchall()]

    def recurring_charges(self, tenant_id: str, until: date, months: int = 3) -> list[dict]:
        """Same description charged in at least 2 of the last `months` months: likely a subscription or a bill."""
        y, m = until.year, until.month
        for _ in range(months - 1):
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        since = date(y, m, 1)
        cur = self.c.execute(
            "SELECT description, COUNT(DISTINCT date_trunc('month', booked_on)), -AVG(amount)::numeric(18,0), "
            " MAX(booked_on), MAX(category) FROM txn "
            "WHERE tenant_id = %s AND kind = 'normal' AND amount < 0 AND booked_on >= %s "
            " AND category IN ('subscriptions', 'bills') "
            "GROUP BY description HAVING COUNT(DISTINCT date_trunc('month', booked_on)) >= 2 ORDER BY 3 DESC",
            (tenant_id, since))
        return [{"description": d, "months": n, "average": Decimal(a), "last": l, "category": c}
                for d, n, a, l, c in cur.fetchall()]

    def daily_spending(self, tenant_id: str, start: date, end: date) -> list[dict]:
        cur = self.c.execute(
            "SELECT booked_on, -SUM(amount) FROM txn WHERE tenant_id = %s AND booked_on >= %s AND booked_on < %s "
            "AND amount < 0 AND kind IN ('normal', 'fee') GROUP BY 1 ORDER BY 1", (tenant_id, start, end))
        return [{"day": d, "amount": Decimal(a)} for d, a in cur.fetchall()]

    # ---- dashboard reads ----------------------------------------------------------------------------
    def latest_month(self, tenant_id: str) -> date | None:
        return self.c.execute(
            "SELECT date_trunc('month', MAX(booked_on))::date FROM txn WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()[0]

    def monthly_series(self, tenant_id: str, last_month: date, months: int = 6) -> list[dict]:
        out = []
        y, m = last_month.year, last_month.month
        starts = []
        for _ in range(months):
            starts.append(date(y, m, 1))
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        for start in reversed(starts):
            end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
            out.append(self.cashflow(tenant_id, start, end))
        while len(out) > 1 and out[0]["transactions"] == 0:  # no empty months before the first data
            out.pop(0)
        return out

    def recent_txns(self, tenant_id: str, limit: int = 15) -> list[dict]:
        cur = self.c.execute(
            "SELECT t.booked_on, a.label, t.description, t.amount, t.kind, t.category FROM txn t "
            "JOIN account a ON a.id = t.account_id AND a.tenant_id = t.tenant_id "
            "WHERE t.tenant_id = %s ORDER BY t.booked_on DESC, t.id DESC LIMIT %s", (tenant_id, limit),
        )
        return [dict(zip(("booked_on", "account", "description", "amount", "kind", "category"), r)) for r in cur.fetchall()]

    def import_batches(self, tenant_id: str, limit: int = 10) -> list[dict]:
        cur = self.c.execute(
            "SELECT b.created_at, a.label, b.source, b.parser, b.status, b.rows_in, b.rows_new, b.rows_dup, b.error "
            "FROM import_batch b LEFT JOIN account a ON a.id = b.account_id AND a.tenant_id = b.tenant_id "
            "WHERE b.tenant_id = %s ORDER BY b.id DESC LIMIT %s", (tenant_id, limit),
        )
        keys = ("created_at", "account", "source", "parser", "status", "rows_in", "rows_new", "rows_dup", "error")
        return [dict(zip(keys, r)) for r in cur.fetchall()]

    def import_batch(self, tenant_id: str, batch_id: int) -> dict | None:
        rows = self.c.execute(
            "SELECT b.created_at, a.label, b.source, b.parser, b.status, b.rows_in, b.rows_new, b.rows_dup, b.error "
            "FROM import_batch b LEFT JOIN account a ON a.id = b.account_id AND a.tenant_id = b.tenant_id "
            "WHERE b.tenant_id = %s AND b.id = %s", (tenant_id, batch_id),
        ).fetchall()
        keys = ("created_at", "account", "source", "parser", "status", "rows_in", "rows_new", "rows_dup", "error")
        return dict(zip(keys, rows[0])) if rows else None

    # ---- admin / ops ---------------------------------------------------------------------------------
    # Cross-tenant on purpose, and limited to operational metadata: counts, statuses, timestamps.
    # No admin method returns descriptions, amounts or account labels of another tenant.
    def admin_tenants(self) -> list[dict]:
        cur = self.c.execute(
            "SELECT t.id,"
            " (SELECT COUNT(*) FROM member m WHERE m.tenant_id = t.id),"
            " (SELECT COUNT(*) FROM account a WHERE a.tenant_id = t.id),"
            " (SELECT COUNT(*) FROM txn x WHERE x.tenant_id = t.id),"
            " (SELECT MAX(b.created_at) FROM import_batch b WHERE b.tenant_id = t.id),"
            " (SELECT COUNT(*) FROM import_batch b WHERE b.tenant_id = t.id AND b.status = 'failed'"
            "   AND b.created_at > now() - interval '7 days') "
            "FROM tenant t ORDER BY t.id"
        )
        keys = ("tenant", "members", "accounts", "transactions", "last_import_at", "failed_imports_7d")
        return [dict(zip(keys, r)) for r in cur.fetchall()]

    def admin_heartbeats(self) -> list[dict]:
        cur = self.c.execute(
            "SELECT tenant_id, component, last_ok, last_error, (last_ok IS NULL OR last_ok < now() - interval '24 hours') "
            "FROM heartbeat ORDER BY tenant_id, component"
        )
        return [dict(zip(("tenant", "component", "last_ok", "last_error", "stale"), r)) for r in cur.fetchall()]

    def admin_recent_batches(self, limit: int = 20) -> list[dict]:
        cur = self.c.execute(
            "SELECT created_at, tenant_id, source, parser, status, rows_in, rows_new, rows_dup, error "
            "FROM import_batch ORDER BY id DESC LIMIT %s", (limit,),
        )
        keys = ("created_at", "tenant", "source", "parser", "status", "rows_in", "rows_new", "rows_dup", "error")
        return [dict(zip(keys, r)) for r in cur.fetchall()]
