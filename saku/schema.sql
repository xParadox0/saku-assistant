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

-- Web account <-> Telegram account. The web identity is the root; the bot trusts a Telegram user only after
-- a one-time code made in the dashboard is redeemed. One Telegram account per member and vice versa.
CREATE TABLE IF NOT EXISTS telegram_link (
    telegram_user_id BIGINT PRIMARY KEY,         -- Telegram's numeric user id, never the username (usernames change)
    email            TEXT NOT NULL UNIQUE REFERENCES member(email) ON DELETE CASCADE,
    tenant_id        TEXT NOT NULL REFERENCES tenant(id),
    linked_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS link_code (
    code_hash   TEXT PRIMARY KEY,                -- sha256 of the code; the code itself is never stored
    email       TEXT NOT NULL REFERENCES member(email) ON DELETE CASCADE,
    expires_at  TIMESTAMPTZ NOT NULL,            -- 10 minutes after creation
    used_at     TIMESTAMPTZ,                     -- single use
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Closed beta. A request is not an account: only approval creates the tenant and the member (HLD 3.10).
CREATE TABLE IF NOT EXISTS signup (
    email           TEXT PRIMARY KEY CHECK (email = lower(email)), -- from the verified Access JWT, never from the form
    display_name    TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 60),
    note            TEXT CHECK (note IS NULL OR length(note) <= 300),
    lang            TEXT NOT NULL DEFAULT 'en' CHECK (lang IN ('en', 'id')),
    consent_version TEXT NOT NULL,                  -- privacy + terms version accepted
    consent_at      TIMESTAMPTZ NOT NULL,           -- kept after the decision as the consent record
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'withdrawn')),
    decided_at      TIMESTAMPTZ,
    decided_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS signup_status ON signup (status, created_at);

-- Own sign-in (HLD 3.11). Codes and session tokens are never stored in plain form.
CREATE TABLE IF NOT EXISTS login_code (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT NOT NULL CHECK (email = lower(email)),
    code_hmac   TEXT NOT NULL,                  -- HMAC(secret, salt | email | code)
    salt        TEXT NOT NULL,
    attempts    INT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    expires_at  TIMESTAMPTZ NOT NULL,           -- 10 minutes after creation
    used_at     TIMESTAMPTZ,                    -- single use; also set when a newer code replaces it
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_code_email ON login_code (email, created_at DESC);

CREATE TABLE IF NOT EXISTS web_session (
    sid_sha256   TEXT PRIMARY KEY,            -- SHA-256 of the session id; the id itself lives only in the browser cookie
    email        TEXT NOT NULL CHECK (email = lower(email)),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,          -- 30 days after creation
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS web_session_email ON web_session (email);

CREATE TABLE IF NOT EXISTS login_event (
    kind        TEXT NOT NULL CHECK (kind IN ('request', 'fail')),
    email_hmac  TEXT NOT NULL,
    ip_hmac     TEXT NOT NULL,
    at          TIMESTAMPTZ NOT NULL DEFAULT now()   -- purged after 24 hours
);
CREATE INDEX IF NOT EXISTS login_event_email ON login_event (kind, email_hmac, at);
CREATE INDEX IF NOT EXISTS login_event_ip ON login_event (kind, ip_hmac, at);
