"""Closed beta (HLD 3.10, PRD F24-F27). Negatives first: a request is not an account, public pages show only the
demo tenant, the gates hold until the owner opens them."""
from datetime import date
from decimal import Decimal

import psycopg
import pytest
from fastapi.testclient import TestClient

from saku.__main__ import load_demo
from saku.normalise import RawTxn
from saku.repo import Repo
from saku.web import i18n
from saku.web.app import Beta, create_app

SECRET = "z" * 40
OPEN = Beta(registration_open=True, pending_cap=3, approved_cap=2, policy_reviewed=i18n.POLICY_VERSION, contact="x@example.org")


class HeaderAuth:
    def identify(self, headers):
        from saku.web.auth import AuthError, Identity
        e = headers.get("x-test-email", "")
        if not e:
            raise AuthError("missing")
        return Identity(e)


def as_(email):
    return {"x-test-email": email}


@pytest.fixture
def world(repo, conn):
    repo.add_member("owner@example.com", "owner", "admin")
    a = repo.add_account("owner", "bank", "bank_a", "Owner Main")
    repo.import_rows("owner", a, [RawTxn(date(2026, 9, 1), Decimal(7_777_000), "OWNER SECRET SALARY")], "csv", "t")
    load_demo(repo, with_member=False)
    return repo


def make(db_url, tmp_path, beta):
    return TestClient(create_app(lambda: psycopg.connect(db_url, autocommit=True), HeaderAuth(), SECRET, str(tmp_path), beta=beta))


def csrf_for(c, email):
    """The same HMAC the app uses (bound to the verified email), so tests can post even when no form is shown."""
    import hashlib, hmac
    return hmac.new(SECRET.encode(), f"csrf|{email}".encode(), hashlib.sha256).hexdigest()


# ---- public pages --------------------------------------------------------------------------------------
def test_public_pages_need_no_login(world, db_url, tmp_path):
    c = make(db_url, tmp_path, Beta())
    for path in ("/", "/demo", "/privacy", "/terms", "/healthz"):
        assert c.get(path).status_code == 200, path


def test_public_pages_show_only_the_demo_tenant(world, db_url, tmp_path):
    c = make(db_url, tmp_path, Beta())
    for path in ("/", "/demo", "/privacy", "/terms"):
        low = c.get(path).text.lower()
        assert "owner secret salary" not in low and "7.777.000" not in low and "owner main" not in low, path
    demo = c.get("/demo").text
    assert "made-up data" in demo and "Bank A (demo)" in demo and "Rp14.445.020" in demo


def test_demo_has_no_write_forms_or_app_links(world, db_url, tmp_path):
    demo = make(db_url, tmp_path, Beta()).get("/demo").text
    assert 'action="/app' not in demo and 'href="/app/accounts' not in demo and 'href="/app/imports' not in demo


def test_demo_tenant_has_no_login(world):
    assert world.member("demo@saku.local") is None


def test_language_switch(world, db_url, tmp_path):
    c = make(db_url, tmp_path, Beta())
    assert "Request beta access" in c.get("/").text or "Registration opens" in c.get("/").text
    r = c.post("/lang", data={"lang": "id", "next": "/privacy"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/privacy" and "saku_lang=id" in r.headers["set-cookie"]
    c.cookies.set("saku_lang", "id")
    page = c.get("/privacy").text
    assert 'lang="id"' in page and "Kebijakan" in page and "UU PDP" in page


def test_privacy_policy_describes_saku_login_and_graph_mail(world, db_url, tmp_path):
    c = make(db_url, tmp_path, Beta())
    page = c.get("/privacy").text
    assert "six-digit code sent through Microsoft 365" in page
    assert "Cloudflare Access when you sign in" not in page
    c.cookies.set("saku_lang", "id")
    page = c.get("/privacy").text
    assert "kode 6 digit melalui Microsoft 365" in page
    assert "diverifikasi oleh Cloudflare Access" not in page


@pytest.mark.parametrize("nxt", ["https://evil.example/", "//evil.example/", "/\\evil.example"])
def test_language_switch_refuses_open_redirect(world, db_url, tmp_path, nxt):
    r = make(db_url, tmp_path, Beta()).post("/lang", data={"lang": "id", "next": nxt}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_bad_language_cookie_falls_back_to_english(world, db_url, tmp_path):
    c = make(db_url, tmp_path, Beta())
    c.cookies.set("saku_lang", "<script>")
    assert 'lang="en"' in c.get("/").text


def test_every_page_keeps_strict_csp(world, db_url, tmp_path):
    c = make(db_url, tmp_path, Beta())
    for path in ("/", "/demo", "/privacy"):
        assert "script-src 'none'" in c.get(path).headers["content-security-policy"]


def test_policy_marked_draft_until_reviewed(world, db_url, tmp_path):
    assert "not final" in make(db_url, tmp_path, Beta()).get("/privacy").text
    assert "not final" not in make(db_url, tmp_path, OPEN).get("/privacy").text


# ---- registration --------------------------------------------------------------------------------------
def test_registration_closed_by_default_and_stores_nothing(world, db_url, tmp_path):
    c = make(db_url, tmp_path, Beta())
    assert "Registration is not open yet" in c.get("/app/register", headers=as_("new@example.com")).text
    tok = csrf_for(c, "new@example.com")
    c.post("/app/register", data={"csrf": tok, "display_name": "New", "consent": "yes"}, headers=as_("new@example.com"))
    assert world.signup_get("new@example.com") is None


def test_register_creates_request_not_account(world, db_url, tmp_path):
    c = make(db_url, tmp_path, OPEN)
    tok = csrf_for(c, "new@example.com")
    r = c.post("/app/register", data={"csrf": tok, "display_name": " New   Person ", "note": "from LinkedIn", "consent": "yes"},
               headers=as_("new@example.com"))
    assert "waiting for review" in r.text
    s = world.signup_get("new@example.com")
    assert s["status"] == "pending" and s["display_name"] == "New Person" and s["consent_version"] == i18n.POLICY_VERSION
    assert world.member("new@example.com") is None
    assert world.tenant_ids() == sorted(world.tenant_ids())  # no new tenant was created
    assert not any(t.startswith("t_") for t in world.tenant_ids())


def test_pending_user_reaches_nothing(world, db_url, tmp_path):
    c = make(db_url, tmp_path, OPEN)
    tok = csrf_for(c, "new@example.com")
    c.post("/app/register", data={"csrf": tok, "display_name": "New", "consent": "yes"}, headers=as_("new@example.com"))
    h = as_("new@example.com")
    for path in ("/app", "/app/accounts", "/app/imports", "/app/settings", "/app/admin"):
        r = c.get(path, headers=h, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/app/register", path
    assert c.post("/app/settings/telegram/code", data={"csrf": tok}, headers=h).status_code == 403
    assert c.post("/app/imports", data={"csrf": tok, "account_id": "1"}, files={"file": ("a.csv", b"x", "text/csv")},
                  headers=h).status_code == 403
    with pytest.raises(ValueError):
        world.create_link_code("new@example.com")


def test_consent_required(world, db_url, tmp_path):
    c = make(db_url, tmp_path, OPEN)
    tok = csrf_for(c, "new@example.com")
    r = c.post("/app/register", data={"csrf": tok, "display_name": "New"}, headers=as_("new@example.com"))
    assert r.status_code == 400 and world.signup_get("new@example.com") is None


def test_register_requires_csrf(world, db_url, tmp_path):
    c = make(db_url, tmp_path, OPEN)
    r = c.post("/app/register", data={"csrf": "nope", "display_name": "New", "consent": "yes"}, headers=as_("new@example.com"))
    assert r.status_code == 403 and world.signup_get("new@example.com") is None


def test_register_without_identity_is_refused(world, db_url, tmp_path):
    r = make(db_url, tmp_path, OPEN).post("/app/register", data={"display_name": "X", "consent": "yes"})
    assert r.status_code == 401


def test_email_comes_from_identity_not_form(world, db_url, tmp_path):
    c = make(db_url, tmp_path, OPEN)
    tok = csrf_for(c, "real@example.com")
    c.post("/app/register", data={"csrf": tok, "display_name": "X", "consent": "yes", "email": "owner@example.com"},
           headers=as_("real@example.com"))
    assert world.signup_get("real@example.com") is not None
    assert world.signup_get("owner@example.com") is None


def test_pending_cap(world, db_url, tmp_path):
    c = make(db_url, tmp_path, OPEN)
    for i in range(3):
        e = f"p{i}@example.com"
        c.post("/app/register", data={"csrf": csrf_for(c, e), "display_name": f"P{i}", "consent": "yes"}, headers=as_(e))
    assert "waiting list is full" in c.get("/app/register", headers=as_("late@example.com")).text
    assert world.signup_counts()["pending"] == 3


def test_resubmission_changes_nothing(world, db_url, tmp_path):
    c = make(db_url, tmp_path, OPEN)
    tok = csrf_for(c, "new@example.com")
    c.post("/app/register", data={"csrf": tok, "display_name": "First", "consent": "yes"}, headers=as_("new@example.com"))
    c.post("/app/register", data={"csrf": tok, "display_name": "Second", "consent": "yes"}, headers=as_("new@example.com"))
    assert world.signup_get("new@example.com")["display_name"] == "First"


def test_member_is_sent_to_app(world, db_url, tmp_path):
    r = make(db_url, tmp_path, OPEN).get("/app/register", headers=as_("owner@example.com"), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app"


# ---- approval ----------------------------------------------------------------------------------------
def _request(c, email):
    c.post("/app/register", data={"csrf": csrf_for(c, email), "display_name": email.split("@")[0], "consent": "yes"},
           headers=as_(email))


def _owner_csrf(c):
    page = c.get("/app/admin", headers=as_("owner@example.com")).text
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def test_approve_creates_exactly_one_tenant_and_member(world, db_url, tmp_path):
    c = make(db_url, tmp_path, OPEN)
    _request(c, "new@example.com")
    before = set(world.tenant_ids())
    r = c.post("/app/admin/signups", data={"csrf": _owner_csrf(c), "email": "new@example.com", "action": "approve"},
               headers=as_("owner@example.com"))
    assert "Approved" in r.text
    added = set(world.tenant_ids()) - before
    assert len(added) == 1 and next(iter(added)).startswith("t_") and "new" not in next(iter(added))
    m = world.member("new@example.com")
    assert m["role"] == "user" and m["tenant_id"] in added
    page = c.get("/app", headers=as_("new@example.com")).text
    assert "first account" in page and "owner secret salary" not in page.lower()


def test_approval_blocked_until_policy_reviewed(world, db_url, tmp_path):
    beta = Beta(registration_open=True, pending_cap=5, approved_cap=5, policy_reviewed="")
    c = make(db_url, tmp_path, beta)
    _request(c, "new@example.com")
    r = c.post("/app/admin/signups", data={"csrf": _owner_csrf(c), "email": "new@example.com", "action": "approve"},
               headers=as_("owner@example.com"))
    assert "Approvals are blocked" in r.text and world.member("new@example.com") is None


def test_reject_keeps_consent_record_and_grants_nothing(world, db_url, tmp_path):
    c = make(db_url, tmp_path, OPEN)
    _request(c, "new@example.com")
    c.post("/app/admin/signups", data={"csrf": _owner_csrf(c), "email": "new@example.com", "action": "reject"},
           headers=as_("owner@example.com"))
    s = world.signup_get("new@example.com")
    assert s["status"] == "rejected" and world.member("new@example.com") is None
    assert "was not approved" in c.get("/app/register", headers=as_("new@example.com")).text


def test_user_role_cannot_approve(world, db_url, tmp_path):
    c = make(db_url, tmp_path, OPEN)
    _request(c, "a@example.com"); _request(c, "b@example.com")
    c.post("/app/admin/signups", data={"csrf": _owner_csrf(c), "email": "a@example.com", "action": "approve"},
           headers=as_("owner@example.com"))
    tok = csrf_for(c, "a@example.com")
    r = c.post("/app/admin/signups", data={"csrf": tok, "email": "b@example.com", "action": "approve"}, headers=as_("a@example.com"))
    assert r.status_code == 403 and world.member("b@example.com") is None
    assert c.get("/app/admin", headers=as_("a@example.com")).status_code == 403


def test_approved_cap(world, db_url, tmp_path):
    c = make(db_url, tmp_path, OPEN)
    for e in ("a@example.com", "b@example.com", "c@example.com"):
        _request(c, e)
    tok = _owner_csrf(c)
    for e in ("a@example.com", "b@example.com"):
        c.post("/app/admin/signups", data={"csrf": tok, "email": e, "action": "approve"}, headers=as_("owner@example.com"))
    r = c.post("/app/admin/signups", data={"csrf": tok, "email": "c@example.com", "action": "approve"}, headers=as_("owner@example.com"))
    assert "beta is full" in r.text and world.member("c@example.com") is None


def test_double_approve_is_harmless(world, db_url, tmp_path):
    c = make(db_url, tmp_path, OPEN)
    _request(c, "new@example.com")
    tok = _owner_csrf(c)
    for _ in range(2):
        r = c.post("/app/admin/signups", data={"csrf": tok, "email": "new@example.com", "action": "approve"}, headers=as_("owner@example.com"))
    assert "no longer pending" in r.text
    assert sum(1 for t in world.tenant_ids() if t.startswith("t_")) == 1


def test_queue_shows_requests_but_no_financial_data(world, db_url, tmp_path):
    c = make(db_url, tmp_path, OPEN)
    _request(c, "new@example.com")
    page = c.get("/app/admin", headers=as_("owner@example.com")).text
    assert "new@example.com" in page and "Beta requests" in page


def test_multiple_access_audiences_accepted():
    import time
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from saku.web.auth import AuthError, CloudflareAccessAuth
    team = "https://example-team.cloudflareaccess.com"
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key(), as_dict=True); jwk.update(kid="k1", alg="RS256")
    auth = CloudflareAccessAuth(team, "aud-app, aud-admin", lambda: {"keys": [jwk]})
    now = int(time.time())
    def tok(aud):
        return jwt.encode({"email": "a@example.com", "aud": [aud], "iss": team, "iat": now, "exp": now + 60}, priv,
                          algorithm="RS256", headers={"kid": "k1"})
    assert auth.identify({"cf-access-jwt-assertion": tok("aud-app")}).email == "a@example.com"
    assert auth.identify({"cf-access-jwt-assertion": tok("aud-admin")}).email == "a@example.com"
    with pytest.raises(AuthError):
        auth.identify({"cf-access-jwt-assertion": tok("aud-other-app")})


def test_indonesian_strings_complete():
    import re
    from saku.web.i18n import EN
    from saku.web.i18n_id import ID
    assert set(EN) == set(ID)
    for k in EN:
        assert set(re.findall(r"{\w+}", EN[k])) == set(re.findall(r"{\w+}", ID[k])), k


def test_contact_email_survives_cloudflare_obfuscation(world, db_url, tmp_path):
    """Cloudflare's Email Obfuscation needs a script that our CSP blocks; email_off keeps the address readable."""
    c = make(db_url, tmp_path, OPEN)
    for lang in ("en", "id"):
        c.cookies.set("saku_lang", lang)
        page = c.get("/privacy").text
        assert "<!--email_off--><a href=\"mailto:x@example.org\">x@example.org</a><!--/email_off-->" in page, lang
