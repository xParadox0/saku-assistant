"""Dashboard tests. The negatives matter most: forged identity, wrong tenant, missing CSRF, admin leakage."""
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import jwt
import psycopg
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
    page = client.get("/accounts", headers=as_(email)).text
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


# ---- tenant isolation ------------------------------------------------------------------------------
def test_overview_shows_only_own_data(client):
    page = client.get("/", headers=as_("alice@example.com")).text
    assert "ALICE SALARY" in page and "Rp5.000.000" in page
    assert "BOB" not in page and "777" not in page


def test_unknown_email_is_refused(client):
    r = client.get("/", headers=as_("stranger@example.com"))
    assert r.status_code == 403 and "ALICE" not in r.text


def test_cannot_touch_another_tenants_account(client, seeded, upload_dir):
    tok = csrf(client, "alice@example.com")
    bob = seeded["bob_acc"]
    r = client.post(f"/accounts/{bob}/rename", data={"csrf": tok, "label": "pwned"}, headers=as_("alice@example.com"))
    assert r.status_code == 404
    r = client.post(f"/accounts/{bob}/delete", data={"csrf": tok}, headers=as_("alice@example.com"))
    assert r.status_code == 404
    r = client.post(f"/accounts/{bob}/import", data={"csrf": tok}, headers=as_("alice@example.com"),
                    files={"file": ("x.csv", b"date,amount,description\n2026-09-01,1,X\n", "text/csv")})
    assert r.status_code == 404
    assert "Bob Secret Account" in client.get("/accounts", headers=as_("bob@example.com")).text


# ---- CSRF --------------------------------------------------------------------------------------------
def test_post_without_csrf_is_refused(client, seeded):
    r = client.post(f"/accounts/{seeded['alice_acc']}/rename", data={"label": "x"}, headers=as_("alice@example.com"))
    assert r.status_code == 403


def test_csrf_token_of_another_user_is_refused(client, seeded):
    bob_tok = csrf(client, "bob@example.com")
    r = client.post(f"/accounts/{seeded['alice_acc']}/rename", data={"csrf": bob_tok, "label": "x"},
                    headers=as_("alice@example.com"))
    assert r.status_code == 403


# ---- admin -------------------------------------------------------------------------------------------
def test_admin_requires_admin_role(client):
    assert client.get("/admin", headers=as_("alice@example.com")).status_code == 403


def test_admin_shows_metadata_not_transactions(client):
    page = client.get("/admin", headers=as_("ops@example.com")).text
    assert "alice" in page and "bob" in page
    for leaked in ("ALICE SALARY", "BOB PRIVATE PURCHASE", "Bob Secret Account", "777.000", "5.000.000"):
        assert leaked not in page
    assert "No component has reported yet" in page  # empty heartbeats are shown as a warning, not as calm


# ---- account management and upload ------------------------------------------------------------------
def test_add_rename_delete_account(client):
    tok = csrf(client, "alice@example.com")
    r = client.post("/accounts", data={"csrf": tok, "kind": "ewallet", "provider": "wallet_x", "label": "Pocket", "last4": ""},
                    headers=as_("alice@example.com"), follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/accounts", headers=as_("alice@example.com")).text
    assert "Pocket" in page


def test_account_with_transactions_is_not_deleted(client, seeded):
    tok = csrf(client, "alice@example.com")
    r = client.post(f"/accounts/{seeded['alice_acc']}/delete", data={"csrf": tok}, headers=as_("alice@example.com"))
    assert "was not deleted" in r.text and "Alice Main" in r.text


def test_full_account_number_rejected_by_form(client):
    tok = csrf(client, "alice@example.com")
    r = client.post("/accounts", data={"csrf": tok, "kind": "bank", "provider": "bank_x", "label": "X", "last4": "1234567890"},
                    headers=as_("alice@example.com"))
    assert r.status_code == 403


def test_csv_upload_imports_dedupes_and_deletes_file(client, seeded, upload_dir):
    tok = csrf(client, "alice@example.com")
    body = b"date,amount,description,kind\n2026-10-01,-45000,KOPI,normal\n02/10/2026,-15000,ADM,fee\n"
    acc = seeded["alice_acc"]
    r1 = client.post(f"/accounts/{acc}/import", data={"csrf": tok}, headers=as_("alice@example.com"),
                     files={"file": ("mutasi.csv", body, "text/csv")})
    assert "2 rows read, 2 new, 0 already imported" in r1.text
    r2 = client.post(f"/accounts/{acc}/import", data={"csrf": tok}, headers=as_("alice@example.com"),
                     files={"file": ("mutasi.csv", body, "text/csv")})
    assert "2 rows read, 0 new, 2 already imported" in r2.text
    assert list(upload_dir.iterdir()) == []


def test_bad_csv_is_recorded_as_failed_and_nothing_imported(client, seeded, conn):
    tok = csrf(client, "alice@example.com")
    before = Repo(conn).txn_count("alice")
    r = client.post(f"/accounts/{seeded['alice_acc']}/import", data={"csrf": tok}, headers=as_("alice@example.com"),
                    files={"file": ("bad.csv", b"date,amount,description\n2026-10-01,1.234.000,X\n", "text/csv")})
    assert "Import failed" in r.text and "line 2" in r.text
    assert Repo(conn).txn_count("alice") == before
    assert "failed" in client.get("/imports", headers=as_("alice@example.com")).text


def test_unsupported_pdf_is_reported(client, seeded):
    tok = csrf(client, "alice@example.com")
    r = client.post(f"/accounts/{seeded['alice_acc']}/import", data={"csrf": tok}, headers=as_("alice@example.com"),
                    files={"file": ("statement.pdf", b"%PDF-1.4 fake", "application/pdf")})
    assert "format not supported yet" in r.text


def test_upload_size_limit(client, seeded):
    tok = csrf(client, "alice@example.com")
    big = b"date,amount,description\n" + b"2026-10-01,-1,X\n" * 400_000
    r = client.post(f"/accounts/{seeded['alice_acc']}/import", data={"csrf": tok}, headers=as_("alice@example.com"),
                    files={"file": ("big.csv", big, "text/csv")})
    assert r.status_code == 413


def test_security_headers(client):
    r = client.get("/", headers=as_("alice@example.com"))
    assert "script-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"


def test_demo_tenant_renders(conn, db_url):
    load_demo(Repo(conn))
    app = create_app(lambda: psycopg.connect(db_url, autocommit=True), DevAuth("demo@saku.local"), SECRET)
    page = TestClient(app).get("/").text
    assert "September 2026" in page and "Rp15.862.010" in page  # matches the CLI demo output


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
    r = TestClient(app).get("/", headers={"cf-access-authenticated-user-email": "alice@example.com"})
    assert r.status_code == 401 and "ALICE" not in r.text


def test_full_request_with_valid_jwt(keypair, seeded):
    priv, jwks = keypair
    app = create_app(lambda: psycopg.connect(seeded["url"], autocommit=True),
                     CloudflareAccessAuth(TEAM, AUD, lambda: jwks), SECRET)
    r = TestClient(app).get("/", headers={"cf-access-jwt-assertion": token(priv)})
    assert r.status_code == 200 and "ALICE SALARY" in r.text


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
