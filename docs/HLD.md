# HLD: Personal Finance Assistant (working name "Saku")

Status: DRAFT v0.1, 2026-10-17. Implements `docs/PRD.md` v0.1. Requirement IDs (F1..F21) refer to the PRD.

## 1. Binding principles

Each principle can veto a later design choice.

1. **Read-only on money.** Nothing in the system can move money or log in to a bank. No credential field exists in the schema, so none can be filled by mistake.
2. **No number without a source.** SQL computes every figure. The model receives query results and phrases them. A reply containing an amount that is not in the query result is a bug, and a test checks for it.
3. **Silence is a bug.** Every ingest path and scheduler records a heartbeat. A stale heartbeat produces a message to the user. A dead component must look different from a quiet month.
4. **Designed for many, opened to one.** `tenant_id` everywhere and one data-access layer from day one. Registration, billing and a public UI are deferred.

## 2. System diagram

```text
                 ┌─────────────── Telegram (user) ───────────────┐
                 │  chat: "mau beli X 2jt?" / upload PDF / "makan 45rb gopay"
                 ▼                                                ▲
          ┌─────────────┐     intents      ┌──────────────────┐   │ replies, reviews,
          │  bot (API)  │ ───────────────▶ │  assistant core  │ ──┘ nudges
          └─────────────┘                  │  (rules + tools) │
                 │ files                   └──────────────────┘
                 ▼                            │ SQL only via repo layer
          ┌─────────────┐  transactions   ┌──────────────────┐
          │  importers  │ ──────────────▶ │   PostgreSQL     │
          │ pdf/csv/mail│  (normalised)   │  tenant_id rows  │
          └─────────────┘                 └──────────────────┘
                 ▲                            ▲          ▲
  mail label ────┘ (read-only, F12)           │          │
                                    ┌─────────┴───┐  ┌───┴──────────────┐
                                    │  scheduler  │  │ income scout     │
                                    │ reviews,    │  │ public job APIs, │
                                    │ reminders,  │  │ scam filter      │
                                    │ heartbeats  │  └──────────────────┘
                                    └─────────────┘
  model API (categorise, phrase) ◀── redacted text only, never raw statements
```

## 3. Components

### 3.1 Bot
- Dedicated Telegram bot for this project. The token lives only in the deployment `.env` (mode 600) and is never committed.
- Telegram Bot API, long polling (no public webhook needed in v1).
- Link-first. The web account (email verified by Cloudflare Access) is the root identity. The bot trusts a Telegram user only after the member redeems a one-time code from Settings > Connect Telegram, sent as a `t.me/<bot>?start=<code>` deep link.
  - Codes: 144-bit random, single use, 10 minutes, stored only as SHA-256, a new code cancels older ones.
  - `telegram_link` maps the numeric Telegram user id (never the username) to one member and tenant. One Telegram account per member; linking a new one replaces the old.
  - A Telegram account already linked to another member cannot be taken over with a new code.
  - Unlinked senders get fixed instructions; nothing they send is stored or logged. Group chats are refused.
  - Unlink from the dashboard or with `/unlink`.
- Accepts text, PDF, CSV and photos (Stage 1). Files go to a temp directory, are parsed, then deleted.

### 3.2 Importers (F2, F3, F12)
One parser per source behind one interface:

```python
class StatementParser(Protocol):
    bank: str                                   # provider slug, e.g. "bank_a", "ewallet_b"
    def can_parse(self, path: Path) -> bool: ...
    def parse(self, path: Path) -> list[RawTxn]: ...   # never touches the database
```

- Each parser has golden-file tests made from **redacted or synthetic** samples. Real statements never enter the repository.

Source matrix. The first user's real providers are listed in a private file outside the repository. For each source Stage 1 records:

```text
source   kind (bank | ewallet)   e-statement (pdf | csv | none)   per-txn email (yes | no)   fallback (manual | screenshot OCR)
```

E-wallet top-ups usually also appear as an outflow on a bank statement. The normaliser treats a bank-to-own-e-wallet top-up as `own_transfer` once the e-wallet is registered as the user's account, so it is not counted as spending twice.
- PDF text extraction is local (pypdf/pdfplumber). OCR is only for scanned statements and receipt photos (F17).
- The email reader (F12) watches one label the user forwards notifications to. It needs read-only mail scope and never sends mail.
- A failed parse is reported to the user with the file name and the parser name. It is never silently skipped.

### 3.3 Normaliser (F4, F5)
- Converts `RawTxn` into the stored shape and computes `fingerprint = sha256(tenant, account, date, amount, normalised_description)` for dedupe.
- An email notification and a statement line for the same transaction match on account, amount and date within 2 days. The statement version wins.
- Own-transfer detection pairs an outflow and an inflow of the same amount between the tenant's accounts within 3 days. The pair is marked `kind='own_transfer'` and excluded from income and spending.

### 3.4 Categoriser (F6)
Order of authority, highest first:
1. User correction for this transaction.
2. User rule ("anything with GRAB is Transport").
3. Built-in rules (merchant keyword lists for Indonesia).
4. Model suggestion with confidence. Under 0.7 it is marked `needs_review`.

Corrections create rules automatically after the same correction happens twice.

### 3.5 Assistant core (F7, F8, F10, F13, F16)
The model never sees the database. It calls a fixed set of tools, each a parameterised SQL query through the repository layer:

```text
cashflow(period)                 income, spending, net, by account
category_spend(period, category)
budget_status(month)             budget, spent, remaining per category
similar_purchases(text, months)  past transactions that look alike
goal_status()                    savings goals and progress
recurring()                      detected subscriptions and fees
balance_estimate()               last statement balance + later known transactions
```

The purchase check (F8):
1. Parse the item, amount and category from the message.
2. Call `budget_status`, `goal_status`, `similar_purchases`.
3. Reply with those numbers, and offer to hold the item in the wishlist for 48 hours.
4. The scheduler asks again after 48 hours: still want it?

The model phrases the reply. A checker compares every amount in the reply with the tool results. If one does not match, the reply is replaced by a plain template built from the tool results.

### 3.6 Scheduler (F10, F14, F16, heartbeats)
- Weekly review Monday 07:30 WIB, monthly review on the 1st, wishlist follow-ups, bill reminders.
- Respects quiet hours (21:00 to 07:00 WIB) and the 1-nudge-per-day limit.
- Writes a heartbeat row for itself and for each ingest source on every run.

### 3.7 Income scout (F15)
- v1 scope: freelance and job openings only (decided 2026-10-17). Local side work is out of scope.
- Reads public job and freelance sources with open APIs (for example Remotive, Himalayas, Jobicy, Arbeitnow and Lever postings), matched against a `skill_profile` the user writes.
- Scam filter: reject on "deposit first", "guaranteed return", MLM terms, pinjol, judi, crypto doubling, payment to apply.
- Delivers a weekly digest of at most 5 items, each with its source URL.

### 3.8 Data model (PostgreSQL)

```sql
CREATE TABLE tenant (
    id          TEXT PRIMARY KEY,                -- 'local' for the first user
    telegram_id BIGINT UNIQUE,                   -- how the bot finds the tenant
    tz          TEXT NOT NULL DEFAULT 'Asia/Jakarta',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE account (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    kind        TEXT NOT NULL,                   -- bank | ewallet | cash
    provider    TEXT NOT NULL,                   -- provider slug; picks the parser
    label       TEXT NOT NULL,                   -- what the user calls it
    last4       TEXT,                            -- masked number, never the full number
    currency    TEXT NOT NULL DEFAULT 'IDR'
    -- no username, password, PIN or OTP column exists, on purpose (principle 1)
);

CREATE TABLE import_batch (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    account_id  BIGINT REFERENCES account(id),
    source      TEXT NOT NULL,                   -- pdf | csv | email | manual | receipt
    file_sha256 TEXT,                            -- proves which file was imported; file itself is deleted
    parser      TEXT,                            -- parser name and version, for re-runs
    status      TEXT NOT NULL,                   -- ok | partial | failed
    rows_in     INT, rows_new INT, rows_dup INT, -- the user sees these counts
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE txn (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    account_id  BIGINT NOT NULL REFERENCES account(id),
    batch_id    BIGINT REFERENCES import_batch(id),
    booked_on   DATE NOT NULL,                   -- bank date
    amount      NUMERIC(18,2) NOT NULL,          -- negative = out; NUMERIC, never float, for money
    currency    TEXT NOT NULL DEFAULT 'IDR',
    description TEXT NOT NULL,                   -- redacted before it reaches any model
    kind        TEXT NOT NULL DEFAULT 'normal',  -- normal | own_transfer | fee | refund
    category    TEXT,
    cat_source  TEXT,                            -- user | rule | builtin | model; explains the label
    cat_conf    REAL,                            -- model confidence; < 0.7 means needs_review
    fingerprint TEXT NOT NULL,                   -- dedupe key, see 3.3
    transfer_pair BIGINT REFERENCES txn(id),     -- the other side of an own transfer
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, fingerprint)
);

CREATE TABLE category_rule (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    pattern     TEXT NOT NULL,                   -- case-insensitive substring or regex
    category    TEXT NOT NULL,
    origin      TEXT NOT NULL,                   -- user | learned (after 2 identical corrections)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE budget (
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    month       DATE NOT NULL,                   -- first day of month
    category    TEXT NOT NULL,
    amount      NUMERIC(18,2) NOT NULL,
    PRIMARY KEY (tenant_id, month, category)
);

CREATE TABLE goal (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    name        TEXT NOT NULL,
    target      NUMERIC(18,2) NOT NULL,
    due_on      DATE,
    saved       NUMERIC(18,2) NOT NULL DEFAULT 0 -- user-reported; the system cannot move money
);

CREATE TABLE wishlist (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    item        TEXT NOT NULL,
    amount      NUMERIC(18,2),
    asked_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- when the user asked
    remind_at   TIMESTAMPTZ NOT NULL,                -- asked_at + 48h; difference is the cooling-off
    outcome     TEXT                                 -- bought | skipped | still_waiting; measures F8
);

CREATE TABLE nudge (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    kind        TEXT NOT NULL,                   -- review | budget | recurring | bill | wishlist | income
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    body        TEXT NOT NULL,
    sources     JSONB NOT NULL,                  -- tool calls and results behind every number (principle 2)
    reaction    TEXT                             -- helpful | not_helpful | muted; tunes the rate limit
);

CREATE TABLE heartbeat (
    tenant_id   TEXT NOT NULL,
    component   TEXT NOT NULL,                   -- scheduler | mail_reader | importer:<bank>
    last_ok     TIMESTAMPTZ,
    last_error  TEXT,
    PRIMARY KEY (tenant_id, component)
);

CREATE TABLE member (
    email       TEXT PRIMARY KEY,                -- verified by Cloudflare Access, stored lowercase
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    role        TEXT NOT NULL                    -- user | admin; admin sees ops metadata, never other tenants' rows
);
```

Every repository function takes `tenant_id` as its first argument and puts it in the `WHERE` clause. Raw SQL outside the repository layer fails code review.

### 3.9 Dashboard (F21, F22)

Server-rendered pages (FastAPI + Jinja2), no JavaScript. The page Content-Security-Policy sets `script-src 'none'`, so an injected script cannot run even if escaping ever failed.

```text
/              overview: latest month income, spending, surplus/deficit, trend, by account, recent rows
/accounts      list, add, rename, delete (only when empty), upload a statement per account
/imports       every import batch, including failures and their error text
/admin         ops view, admin role only: tenants, heartbeats, recent batches (metadata only)
/healthz       liveness, no data
```

Identity and access:

```text
Browser ──▶ Cloudflare Access (email login) ──▶ Cloudflare Tunnel ──▶ 127.0.0.1:8090 (dashboard)
```

- Identity comes only from the `Cf-Access-Jwt-Assertion` JWT. The dashboard verifies its RS256 signature against the team's published keys, plus `aud`, `iss` and `exp`. The unsigned `Cf-Access-Authenticated-User-Email` header is ignored, and a test checks that it alone gets a 401.
- The verified email is looked up in the `member` table, which maps it to one tenant and a role (`user` or `admin`). An email that passed Access but is not a member gets 403. Access decides who may reach the site; `member` decides what they see.
- Every POST carries an HMAC token bound to the user's email. A token from another user is rejected.
- Uploads: 5 MB limit, written to a private temp directory, parsed, then the directory is deleted. Only the SHA-256 of the file is stored. A failed parse is recorded as a failed batch with a message that never contains row contents.
- The admin page uses separate repository methods that return counts, statuses and timestamps only. A test asserts that descriptions, amounts and account labels of other tenants never appear on it.
- An empty heartbeat list is shown as a warning. Nothing reporting is not the same as everything fine (principle 3).
- Dev mode (fixed demo identity) refuses to start on a non-loopback address, except inside the demo container whose host port is bound to loopback.

The first parser is Saku's own CSV format (`date,amount,description[,kind]`), so any account can be imported before its bank-specific parser exists. Bank-specific PDF and CSV parsers are added in Stage 1.

## 4. Main flows

### 4.1 Statement import
1. User sends a PDF in Telegram.
2. Bot stores it in a temp directory and records `file_sha256`.
3. `can_parse` picks the parser. No parser matches: reply "format not supported yet", keep nothing.
4. Parser returns rows. The normaliser dedupes and detects own transfers. The categoriser labels.
5. The batch row records counts. The temp file is deleted.
6. Reply: "Bank A Oktober: 84 transaksi, 79 baru, 5 duplikat, 6 perlu dicek kategorinya."

### 4.2 Purchase check
See 3.5. The 48-hour follow-up is a scheduler job keyed on `wishlist.remind_at`.

### 4.3 Failure flow
- Parser exception: batch `status='failed'`, error saved, the user is told the file name and that nothing was imported. A partial parse is never committed as complete.
- Mail reader cannot authenticate: heartbeat stops updating. The next scheduler run sees a heartbeat older than 24 hours and messages the user.
- Model API down: categorisation falls back to rules plus `needs_review`, and replies fall back to templates. Numbers still come from SQL, so reviews still go out.
- Scheduler itself dead: an external check (n8n or cron on another guest) reads the heartbeat table and alerts. The thing that reports silence must not depend on the thing that went silent.

## 5. Decisions and why

| Decision | Why | Rejected alternative |
|---|---|---|
| E-statement and email first | Works for every bank without a license or a partner agreement | Licensed aggregator: needs a business entity and contract; revisit later (F20) |
| No credentials, ever | Removes the biggest breach and legal risk | Screen scraping: fragile, breaks bank terms, high liability |
| Telegram first | The first user already lives there; no public web surface to secure | Mobile app: months of work before the first useful output |
| SQL computes, model phrases | Hallucinated amounts destroy trust in a finance tool | Model reads tables and answers: untestable numbers |
| PostgreSQL in the project's own container | Decided 2026-10-17. A cloned repo runs with one `docker compose up`; NUMERIC money type; tenant filtering | A shared database on other infrastructure: couples a public project to private systems. SQLite: fine for one user, weak for the multi-user path |
| Build, informed by references | Indonesia-specific parsers and proactive features are the gap | Adopt Actual Budget or Firefly III: good ledgers, but the proactive layer and Indonesian sources would still be built; their schemas are used as reference only, no code copied |

## 6. Deliberately not designed yet

- Registration, billing, roles and quotas.
- Budgets, goals, wishlist and category management in the dashboard (F23).
- Licensed aggregator integration (F20).
- Multi-currency rates (F19).
- Anything that writes to a bank, wallet or payment provider. Never planned.

## 7. Build order

Every stage is useful on its own. Each line also says what it does not deliver.

```text
Stage 0  Repo, docker compose, schema, synthetic demo data, tests
         no bot, no real data
Stage 0b Dashboard: overview, accounts, statement upload (Saku CSV), import history, ops page,
         Cloudflare Access JWT verification, demo container on loopback
         no public hostname yet, no bank-specific parsers
Stage 0c Deploy the dashboard behind Cloudflare Access on a subdomain, origin on 127.0.0.1
         needs owner approval for the tunnel route and the Access policy; no real data until Stage 1
Stage 1  Telegram bot + manual entry + CSV/PDF import for the 2 sources that prove
         easiest in the source matrix + cash-flow summary
         -> measures the PRD "Now" baseline and fills the source matrix
         no email ingest, no nudges
Stage 2  Categoriser with rules and corrections + weekly review + heartbeats
         no purchase check yet
Stage 3  Purchase check + wishlist 48h follow-up + budgets/goals
         no income scout
Stage 4  Recurring/fee detection + email notification ingest + post-spend nudges
Stage 5  Income scout with scam filter
Stage 6  Public demo repo (synthetic data) + LinkedIn write-up
         still no public multi-user service
Stage 7  Multi-user readiness review: PSE, UU PDP, OJK boundary, security review
         opening to other users only after this passes
```

## 8. How to verify

```text
Stage 0  docker compose up on a clean machine; demo seed loads; tests pass;
         information_schema.columns has no column matching password|pin|otp
Stage 0b a forged or unsigned identity gets 401; a non-member gets 403; a user cannot rename,
         delete or upload into another tenant's account; a POST without or with someone else's
         CSRF token gets 403; the ops page contains no other tenant's descriptions or amounts;
         the upload temp directory is empty after every upload
Stage 0c from a browser without an Access session the subdomain shows the Access login, not the
         page; nothing answers on the origin host's LAN or public interfaces
Stage 1  import a real statement; rows_in = rows_new + rows_dup; re-import the same file
         -> 0 new rows; temp directory is empty afterwards
Stage 2  correct the same merchant twice -> a learned rule appears; stop the scheduler
         -> the external check reports it within 24h
Stage 3  ask a purchase check; every amount in the reply exists in nudge.sources;
         a forced wrong amount is replaced by the template
Stage 4  forward a bank email; one transaction appears; the matching statement line
         later does not create a duplicate
Stage 5  feed a known scam posting -> filtered; digest has at most 5 items with URLs
Stage 6  a second person clones the repo and runs the demo in under 10 minutes
Stage 7  a request with another tenant's telegram_id returns nothing from tenant 'local'
```

The negatives (no credential columns, no duplicates, dead scheduler reported, other tenant sees nothing) are the checks that matter most.
