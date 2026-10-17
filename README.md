# Saku

A proactive personal finance assistant for people whose money is spread across several bank and e-wallet accounts.

Saku is being built in public. This repository is at **Stage 0b**: database schema, ledger logic, a web dashboard, synthetic demo data and tests. There is no Telegram bot yet and no real data.

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

## Design

Server-rendered HTML, no JavaScript, two self-hosted typefaces (Space Grotesk and Inter, both SIL OFL, licence files in `saku/web/static/fonts/`). Flat near-black surfaces separated by tone, one cyan accent for income and the primary action, neutral grey for spending, red only for a deficit or a failed import. The stylesheet passes the [taste-skill](https://github.com/Leonxlnx/taste-skill) lint with no failures. Public dashboard shots for reference were studied for layout principles only; no third-party asset is used.

## Run the dashboard

```bash
docker compose --profile dashboard up --build web
# open http://127.0.0.1:8090
```

Without Docker: `python tools/run_dashboard_local.py`.

The demo dashboard signs you in as a fixed demo user with synthetic data, and only listens on 127.0.0.1. It has four pages:

| Page | What it does |
|---|---|
| Overview | Latest month income, spending and surplus across all accounts, monthly trend, totals per account, recent transactions |
| Accounts | Add, rename and delete accounts; upload a statement per account; download the CSV template |
| Imports | Every upload with rows read, new and duplicate, including failed ones and why |
| Ops | Admin only. Per-tenant counts, failed imports and component heartbeats, without any transaction content |

For real data the dashboard runs behind Cloudflare Access. It verifies the signed Access token itself and maps the email to a tenant through the `member` table:

```bash
SAKU_AUTH_MODE=cloudflare SAKU_CF_TEAM=https://<team>.cloudflareaccess.com SAKU_CF_AUD=<aud-tag> \
SAKU_SECRET=<32+ random chars> DATABASE_URL=... python -m saku serve --host 127.0.0.1
python -m saku add-member you@example.com your-tenant admin
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
| 0b | Dashboard: overview, accounts, upload, imports, ops page, Access JWT checks | done |
| 0c | Dashboard deployed behind Cloudflare Access | next |
| 1 | Telegram bot, manual entry, statement import for two providers | planned |
| 2 | Categorisation, weekly review, heartbeats | planned |
| 3 | Purchase check, 48-hour wishlist, budgets and goals | planned |

## Not financial advice

Saku shows your own numbers and general education. It does not recommend investment, lending or insurance products, and it never moves money.

## License

MIT
