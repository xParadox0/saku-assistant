from datetime import date
from decimal import Decimal

from saku import demo
from saku.normalise import RawTxn


def _acc(repo, tenant="t1"):
    repo.ensure_tenant(tenant)
    return repo.add_account(tenant, "bank", "demo_bank_a", "A")


def test_reimport_stores_nothing_twice(repo):
    acc = _acc(repo)
    rows = [RawTxn(date(2026, 7, d), Decimal(-10_000 * d), f"SHOP {d}") for d in range(1, 11)]
    first = repo.import_rows("t1", acc, rows, "csv", "test")
    second = repo.import_rows("t1", acc, rows, "csv", "test")
    assert (first.rows_new, first.rows_dup) == (10, 0)
    assert (second.rows_new, second.rows_dup) == (0, 10)
    assert repo.txn_count("t1") == 10


def test_identical_rows_same_day_both_kept(repo):
    acc = _acc(repo)
    coffee = RawTxn(date(2026, 7, 1), Decimal(-25_000), "KOPI DEMO")
    res = repo.import_rows("t1", acc, [coffee, coffee], "csv", "test")
    assert res.rows_new == 2


def test_batch_counts_add_up(repo, conn):
    acc = _acc(repo)
    rows = [RawTxn(date(2026, 7, 1), Decimal(-1), "A"), RawTxn(date(2026, 7, 2), Decimal(-2), "B")]
    repo.import_rows("t1", acc, rows, "csv", "test")
    repo.import_rows("t1", acc, rows + [RawTxn(date(2026, 7, 3), Decimal(-3), "C")], "csv", "test")
    bad = conn.execute("SELECT id FROM import_batch WHERE rows_in <> rows_new + rows_dup OR status <> 'ok'").fetchall()
    assert bad == []


def _load_demo(repo):
    repo.ensure_tenant(demo.DEMO_TENANT)
    data = demo.generate()
    for key, kind, provider, label, last4 in demo.ACCOUNTS:
        acc = repo.add_account(demo.DEMO_TENANT, kind, provider, label, last4)
        repo.import_rows(demo.DEMO_TENANT, acc, data.rows[key], "demo", "demo-generator/1")
    return data, repo.detect_own_transfers(demo.DEMO_TENANT)


def test_own_transfers_paired_exactly(repo):
    data, pairs = _load_demo(repo)
    assert pairs == sum(t.transfer_pairs for t in data.truth.values())
    assert repo.detect_own_transfers(demo.DEMO_TENANT) == 0  # second run finds nothing new


def test_cashflow_matches_ground_truth(repo):
    data, _ = _load_demo(repo)
    for (y, m), truth in data.truth.items():
        start, end = date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1)
        cf = repo.cashflow(demo.DEMO_TENANT, start, end)
        assert cf["income"] == truth.income, (m, cf["income"], truth.income)
        assert cf["spending"] == truth.spending, (m, cf["spending"], truth.spending)
        assert cf["refunds"] == truth.refunds
        assert cf["net"] == truth.income - truth.spending
        assert cf["own_transfer_rows_excluded"] == 2 * truth.transfer_pairs


def test_refund_matching_a_purchase_is_not_hidden_as_transfer(repo):
    repo.ensure_tenant("t1")
    a = repo.add_account("t1", "bank", "demo_bank_a", "A")
    b = repo.add_account("t1", "bank", "demo_bank_b", "B")
    repo.import_rows("t1", a, [RawTxn(date(2026, 7, 1), Decimal(-120_000), "TOKO DEMO")], "csv", "t")
    repo.import_rows("t1", b, [RawTxn(date(2026, 7, 2), Decimal(120_000), "REFUND TOKO DEMO")], "csv", "t")
    assert repo.detect_own_transfers("t1") == 0


def test_manual_transfer_is_excluded_from_cashflow_and_keeps_both_sides(repo):
    repo.ensure_tenant("t1")
    source = repo.add_account("t1", "bank", "bank_a", "Main bank")
    target = repo.add_account("t1", "ewallet", "gopay", "GoPay")
    rows = [
        RawTxn(date(2026, 10, 1), Decimal(-250_000), "TRANSFER TO GOPAY", "own_transfer"),
        RawTxn(date(2026, 10, 1), Decimal(250_000), "TRANSFER FROM MAIN BANK", "own_transfer"),
    ]
    repo.import_manual_rows("t1", source, [rows[0]], "telegram:test:source")
    repo.import_manual_rows("t1", target, [rows[1]], "telegram:test:target")
    repo.detect_own_transfers("t1")
    cf = repo.cashflow("t1", date(2026, 10, 1), date(2026, 11, 1))
    assert cf["income"] == Decimal("0")
    assert cf["spending"] == Decimal("0")
    assert cf["own_transfer_rows_excluded"] == 2
    assert {row["kind"] for row in repo.recent_txns("t1", 10)} == {"own_transfer"}
