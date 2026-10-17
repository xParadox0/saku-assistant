"""Command line entry point.

    python -m saku init-db                         apply the schema
    python -m saku demo                            load synthetic demo data and print monthly cash flow
    python -m saku add-member EMAIL TENANT [admin] allow an email (verified by Cloudflare Access) into a tenant
    python -m saku serve [--host H] [--port P]     run the dashboard
"""
from __future__ import annotations

import sys
from datetime import date

from . import demo
from .db import connect, init_schema
from .format import rupiah
from .repo import Repo

DEMO_EMAIL = "demo@saku.local"


def load_demo(repo: Repo) -> demo.DemoData:
    """Load the synthetic demo tenant once. Safe to call repeatedly."""
    repo.ensure_tenant(demo.DEMO_TENANT)
    data = demo.generate()
    existing = {a["provider"]: a["id"] for a in repo.accounts(demo.DEMO_TENANT)}
    for key, kind, provider, label, last4 in demo.ACCOUNTS:
        acc = existing.get(provider) or repo.add_account(demo.DEMO_TENANT, kind, provider, label, last4)
        res = repo.import_rows(demo.DEMO_TENANT, acc, data.rows[key], source="demo", parser="demo-generator/1")
        print(f"import {label:<15} rows {res.rows_in:>3}  new {res.rows_new:>3}  duplicate {res.rows_dup:>3}")
    print(f"own transfers paired: {repo.detect_own_transfers(demo.DEMO_TENANT)}")
    repo.add_member(DEMO_EMAIL, demo.DEMO_TENANT, "admin")
    return data


def run_demo() -> None:
    conn = connect()
    init_schema(conn)
    repo = Repo(conn)
    data = load_demo(repo)
    print()
    for (y, m) in sorted(data.truth):
        start, end = date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1)
        cf = repo.cashflow(demo.DEMO_TENANT, start, end)
        verdict = "surplus" if cf["net"] >= 0 else "deficit"
        print(f"{start:%B %Y}: income {rupiah(cf['income'])}, spending {rupiah(cf['spending'])}, "
              f"{verdict} {rupiah(cf['net'])} ({cf['own_transfer_rows_excluded']} own-transfer rows excluded)")
        for a in cf["by_account"]:
            print(f"   {a['label']:<15} in {rupiah(a['money_in']):>14}   out {rupiah(a['money_out']):>14}")
    print("\nAll figures come from SQL over synthetic data. No real accounts are involved.")


def serve(argv: list[str]) -> int:
    import os

    import uvicorn

    from .web.app import app_from_env

    host = argv[argv.index("--host") + 1] if "--host" in argv else "127.0.0.1"
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 8090
    if os.environ.get("SAKU_LOAD_DEMO") == "1":
        with connect() as c:
            init_schema(c)
            load_demo(Repo(c))
    uvicorn.run(app_from_env(host), host=host, port=port, proxy_headers=False, server_header=False)
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "demo"
    if cmd == "init-db":
        init_schema(connect()); print("schema applied"); return 0
    if cmd == "demo":
        run_demo(); return 0
    if cmd == "add-member" and len(argv) >= 4:
        conn = connect(); init_schema(conn)
        Repo(conn).add_member(argv[2], argv[3], argv[4] if len(argv) > 4 else "user")
        print(f"member {argv[2].lower()} -> tenant {argv[3]}"); return 0
    if cmd == "serve":
        return serve(argv)
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
