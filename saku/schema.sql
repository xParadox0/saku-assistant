-- Saku schema, Stage 0. Source of truth for docs/HLD.md section 3.8.
-- Every table carries tenant_id. All access goes through saku/repo.py.
-- Enum-like columns use CHECK constraints so a typo fails loudly instead of creating a new category.

CREATE TABLE IF NOT EXISTS tenant (
    id          TEXT PRIMARY KEY,                -- 'local' for the first user, 'demo' for synthetic data
    telegram_id BIGINT UNIQUE,                   -- how the bot finds the tenant
    tz          TEXT NOT NULL DEFAULT 'Asia/Jakarta',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS account (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    kind        TEXT NOT NULL CHECK (kind IN ('bank', 'ewallet', 'cash')),
    provider    TEXT NOT NULL,                   -- provider slug; picks the parser
    label       TEXT NOT NULL,                   -- what the user calls it
    last4       TEXT CHECK (last4 ~ '^[0-9]{4}$'), -- masked number, never the full number
    currency    TEXT NOT NULL DEFAULT 'IDR',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    -- no username, password, PIN or OTP column exists, on purpose (HLD principle 1)
);

CREATE TABLE IF NOT EXISTS import_batch (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    account_id  BIGINT REFERENCES account(id),
    source      TEXT NOT NULL CHECK (source IN ('pdf', 'csv', 'email', 'manual', 'receipt', 'demo')),
    file_sha256 TEXT,                            -- proves which file was imported; the file itself is deleted
    parser      TEXT,                            -- parser name and version, for re-runs
    status      TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'failed')),
    rows_in     INT,                             -- rows the parser produced
    rows_new    INT,                             -- rows stored
    rows_dup    INT,                             -- rows skipped as duplicates; rows_in = rows_new + rows_dup
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS txn (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL REFERENCES tenant(id),
    account_id    BIGINT NOT NULL REFERENCES account(id),
    batch_id      BIGINT REFERENCES import_batch(id),
    booked_on     DATE NOT NULL,                 -- bank date
    amount        NUMERIC(18,2) NOT NULL,        -- negative = out; NUMERIC, never float, for money
    currency      TEXT NOT NULL DEFAULT 'IDR',
    description   TEXT NOT NULL,                 -- stored redacted (no account numbers, phones, emails)
    kind          TEXT NOT NULL DEFAULT 'normal' CHECK (kind IN ('normal', 'own_transfer', 'fee', 'refund')),
    category      TEXT,
    cat_source    TEXT CHECK (cat_source IN ('user', 'rule', 'builtin', 'model')), -- explains the label
    cat_conf      REAL,                          -- model confidence; < 0.7 means needs_review
    fingerprint   TEXT NOT NULL,                 -- dedupe key, see saku/normalise.py
    transfer_pair BIGINT REFERENCES txn(id),     -- the other side of an own transfer
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS txn_tenant_date ON txn (tenant_id, booked_on);

CREATE TABLE IF NOT EXISTS category_rule (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    pattern     TEXT NOT NULL,                   -- case-insensitive substring or regex
    category    TEXT NOT NULL,
    origin      TEXT NOT NULL CHECK (origin IN ('user', 'learned')), -- learned after 2 identical corrections
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS budget (
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    month       DATE NOT NULL,                   -- first day of month
    category    TEXT NOT NULL,
    amount      NUMERIC(18,2) NOT NULL,
    PRIMARY KEY (tenant_id, month, category)
);

CREATE TABLE IF NOT EXISTS goal (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    name        TEXT NOT NULL,
    target      NUMERIC(18,2) NOT NULL,
    due_on      DATE,
    saved       NUMERIC(18,2) NOT NULL DEFAULT 0 -- user-reported; the system cannot move money
);

CREATE TABLE IF NOT EXISTS wishlist (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    item        TEXT NOT NULL,
    amount      NUMERIC(18,2),
    asked_at    TIMESTAMPTZ NOT NULL DEFAULT now(), -- when the user asked
    remind_at   TIMESTAMPTZ NOT NULL,               -- asked_at + 48h; the difference is the cooling-off
    outcome     TEXT CHECK (outcome IN ('bought', 'skipped', 'still_waiting'))
);

CREATE TABLE IF NOT EXISTS nudge (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    kind        TEXT NOT NULL CHECK (kind IN ('review', 'budget', 'recurring', 'bill', 'wishlist', 'income')),
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    body        TEXT NOT NULL,
    sources     JSONB NOT NULL,                  -- tool calls and results behind every number (HLD principle 2)
    reaction    TEXT CHECK (reaction IN ('helpful', 'not_helpful', 'muted'))
);

CREATE TABLE IF NOT EXISTS heartbeat (
    tenant_id   TEXT NOT NULL,
    component   TEXT NOT NULL,                   -- scheduler | mail_reader | importer:<provider>
    last_ok     TIMESTAMPTZ,
    last_error  TEXT,
    PRIMARY KEY (tenant_id, component)
);

CREATE TABLE IF NOT EXISTS member (
    email       TEXT PRIMARY KEY CHECK (email = lower(email)), -- identity verified by Cloudflare Access, stored lowercase
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    role        TEXT NOT NULL CHECK (role IN ('user', 'admin')), -- admin sees operational metadata of every tenant, never their transactions
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
