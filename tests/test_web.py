"""Dashboard tests. The negatives matter most: forged identity, wrong tenant, missing CSRF, admin leakage."""
import time
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path

import jwt
import psycopg
from psycopg import sql
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from saku.__main__ import load_demo
from saku.normalise import RawTxn
from saku.repo import Repo
from saku.web.app import app_from_env, create_app
from saku.web.auth import CloudflareAccessAuth, DevAuth

TEAM = "https://example-team.cloudflareaccess.com"
AUD = "test-aud-tag"
SECRET = "x" * 40


class HeaderAuth:
    """Test-only identity from a header, so one app can serve several users in a test."""
    def identify(self, headers):
        from saku.web.auth import Identity
        return Identity(headers.get("x-test-email", ""))


@pytest.fixture
def seeded(conn, db_url):
    repo = Repo(conn)
    repo.add_member("alice@example.com", "alice")
    repo.add_member("bob@example.com", "bob")
    repo.add_member("ops@example.com", "ops", "admin")
    a = repo.add_account("alice", "bank", "bank_a", "Alice Main")
    b = repo.add_account("bob", "bank", "bank_b", "Bob Secret Account")
    repo.import_rows("alice", a, [RawTxn(date(2026, 9, 1), Decimal(5_000_000), "ALICE SALARY"),
                                  RawTxn(date(2026, 9, 3), Decimal(-80_000), "ALICE LUNCH")], "csv", "test")
    repo.import_rows("bob", b, [RawTxn(date(2026, 9, 2), Decimal(-777_000), "BOB PRIVATE PURCHASE")], "csv", "test")
    return {"alice_acc": a, "bob_acc": b, "url": db_url}


@pytest.fixture
def upload_dir(tmp_path):
    d = tmp_path / "uploads"; d.mkdir()
    return d


@pytest.fixture
def client(seeded, upload_dir):
    app = create_app(lambda: psycopg.connect(seeded["url"], autocommit=True), HeaderAuth(), SECRET, str(upload_dir))
    return TestClient(app)


def as_(email):
    return {"x-test-email": email}


def csrf(client, email):
    page = client.get("/app/accounts", headers=as_(email)).text
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


# ---- tenant isolation ------------------------------------------------------------------------------
def test_overview_shows_only_own_data(client):
    page = client.get("/app", headers=as_("alice@example.com")).text
    low = page.lower()  # descriptions are shown in title case; compare case-insensitively
    assert "alice salary" in low and "Rp5.000.000" in page
    assert "bob" not in low and "777" not in page


def test_overview_explains_that_account_movements_include_transfers(client):
    page = client.get("/app", headers=as_("alice@example.com")).text
    assert "Movements include internal transfers" in page


def test_dashboard_category_link_uses_the_displayed_month(client, conn):
    conn.execute("UPDATE txn SET category = 'transport' WHERE tenant_id = 'alice' AND description = 'ALICE LUNCH'")
    page = client.get("/app", headers=as_("alice@example.com")).text
    assert 'href="/app/transactions?category=transport&amp;start=2026-09-01&amp;end=2026-10-01"' in page


def test_category_detail_is_tenant_scoped_and_reconciles_category_total(client, seeded, conn):
    repo = Repo(conn)
    repo.import_manual_rows("alice", seeded["alice_acc"], [
        RawTxn(date(2026, 9, 12), Decimal(-20_000), "TAXI SEPT"),
        RawTxn(date(2026, 8, 31), Decimal(-30_000), "TAXI AUGUST"),
        RawTxn(date(2026, 10, 1), Decimal(-40_000), "TAXI OCTOBER"),
    ], "test:category-detail")
    conn.execute("UPDATE txn SET category = 'transport' WHERE tenant_id = 'alice' AND description IN ('ALICE LUNCH', 'TAXI SEPT', 'TAXI AUGUST', 'TAXI OCTOBER')")
    conn.execute("UPDATE txn SET category = 'transport' WHERE tenant_id = 'bob' AND description = 'BOB PRIVATE PURCHASE'")

    response = client.get("/app/transactions?category=transport&start=2026-09-01&end=2026-10-01",
                          headers=as_("alice@example.com"))
    page = response.text
    assert response.status_code == 200
    assert "Transport" in page and "Rp100.000" in page
    assert "Alice Lunch" in page and "Taxi Sept" in page
    assert "Taxi August" not in page and "Taxi October" not in page
    assert "Bob Private Purchase" not in page
    assert "Read-only category detail" in page
    assert 'id="add-transaction"' not in page and "Delete transaction" not in page


def test_category_detail_reconciles_uncategorized_rows(client, conn):
    conn.execute("UPDATE txn SET category = NULL WHERE tenant_id = 'alice' AND description = 'ALICE LUNCH'")
    repo = Repo(conn)
    expected = next(row['amount'] for row in repo.spending_by_category('alice', date(2026, 9, 1), date(2026, 10, 1))
                    if row['category'] == 'other')
    response = client.get('/app/transactions?category=other&start=2026-09-01&end=2026-10-01',
                          headers=as_('alice@example.com'))
    assert response.status_code == 200
    assert 'Alice Lunch' in response.text
    assert repo.spending_transactions('alice', 'other', date(2026, 9, 1), date(2026, 10, 1))
    assert sum(-r['amount'] for r in repo.spending_transactions('alice', 'other', date(2026, 9, 1), date(2026, 10, 1))) == expected


def test_category_detail_accepts_legacy_category_present_in_tenant_aggregate(client, conn):
    conn.execute("UPDATE txn SET category = 'legacy_transport' WHERE tenant_id = 'alice' AND description = 'ALICE LUNCH'")
    response = client.get('/app/transactions?category=legacy_transport&start=2026-09-01&end=2026-10-01',
                          headers=as_('alice@example.com'))
    assert response.status_code == 200
    assert 'Alice Lunch' in response.text


def test_invalid_category_detail_filter_is_rejected_not_broadened(client):
    response = client.get("/app/transactions?category=income&start=2026-09-01&end=2026-10-01",
                          headers=as_("alice@example.com"))
    assert response.status_code == 403


def test_category_detail_has_an_explicit_empty_state(client):
    response = client.get("/app/transactions?category=health&start=2026-09-01&end=2026-10-01",
                          headers=as_("alice@example.com"))
    assert response.status_code == 200
    assert "No Health spending transactions for this period." in response.text
    assert "All transactions" not in response.text


def test_overview_money_in_and_out_include_internal_transfers(client, seeded):
    with psycopg.connect(seeded["url"], autocommit=True) as conn:
        repo = Repo(conn)
        target = repo.add_account("alice", "ewallet", "gopay", "Alice GoPay")
        repo.import_manual_rows("alice", seeded["alice_acc"], [
            RawTxn(date(2026, 9, 4), Decimal(-700_000), "TRANSFER TO GOPAY", "own_transfer"),
        ], "telegram:test:source")
        repo.import_manual_rows("alice", target, [
            RawTxn(date(2026, 9, 4), Decimal(700_000), "TRANSFER FROM MAIN BANK", "own_transfer"),
        ], "telegram:test:target")
        repo.detect_own_transfers("alice")

    page = client.get("/app", headers=as_("alice@example.com")).text
    assert '<span class="k">Money in</span><b>Rp5.700.000</b>' in page
    assert '<span class="k">Money out</span><b>Rp780.000</b>' in page
    assert "September 2026: in Rp5.700.000, out Rp780.000" in page


def test_unknown_email_gets_registration_not_data(client):
    """Since the closed beta, a verified non-member is sent to /app/register (closed by default) and sees no data."""
    r = client.get("/app", headers=as_("stranger@example.com"), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/register"
    page = client.get("/app/register", headers=as_("stranger@example.com")).text
    assert "Registration is not open yet" in page and "alice salary" not in page.lower()
    for path in ("/app/accounts", "/app/imports", "/app/settings", "/app/admin"):
        r = client.get(path, headers=as_("stranger@example.com"), follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/app/register", path
    r = client.post("/app/accounts", data={"csrf": "x", "kind": "bank", "provider": "bank_x", "label": "X"},
                    headers=as_("stranger@example.com"))
    assert r.status_code == 403


def test_cannot_touch_another_tenants_account(client, seeded, upload_dir):
    tok = csrf(client, "alice@example.com")
    bob = seeded["bob_acc"]
    r = client.post(f"/app/accounts/{bob}/rename", data={"csrf": tok, "label": "pwned"}, headers=as_("alice@example.com"))
    assert r.status_code == 404
    r = client.post(f"/app/accounts/{bob}/delete", data={"csrf": tok}, headers=as_("alice@example.com"))
    assert r.status_code == 404
    r = client.post(f"/app/accounts/{bob}/import", data={"csrf": tok}, headers=as_("alice@example.com"),
                    files={"file": ("x.csv", b"date,amount,description\n2026-09-01,1,X\n", "text/csv")})
    assert r.status_code == 404
    assert "Bob Secret Account" in client.get("/app/accounts", headers=as_("bob@example.com")).text


# ---- CSRF --------------------------------------------------------------------------------------------
def test_post_without_csrf_is_refused(client, seeded):
    r = client.post(f"/app/accounts/{seeded['alice_acc']}/rename", data={"label": "x"}, headers=as_("alice@example.com"))
    assert r.status_code == 403


def test_csrf_token_of_another_user_is_refused(client, seeded):
    bob_tok = csrf(client, "bob@example.com")
    r = client.post(f"/app/accounts/{seeded['alice_acc']}/rename", data={"csrf": bob_tok, "label": "x"},
                    headers=as_("alice@example.com"))
    assert r.status_code == 403


# ---- admin -------------------------------------------------------------------------------------------
def test_admin_requires_admin_role(client):
    assert client.get("/app/admin", headers=as_("alice@example.com")).status_code == 403


def test_admin_shows_metadata_not_transactions(client):
    page = client.get("/app/admin", headers=as_("ops@example.com")).text
    assert "alice" in page and "bob" in page
    for leaked in ("alice salary", "bob private purchase", "bob secret account", "777.000", "5.000.000"):
        assert leaked not in page.lower()
    assert "No component has reported yet" in page  # empty heartbeats are shown as a warning, not as calm


# ---- account management and upload ------------------------------------------------------------------
def test_add_rename_delete_account(client):
    tok = csrf(client, "alice@example.com")
    r = client.post("/app/accounts", data={"csrf": tok, "kind": "ewallet", "provider": "wallet_x", "label": "Pocket", "last4": ""},
                    headers=as_("alice@example.com"), follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/app/accounts", headers=as_("alice@example.com")).text
    assert "Pocket" in page


def test_web_transaction_crud_is_tenant_scoped(client, seeded):
    token = csrf(client, "alice@example.com")
    response = client.post("/app/transactions", data={
        "csrf": token, "account_id": seeded["alice_acc"], "booked_on": "2026-10-01",
        "amount": "-45000", "description": "Kopi", "kind": "normal",
    }, headers=as_("alice@example.com"), follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/app/transactions", headers=as_("alice@example.com")).text
    assert "Kopi" in page
    txn_id = int(page.split('data-txn-id="', 1)[1].split('"', 1)[0])
    response = client.post(f"/app/transactions/{txn_id}", data={
        "csrf": token, "account_id": seeded["alice_acc"], "booked_on": "2026-10-02",
        "amount": "-50000", "description": "Updated coffee", "kind": "normal",
    }, headers=as_("alice@example.com"), follow_redirects=False)
    assert response.status_code == 303
    assert "Updated Coffee" in client.get("/app/transactions", headers=as_("alice@example.com")).text
    assert client.post(f"/app/transactions/{txn_id}/delete", data={"csrf": token},
                       headers=as_("alice@example.com"), follow_redirects=False).status_code == 303
    assert "Updated Coffee" not in client.get("/app/transactions", headers=as_("alice@example.com")).text


def test_add_transaction_form_hides_internal_type_and_marks_amount_help(client):
    page = client.get("/app/transactions", headers=as_("alice@example.com")).text
    add_form = page.split('id="add-transaction"', 1)[1].split("</form>", 1)[0]
    assert '<span>Type</span>' not in add_form
    assert 'name="kind"' not in add_form
    assert 'class="field amount-field"' in add_form
    assert 'aria-describedby="amount-help"' in add_form


def test_add_transaction_forces_normal_kind_server_side(client, seeded, conn):
    token = csrf(client, "alice@example.com")
    response = client.post("/app/transactions", data={
        "csrf": token, "account_id": seeded["alice_acc"], "booked_on": "2026-10-03",
        "amount": "-12500", "description": "Forged fee kind", "kind": "fee",
    }, headers=as_("alice@example.com"), follow_redirects=False)
    assert response.status_code == 303
    row = conn.execute(
        "SELECT kind FROM txn WHERE tenant_id = 'alice' AND description = 'FORGED FEE KIND'"
    ).fetchone()
    assert row == ("normal",)


def test_transaction_list_is_compact_table_with_disclosed_editor(client, conn):
    lunch_id = conn.execute(
        "SELECT id FROM txn WHERE tenant_id = 'alice' AND description = 'ALICE LUNCH'"
    ).fetchone()[0]
    conn.execute("UPDATE txn SET kind = 'own_transfer' WHERE id = %s", (lunch_id,))
    page = client.get("/app/transactions", headers=as_("alice@example.com")).text
    assert 'class="transaction-table-head"' in page
    assert all(f">{heading}<" in page for heading in ("Date", "Account", "Description", "Type", "Amount"))
    assert '<details class="transaction-row"' in page
    assert '<summary class="transaction-summary"' in page
    assert 'class="transaction-editor"' in page
    assert 'class="transaction-edit-label"' in page
    first_row = page.split('<details class="transaction-row"', 1)[1].split("</details>", 1)[0]
    details_open_tag = first_row.split(">", 1)[0]
    assert f'data-txn-id="{lunch_id}"' in details_open_tag
    assert " open" not in details_open_tag
    summary_open_tag = first_row.split("<summary", 1)[1].split(">", 1)[0]
    assert "aria-label" not in summary_open_tag
    assert '<time class="transaction-date" datetime="2026-09-03">3 Sep 2026</time>' in first_row
    assert first_row.count('name="csrf"') == 2
    assert f'action="/app/transactions/{lunch_id}/delete"' in first_row
    assert '<option value="own_transfer" selected>' in first_row
    assert '<span>Account</span>' in first_row
    assert '<span>Date</span>' in first_row
    assert '<span>Amount</span>' in first_row
    assert '<span>Description</span>' in first_row
    assert '<span>Type</span>' in first_row


def test_web_transaction_cannot_edit_other_tenant(client, seeded, conn):
    token = csrf(client, "alice@example.com")
    bob_txn = conn.execute("SELECT id FROM txn WHERE tenant_id = 'bob' LIMIT 1").fetchone()[0]
    response = client.post(f"/app/transactions/{bob_txn}", data={
        "csrf": token, "account_id": seeded["alice_acc"], "booked_on": "2026-10-02",
        "amount": "-1", "description": "Nope", "kind": "normal",
    }, headers=as_("alice@example.com"))
    assert response.status_code == 404


def test_account_with_transactions_is_not_deleted(client, seeded):
    tok = csrf(client, "alice@example.com")
    r = client.post(f"/app/accounts/{seeded['alice_acc']}/delete", data={"csrf": tok}, headers=as_("alice@example.com"))
    assert "was not deleted" in r.text and "Alice Main" in r.text


def test_full_account_number_rejected_by_form(client):
    tok = csrf(client, "alice@example.com")
    r = client.post("/app/accounts", data={"csrf": tok, "kind": "bank", "provider": "bank_x", "label": "X", "last4": "1234567890"},
                    headers=as_("alice@example.com"))
    assert r.status_code == 403


def test_accounts_form_has_searchable_provider_numeric_last4_and_ewallets(client):
    page = client.get("/app/accounts", headers=as_("alice@example.com")).text
    assert 'type="hidden" name="provider" data-provider-value' in page
    assert 'id="provider-search" type="search" required' in page
    assert 'id="provider-search" type="search" name="provider"' not in page
    assert "<datalist" not in page
    assert 'role="listbox"' in page and 'data-kind="bank"' in page and 'data-kind="ewallet"' in page
    assert '/static/accounts.js?v=' in page and 'defer></script>' in page
    assert 'class="grid-form account-form"' in page
    assert 'type="number" name="last4"' in page
    for provider in ("GoPay", "OVO", "DANA", "ShopeePay", "LinkAja"):
        assert provider in page


def test_navigation_calls_app_home_dashboard_and_register_signout_is_a_button(client):
    page = client.get("/app", headers=as_("alice@example.com")).text
    assert '<span>Dashboard</span>' in page
    assert 'class="nav" aria-label="Main" data-mobile-layout="all-visible"' in page
    pending = client.get("/app/register", headers=as_("stranger@example.com")).text
    assert 'class="button signout-button"' in pending


def test_empty_dashboard_keeps_summary_and_analysis_structure(client):
    page = client.get("/app", headers=as_("ops@example.com")).text
    for heading in ("Monthly summary", "Spending analysis", "Account overview", "Latest activity"):
        assert heading in page
    assert "Add your first account" in page


def test_mutable_static_assets_use_cache_busting_versions(client):
    page = client.get("/app/accounts", headers=as_("alice@example.com")).text
    assert '/static/saku.css?v=' in page
    assert '/static/accounts.js?v=' in page
    assert '<link rel="stylesheet" href="/static/saku.css">' not in page
    assert '<script src="/static/accounts.js" defer></script>' not in page
    assert '/static/saku.css?v=' in client.get("/").text


def test_csv_upload_imports_dedupes_and_deletes_file(client, seeded, upload_dir):
    tok = csrf(client, "alice@example.com")
    body = b"date,amount,description,kind\n2026-10-01,-45000,KOPI,normal\n02/10/2026,-15000,ADM,fee\n"
    acc = seeded["alice_acc"]
    r1 = client.post(f"/app/accounts/{acc}/import", data={"csrf": tok}, headers=as_("alice@example.com"),
                     files={"file": ("mutasi.csv", body, "text/csv")})
    assert "2 rows read, 2 new, 0 already imported" in r1.text
    r2 = client.post(f"/app/accounts/{acc}/import", data={"csrf": tok}, headers=as_("alice@example.com"),
                     files={"file": ("mutasi.csv", body, "text/csv")})
    assert "2 rows read, 0 new, 2 already imported" in r2.text
    assert list(upload_dir.iterdir()) == []


def test_bad_csv_is_recorded_as_failed_and_nothing_imported(client, seeded, conn):
    tok = csrf(client, "alice@example.com")
    before = Repo(conn).txn_count("alice")
    r = client.post(f"/app/accounts/{seeded['alice_acc']}/import", data={"csrf": tok}, headers=as_("alice@example.com"),
                    files={"file": ("bad.csv", b"date,amount,description\n2026-10-01,1.234.000,X\n", "text/csv")})
    assert "Import failed" in r.text and "line 2" in r.text
    assert Repo(conn).txn_count("alice") == before
    assert "failed" in client.get("/app/imports", headers=as_("alice@example.com")).text


def test_unsupported_pdf_is_reported(client, seeded):
    tok = csrf(client, "alice@example.com")
    r = client.post(f"/app/accounts/{seeded['alice_acc']}/import", data={"csrf": tok}, headers=as_("alice@example.com"),
                    files={"file": ("statement.pdf", b"%PDF-1.4 fake", "application/pdf")})
    assert "format not supported yet" in r.text


def test_upload_size_limit(client, seeded):
    tok = csrf(client, "alice@example.com")
    big = b"date,amount,description\n" + b"2026-10-01,-1,X\n" * 400_000
    r = client.post(f"/app/accounts/{seeded['alice_acc']}/import", data={"csrf": tok}, headers=as_("alice@example.com"),
                    files={"file": ("big.csv", big, "text/csv")})
    assert r.status_code == 413


def test_security_headers(client):
    r = client.get("/app", headers=as_("alice@example.com"))
    assert "script-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    accounts = client.get("/app/accounts", headers=as_("alice@example.com"))
    assert "script-src 'self'" in accounts.headers["content-security-policy"]
    assert "unsafe-inline" not in accounts.headers["content-security-policy"]
    script = client.get("/static/accounts.js")
    assert script.status_code == 200 and "provider-options" in script.text


def test_demo_tenant_renders(conn, db_url):
    load_demo(Repo(conn))
    app = create_app(lambda: psycopg.connect(db_url, autocommit=True), DevAuth("demo@saku.local"), SECRET)
    page = TestClient(app).get("/app").text
    assert "your September" in page and "Rp14.445.020" in page  # matches the CLI demo output


# ---- Cloudflare Access JWT ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key(), as_dict=True)
    jwk.update(kid="k1", alg="RS256", use="sig")
    return priv, {"keys": [jwk]}


def token(priv, **over):
    now = int(time.time())
    claims = {"email": "Alice@Example.com", "aud": [AUD], "iss": TEAM, "iat": now, "exp": now + 300, **over}
    return jwt.encode(claims, priv, algorithm="RS256", headers={"kid": "k1"})


def test_access_jwt_valid(keypair):
    priv, jwks = keypair
    auth = CloudflareAccessAuth(TEAM, AUD, lambda: jwks)
    assert auth.identify({"cf-access-jwt-assertion": token(priv)}).email == "alice@example.com"


@pytest.mark.parametrize("over", [{"aud": ["other-app"]}, {"iss": "https://evil.cloudflareaccess.com"},
                                  {"exp": int(time.time()) - 10}, {"email": ""}])
def test_access_jwt_rejected(keypair, over):
    from saku.web.auth import AuthError
    priv, jwks = keypair
    auth = CloudflareAccessAuth(TEAM, AUD, lambda: jwks)
    with pytest.raises(AuthError):
        auth.identify({"cf-access-jwt-assertion": token(priv, **over)})


def test_access_jwt_signed_by_other_key_rejected(keypair):
    from saku.web.auth import AuthError
    _, jwks = keypair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = CloudflareAccessAuth(TEAM, AUD, lambda: jwks)
    with pytest.raises(AuthError):
        auth.identify({"cf-access-jwt-assertion": token(other)})


def test_unsigned_email_header_alone_is_not_identity(keypair, seeded):
    _, jwks = keypair
    app = create_app(lambda: psycopg.connect(seeded["url"], autocommit=True),
                     CloudflareAccessAuth(TEAM, AUD, lambda: jwks), SECRET)
    r = TestClient(app).get("/app", headers={"cf-access-authenticated-user-email": "alice@example.com"})
    assert r.status_code == 401 and "alice salary" not in r.text.lower()


def test_full_request_with_valid_jwt(keypair, seeded):
    priv, jwks = keypair
    app = create_app(lambda: psycopg.connect(seeded["url"], autocommit=True),
                     CloudflareAccessAuth(TEAM, AUD, lambda: jwks), SECRET)
    r = TestClient(app).get("/app", headers={"cf-access-jwt-assertion": token(priv)})
    assert r.status_code == 200 and "alice salary" in r.text.lower()


# ---- startup guards ----------------------------------------------------------------------------------
def test_dev_auth_refuses_public_interface(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("SAKU_AUTH_MODE", "dev")
    monkeypatch.delenv("SAKU_DEV_CONTAINER", raising=False)
    with pytest.raises(SystemExit):
        app_from_env("0.0.0.0")


def test_cloudflare_mode_requires_secret(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("SAKU_AUTH_MODE", "cloudflare")
    monkeypatch.setenv("SAKU_CF_TEAM", TEAM)
    monkeypatch.setenv("SAKU_CF_AUD", AUD)
    monkeypatch.setenv("SAKU_SECRET", "short")
    with pytest.raises(SystemExit):
        app_from_env()



def test_upload_via_picker_route(client, seeded):
    """POST /imports takes the account from the form; tenant and CSRF checks still apply."""
    body = b"date,amount,description\n2026-09-10,1000,PICKER TEST\n"
    alice = "alice@example.com"
    r = client.post("/app/imports", data={"csrf": "wrong", "account_id": str(seeded["alice_acc"])},
                    files={"file": ("s.csv", body, "text/csv")}, headers=as_(alice), follow_redirects=False)
    assert r.status_code == 403
    tok = csrf(client, alice)
    r = client.post("/app/imports", data={"csrf": tok, "account_id": str(seeded["bob_acc"])},
                    files={"file": ("s.csv", body, "text/csv")}, headers=as_(alice), follow_redirects=False)
    assert r.status_code == 404
    r = client.post("/app/imports", data={"csrf": tok, "account_id": str(seeded["alice_acc"])},
                    files={"file": ("s.csv", body, "text/csv")}, headers=as_(alice), follow_redirects=False)
    assert r.status_code == 303
    assert "picker test" in client.get("/app", headers=as_(alice)).text.lower()


# ---- signing keys unreachable (found in the first production deploy) -----------------------------------
def test_unreachable_jwks_is_503_not_500(keypair, seeded):
    priv, _ = keypair
    def down():
        raise OSError("Temporary failure in name resolution")
    app = create_app(lambda: psycopg.connect(seeded["url"], autocommit=True),
                     CloudflareAccessAuth(TEAM, AUD, down), SECRET)
    r = TestClient(app, raise_server_exceptions=False).get("/app", headers={"cf-access-jwt-assertion": token(priv)})
    assert r.status_code == 503 and "could not reach Cloudflare" in r.text
    assert "alice salary" not in r.text.lower()


def test_cached_keys_survive_a_failed_refresh(keypair):
    priv, jwks = keypair
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("network down")
        return jwks
    auth = CloudflareAccessAuth(TEAM, AUD, flaky)
    assert auth.identify({"cf-access-jwt-assertion": token(priv)}).email == "alice@example.com"
    auth._fetched_at -= 7200  # force a refresh that fails
    assert auth.identify({"cf-access-jwt-assertion": token(priv)}).email == "alice@example.com"
    assert calls["n"] == 2


def _seed_data_rights_rows(conn):
    conn.execute("INSERT INTO category_rule (tenant_id, pattern, category, origin) VALUES ('alice', 'taxi', 'transport', 'user'), ('bob', 'private', 'shopping', 'user')")
    conn.execute("INSERT INTO budget (tenant_id, month, category, amount) VALUES ('alice', '2026-09-01', 'transport', 100000), ('bob', '2026-09-01', 'shopping', 200000)")
    conn.execute("INSERT INTO goal (tenant_id, name, target, due_on, saved) VALUES ('alice', 'Emergency reserve', 5000000, NULL, 250000)")
    conn.execute("INSERT INTO wishlist (tenant_id, item, amount, remind_at) VALUES ('alice', 'Bike repair', 300000, now() + interval '48 hours')")
    conn.execute("INSERT INTO nudge (tenant_id, kind, body, sources) VALUES ('alice', 'review', 'Alice insight', '{\"origin\":\"test\"}'::jsonb)")
    conn.execute("INSERT INTO heartbeat (tenant_id, component, last_ok) VALUES ('alice', 'scheduler', now())")
    conn.execute("INSERT INTO telegram_link (telegram_user_id, email, tenant_id) VALUES (10101, 'alice@example.com', 'alice'), (20202, 'bob@example.com', 'bob')")
    conn.execute("INSERT INTO telegram_draft (telegram_user_id, tenant_id, payload, expires_at) VALUES (10101, 'alice', '{\"amount\":100}'::jsonb, now() + interval '10 minutes')")
    conn.execute("INSERT INTO signup (email, display_name, note, lang, consent_version, consent_at, status) VALUES ('alice@example.com', 'Alice', 'Synthetic request', 'en', 'test-v1', now(), 'approved')")
    conn.execute("INSERT INTO link_code (code_hash, email, expires_at) VALUES (%s, 'alice@example.com', now() + interval '10 minutes')", ("a" * 64,))
    conn.execute("INSERT INTO login_code (email, code_hmac, salt, expires_at) VALUES ('alice@example.com', 'synthetic-hmac', 'synthetic-salt', now() + interval '10 minutes')")
    conn.execute("INSERT INTO web_session (sid_sha256, email, expires_at) VALUES (%s, 'alice@example.com', now() + interval '1 day')", ("b" * 64,))


def test_settings_offers_export_and_delete_with_backup_boundary(client):
    page = client.get("/app/settings", headers=as_("alice@example.com")).text
    assert 'href="/app/settings/export.json"' in page
    assert 'action="/app/settings/delete-data"' in page
    assert "DELETE MY SAKU DATA" in page
    assert "server backups" in page


def test_data_export_contains_tenant_data_and_excludes_other_tenants_and_auth_secrets(client, conn):
    _seed_data_rights_rows(conn)
    response = client.get("/app/settings/export.json", headers=as_("alice@example.com"))
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    exported = response.json()
    data = exported["data"]
    assert exported["tenant_id"] == "alice"
    assert data["tenant"][0]["id"] == "alice"
    assert data["accounts"][0]["label"] == "Alice Main"
    assert any(row["description"] == "ALICE LUNCH" for row in data["transactions"])
    assert data["category_rules"][0]["pattern"] == "taxi"
    assert data["budgets"][0]["category"] == "transport"
    assert data["goals"][0]["name"] == "Emergency reserve"
    assert data["wishlist"][0]["item"] == "Bike repair"
    assert data["nudges"][0]["body"] == "Alice insight"
    assert data["telegram_links"][0]["telegram_user_id"] == 10101
    assert data["telegram_drafts"][0]["payload"] == {"amount": 100}
    assert data["signup"][0]["display_name"] == "Alice"
    assert data["web_sessions"][0]["email"] == "alice@example.com"
    assert "last_seen" in data["web_sessions"][0]
    assert "sid_sha256" not in data["web_sessions"][0]
    assert "BOB PRIVATE PURCHASE" not in response.text and "Bob Secret Account" not in response.text
    assert not {"login_codes", "link_codes"} & data.keys()
    assert "synthetic-hmac" not in response.text and "synthetic-salt" not in response.text
    assert ("b" * 64) not in response.text and ("a" * 64) not in response.text


def test_export_waits_for_tenant_delete_lock(seeded, conn, db_url):
    started = threading.Event()
    finished = threading.Event()
    outcome = []

    def export():
        try:
            with psycopg.connect(db_url, autocommit=True) as other:
                started.set()
                outcome.append(Repo(other).export_tenant_data('alice', 'alice@example.com'))
        finally:
            finished.set()

    with conn.transaction():
        conn.execute("SELECT 1 FROM tenant WHERE id = 'alice' FOR UPDATE")
        worker = threading.Thread(target=export, daemon=True)
        worker.start()
        assert started.wait(2)
        blocked = not finished.wait(0.3)
    worker.join(2)
    assert blocked, "export must serialize with deletion"
    assert not worker.is_alive() and outcome[0]['tenant'][0]['id'] == 'alice'


def test_data_delete_requires_csrf_and_exact_confirmation_without_mutation(client, conn):
    _seed_data_rights_rows(conn)
    wrong_csrf = client.post("/app/settings/delete-data", data={"confirmation": "DELETE MY SAKU DATA"},
                             headers=as_("alice@example.com"))
    assert wrong_csrf.status_code == 403
    wrong_phrase = client.post("/app/settings/delete-data", data={
        "csrf": csrf(client, "alice@example.com"), "confirmation": "DELETE",
    }, headers=as_("alice@example.com"))
    assert wrong_phrase.status_code == 403
    assert conn.execute("SELECT COUNT(*) FROM account WHERE tenant_id = 'alice'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM tenant WHERE id = 'alice'").fetchone()[0] == 1


def test_data_delete_removes_only_requesting_tenants_rows_and_signs_out(client, conn):
    _seed_data_rights_rows(conn)
    response = client.post("/app/settings/delete-data", data={
        "csrf": csrf(client, "alice@example.com"), "confirmation": "DELETE MY SAKU DATA",
    }, headers=as_("alice@example.com"), follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert "saku_session" in response.headers.get("set-cookie", "")
    tenant_tables = conn.execute(
        "SELECT DISTINCT table_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND column_name = 'tenant_id' ORDER BY table_name"
    ).fetchall()
    for (table,) in tenant_tables:
        query = sql.SQL("SELECT COUNT(*) FROM {} WHERE tenant_id = %s").format(sql.Identifier(table))
        assert conn.execute(query, ("alice",)).fetchone()[0] == 0, table
    assert conn.execute("SELECT COUNT(*) FROM tenant WHERE id = 'alice'").fetchone()[0] == 0
    for table in ("signup", "link_code", "login_code", "web_session"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE email = 'alice@example.com'").fetchone()[0] == 0, table
    assert conn.execute("SELECT COUNT(*) FROM account WHERE tenant_id = 'bob'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM txn WHERE tenant_id = 'bob'").fetchone()[0] == 1
    repo = Repo(conn)
    assert repo.session_new('c' * 64, 'alice@example.com', 3600) is False
    assert conn.execute("SELECT COUNT(*) FROM web_session WHERE email = 'alice@example.com'").fetchone()[0] == 0
    assert repo.beat('alice', 'scheduler') is None
    assert conn.execute("SELECT COUNT(*) FROM heartbeat WHERE tenant_id = 'alice'").fetchone()[0] == 0
    assert repo.login_code_new('alice@example.com', 'fresh-code', 'fresh-salt', 600,
                               registration_open=True, expected_tenant='alice') is False
    assert conn.execute("SELECT COUNT(*) FROM login_code WHERE email = 'alice@example.com'").fetchone()[0] == 0
    assert Repo(conn).login_event_add("fail", "a" * 64, "b" * 64,
                                     email="alice@example.com", expected_tenant="alice") is False
    assert conn.execute("SELECT COUNT(*) FROM login_event WHERE email_hmac = %s", ("a" * 64,)).fetchone()[0] == 0


def test_login_event_writer_wins_then_delete_cleans_it(seeded, conn, db_url):
    _seed_data_rights_rows(conn)
    email_hmac, ip_hmac = "a" * 64, "b" * 64
    started = threading.Event()
    finished = threading.Event()
    outcome = []

    def delete():
        try:
            with psycopg.connect(db_url, autocommit=True) as other:
                started.set()
                outcome.append(Repo(other).delete_tenant_data("alice", "alice@example.com", email_hmac))
        finally:
            finished.set()

    with conn.transaction():
        assert Repo(conn).login_event_add("fail", email_hmac, ip_hmac, email="alice@example.com",
                                          expected_tenant="alice")
        worker = threading.Thread(target=delete, daemon=True)
        worker.start()
        assert started.wait(2)
        blocked = not finished.wait(0.3)
    worker.join(3)
    assert blocked and not worker.is_alive() and outcome == [True]
    assert conn.execute("SELECT COUNT(*) FROM login_event WHERE email_hmac = %s", (email_hmac,)).fetchone()[0] == 0


def test_tenant_delete_wins_over_late_login_event(seeded, conn, db_url):
    _seed_data_rights_rows(conn)
    email_hmac, ip_hmac = "c" * 64, "d" * 64
    started = threading.Event()
    finished = threading.Event()
    outcome = []

    def write_event():
        try:
            with psycopg.connect(db_url, autocommit=True) as other:
                started.set()
                outcome.append(Repo(other).login_event_add("fail", email_hmac, ip_hmac,
                    email="alice@example.com", expected_tenant="alice"))
        finally:
            finished.set()

    with conn.transaction():
        conn.execute("SELECT 1 FROM tenant WHERE id = 'alice' FOR UPDATE")
        worker = threading.Thread(target=write_event, daemon=True)
        worker.start()
        assert started.wait(2)
        blocked = not finished.wait(0.3)
        assert Repo(conn).delete_tenant_data("alice", "alice@example.com", email_hmac)
    worker.join(3)
    assert blocked and not worker.is_alive() and outcome == [False]
    assert conn.execute("SELECT COUNT(*) FROM tenant WHERE id = 'alice'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM login_event WHERE email_hmac = %s", (email_hmac,)).fetchone()[0] == 0


def _race_write(repo, action, email_hmac="e" * 64, ip_hmac="f" * 64, code_id=None):
    if action == "event":
        return repo.login_event_add("fail", email_hmac, ip_hmac, email="alice@example.com", expected_tenant="alice")
    if action == "code":
        return repo.login_code_new("alice@example.com", "new-code", "new-salt", 600,
                                   registration_open=True, expected_tenant="alice")
    if action == "session":
        return repo.session_new("1" * 64, "alice@example.com", 3600)
    if action == "heartbeat":
        return repo.beat("alice", "scheduler")
    if action == "redeem":
        return repo.redeem_code_session(code_id, "2" * 64, "alice@example.com", 3600)
    raise AssertionError(action)


@pytest.mark.parametrize("action", ["event", "code", "session", "heartbeat", "redeem"])
def test_tenant_delete_wins_over_late_auth_or_heartbeat_write(seeded, conn, db_url, action):
    _seed_data_rights_rows(conn)
    email_hmac, ip_hmac = "e" * 64, "f" * 64
    code_id = None
    if action == "redeem":
        repo = Repo(conn)
        assert repo.login_code_new("alice@example.com", "old-code", "old-salt", 600,
                                   expected_tenant="alice")
        code_id = conn.execute("SELECT id FROM login_code WHERE email = 'alice@example.com' ORDER BY id DESC LIMIT 1").fetchone()[0]
    started, finished, outcome = threading.Event(), threading.Event(), []

    def write_late():
        try:
            with psycopg.connect(db_url, autocommit=True) as other:
                started.set()
                outcome.append(_race_write(Repo(other), action, email_hmac, ip_hmac, code_id))
        finally:
            finished.set()

    with conn.transaction():
        conn.execute("SELECT 1 FROM tenant WHERE id = 'alice' FOR UPDATE")
        worker = threading.Thread(target=write_late, daemon=True)
        worker.start()
        assert started.wait(2)
        blocked = not finished.wait(0.3)
        assert Repo(conn).delete_tenant_data("alice", "alice@example.com", email_hmac)
    worker.join(3)
    assert blocked and not worker.is_alive()
    assert outcome == [None if action == "heartbeat" else False]
    assert conn.execute("SELECT COUNT(*) FROM tenant WHERE id = 'alice'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM login_event WHERE email_hmac = %s", (email_hmac,)).fetchone()[0] == 0
    for table in ("login_code", "web_session", "heartbeat"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE " +
                            ("email = 'alice@example.com'" if table != "heartbeat" else "tenant_id = 'alice'")).fetchone()[0] == 0


@pytest.mark.parametrize("action", ["event", "code", "session", "heartbeat", "redeem"])
def test_auth_or_heartbeat_write_wins_then_delete_cleans(seeded, conn, db_url, action):
    _seed_data_rights_rows(conn)
    email_hmac, ip_hmac = "e" * 64, "f" * 64
    code_id = None
    if action == "redeem":
        repo = Repo(conn)
        assert repo.login_code_new("alice@example.com", "old-code", "old-salt", 600,
                                   expected_tenant="alice")
        code_id = conn.execute("SELECT id FROM login_code WHERE email = 'alice@example.com' ORDER BY id DESC LIMIT 1").fetchone()[0]
    started, finished, outcome = threading.Event(), threading.Event(), []

    def delete_late():
        try:
            with psycopg.connect(db_url, autocommit=True) as other:
                started.set()
                outcome.append(Repo(other).delete_tenant_data("alice", "alice@example.com", email_hmac))
        finally:
            finished.set()

    with conn.transaction():
        write_result = _race_write(Repo(conn), action, email_hmac, ip_hmac, code_id)
        worker = threading.Thread(target=delete_late, daemon=True)
        worker.start()
        assert started.wait(2)
        blocked = not finished.wait(0.3)
    worker.join(3)
    assert blocked and not worker.is_alive() and outcome == [True]
    assert (write_result is None) if action == "heartbeat" else bool(write_result)
    assert conn.execute("SELECT COUNT(*) FROM tenant WHERE id = 'alice'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM login_event WHERE email_hmac = %s", (email_hmac,)).fetchone()[0] == 0
    for table, column in (("login_code", "email"), ("web_session", "email"), ("heartbeat", "tenant_id")):
        val = "alice@example.com" if column == "email" else "alice"
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = %s", (val,)).fetchone()[0] == 0


def test_session_creation_waits_for_tenant_delete_lock(seeded, conn, db_url):
    started = threading.Event()
    finished = threading.Event()
    outcome = []

    def create_session():
        try:
            with psycopg.connect(db_url, autocommit=True) as other:
                started.set()
                outcome.append(Repo(other).session_new('e' * 64, 'alice@example.com', 3600))
        finally:
            finished.set()

    with conn.transaction():
        conn.execute("SELECT 1 FROM tenant WHERE id = 'alice' FOR UPDATE")
        worker = threading.Thread(target=create_session, daemon=True)
        worker.start()
        assert started.wait(2)
        blocked = not finished.wait(0.3)
    worker.join(2)
    assert blocked, "session write must serialize with tenant deletion"
    assert not worker.is_alive() and outcome == [True]


def test_data_delete_refuses_shared_tenant(client, seeded, conn):
    _seed_data_rights_rows(conn)
    conn.execute("INSERT INTO member (email, tenant_id, role) VALUES ('carol@example.com', 'alice', 'user')")
    response = client.post("/app/settings/delete-data", data={
        "csrf": csrf(client, "alice@example.com"), "confirmation": "DELETE MY SAKU DATA",
    }, headers=as_("alice@example.com"))
    assert response.status_code == 403
    assert conn.execute("SELECT COUNT(*) FROM account WHERE tenant_id = 'alice'").fetchone()[0] == 1
