# HLD: Personal Finance Assistant (working name "Saku")

Status: DRAFT v0.2, 2026-10-18. Implements `docs/PRD.md` v0.2. Requirement IDs (F1..F27) refer to the PRD.

## 1. Binding principles

Each principle can veto a later design choice.

1. **Read-only on money.** Nothing in the system can move money or log in to a bank. No credential field exists in the schema, so none can be filled by mistake.
2. **No number without a source.** SQL computes every figure. The model receives query results and phrases them. A reply containing an amount that is not in the query result is a bug, and a test checks for it.
3. **Silence is a bug.** Every ingest path and scheduler records a heartbeat. A stale heartbeat produces a message to the user. A dead component must look different from a quiet month.
4. **Designed for many, opened by approval.** `tenant_id` everywhere and one data-access layer from day one. Anyone may ask for an account; only the owner creates one. A request is not an account and grants nothing.

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
- Link-first. The web account (email verified at sign-in, 3.11) is the root identity. The bot trusts a Telegram user only after the member redeems a one-time code from Settings > Connect Telegram, sent as a `t.me/<bot>?start=<code>` deep link.
  - Codes: 144-bit random, single use, 10 minutes, stored only as SHA-256, a new code cancels older ones.
  - `telegram_link` maps the numeric Telegram user id (never the username) to one member and tenant. One Telegram account per member; linking a new one replaces the old.
  - A Telegram account already linked to another member cannot be taken over with a new code.
  - Unlinked senders get fixed instructions; nothing they send is stored or logged. Group chats are refused.
  - Unlink from the dashboard or with `/unlink`.
  - `/start` explains what Saku does, the security boundary, how to link, the commands, and a complete example.
  - Linked text entry uses `claude-haiku-5-5` only for structured extraction. The model receives redacted text and account display labels, never database access or bank credentials. Application code validates every field before writing through `Repo`.
  - Missing data is explicit. The bot says `Belum disimpan`, names the missing field, and keeps a structured 10-minute draft when it only needs the account. The raw chat message is not retained.
  - Voice notes (`voice` or `audio` messages) are transcribed on this machine by `saku/stt_bridge.py` (faster-whisper `small`, int8, Indonesian, vocabulary hint for ribu/juta/cash/tunai and provider names). The bot reaches it over a read-only mounted Unix socket, like the Haiku bridge. Size (1 MB) and length (60 s) are checked from Telegram metadata before download; `audio` messages must carry a common audio MIME type, and the bridge accepts only known audio container signatures before decoding. The bridge runs under bubblewrap (`deploy/saku-stt-sandbox.sh`): no network, no capabilities, an empty `/home` except the virtualenv, the model and the `saku` package (read-only), and the socket directory (mode 711). At most two requests run at once; extra ones get `busy`. The bot first sends `typing` (only to linked private chats), then replies `Saya dengar: "<transcript>"` followed by the normal saved, missing-data or unavailable outcome. Audio stays in memory, is never written to disk or the database, and is never sent to a third-party speech service. Transcripts are not logged. If the bridge is down, the bot says voice is unavailable and saves nothing. Measured on VM110: about 740-830 MB RSS and about 3 s per short note after the model loads; the model unloads after 15 minutes idle.
  - Cash: a cash word (`cash`, `tunai`, `uang cash`, `kas` …) only ever means a `kind='cash'` account, never a label that contains the word. With one cash account it is selected even if labelled differently. While an account question is pending, a reply is taken as the answer only when, after filler words (`pakai`, `aja`, `dari` …), it is just an account name of at most three words with no number, amount word or negation; anything else (`gopay 20rb`, `jangan gopay`) goes to the agent as a new message. If no cash account exists and the user said cash, the bot offers to create one named `Cash`; the account and the transaction are created in one database transaction, so a refused save leaves no account. A missing account is never guessed; the question lists cash accounts first. An account answer only fills rows that named no account (and, for a cash account, rows that said cash). If a row names an account Saku does not have (`BNI` with no BNI account), nothing is saved, no draft is kept, and the bot names that account so the user can add it or resend. Partial account matches need at least four characters, so fragments like `se` or `pay` select nothing. The bridge reads the whole request within 30 seconds before it takes one of the two decoder slots.
- A transfer between two of the user's accounts is represented by two linked rows: an outflow on the source account and an inflow on the destination account. Both rows use `kind='own_transfer'`. Dashboard **Money in**, **Money out**, per-account movements, and Latest activity include both sides. Income, spending analysis, and economic net cashflow exclude them.
- The bot confirmation includes money in, money out or internal-transfer totals plus each saved detail. A transfer is never reported as income or spending.
  - Every accepted message gets a saved, duplicate, missing-data, or unavailable reply. The polling offset advances only after the reply is delivered, so a temporary Telegram send failure retries safely. Manual imports are idempotent by Telegram message id.
  - PDF, CSV and photos remain Stage 1 work. Files will go to a temp directory, be parsed, then deleted.

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

### 3.3a Web transaction management
- `/app/transactions` is a tenant-scoped CRUD surface for manual rows. It uses the same `txn` table and repository layer as statement imports and Telegram entries.
- Transactions render as compact summary rows by default. A keyboard-accessible disclosure opens the edit and delete controls for one row without forcing every transaction into an always-open form.
- Every create, edit and delete request requires the user's CSRF token and an account belonging to the current tenant. Deleting one side of a paired own transfer deletes both sides together.
- Amounts are signed at the form boundary: negative is money out and positive is money in. Passwords, PINs, OTPs and full account numbers are not accepted or stored.

### 3.3b Web financial plans (F28)
- `/app/plans` is an authenticated, tenant-scoped surface over `goal` and `goal_entry`. Create/edit/status/activity POSTs use the existing Saku identity and email-bound CSRF token.
- A plan is virtual guidance, not an account or ledger object. Contributions and uses update `goal.saved` and append a positive-amount `goal_entry` in one database transaction; entry type determines whether it adds or subtracts. No route writes `txn`, changes an account balance, or moves money.
- `goal_entry` is keyed by `(tenant_id, goal_id)` through a composite foreign key. Repository reads and writes filter by tenant, and updates lock the selected goal before checking that a use does not exceed its virtual saved amount. F11 export includes plans and entries; tenant deletion removes both.
- Optional schedules are fixed weekly or monthly amounts with an explicit next date. Monthly advancement retains the chosen day-of-month across shorter months. If a contribution is recorded on or after the scheduled date, the next date advances; a use does not advance it.
- An in-app reminder state is computed in the tenant's timezone (`due today`, `upcoming`, `overdue`); no Telegram/email is sent. If target date and schedule exist, the projection is current saved amount plus scheduled installments from today through the target date. Without those inputs the page says that a projection is unavailable rather than implying one.
- `schema.sql` adds the fields idempotently and carries any pre-existing positive `goal.saved` into one `opening` entry; the partial unique index makes repeated startup safe.

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
    saved       NUMERIC(18,2) NOT NULL DEFAULT 0, -- cached virtual balance; never an account balance
    status      TEXT NOT NULL DEFAULT 'active',   -- active | paused | completed
    schedule_amount NUMERIC(18,2),                -- optional recurring virtual contribution
    schedule_cadence TEXT,                         -- weekly | monthly
    schedule_day SMALLINT,                        -- preserve monthly anchor through short months
    next_contribution_on DATE,
    review_on   DATE,
    UNIQUE (tenant_id, id)
);

CREATE TABLE goal_entry (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    goal_id     BIGINT NOT NULL,
    entry_type  TEXT NOT NULL,                    -- opening | contribution | use
    amount      NUMERIC(18,2) NOT NULL CHECK (amount > 0), -- type controls its effect on saved
    booked_on   DATE NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, goal_id) REFERENCES goal(tenant_id, id) ON DELETE CASCADE
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
    email       TEXT PRIMARY KEY,                -- verified at sign-in, stored lowercase
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
/transactions list, edit, delete; category detail is read-only, tenant-scoped, and can change category/period or clear its filters
/plans         create, edit, pause/resume, complete; virtual progress and activity only (F28)
/imports       every import batch, including failures and their error text
/admin         ops view, admin role only: tenants, heartbeats, recent batches (metadata only)
/healthz       liveness, no data
```

Identity and access:

```text
Browser ──▶ Cloudflare Tunnel ──▶ dashboard container (no published port)
             /app/admin also passes Cloudflare Access first
```

Since v0.2 the app lives under `/app` and the public pages under `/` (see 3.10). Since 2026-10-18 sign-in is Saku's own (3.11); the paragraph below describes the Access JWT check that still guards `/app/admin`.

- On `/app/admin`, the Access identity comes only from the `Cf-Access-Jwt-Assertion` JWT. The dashboard verifies its RS256 signature against the team's published keys, plus `aud`, `iss` and `exp`. The unsigned `Cf-Access-Authenticated-User-Email` header is ignored, and a test checks that it alone gets a 401.
- The verified email is looked up in the `member` table, which maps it to one tenant and a role (`user` or `admin`). A signed-in email that is not a member sees only the registration flow. Sign-in proves the address; `member` decides what it sees.
- Every POST carries an HMAC token bound to the user's email. A token from another user is rejected.
- Uploads: 5 MiB per file and a 5 MiB + 64 KiB multipart-body cap; the import body deadline is 30 seconds. The web process admits one import request at a time with no wait queue and caps each authenticated tenant at five admitted upload attempts per ten minutes; excess requests receive 429 before multipart parsing. Only SHA-256, selected account, source and parser version are recorded—never the original file or filename.
- Parsing runs in a separate Compose service over a private Unix-socket volume. The parser container has `network_mode: none`, no database/application credentials, a read-only root filesystem, no Linux capabilities, `no-new-privileges`, 32 PID limit, 1 GiB memory limit, one CPU and a 64 MiB `/tmp`. It admits one parse at a time (socket backlog 2; busy requests fail closed). Its per-upload subprocess also has allowlisted environment, 768 MiB address-space and 20 CPU-second limits, 30-second wall timeout, 7 MiB captured-stdout cap, page/text/row bounds, and discarded stderr; transaction amounts are checked against `NUMERIC(18,2)` before persistence. Temporary files are deleted after each request.
- After parsing, the web app re-authenticates the request and the repository locks tenant, member and selected account in that order, rechecks membership/provider/last4, then writes the batch, rows, transfer pairing and categories in one transaction. A failed or unsupported parse creates no ledger rows; account or membership changes during parsing produce 403 and no batch. Unsupported filename suffixes are rejected with 415 rather than mislabeled as CSV.
- The admin page uses separate repository methods that return counts, statuses and timestamps only. A test asserts that descriptions, amounts and account labels of other tenants never appear on it.
- An empty heartbeat list is shown as a warning. Nothing reporting is not the same as everything fine (principle 3).
- Dev mode (fixed demo identity) refuses to start on a non-loopback address, except inside the demo container whose host port is bound to loopback.

The first parser is Saku's own CSV format (`date,amount,description[,kind]`), so any account can be imported before its bank-specific parser exists.

The first bank parser is `bca-pdf/1` (`saku/parsers/bca_pdf.py`) for the monthly BCA e-statement PDF:
- BCA encrypts the file with an empty user password, so it opens without asking the user anything. A PDF that needs a real password is rejected with instructions to save an unlocked copy. Saku never asks for or stores a statement password.
- Rows are read by column position. A row starts with a `DD/MM` date; following lines belong to it. MUTASI is debit when followed by `DB`. The repeated amount on the first continuation line is dropped.
- The whole file is rejected, and nothing is imported, unless every printed SALDO equals the running balance and the summary block (opening balance, CR/DB totals and counts, closing balance) agrees with the rows.
- The statement must be uploaded to an account with provider `bca`; the account's last 4 digits must be saved and must match the statement. An absent or unreadable identifier is rejected before any rows are imported.
- `BIAYA ...` and `PAJAK ...` rows are tagged `fee`. Descriptions pass through the normal redaction, so card, wallet and account numbers are stored as `[NUM]`.
- Tests use synthetic PDFs built by `tests/bca_fixture.py` that copy the measured layout and encryption. No real statement is committed.

Dashboard category details query the same tenant, category, half-open period `[start, end)`, and negative `normal`/`fee` transaction rules as the category aggregate. The filtered view is read-only; each row shows its category. Accessible controls allow changing category and inclusive date range, clearing the filter, or returning to the dashboard. Invalid filters are rejected rather than falling back to the unfiltered transaction list. Tests cover fee inclusion and refund/own-transfer exclusion using the same aggregate query.

### 3.10 Closed beta: public pages, registration, approval (F24 to F27)

```text
/                public landing                  no login
/demo            read-only demo, synthetic data   no login, no forms
/privacy /terms  versioned policy pages           no login
/healthz         liveness                         no login, no data
/app/...         the dashboard from 3.9           Saku session (3.11)
/app/admin...    ops page and approval queue      Saku session with role admin + Access app, owner email only
```

- Sign-in is Saku's own email-code login (3.11). Cloudflare Access protects only `/app/admin`, and the admin routes also check `role = admin` in the database.
- Signing in proves control of an email address and nothing more. The `member` table still decides access: a verified email that is not a member sees only the registration form or its waiting page.
- Public routes read only the demo tenant. They have no forms that write. The only cookie is `saku_lang` (en or id), a functional preference set by the language switch.
- Two switches guard the beta in code. `SAKU_REGISTRATION=open` is needed before the form accepts anything; the default is closed. `SAKU_POLICY_REVIEWED` must equal the current policy version before approve works for anyone; until then the approval endpoint refuses.
- While registration is closed, only members and existing requests receive a sign-in code. Opening registration is one switch.
- Registration writes one `signup` row. Approval, in one transaction, creates the tenant, the member and marks the request approved. Rejection records who and when; the row is kept as the consent record.
- Open pending requests are capped (`SAKU_PENDING_CAP`). Over the cap the form says registration is paused and stores nothing.

```sql
CREATE TABLE signup (
    email           TEXT PRIMARY KEY CHECK (email = lower(email)), -- from the signed-in session, never from the form
    display_name    TEXT NOT NULL,                  -- what the owner sees in the queue; 1 to 60 characters
    note            TEXT,                           -- optional: why they want to try it; max 300 characters
    consent_version TEXT NOT NULL,                  -- privacy + terms version accepted, e.g. 2026-10-18
    consent_at      TIMESTAMPTZ NOT NULL,           -- when they accepted; kept after approval as the consent record
    status          TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'withdrawn')),
    decided_at      TIMESTAMPTZ,                    -- request to decision is the queue latency
    decided_by      TEXT,                           -- the admin email that decided
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Tenant ids for new users are random (`t_` + 12 hex characters), never derived from the email, so a tenant id in a log does not identify a person.

### 3.11 Sign-in (own login, email code)

Decided 2026-10-18. The site is public and people register, so the sign-in page has to be Saku's own and must not depend on a per-user seat in someone else's product. Cloudflare Access stays only in front of `/app/admin`, as a second lock for the owner.

```text
/login         email form; always answers the same way
/login/code    6-digit code form
/logout        POST, ends the session
/app/...       needs a Saku session
/app/admin...  needs a Saku session with role admin AND a valid Access JWT for the same email
```

Flow:
1. The visitor enters an email on `/login`. The response is the same for every address: "if this address can sign in, a code is on its way".
2. A code is sent only when the address is a member, has an open or decided request, or registration is open. Otherwise nothing is sent and no code row is stored.
3. The code is 6 digits, valid 10 minutes, single use. Only an HMAC of it is stored. A new code cancels older unused ones for that address.
4. The browser keeps a short signed cookie that says which address is waiting for a code. It holds no secret.
5. A correct code creates a session. The session token is 32 random bytes in an `HttpOnly`, `Secure`, `SameSite=Lax` cookie. The database stores only its SHA-256.
6. Sessions end after 30 days, or after 7 days without use, or on sign out. The server can revoke any session.
7. Successful navigation remembers one of the fixed authenticated destinations (`/app`, Accounts, Imports, or Settings). A visit to `/` with a live member session redirects there. Missing or invalid state falls back to `/app`; unauthenticated visitors still get the public landing page.

Limits, all counted per HMAC of the email and per HMAC of the client IP (Cloudflare's `CF-Connecting-IP`; the origin is only reachable through the tunnel):
- 5 code requests per email per hour, 20 per IP per hour. Counted for every address, so hitting the limit says nothing about whether an address has an account.
- 5 wrong tries per code; 10 wrong tries per email per hour, 50 per IP per hour.
- Rate-limit records keep only the two HMACs and a time, and are deleted after 24 hours.

Mail goes out through Microsoft Graph `sendMail` from one mailbox of the owner's Microsoft 365 tenant (app-only, client credentials). The app registration is limited to that mailbox with an Exchange application access policy, so the credential cannot send as anyone else. If sending fails, the page says so and no session is created.

Forms on `/login` carry a CSRF token tied to a random pre-login cookie, so another site cannot sign a visitor in to the attacker's account.

```sql
CREATE TABLE login_code (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT NOT NULL,          -- lowercase
    code_hmac   TEXT NOT NULL,          -- HMAC(secret, salt | email | code); the code itself is never stored
    salt        TEXT NOT NULL,
    attempts    INT NOT NULL DEFAULT 0, -- wrong tries on this code; dead at 5
    expires_at  TIMESTAMPTZ NOT NULL,   -- 10 minutes
    used_at     TIMESTAMPTZ,            -- single use; also set when a newer code replaces it
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE web_session (
    sid_sha256 TEXT PRIMARY KEY,      -- the token itself lives only in the browser cookie
    email        TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_path    TEXT,                       -- validated against fixed internal /app destinations
    expires_at   TIMESTAMPTZ NOT NULL,  -- 30 days
    revoked_at   TIMESTAMPTZ
);
CREATE TABLE login_event (
    kind        TEXT NOT NULL CHECK (kind IN ('request', 'fail')),
    email_hmac  TEXT NOT NULL,
    ip_hmac     TEXT NOT NULL,
    at          TIMESTAMPTZ NOT NULL DEFAULT now()  -- deleted after 24 hours
);
```

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

### 4.3 Registration and approval
1. A visitor opens `/`, reads, optionally opens `/demo`, presses Register.
2. `/login` asks for an email and sends a 6-digit code (3.11). The visitor enters it and reaches `/app`.
3. The app sees a verified email with no member row and no signup row, and shows the registration form.
4. The visitor submits a display name, an optional note and the consent checkbox. One `signup` row is stored as `pending`.
5. Any later visit shows the waiting page. Uploads, Telegram linking and every financial page return 403 for that email.
6. The owner opens `/app/admin`, sees the queue, approves. One transaction creates `tenant`, `member` (role `user`) and sets `signup.status = 'approved'`.
7. The next visit by that email lands on the first-run welcome page.

What can go wrong, and what happens then:
- A form without consent is refused with nothing stored.
- Over the pending cap the form is closed and nothing is stored.
- A second submission from the same email updates nothing.
- Approving a request that is no longer pending does nothing.

### 4.4 Failure flow
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
| Closed beta with owner approval | Decided 2026-10-18. Lets real people ask from LinkedIn while one person can still review every account and the legal checks finish | Open self-service sign-up: no review of who stores financial data on a personal server before the legal basis is clear |
| Own email-code login, Access only on `/app/admin` | Decided 2026-10-18. A public sign-up needs a sign-in page on Saku's own domain, and no per-user seat in a third-party product. Codes by email keep the no-password property | Cloudflare Access for everyone: login page on a cloudflareaccess.com domain and one seat per person who ever signs in. Email and password: password storage, reset mail and credential stuffing to defend |
| Login mail through Microsoft Graph from the owner's tenant | Decided 2026-10-18. No new processor of users' email addresses; the domain already sends through Microsoft 365 | A transactional mail service: quicker set-up, but one more company handling user data. SMTP with a mailbox password: a password on the server and a protocol Microsoft is phasing out |
| A request is not an account | Approval is the only code path that creates a tenant, so a bug in registration cannot hand out data access | Create the tenant at registration and flag it pending: every query would need a pending check, and one forgotten check leaks |
| Build, informed by references | Indonesia-specific parsers and proactive features are the gap | Adopt Actual Budget or Firefly III: good ledgers, but the proactive layer and Indonesian sources would still be built; their schemas are used as reference only, no code copied |

## 6. Deliberately not designed yet

- Open self-service sign-up without approval, billing, quotas.
- Email notifications to applicants about a decision (they see the status when they log in).
- Passkeys as a second way to sign in.
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
Stage 0d Closed beta plumbing: public landing, /demo, app moved under /app, registration form,
         signup table, approval queue, Access apps for /app (any email) and /app/admin (owner)
         nobody except the owner is approved in this stage
Stage 0f Own sign-in: /login with email code, sessions, rate limits, mail through Microsoft Graph;
         Access kept only on /app/admin
         no passkeys, no password
Stage 0e Beta gate: privacy policy and terms (versioned), export and delete (F11),
         PSE / UU PDP / OJK check recorded with sources, seat limit and seat expiration checked
         only after this stage may the owner approve another person
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
Stage 7  Open-launch readiness review: security review, support load, cost; decide whether
         approval can be dropped. Closed beta users exist before this stage (Stage 0e)
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
Stage 0d without an Access session /app shows the Access login while / and /demo load; / and /demo
         contain no data from any tenant except the demo tenant; a verified non-member gets the form,
         not a page with data; a pending user gets 403 on upload, on Connect Telegram and on every
         financial page; a JWT whose aud belongs to neither Access app gets 401; a user-role member
         gets 403 on /app/admin and on approve; consent unchecked stores nothing; over the pending
         cap stores nothing; approve creates exactly one tenant and one member
Stage 0f a wrong code 5 times kills the code; a used or expired code fails; the 6th code request
         in an hour sends nothing; /login answers the same for a member, a stranger and a limited
         address; no row holds a plain code or session token; a session cookie from a revoked or
         expired session gets the login page; /app/admin without the Access JWT gets 401 even with
         an admin session; a login form posted from another origin is refused
Stage 0e export returns every row of the tenant and nothing of another, while excluding authentication
         secrets; deletion leaves 0 rows in every tenant_id table, removes signup/auth/Telegram state,
         refuses a shared tenant, requires exact confirmation and signs the user out; backup retention
         is disclosed; the accepted policy version is stored
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
Stage 7  external security review findings closed; a request with another tenant's telegram_id or
         JWT returns nothing from any other tenant
```

The negatives (no credential columns, no duplicates, dead scheduler reported, other tenant sees nothing) are the checks that matter most.
