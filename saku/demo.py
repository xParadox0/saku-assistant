"""Synthetic demo data. Deterministic, fictional, safe to publish.

Three fictional accounts, three months. The generator also returns the expected totals, computed here from
its own labels and independently of the SQL path, so tests can check the database against ground truth.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .normalise import RawTxn

DEMO_TENANT = "demo"
ACCOUNTS = [  # (key, kind, provider, label, last4)
    ("bank_a", "bank", "demo_bank_a", "Bank A (demo)", "1111"),
    ("bank_b", "bank", "demo_bank_b", "Bank B (demo)", "2222"),
    ("wallet", "ewallet", "demo_wallet", "Wallet (demo)", None),
]
FREELANCE_AMOUNTS = [1_750_000, 2_250_000, 2_750_000, 4_000_000]  # never equal to a transfer amount


@dataclass
class MonthTruth:
    income: Decimal = Decimal(0)
    spending: Decimal = Decimal(0)
    refunds: Decimal = Decimal(0)
    transfer_pairs: int = 0


@dataclass
class DemoData:
    rows: dict[str, list[RawTxn]] = field(default_factory=lambda: {k: [] for k, *_ in ACCOUNTS})
    truth: dict[tuple[int, int], MonthTruth] = field(default_factory=dict)


def _d(n: int) -> Decimal:
    return Decimal(n)


def generate(year: int = 2026, months: tuple[int, ...] = (7, 8, 9), seed: int = 42) -> DemoData:
    rnd = random.Random(seed)
    out = DemoData()

    def add(key, day, month, amount, desc, kind="normal"):
        out.rows[key].append(RawTxn(date(year, month, day), _d(amount), desc, kind))

    for m in months:
        t = out.truth.setdefault((year, m), MonthTruth())

        add("bank_a", 1, m, 15_000_000, "GAJI PT CONTOH SEJAHTERA"); t.income += 15_000_000
        for _ in range(rnd.randint(0, 2)):
            amt = rnd.choice(FREELANCE_AMOUNTS)
            add("bank_b", rnd.randint(2, 27), m, amt, "TRSF CR KLIEN DEMO"); t.income += amt

        # own transfers: one bank to bank, four top-ups to the wallet
        add("bank_a", 5, m, -3_000_000, "TRSF E-BANKING DB KE REK 2222")
        add("bank_b", 6, m, 3_000_000, "TRSF E-BANKING CR DARI REK 1111"); t.transfer_pairs += 1
        for day in (3, 10, 17, 24):
            add("bank_a", day, m, -500_000, "TOPUP WALLET DEMO")
            add("wallet", day, m, 500_000, "TOP UP DARI BANK A"); t.transfer_pairs += 1

        spend = [
            *[("bank_a", rnd.randint(1, 28), -rnd.randrange(150_000, 600_001, 1000), "SUPERMARKET DEMO") for _ in range(4)],
            *[("wallet", rnd.randint(1, 28), -rnd.randrange(20_000, 90_001, 1000), "WARUNG DEMO") for _ in range(15)],
            *[("wallet", rnd.randint(1, 28), -rnd.randrange(15_000, 60_001, 1000), "OJEK DEMO") for _ in range(10)],
            ("bank_b", 12, -54_990, "STREAMING DEMO SUBS"),
            ("bank_a", 20, -rnd.randrange(350_000, 600_001, 1000), "TOKEN LISTRIK DEMO"),
        ]
        for key, day, amt, desc in spend:
            add(key, day, m, amt, desc); t.spending += -amt

        # more variety for the dashboard; a second generator so the original sequence above stays unchanged
        rnd2 = random.Random(seed * 100 + m)
        extra = [
            *[("wallet", rnd2.randint(1, 28), -rnd2.randrange(18_000, 45_001, 1000), "KOPI SUSU DEMO") for _ in range(4 + m % 3)],
            *[("bank_b", rnd2.randint(1, 28), -rnd2.randrange(120_000, 750_001, 1000), "TOKO ONLINE DEMO") for _ in range(1 + m % 2)],
            ("bank_a", rnd2.randint(1, 28), -rnd2.randrange(60_000, 240_001, 1000), "APOTEK DEMO"),
            ("bank_b", 7, -349_000, "INTERNET RUMAH DEMO"),
            ("bank_b", 18, -59_990, "MUSIC DEMO SUBS"),
            *[("wallet", rnd2.randint(1, 28), -5_000, "PARKIR DEMO") for _ in range(3)],
        ]
        for key, day, amt, desc in extra:
            add(key, day, m, amt, desc); t.spending += -amt
        add("bank_a", 28, m, -15_000, "BIAYA ADM", kind="fee"); t.spending += 15_000

        if m == months[1]:
            add("bank_b", 15, m, 120_000, "REFUND TOKO DEMO", kind="refund")
            t.refunds += 120_000; t.spending -= 120_000

    for key in out.rows:
        out.rows[key].sort(key=lambda r: r.booked_on)
    return out
