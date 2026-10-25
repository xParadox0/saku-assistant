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

July 2026: income Rp19.500.000, spending Rp4.657.980, surplus Rp14.842.020 (10 own-transfer rows excluded)
```

## Design

Server-rendered HTML, no JavaScript. A bento overview: what is left this month, where the money went (category ring), plain-language observations computed from SQL, month-by-month trend, accounts, latest activity and charges that repeat every month. Motion is CSS only and switches off for users who ask for reduced motion.

- Logo: three blocks (your accounts) dropping into one pocket ("saku" means pocket). Vector files in `saku/web/static/brand/`, rebuilt with `tools/build_logo.py`.
- Palette from the logo: pocket green `#0F6B4F`, mint `#A7D7B5`, coral `#E67D63`, honey `#F0BE63`, ink `#1F2D28` on warm paper `#F7F3EA`.
- Typefaces: Nunito (headings, figures, wordmark) and Plus Jakarta Sans (text), both SIL OFL, self-hosted (`saku/web/static/fonts/`).
- Icons: [Phosphor Icons](https://github.com/phosphor-icons/core) duotone, MIT (`saku/web/static/icons/LICENSE-phosphor.txt`), rebuilt with `tools/build_icons.py`.
- The stylesheet passes the [taste-skill](https://github.com/Leonxlnx/taste-skill) lint with no failures.

## Run the dashboard

```bash
docker compose --profile dashboard up --build web
# open http://127.0.0.1:8090
```

Without Docker: `python tools/run_dashboard_local.py`.

The demo dashboard signs you in as a fixed demo user with synthetic data, and only listens on 127.0.0.1. It has four pages:

| Page | What it does |
|---|---|
| Overview | Latest-month Money in/out and per-account movements including internal transfers; income/spending analysis excluding internal transfers; monthly trend and recent transactions |
| Accounts | Add, rename and delete accounts; upload a statement per account (BCA e-statement PDF or Saku CSV); download the CSV template |
| Transactions | Review transactions in a compact responsive table; expand one row to edit or delete it |
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
| 0c | Dashboard deployed with Saku email-code login; Access remains on Ops | done |
| 1a | Telegram link, Haiku 5.5 manual entry, explicit follow-up, internal-transfer pairing and Dashboard write | done |
| 1a.1 | Web transaction add, edit and delete management | done |
| 1a.2 | Telegram voice notes transcribed locally, and cash entry | done |
| 1b | Statement import for two providers | planned |
| 2 | Categorisation, weekly review, heartbeats | planned |
| 3 | Purchase check, 48-hour wishlist, budgets and goals | planned |

## Not financial advice

Saku shows your own numbers and general education. It does not recommend investment, lending or insurance products, and it never moves money.

## License

MIT
