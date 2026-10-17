"""Categories and insights: deterministic labels, numbers that match SQL, no advice."""
from datetime import date
from decimal import Decimal

from saku import insights
from saku.normalise import RawTxn


def _acc(repo, tenant="t1"):
    repo.ensure_tenant(tenant)
    return repo.add_account(tenant, "bank", "bank_x", "Main")


def test_builtin_categories_and_kinds(repo):
    a = _acc(repo)
    repo.import_rows("t1", a, [
        RawTxn(date(2026, 9, 1), Decimal(10_000_000), "SALARY"),
        RawTxn(date(2026, 9, 2), Decimal(-45_000), "KOPI SUSU"),
        RawTxn(date(2026, 9, 3), Decimal(-25_000), "GOJEK RIDE"),
        RawTxn(date(2026, 9, 4), Decimal(-54_990), "NETFLIX.COM"),
        RawTxn(date(2026, 9, 5), Decimal(-400_000), "TOKEN LISTRIK PLN"),
        RawTxn(date(2026, 9, 6), Decimal(-15_000), "BIAYA ADM", "fee"),
        RawTxn(date(2026, 9, 7), Decimal(-99_000), "SOMETHING UNKNOWN"),
        RawTxn(date(2026, 9, 8), Decimal(-59_990), "MUSIC SUBS"),
        RawTxn(date(2026, 9, 9), Decimal(-200_000), "PLN PASCABAYAR"),
        RawTxn(date(2026, 9, 10), Decimal(-30_000), "TOL CIPULARANG"),
    ], "csv", "test")
    repo.apply_categories("t1")
    got = {r["description"]: r["category"] for r in repo.recent_txns("t1", 20)}
    assert got == {"SALARY": "income", "KOPI SUSU": "food", "GOJEK RIDE": "transport", "NETFLIX.COM": "subscriptions",
                   "TOKEN LISTRIK PLN": "bills", "BIAYA ADM": "fees", "SOMETHING UNKNOWN": "other",
                   "MUSIC SUBS": "subscriptions", "PLN PASCABAYAR": "bills", "TOL CIPULARANG": "transport"}


def test_user_label_is_never_overwritten(repo, conn):
    a = _acc(repo)
    repo.import_rows("t1", a, [RawTxn(date(2026, 9, 2), Decimal(-45_000), "KOPI SUSU")], "csv", "test")
    conn.execute("UPDATE txn SET category = 'health', cat_source = 'user' WHERE tenant_id = 't1'")
    repo.apply_categories("t1")
    assert repo.recent_txns("t1", 1)[0]["category"] == "health"


def test_tenant_rule_beats_builtin(repo, conn):
    a = _acc(repo)
    conn.execute("INSERT INTO category_rule (tenant_id, pattern, category, origin) VALUES ('t1', 'kopi', 'health', 'user')")
    repo.import_rows("t1", a, [RawTxn(date(2026, 9, 2), Decimal(-45_000), "KOPI SUSU")], "csv", "test")
    repo.apply_categories("t1")
    assert repo.recent_txns("t1", 1)[0]["category"] == "health"


def test_category_totals_equal_cashflow_spending(repo):
    a = _acc(repo)
    repo.import_rows("t1", a, [
        RawTxn(date(2026, 9, 1), Decimal(5_000_000), "SALARY"),
        RawTxn(date(2026, 9, 2), Decimal(-45_000), "KOPI"),
        RawTxn(date(2026, 9, 3), Decimal(-120_500), "INDOMARET"),
        RawTxn(date(2026, 9, 4), Decimal(-15_000), "BIAYA ADM", "fee"),
    ], "csv", "test")
    repo.apply_categories("t1")
    cats = repo.spending_by_category("t1", date(2026, 9, 1), date(2026, 10, 1))
    cf = repo.cashflow("t1", date(2026, 9, 1), date(2026, 10, 1))
    assert sum(c["amount"] for c in cats) == cf["spending"] + cf["refunds"]


def test_recurring_needs_two_months(repo):
    a = _acc(repo)
    repo.import_rows("t1", a, [RawTxn(date(2026, 9, 12), Decimal(-54_990), "NETFLIX.COM")], "csv", "test")
    repo.apply_categories("t1")
    assert repo.recurring_charges("t1", date(2026, 9, 1)) == []
    repo.import_rows("t1", a, [RawTxn(date(2026, 8, 12), Decimal(-54_990), "NETFLIX.COM")], "csv", "test")
    repo.apply_categories("t1")
    rec = repo.recurring_charges("t1", date(2026, 9, 1))
    assert len(rec) == 1 and rec[0]["months"] == 2 and rec[0]["average"] == Decimal(54_990)


def _cf(income, spending):
    return {"income": Decimal(income), "spending": Decimal(spending), "net": Decimal(income) - Decimal(spending),
            "refunds": Decimal(0)}


def test_insight_numbers_come_from_inputs():
    notes = insights.build(_cf(10_000_000, 4_000_000), None, [], [], [], [])
    assert notes[0].title == "You kept 60% of your income"
    assert "Rp6.000.000" in notes[0].body and "Rp4.000.000" in notes[0].body


def test_deficit_is_named_plainly():
    notes = insights.build(_cf(3_000_000, 4_250_000), None, [], [], [], [])
    assert notes[0].tone == "watch" and "Rp1.250.000" in notes[0].body


def test_category_move_and_no_advice_words():
    cats = [{"category": "food", "amount": Decimal(900_000), "count": 20}]
    prev = [{"category": "food", "amount": Decimal(600_000), "count": 14}]
    notes = insights.build(_cf(10_000_000, 2_000_000), _cf(10_000_000, 1_800_000), cats, prev, [], [])
    move = [n for n in notes if "Food" in n.title][0]
    assert move.title == "Food & drink went up Rp300.000"
    text = " ".join(n.title + " " + n.body for n in notes).lower()
    for banned in ("invest", "loan", "pinjam", "saham", "crypto", "reksa"):
        assert banned not in text
