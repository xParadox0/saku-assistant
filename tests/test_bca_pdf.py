"""BCA e-statement PDF parser. Fixtures are synthetic PDFs that copy the real layout (tests/bca_fixture.py)."""
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from bca_fixture import Row, Statement, build, sample
from saku import parsers
from saku.parsers import ParseError
from saku.parsers.bca_pdf import BcaPdfParser


def write(tmp_path, data: bytes, name: str = "statement.pdf"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_reads_every_row_with_direction_date_and_kind(tmp_path):
    path = write(tmp_path, build(sample()))
    parser = parsers.pick(path, "bca")
    assert isinstance(parser, BcaPdfParser)
    st = parser.parse_statement(path)
    rows = st.rows
    assert len(rows) == 10
    assert [r.booked_on for r in rows[:3]] == [date(2026, 9, 2), date(2026, 9, 2), date(2026, 9, 5)]
    assert rows[0].amount == Decimal("1500000.00") and rows[1].amount == Decimal("-1500000.00")
    assert rows[2].amount == Decimal("-15000.00") and rows[2].kind == "fee"
    assert rows[8].amount == Decimal("312.50") and rows[8].kind == "normal"     # BUNGA
    assert rows[9].amount == Decimal("-62.50") and rows[9].kind == "fee"        # PAJAK BUNGA
    assert st.opening == Decimal("250000.00")
    assert st.closing == st.opening + sum(r.amount for r in rows)
    assert st.account_last4 == "6789" and st.period == date(2026, 9, 1)


def test_description_joins_continuation_lines_and_drops_repeated_amount(tmp_path):
    rows = BcaPdfParser().parse(write(tmp_path, build(sample())))
    assert rows[0].description == "TRSF E-BANKING CR 0209/FTSCY/WS95031 BUDI CONTOH SAJA"
    assert rows[1].description == "TRSF E-BANKING DB 0209/FTSCY/WS95031 pindah dana ANI CONTOH SAJA"
    assert rows[3].description == "DB DEBIT TOKO*CONTOH QRC 1234567890123456/Q"
    # continuation line that starts in the left column is still part of the row, not a new row
    assert rows[4].description == "TRSF E-BANKING CR 1209/FTSCY/WS95031 TANGGAL :12/09 BUDI CONTOH SAJA"
    assert rows[6].description.endswith("GoPay Bank Contoh ID1234567890123456 CONTOH NAMA SAJA")


def test_row_on_page_two_and_summary_are_read(tmp_path):
    st = sample()
    assert len(st.rows) > st.rows_per_page          # fixture really spans two pages
    rows = BcaPdfParser().parse(write(tmp_path, build(st)))
    assert rows[-1].booked_on == date(2026, 9, 30)


def test_unencrypted_copy_is_also_accepted(tmp_path):
    rows = BcaPdfParser().parse(write(tmp_path, build(sample(), encrypt=False)))
    assert len(rows) == 10


def test_running_balance_mismatch_rejects_whole_file(tmp_path):
    bad = replace(sample(), tamper_saldo_row=3)
    with pytest.raises(ParseError, match="running balance"):
        BcaPdfParser().parse(write(tmp_path, build(bad)))


def test_summary_db_total_mismatch_rejects_whole_file(tmp_path):
    bad = replace(sample(), tamper_db_total=Decimal("1.00"))
    with pytest.raises(ParseError, match="CR/DB totals"):
        BcaPdfParser().parse(write(tmp_path, build(bad)))


def test_db_marker_on_a_continuation_line_is_rejected(tmp_path):
    bad = replace(sample(), stray_db_on_continuation=0)
    with pytest.raises(ParseError, match="continuation line"):
        BcaPdfParser().parse(write(tmp_path, build(bad)))


def test_rows_after_the_summary_are_rejected(tmp_path):
    bad = replace(sample(), rows_after_summary=True)
    with pytest.raises(ParseError, match="after the summary"):
        BcaPdfParser().parse(write(tmp_path, build(bad)))


@pytest.mark.parametrize("period", ["SEPTEMBER 0000", "SEPTEMBER 99999", "SEPTEMBER"])
def test_bad_period_is_a_parse_error_not_a_crash(tmp_path, period):
    bad = replace(sample(), period_text=period)
    with pytest.raises(ParseError):
        BcaPdfParser().parse(write(tmp_path, build(bad)))


def test_unicode_digits_in_period_are_a_parse_error():
    """Fullwidth digits pass str.isdigit(); the fixture font cannot draw them, so feed the line directly."""
    from saku.parsers.bca_pdf import _Line
    line = _Line(top=100, words=[{"text": "PERIODE", "x0": 400}, {"text": ":", "x0": 440},
                                 {"text": "SEPTEMBER", "x0": 450}, {"text": "２０２６", "x0": 500}])
    with pytest.raises(ParseError, match="PERIODE"):
        BcaPdfParser._period([line])


def test_missing_currency_is_rejected(tmp_path):
    with pytest.raises(ParseError, match="MATA UANG"):
        BcaPdfParser().parse(write(tmp_path, build(replace(sample(), show_currency=False))))


def test_credit_starting_with_biaya_is_not_a_fee(tmp_path):
    st = Statement(rows=[Row(3, "BIAYA ADM", Decimal("15000.00"), True, [])])
    rows = BcaPdfParser().parse(write(tmp_path, build(st)))
    assert rows[0].amount > 0 and rows[0].kind == "normal"


def test_unexpected_library_failure_becomes_parse_error(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("internal detail BUDI CONTOH 0123456789")
    monkeypatch.setattr("saku.parsers.bca_pdf._lines", boom)
    with pytest.raises(ParseError) as e:
        BcaPdfParser().parse(write(tmp_path, build(sample())))
    assert "BUDI" not in str(e.value) and "0123456789" not in str(e.value)


def test_page_with_too_many_characters_is_rejected(tmp_path):
    from bca_fixture import bomb
    with pytest.raises(ParseError, match="too much text"):
        BcaPdfParser().parse(write(tmp_path, bomb(60_000)))


def test_closing_balance_mismatch_rejects_whole_file(tmp_path):
    bad = replace(sample(), tamper_closing=Decimal("999.00"))
    with pytest.raises(ParseError, match="closing balance"):
        BcaPdfParser().parse(write(tmp_path, build(bad)))


def test_summary_row_count_mismatch_rejects_whole_file(tmp_path):
    bad = replace(sample(), tamper_cr_count=99)
    with pytest.raises(ParseError, match="count"):
        BcaPdfParser().parse(write(tmp_path, build(bad)))


def test_missing_summary_means_incomplete_statement(tmp_path):
    bad = replace(sample(), omit_summary=True)
    with pytest.raises(ParseError, match="summary"):
        BcaPdfParser().parse(write(tmp_path, build(bad)))


def test_password_protected_pdf_explains_what_to_do(tmp_path):
    path = write(tmp_path, build(sample(), user_password="secret123"))
    assert parsers.needs_password(path)
    assert not BcaPdfParser().can_parse(path)        # not claimed blindly; the sandbox gives the message
    with pytest.raises(ParseError, match="password"):
        BcaPdfParser().parse(path)


def test_error_messages_never_contain_row_content(tmp_path):
    bad = replace(sample(), tamper_saldo_row=0)
    with pytest.raises(ParseError) as e:
        BcaPdfParser().parse(write(tmp_path, build(bad)))
    msg = str(e.value)
    for secret in ("BUDI", "CONTOH", "1500000", "1,500,000", "0123456789"):
        assert secret not in msg


def test_december_statement_rows_get_the_statement_year(tmp_path):
    st = Statement(month=12, year=2026, rows=[Row(31, "BIAYA ADM", Decimal("10000.00"), False, [])])
    rows = BcaPdfParser().parse(write(tmp_path, build(st)))
    assert rows[0].booked_on == date(2026, 12, 31)


def test_non_bca_pdf_and_csv_are_not_claimed(tmp_path):
    from bca_fixture import _pdf
    other = write(tmp_path, _pdf([[(50, 100, "INVOICE"), (50, 120, "TOTAL 10.000")]]), "other.pdf")
    assert not BcaPdfParser().can_parse(other)
    assert not BcaPdfParser().can_parse(write(tmp_path, b"date,amount,description\n", "x.csv"))


def test_transaction_amount_over_database_range_is_rejected(tmp_path, monkeypatch):
    parser = BcaPdfParser()
    original = parser._mutasi
    changed = False

    def over_range(line):
        nonlocal changed
        amount, credit = original(line)
        if amount is not None and not changed:
            changed = True
            return Decimal("10000000000000000.00"), credit
        return amount, credit

    monkeypatch.setattr(parser, "_mutasi", over_range)
    with pytest.raises(ParseError, match="supported numeric range"):
        parser.parse(write(tmp_path, build(sample())))
    assert changed
    assert not BcaPdfParser().can_parse(write(tmp_path, b"%PDF-1.4 not really", "broken.pdf"))


def test_statement_with_no_transactions_is_rejected(tmp_path):
    with pytest.raises(ParseError, match="no transactions"):
        BcaPdfParser().parse(write(tmp_path, build(Statement(rows=[]))))
