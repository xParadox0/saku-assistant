"""The negative checks from docs/HLD.md section 8. These matter most."""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from saku.normalise import RawTxn, redact

ROOT = Path(__file__).resolve().parents[1]


def test_no_credential_columns(conn):
    rows = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
        "AND column_name ~* '(^|_)(password|passwd|pin|otp|secret|credential|token|username)(_|$)'"
    ).fetchall()
    assert rows == []


def test_money_columns_are_numeric(conn):
    types = dict(conn.execute(
        "SELECT table_name || '.' || column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name IN ('amount', 'target', 'saved')"
    ).fetchall())
    assert types and set(types.values()) == {"numeric"}, types


def test_float_amount_rejected():
    with pytest.raises(TypeError):
        RawTxn(date(2026, 7, 1), 1.5, "X")  # type: ignore[arg-type]


def test_full_account_number_cannot_be_stored(repo):
    repo.ensure_tenant("t1")
    with pytest.raises(ValueError):
        repo.add_account("t1", "bank", "demo_bank_a", "A", last4="1234567890")


def test_redaction():
    s = redact("TRSF KE 1234567890 / HP 081234567890 / a.b@example.com / +6281298765432")
    assert "1234567890" not in s and "081234567890" not in s and "example.com" not in s and "6281298765432" not in s
    assert s.count("[NUM]") == 1 and s.count("[PHONE]") == 2 and s.count("[EMAIL]") == 1


def test_other_tenant_sees_nothing(repo):
    repo.ensure_tenant("alice"); repo.ensure_tenant("bob")
    acc = repo.add_account("alice", "bank", "demo_bank_a", "A")
    repo.import_rows("alice", acc, [RawTxn(date(2026, 7, 1), Decimal(100), "X")], "manual", "test")
    assert repo.txn_count("bob") == 0
    assert repo.accounts("bob") == []
    assert repo.cashflow("bob", date(2026, 1, 1), date(2027, 1, 1))["transactions"] == 0


def test_cannot_import_into_another_tenants_account(repo):
    repo.ensure_tenant("alice"); repo.ensure_tenant("bob")
    acc = repo.add_account("alice", "bank", "demo_bank_a", "A")
    with pytest.raises(PermissionError):
        repo.import_rows("bob", acc, [RawTxn(date(2026, 7, 1), Decimal(1), "X")], "manual", "test")


def test_sql_only_in_repo_layer():
    offenders = [p.name for p in (ROOT / "saku").glob("*.py")
                 if p.name not in {"repo.py", "db.py"} and ".execute(" in p.read_text()]
    assert offenders == []
