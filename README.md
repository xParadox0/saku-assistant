# Saku

A proactive personal finance assistant for people whose money is spread across several bank and e-wallet accounts.

Saku is being built in public. This repository is at **Stage 0**: database schema, ledger logic, synthetic demo data and tests. There is no Telegram bot yet and no real data.

## Why

Many people in Indonesia use three to eight banks and e-wallets. Each app shows its own balance. None shows the whole month, and none acts on it. Popular budgeting apps that offer bank sync do not list Indonesia. Local apps are good at recording transactions, but little else.

Saku aims to:

- turn every account into one honest monthly picture, without counting transfers between your own accounts as spending;
- point out recurring charges and fees;
- answer "should I buy this?" with your real budget and history, and offer a 48-hour wait;
- surface freelance and job leads that match your skills, with a scam filter.

The design is written up in [docs/PRD.md](docs/PRD.md) and [docs/HLD.md](docs/HLD.md).

## Rules the code enforces

- **Read-only on money.** No credential column exists anywhere. A test fails if one is ever added.
- **No number without a source.** SQL computes every figure. A language model may only phrase results.
- **Money is `NUMERIC`, never float.** A float amount is rejected at the boundary.
- **Tenant isolation.** All SQL lives in one module and filters by tenant. A test checks that one user sees nothing of another.
- **Redaction before storage.** Account numbers, phone numbers and emails are removed from descriptions.

## Run the demo

With Docker:

```bash
docker compose up --build --abort-on-container-exit --exit-code-from app
```

Without Docker (starts a throwaway local PostgreSQL):

```bash
pip install -e ".[dev]"
python tools/run_demo_local.py
```

Example output, all synthetic:

```text
own transfers paired: 15

July 2026: income Rp19.500.000, spending Rp2.810.990, surplus Rp16.689.010 (10 own-transfer rows excluded)
```

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

Tests use `TEST_DATABASE_URL` when set, otherwise a throwaway PostgreSQL started by `pgserver`.

## Status

| Stage | Delivers | Status |
|---|---|---|
| 0 | Schema, ledger, own-transfer detection, demo data, tests | done |
| 1 | Telegram bot, manual entry, statement import for two providers | next |
| 2 | Categorisation, weekly review, heartbeats | planned |
| 3 | Purchase check, 48-hour wishlist, budgets and goals | planned |

## Not financial advice

Saku shows your own numbers and general education. It does not recommend investment, lending or insurance products, and it never moves money.

## License

MIT
