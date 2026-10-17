from decimal import Decimal

from saku.format import rupiah, rupiah_short, signed_rupiah


def test_rupiah():
    assert rupiah(Decimal("16689010")) == "Rp16.689.010"
    assert rupiah(Decimal("-45000")) == "-Rp45.000"


def test_signed():
    assert signed_rupiah(Decimal("1000")) == "+Rp1.000"
    assert signed_rupiah(Decimal("-1000")) == "-Rp1.000"
    assert signed_rupiah(0) == "Rp0"


def test_short():
    assert rupiah_short(Decimal("19500000")) == "19,5 jt"
    assert rupiah_short(Decimal("2810990")) == "2,8 jt"
    assert rupiah_short(Decimal("850000")) == "850 rb"
    assert rupiah_short(Decimal("3000000")) == "3 jt"
