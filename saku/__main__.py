"""Command line entry point.

    python -m saku init-db          apply the schema
    python -m saku demo             load synthetic demo data and print monthly cash flow
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal

from . import demo
from .db import connect, init_schema
from .repo import Repo


def rupiah(v: Decimal) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}Rp{abs(int(v)):,}".replace(",", ".")


def run_demo() -> None:
    conn = connect()
    init_schema(conn)
    repo = Repo(conn)
    repo.ensure_tenant(demo.DEMO_TENANT)
    data = demo.generate()

    existing = {a["provider"]: a["id"] for a in repo.accounts(demo.DEMO_TENANT)}
    for key, kind, provider, label, last4 in demo.ACCOUNTS:
        acc = existing.get(provider) or repo.add_account(demo.DEMO_TENANT, kind, provider, label, last4)
        res = repo.import_rows(demo.DEMO_TENANT, acc, data.rows[key], source="demo", parser="demo-generator/1")
        print(f"import {label:<15} rows {res.rows_in:>3}  new {res.rows_new:>3}  duplicate {res.rows_dup:>3}")
    pairs = repo.detect_own_transfers(demo.DEMO_TENANT)
    print(f"own transfers paired: {pairs}\n")

    for (y, m) in sorted(data.truth):
        start, end = date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1)
        cf = repo.cashflow(demo.DEMO_TENANT, start, end)
        verdict = "surplus" if cf["net"] >= 0 else "deficit"
        print(f"{start:%B %Y}: income {rupiah(cf['income'])}, spending {rupiah(cf['spending'])}, "
              f"{verdict} {rupiah(cf['net'])} ({cf['own_transfer_rows_excluded']} own-transfer rows excluded)")
        for a in cf["by_account"]:
            print(f"   {a['label']:<15} in {rupiah(a['money_in']):>14}   out {rupiah(a['money_out']):>14}")
    print("\nAll figures come from SQL over synthetic data. No real accounts are involved.")


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "demo"
    if cmd == "init-db":
        init_schema(connect()); print("schema applied"); return 0
    if cmd == "demo":
        run_demo(); return 0
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
