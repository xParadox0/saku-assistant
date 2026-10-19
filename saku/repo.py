"""The only module that reads or writes the database.

Every public method takes tenant_id first and puts it in the WHERE clause. Cross-tenant leaks come from one
query that forgot the filter; keeping all SQL here means there is one place to review.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import psycopg

from .normalise import RawTxn, fingerprints, money, normalise_description

ACCOUNT_KINDS = {"bank", "ewallet", "cash"}
SAFE_APP_PATHS = {"/app", "/app/accounts", "/app/imports", "/app/transactions", "/app/settings"}

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

    def tenant_timezone(self, tenant_id: str) -> str:
        row = self.c.execute("SELECT tz FROM tenant WHERE id = %s", (tenant_id,)).fetchone()
        if row is None:
            raise ValueError("unknown tenant")
        return row[0]

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

    def import_manual_rows(self, tenant_id: str, account_id: int, rows: list[RawTxn], source_ref: str) -> ImportResult:
        """Store one chat message atomically and make delivery retries idempotent."""
        self._own_account(tenant_id, account_id)
        source_sha = hashlib.sha256(source_ref.encode()).hexdigest()
        with self.c.transaction():
            batch_id = self.c.execute(
                "INSERT INTO import_batch (tenant_id, account_id, source, file_sha256, parser, status, rows_in) "
                "VALUES (%s, %s, 'manual', %s, 'telegram-agent/1', 'running', %s) RETURNING id",
                (tenant_id, account_id, source_sha, len(rows)),
            ).fetchone()[0]
            new = 0
            for index, row in enumerate(rows):
                description = normalise_description(row.description)
                amount = money(row.amount)
                fp = hashlib.sha256(
                    f"{tenant_id}|manual|{source_sha}|{index}|{row.booked_on}|{amount}|{description}".encode()
                ).hexdigest()
                got = self.c.execute(
                    "INSERT INTO txn (tenant_id, account_id, batch_id, booked_on, amount, description, kind, fingerprint) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (tenant_id, fingerprint) DO NOTHING RETURNING id",
                    (tenant_id, account_id, batch_id, row.booked_on, amount, description, row.kind, fp),
                ).fetchone()
                new += got is not None
            duplicate = len(rows) - new
            self.c.execute(
                "UPDATE import_batch SET status = 'ok', rows_new = %s, rows_dup = %s "
                "WHERE id = %s AND tenant_id = %s",
                (new, duplicate, batch_id, tenant_id),
            )
        return ImportResult(batch_id, len(rows), new, duplicate)

    # ---- manual transaction management ------------------------------------------------------------
    def transactions(self, tenant_id: str, limit: int = 100) -> list[dict]:
        cur = self.c.execute(
            "SELECT t.id, t.booked_on, t.account_id, a.label, t.amount, t.description, t.kind, t.category "
            "FROM txn t JOIN account a ON a.id = t.account_id AND a.tenant_id = t.tenant_id "
            "WHERE t.tenant_id = %s ORDER BY t.booked_on DESC, t.id DESC LIMIT %s", (tenant_id, limit))
        keys = ("id", "booked_on", "account_id", "account", "amount", "description", "kind", "category")
        return [dict(zip(keys, row)) for row in cur.fetchall()]

    def _manual_fingerprint(self, tenant_id: str, account_id: int, booked_on: date,
                            amount: Decimal, description: str, source_ref: str) -> str:
        return hashlib.sha256(
            f"{tenant_id}|web|{source_ref}|{account_id}|{booked_on}|{money(amount)}|{normalise_description(description)}".encode()
        ).hexdigest()

    def create_transaction(self, tenant_id: str, account_id: int, booked_on: date, amount: Decimal,
                           description: str, kind: str = "normal", source_ref: str = "web") -> int:
        self._own_account(tenant_id, account_id)
        row = RawTxn(booked_on, amount, description, kind)
        with self.c.transaction():
            batch_id = self.c.execute(
                "INSERT INTO import_batch (tenant_id, account_id, source, parser, status, rows_in, rows_new, rows_dup) "
                "VALUES (%s, %s, 'manual', 'web/1', 'ok', 1, 1, 0) RETURNING id",
                (tenant_id, account_id)).fetchone()[0]
            fingerprint = self._manual_fingerprint(tenant_id, account_id, row.booked_on, row.amount, row.description, source_ref)
            inserted = self.c.execute(
                "INSERT INTO txn (tenant_id, account_id, batch_id, booked_on, amount, description, kind, fingerprint) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (tenant_id, account_id, batch_id, row.booked_on, money(row.amount),
                 normalise_description(row.description), row.kind, fingerprint)).fetchone()
        return inserted[0]

    def update_transaction(self, tenant_id: str, txn_id: int, account_id: int, booked_on: date,
                           amount: Decimal, description: str, kind: str = "normal") -> bool:
        self._own_account(tenant_id, account_id)
        RawTxn(booked_on, amount, description, kind)
        current = self.c.execute(
            "SELECT transfer_pair FROM txn WHERE id = %s AND tenant_id = %s", (txn_id, tenant_id)).fetchone()
        if current is None:
            return False
        with self.c.transaction():
            if current[0] is not None:
                self.c.execute("UPDATE txn SET transfer_pair = NULL, kind = 'normal', category = NULL, cat_source = NULL "
                               "WHERE id = %s AND tenant_id = %s", (current[0], tenant_id))
            fingerprint = self._manual_fingerprint(tenant_id, account_id, booked_on, amount, description, f"edit:{txn_id}")
            self.c.execute(
                "UPDATE txn SET account_id = %s, booked_on = %s, amount = %s, description = %s, kind = %s, "
                "transfer_pair = NULL, category = NULL, cat_source = NULL, fingerprint = %s "
                "WHERE id = %s AND tenant_id = %s",
                (account_id, booked_on, money(amount), normalise_description(description), kind, fingerprint, txn_id, tenant_id))
        return True

    def delete_transaction(self, tenant_id: str, txn_id: int) -> int:
        row = self.c.execute(
            "SELECT transfer_pair FROM txn WHERE id = %s AND tenant_id = %s", (txn_id, tenant_id)).fetchone()
        if row is None:
            return 0
        pair = row[0]
        with self.c.transaction():
            if pair is not None:
                self.c.execute("UPDATE txn SET transfer_pair = NULL WHERE id IN (%s, %s) AND tenant_id = %s",
                               (txn_id, pair, tenant_id))
                self.c.execute("DELETE FROM txn WHERE id IN (%s, %s) AND tenant_id = %s",
                               (txn_id, pair, tenant_id))
                return 2
            self.c.execute("DELETE FROM txn WHERE id = %s AND tenant_id = %s", (txn_id, tenant_id))
        return 1

    # ---- own transfers --------------------------------------------------------------------------
    def detect_own_transfers(self, tenant_id: str, window_days: int = 3) -> int:
        """Pair an outflow and an inflow of the same amount between two of the tenant's accounts within
        window_days, where at least one side looks like a transfer or top-up. Both rows become own_transfer
        and stop counting as income or spending. Returns the number of pairs made."""
        rows = self.c.execute(
            "SELECT id, account_id, booked_on, amount, description FROM txn "
            "WHERE tenant_id = %s AND kind IN ('normal', 'own_transfer') AND transfer_pair IS NULL ORDER BY booked_on, id",
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
        """Income, spending and net for [start, end). Own transfers are excluded globally but included per account."""
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
            " COALESCE(SUM(t.amount) FILTER (WHERE t.amount > 0), 0),"
            " COALESCE(-SUM(t.amount) FILTER (WHERE t.amount < 0), 0) "
            "FROM account a LEFT JOIN txn t ON t.account_id = a.id AND t.tenant_id = a.tenant_id "
            " AND t.booked_on >= %s AND t.booked_on < %s "
            "WHERE a.tenant_id = %s GROUP BY a.id, a.label ORDER BY a.id",
            (start, end, tenant_id),
        ).fetchall()
        by_account = [
            {"label": l, "transactions": c, "money_in": Decimal(i), "money_out": Decimal(o)}
            for l, c, i, o in by_account
        ]
        money_in = sum((row["money_in"] for row in by_account), Decimal("0"))
        money_out = sum((row["money_out"] for row in by_account), Decimal("0"))
        return {
            "start": start, "end": end, "transactions": n,
            "income": Decimal(income), "spending": Decimal(spending), "net": Decimal(income) - Decimal(spending),
            "money_in": money_in, "money_out": money_out, "movement_net": money_in - money_out,
            "refunds": Decimal(refunds), "own_transfer_rows_excluded": transfers,
            "by_account": by_account,
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

    # ---- closed beta: signups ---------------------------------------------------------------------------
    def signup_get(self, email: str) -> dict | None:
        row = self.c.execute(
            "SELECT email, display_name, note, lang, consent_version, consent_at, status, decided_at, created_at "
            "FROM signup WHERE email = %s", (email.strip().lower(),)).fetchone()
        keys = ("email", "display_name", "note", "lang", "consent_version", "consent_at", "status", "decided_at", "created_at")
        return dict(zip(keys, row)) if row else None

    def signup_counts(self) -> dict:
        rows = self.c.execute("SELECT status, COUNT(*) FROM signup GROUP BY status").fetchall()
        out = {"pending": 0, "approved": 0, "rejected": 0, "withdrawn": 0}
        out.update({s: n for s, n in rows})
        return out

    def signup_create(self, email: str, display_name: str, note: str | None, lang: str,
                      consent_version: str, pending_cap: int) -> str:
        """Outcomes: created, exists, full, invalid. Never creates a tenant or a member."""
        email = email.strip().lower()
        name = " ".join((display_name or "").split())
        note = " ".join((note or "").split()) or None
        if not email or not 1 <= len(name) <= 60 or (note and len(note) > 300) or lang not in ("en", "id"):
            return "invalid"
        with self.c.transaction():
            self.c.execute("SELECT pg_advisory_xact_lock(7210001)")  # serialise the cap check
            if self.member(email) is not None or self.signup_get(email) is not None:
                return "exists"
            pending = self.c.execute("SELECT COUNT(*) FROM signup WHERE status = 'pending'").fetchone()[0]
            if pending >= pending_cap:
                return "full"
            self.c.execute(
                "INSERT INTO signup (email, display_name, note, lang, consent_version, consent_at) "
                "VALUES (%s, %s, %s, %s, %s, now())", (email, name, note, lang, consent_version))
        return "created"

    def signups(self, status: str = "pending", limit: int = 100) -> list[dict]:
        cur = self.c.execute(
            "SELECT email, display_name, note, lang, consent_version, created_at, decided_at, decided_by "
            "FROM signup WHERE status = %s ORDER BY created_at LIMIT %s", (status, limit))
        keys = ("email", "display_name", "note", "lang", "consent_version", "created_at", "decided_at", "decided_by")
        return [dict(zip(keys, r)) for r in cur.fetchall()]

    def signup_decide(self, email: str, approve: bool, decided_by: str, approved_cap: int) -> str:
        """Outcomes: approved, rejected, not_pending, full. Approval is the only path that creates a tenant."""
        import secrets
        email = email.strip().lower()
        with self.c.transaction():
            self.c.execute("SELECT pg_advisory_xact_lock(7210002)")
            row = self.c.execute("SELECT status FROM signup WHERE email = %s FOR UPDATE", (email,)).fetchone()
            if not row or row[0] != "pending":
                return "not_pending"
            if not approve:
                self.c.execute("UPDATE signup SET status = 'rejected', decided_at = now(), decided_by = %s "
                               "WHERE email = %s", (decided_by, email))
                return "rejected"
            approved = self.c.execute("SELECT COUNT(*) FROM signup WHERE status = 'approved'").fetchone()[0]
            if approved >= approved_cap:
                return "full"
            if self.member(email) is not None:
                return "not_pending"
            tenant = "t_" + secrets.token_hex(6)  # random, never derived from the email
            self.ensure_tenant(tenant)
            self.c.execute("INSERT INTO member (email, tenant_id, role) VALUES (%s, %s, 'user')", (email, tenant))
            self.c.execute("UPDATE signup SET status = 'approved', decided_at = now(), decided_by = %s "
                           "WHERE email = %s", (decided_by, email))
        return "approved"

    def demo_reader_ok(self, tenant_id: str) -> bool:
        """Public pages may only ever read the demo tenant."""
        return tenant_id == "demo"

    # ---- Telegram linking -------------------------------------------------------------------------------
    LINK_TTL_MINUTES = 10

    def create_link_code(self, email: str) -> str:
        """New one-time code for this member. Older unused codes of the same member stop working."""
        import secrets
        email = email.strip().lower()
        if self.member(email) is None:
            raise ValueError("unknown member")
        code = secrets.token_urlsafe(18)  # 144 bits; fits Telegram's /start parameter (A-Z a-z 0-9 _ -, max 64)
        with self.c.transaction():
            self.c.execute("UPDATE link_code SET used_at = now() WHERE email = %s AND used_at IS NULL", (email,))
            self.c.execute(
                "INSERT INTO link_code (code_hash, email, expires_at) VALUES (%s, %s, now() + make_interval(mins => %s))",
                (_sha(code), email, self.LINK_TTL_MINUTES))
        return code

    def redeem_link_code(self, code: str, telegram_user_id: int) -> tuple[str, str | None]:
        """Returns (outcome, email). Outcomes: linked, relinked, invalid, taken.
        invalid covers unknown, expired and already used codes alike, so a guesser learns nothing."""
        if not code or len(code) > 64:
            return "invalid", None
        with self.c.transaction():
            row = self.c.execute(
                "SELECT email FROM link_code WHERE code_hash = %s AND used_at IS NULL AND expires_at > now() FOR UPDATE",
                (_sha(code),)).fetchone()
            if row is None:
                return "invalid", None
            email = row[0]
            owner = self.c.execute("SELECT email FROM telegram_link WHERE telegram_user_id = %s",
                                   (telegram_user_id,)).fetchone()
            if owner and owner[0] != email:
                return "taken", None  # this Telegram account belongs to another member; code stays usable
            self.c.execute("UPDATE link_code SET used_at = now() WHERE code_hash = %s", (_sha(code),))
            tenant = self.member(email)["tenant_id"]
            replaced = self.c.execute("DELETE FROM telegram_link WHERE email = %s AND telegram_user_id <> %s",
                                      (email, telegram_user_id)).rowcount
            self.c.execute(
                "INSERT INTO telegram_link (telegram_user_id, email, tenant_id) VALUES (%s, %s, %s) "
                "ON CONFLICT (telegram_user_id) DO UPDATE SET linked_at = now()",
                (telegram_user_id, email, tenant))
        return ("relinked" if replaced else "linked"), email

    def telegram_identity(self, telegram_user_id: int) -> dict | None:
        """The only way the bot resolves a sender. None means: not linked, refuse and store nothing."""
        row = self.c.execute(
            "SELECT l.email, l.tenant_id, m.role FROM telegram_link l JOIN member m ON m.email = l.email "
            "WHERE l.telegram_user_id = %s", (telegram_user_id,)).fetchone()
        return {"email": row[0], "tenant_id": row[1], "role": row[2]} if row else None

    def telegram_status(self, email: str) -> dict | None:
        row = self.c.execute("SELECT telegram_user_id, linked_at FROM telegram_link WHERE email = %s",
                             (email.strip().lower(),)).fetchone()
        return {"telegram_user_id": row[0], "linked_at": row[1]} if row else None

    def unlink_telegram(self, email: str | None = None, telegram_user_id: int | None = None) -> bool:
        if email is not None:
            n = self.c.execute("DELETE FROM telegram_link WHERE email = %s", (email.strip().lower(),)).rowcount
        elif telegram_user_id is not None:
            n = self.c.execute("DELETE FROM telegram_link WHERE telegram_user_id = %s", (telegram_user_id,)).rowcount
        else:
            raise ValueError("email or telegram_user_id required")
        return n > 0

    def telegram_draft_set(self, telegram_user_id: int, tenant_id: str, payload: dict, ttl_minutes: int = 10) -> None:
        self.c.execute(
            "INSERT INTO telegram_draft (telegram_user_id, tenant_id, payload, expires_at) "
            "VALUES (%s, %s, %s::jsonb, now() + make_interval(mins => %s)) "
            "ON CONFLICT (telegram_user_id) DO UPDATE SET tenant_id = EXCLUDED.tenant_id, "
            "payload = EXCLUDED.payload, expires_at = EXCLUDED.expires_at, created_at = now()",
            (telegram_user_id, tenant_id, json.dumps(payload), ttl_minutes),
        )

    def telegram_draft_get(self, telegram_user_id: int, tenant_id: str) -> dict | None:
        self.c.execute(
            "DELETE FROM telegram_draft WHERE telegram_user_id = %s AND tenant_id = %s AND expires_at <= now()",
            (telegram_user_id, tenant_id),
        )
        row = self.c.execute(
            "SELECT payload FROM telegram_draft WHERE telegram_user_id = %s AND tenant_id = %s AND expires_at > now()",
            (telegram_user_id, tenant_id),
        ).fetchone()
        return row[0] if row else None

    def telegram_draft_clear(self, telegram_user_id: int, tenant_id: str) -> None:
        self.c.execute("DELETE FROM telegram_draft WHERE telegram_user_id = %s AND tenant_id = %s",
                       (telegram_user_id, tenant_id))

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
            " (SELECT COUNT(*) FROM telegram_link l WHERE l.tenant_id = t.id),"
            " (SELECT COUNT(*) FROM account a WHERE a.tenant_id = t.id),"
            " (SELECT COUNT(*) FROM txn x WHERE x.tenant_id = t.id),"
            " (SELECT MAX(b.created_at) FROM import_batch b WHERE b.tenant_id = t.id),"
            " (SELECT COUNT(*) FROM import_batch b WHERE b.tenant_id = t.id AND b.status = 'failed'"
            "   AND b.created_at > now() - interval '7 days') "
            "FROM tenant t ORDER BY t.id"
        )
        keys = ("tenant", "members", "telegram_linked", "accounts", "transactions", "last_import_at", "failed_imports_7d")
        return [dict(zip(keys, r)) for r in cur.fetchall()]

    def beat(self, tenant_id: str, component: str, error: str | None = None) -> None:
        """Record that a component is alive (error None) or that its last run failed."""
        if error is None:
            self.c.execute(
                "INSERT INTO heartbeat (tenant_id, component, last_ok) VALUES (%s, %s, now()) "
                "ON CONFLICT (tenant_id, component) DO UPDATE SET last_ok = now(), last_error = NULL",
                (tenant_id, component))
        else:
            self.c.execute(
                "INSERT INTO heartbeat (tenant_id, component, last_error) VALUES (%s, %s, %s) "
                "ON CONFLICT (tenant_id, component) DO UPDATE SET last_error = EXCLUDED.last_error",
                (tenant_id, component, error[:300]))

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


    # ---- own sign-in (HLD 3.11) -----------------------------------------------------------------------
    # Cross-tenant by design, like member(): sign-in happens before a tenant is known. Only HMACs and hashes.
    def login_events(self, kind: str, email_hmac: str, ip_hmac: str, window_s: int) -> tuple[int, int]:
        row = self.c.execute(
            "SELECT COUNT(*) FILTER (WHERE email_hmac = %s), COUNT(*) FILTER (WHERE ip_hmac = %s) FROM login_event "
            "WHERE kind = %s AND at > now() - make_interval(secs => %s)", (email_hmac, ip_hmac, kind, window_s)).fetchone()
        return int(row[0]), int(row[1])

    def login_event_add(self, kind: str, email_hmac: str, ip_hmac: str) -> None:
        self.c.execute("INSERT INTO login_event (kind, email_hmac, ip_hmac) VALUES (%s, %s, %s)", (kind, email_hmac, ip_hmac))
        self.c.execute("DELETE FROM login_event WHERE at < now() - interval '24 hours'")

    def login_code_new(self, email: str, code_hmac: str, salt: str, ttl_s: int) -> None:
        """A new code cancels every older unused code for the address."""
        email = email.strip().lower()
        with self.c.transaction():
            self.c.execute("UPDATE login_code SET used_at = now() WHERE email = %s AND used_at IS NULL", (email,))
            self.c.execute("INSERT INTO login_code (email, code_hmac, salt, expires_at) "
                           "VALUES (%s, %s, %s, now() + make_interval(secs => %s))", (email, code_hmac, salt, ttl_s))
            self.c.execute("DELETE FROM login_code WHERE created_at < now() - interval '2 days'")

    def login_code_live(self, email: str) -> dict | None:
        """The one code that may still be redeemed: unused, unexpired, fewer than the allowed wrong tries."""
        row = self.c.execute(
            "SELECT id, code_hmac, salt, attempts FROM login_code WHERE email = %s AND used_at IS NULL "
            "AND expires_at > now() ORDER BY created_at DESC LIMIT 1", (email.strip().lower(),)).fetchone()
        return {"id": row[0], "code_hmac": row[1], "salt": row[2], "attempts": row[3]} if row else None

    def login_code_fail(self, code_id: int, max_attempts: int) -> None:
        self.c.execute("UPDATE login_code SET attempts = attempts + 1, "
                       "used_at = CASE WHEN attempts + 1 >= %s THEN now() ELSE used_at END WHERE id = %s",
                       (max_attempts, code_id))

    def login_code_use(self, code_id: int) -> bool:
        """Single use, race safe: only one request can flip used_at."""
        cur = self.c.execute("UPDATE login_code SET used_at = now() WHERE id = %s AND used_at IS NULL "
                             "AND expires_at > now()", (code_id,))
        return cur.rowcount == 1

    def may_sign_in(self, email: str) -> bool:
        """A member, or an address with a registration request in any state."""
        return self.c.execute("SELECT EXISTS (SELECT 1 FROM member WHERE email = %s) "
                              "OR EXISTS (SELECT 1 FROM signup WHERE email = %s)",
                              (email, email)).fetchone()[0]

    def session_new(self, sid_sha256: str, email: str, ttl_s: int) -> None:
        self.c.execute("INSERT INTO web_session (sid_sha256, email, expires_at) "
                       "VALUES (%s, %s, now() + make_interval(secs => %s))", (sid_sha256, email.strip().lower(), ttl_s))
        self.c.execute("DELETE FROM web_session WHERE expires_at < now() - interval '7 days'")

    def session_email(self, sid_sha256: str, idle_s: int) -> str | None:
        """Email of a live session, sliding last_seen. Expired, idle or revoked sessions return None."""
        row = self.c.execute(
            "UPDATE web_session SET last_seen = now() WHERE sid_sha256 = %s AND revoked_at IS NULL "
            "AND expires_at > now() AND last_seen > now() - make_interval(secs => %s) RETURNING email",
            (sid_sha256, idle_s)).fetchone()
        return row[0] if row else None

    def session_remember_path(self, sid_sha256: str, path: str) -> bool:
        if path not in SAFE_APP_PATHS:
            return False
        return self.c.execute(
            "UPDATE web_session SET last_path = %s WHERE sid_sha256 = %s AND revoked_at IS NULL "
            "AND expires_at > now()",
            (path, sid_sha256),
        ).rowcount == 1

    def session_last_path(self, sid_sha256: str) -> str:
        row = self.c.execute(
            "SELECT last_path FROM web_session WHERE sid_sha256 = %s AND revoked_at IS NULL AND expires_at > now()",
            (sid_sha256,),
        ).fetchone()
        path = row[0] if row else None
        return path if path in SAFE_APP_PATHS else "/app"

    def session_revoke(self, sid_sha256: str) -> None:
        self.c.execute("UPDATE web_session SET revoked_at = now() WHERE sid_sha256 = %s AND revoked_at IS NULL",
                       (sid_sha256,))

    def sessions_revoke_all(self, email: str) -> int:
        return self.c.execute("UPDATE web_session SET revoked_at = now() WHERE email = %s AND revoked_at IS NULL",
                              (email.strip().lower(),)).rowcount


def _sha(code: str) -> str:
    import hashlib
    return hashlib.sha256(code.encode()).hexdigest()
