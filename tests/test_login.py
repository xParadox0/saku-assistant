"""Own sign-in (HLD 3.11, Stage 0f). Negatives first: no plain secrets stored, codes die, limits hold,
the login page never reveals whether an address has an account, admin needs the second lock."""
import hashlib
import hmac
import re

import psycopg
import pytest
from fastapi.testclient import TestClient

from saku.web import i18n
from saku.web.app import Beta, create_app
from saku.web.auth import AuthError, Identity
from saku.web.login import Limits, Login, MailError, OutboxMailer, email_message

SECRET = "s" * 40


class FakeAccess:
    """Stands in for Cloudflare Access on /app/admin: trusts x-access-email."""
    def identify(self, headers):
        e = headers.get("x-access-email", "")
        if not e:
            raise AuthError("missing")
        return Identity(e)


class BrokenMailer:
    def send_code(self, to, code, lang):
        raise MailError("down")


@pytest.fixture
def world(repo):
    repo.add_member("owner@example.com", "owner", "admin")
    repo.add_member("user@example.com", "u1", "user")
    return repo


def make(db_url, tmp_path, beta=None, mailer=None, limits=None):
    mailer = mailer or OutboxMailer()
    login = Login(SECRET.encode(), mailer, limits)
    app = create_app(lambda: psycopg.connect(db_url, autocommit=True), None, SECRET, str(tmp_path),
                     beta=beta or Beta(), login=login, admin_guard=FakeAccess())
    c = TestClient(app, base_url="https://testserver")
    return c, mailer


def form_csrf(html):
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def ask(c, email, ip="198.51.100.7"):
    page = c.get("/login")
    return c.post("/login", data={"csrf": form_csrf(page.text), "email": email, "next": "/app"},
                  headers={"cf-connecting-ip": ip}, follow_redirects=False)


def enter(c, email, code, ip="198.51.100.7"):
    page = c.get("/login")
    return c.post("/login/code", data={"csrf": form_csrf(page.text), "email": email, "code": code, "next": "/app"},
                  headers={"cf-connecting-ip": ip}, follow_redirects=False)


def sign_in(c, mailer, email):
    assert ask(c, email).status_code == 200
    code = [m for m in mailer.sent if m[0] == email][-1][1]
    r = enter(c, email, code)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    return r


# ---- the happy path ----------------------------------------------------------------------------------
def test_member_signs_in_with_a_code(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path)
    assert c.get("/app", follow_redirects=False).headers["location"].startswith("/login")
    sign_in(c, mailer, "user@example.com")
    assert c.get("/app").status_code == 200
    cookie = c.cookies.get("saku_session")
    assert cookie and len(cookie) >= 40


def test_root_redirects_a_live_member_session_to_latest_safe_app_page(world, db_url, tmp_path, conn):
    c, mailer = make(db_url, tmp_path)
    sign_in(c, mailer, "user@example.com")

    assert c.get("/app/accounts").status_code == 200
    home = c.get("/", follow_redirects=False)

    assert home.status_code == 303
    assert home.headers["location"] == "/app/accounts"


def test_root_stays_public_without_a_live_session(world, db_url, tmp_path):
    c, _ = make(db_url, tmp_path)
    home = c.get("/", follow_redirects=False)
    assert home.status_code == 200
    assert "Saku" in home.text


def test_root_never_redirects_to_an_unsafe_saved_path(world, db_url, tmp_path, conn):
    c, mailer = make(db_url, tmp_path)
    sign_in(c, mailer, "user@example.com")
    token = c.cookies.get("saku_session")
    conn.execute("UPDATE web_session SET last_path = %s WHERE sid_sha256 = %s",
                 ("//evil.example", hashlib.sha256(token.encode()).hexdigest()))

    home = c.get("/", follow_redirects=False)

    assert home.status_code == 303
    assert home.headers["location"] == "/app"


def test_session_cookie_flags(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path)
    ask(c, "user@example.com")
    r = enter(c, "user@example.com", mailer.sent[-1][1])
    sc = r.headers["set-cookie"].lower()
    assert "saku_session=" in sc and "httponly" in sc and "secure" in sc and "samesite=lax" in sc


# ---- nothing secret is stored in plain form -----------------------------------------------------------------
def test_no_plain_code_or_token_in_the_database(world, db_url, tmp_path, conn):
    c, mailer = make(db_url, tmp_path)
    sign_in(c, mailer, "user@example.com")
    code, token = mailer.sent[-1][1], c.cookies.get("saku_session")
    dump = " ".join(str(r) for t in ("login_code", "web_session", "login_event")
                    for r in conn.execute(f"SELECT * FROM {t}").fetchall())
    assert code not in dump and token not in dump and "user@example.com" not in str(
        conn.execute("SELECT * FROM login_event").fetchall())
    assert conn.execute("SELECT sid_sha256 FROM web_session").fetchone()[0] == hashlib.sha256(token.encode()).hexdigest()


# ---- codes die -------------------------------------------------------------------------------------------
def test_code_is_single_use(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path)
    sign_in(c, mailer, "user@example.com")
    c.cookies.clear()
    assert enter(c, "user@example.com", mailer.sent[-1][1]).status_code == 400


def test_five_wrong_tries_kill_the_code(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path)
    ask(c, "user@example.com")
    good = mailer.sent[-1][1]
    wrong = "000000" if good != "000000" else "111111"
    for _ in range(5):
        assert enter(c, "user@example.com", wrong).status_code == 400
    assert enter(c, "user@example.com", good).status_code == 400


def test_new_code_cancels_the_old_one(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path)
    ask(c, "user@example.com"); first = mailer.sent[-1][1]
    ask(c, "user@example.com"); second = mailer.sent[-1][1]
    if first != second:
        assert enter(c, "user@example.com", first).status_code == 400
    assert enter(c, "user@example.com", second).status_code == 303


def test_expired_code_fails(world, db_url, tmp_path, conn):
    c, mailer = make(db_url, tmp_path)
    ask(c, "user@example.com")
    conn.execute("UPDATE login_code SET expires_at = now() - interval '1 second'")
    assert enter(c, "user@example.com", mailer.sent[-1][1]).status_code == 400


def test_code_for_one_address_does_not_open_another(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path)
    ask(c, "user@example.com")
    assert enter(c, "owner@example.com", mailer.sent[-1][1]).status_code == 400


# ---- no account enumeration ------------------------------------------------------------------------------------
def test_login_answers_the_same_for_member_and_stranger(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path)
    a = ask(c, "user@example.com").text.replace("user@example.com", "X")
    b = ask(c, "nobody@example.com").text.replace("nobody@example.com", "X")
    strip = lambda h: re.sub(r'name="csrf" value="[0-9a-f]+"', "", h)
    assert strip(a) == strip(b)
    assert [m[0] for m in mailer.sent] == ["user@example.com"]  # the stranger got nothing


def test_closed_registration_sends_no_code_to_strangers_open_does(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path, Beta(registration_open=False))
    ask(c, "new@example.com")
    assert mailer.sent == []
    c2, mailer2 = make(db_url, tmp_path, Beta(registration_open=True, policy_reviewed=i18n.POLICY_VERSION))
    sign_in(c2, mailer2, "new@example.com")
    assert c2.get("/app", follow_redirects=False).headers["location"] == "/app/register"


# ---- rate limits ---------------------------------------------------------------------------------------------
def test_sixth_request_in_an_hour_sends_nothing(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path)
    for _ in range(5):
        assert ask(c, "user@example.com").status_code == 200
    assert ask(c, "user@example.com").status_code == 429
    assert len(mailer.sent) == 5


def test_limit_counts_strangers_too(world, db_url, tmp_path):
    """Hitting the limit must not reveal that an address is unknown."""
    c, mailer = make(db_url, tmp_path)
    for _ in range(5):
        ask(c, "nobody@example.com")
    assert ask(c, "nobody@example.com").status_code == 429


def test_per_ip_limit(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path, limits=Limits(requests_per_ip=3))
    for i in range(3):
        assert ask(c, f"a{i}@example.com", ip="203.0.113.9").status_code == 200
    assert ask(c, "user@example.com", ip="203.0.113.9").status_code == 429
    assert ask(c, "user@example.com", ip="203.0.113.10").status_code == 200


def test_wrong_codes_lock_the_address_for_an_hour(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path, limits=Limits(fails_per_email=3, requests_per_email=50))
    for _ in range(3):
        ask(c, "user@example.com")
        enter(c, "user@example.com", "999999" if mailer.sent[-1][1] != "999999" else "888888")
    ask(c, "user@example.com")
    assert enter(c, "user@example.com", mailer.sent[-1][1]).status_code == 400


# ---- sessions ----------------------------------------------------------------------------------------------
def test_sign_out_revokes_the_session(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path)
    sign_in(c, mailer, "user@example.com")
    token = c.cookies.get("saku_session")
    page = c.get("/app/settings").text
    csrf = re.search(r'action="/logout"><input type="hidden" name="csrf" value="([0-9a-f]+)"', page).group(1)
    assert c.post("/logout", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    c.cookies.set("saku_session", token)  # replaying the old cookie
    assert c.get("/app", follow_redirects=False).headers["location"].startswith("/login")


def test_expired_or_idle_session_needs_login(world, db_url, tmp_path, conn):
    c, mailer = make(db_url, tmp_path)
    sign_in(c, mailer, "user@example.com")
    conn.execute("UPDATE web_session SET last_seen = now() - interval '8 days'")
    assert c.get("/app", follow_redirects=False).headers["location"].startswith("/login")
    sign_in(c, mailer, "user@example.com")
    conn.execute("UPDATE web_session SET expires_at = now() - interval '1 second'")
    assert c.get("/app", follow_redirects=False).headers["location"].startswith("/login")


def test_forged_session_cookie_is_ignored(world, db_url, tmp_path):
    c, _ = make(db_url, tmp_path)
    c.cookies.set("saku_session", "x" * 43)
    assert c.get("/app", follow_redirects=False).headers["location"].startswith("/login")


def test_access_headers_alone_do_not_sign_in(world, db_url, tmp_path):
    c, _ = make(db_url, tmp_path)
    r = c.get("/app", headers={"Cf-Access-Authenticated-User-Email": "owner@example.com",
                               "x-access-email": "owner@example.com"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


# ---- admin needs both locks ----------------------------------------------------------------------------------
def test_admin_needs_session_and_access_for_the_same_email(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path)
    sign_in(c, mailer, "owner@example.com")
    assert c.get("/app/admin").status_code == 401                                      # no Access JWT
    assert c.get("/app/admin", headers={"x-access-email": "user@example.com"}).status_code == 401  # other email
    assert c.get("/app/admin", headers={"x-access-email": "owner@example.com"}).status_code == 200


def test_user_role_gets_403_on_admin_even_with_access(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path)
    sign_in(c, mailer, "user@example.com")
    assert c.get("/app/admin", headers={"x-access-email": "user@example.com"}).status_code == 403


# ---- forms -----------------------------------------------------------------------------------------------------
def test_login_post_without_the_pre_cookie_token_is_refused(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path)
    r = c.post("/login", data={"csrf": "0" * 64, "email": "user@example.com"}, follow_redirects=False)
    assert r.status_code == 403 and mailer.sent == []
    other, _ = make(db_url, tmp_path)
    token = form_csrf(other.get("/login").text)  # a token minted for another browser
    assert c.post("/login", data={"csrf": token, "email": "user@example.com"}).status_code == 403


def test_next_cannot_leave_the_app(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path)
    page = c.get("/login?next=https://evil.example/x")
    assert 'value="/app"' in page.text
    ask(c, "user@example.com")
    pg = c.get("/login")
    r = c.post("/login/code", data={"csrf": form_csrf(pg.text), "email": "user@example.com",
                                    "code": mailer.sent[-1][1], "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/app"


def test_mail_failure_creates_no_session_and_says_so(world, db_url, tmp_path):
    c, _ = make(db_url, tmp_path, mailer=BrokenMailer())
    r = ask(c, "user@example.com")
    assert r.status_code == 503 and "saku_session" not in c.cookies


def test_login_pages_render_in_both_languages(world, db_url, tmp_path):
    c, _ = make(db_url, tmp_path)
    assert "Sign in to Saku" in c.get("/login").text
    c.cookies.set("saku_lang", "id")
    assert "Masuk ke Saku" in c.get("/login").text


def test_closed_registration_is_explained_not_a_dead_end(world, db_url, tmp_path):
    """While registration is closed the landing does not offer a sign-up that can never send a code,
    and /login says so for everyone (global state, so it reveals nothing about any address)."""
    c, _ = make(db_url, tmp_path, Beta(registration_open=False))
    home = c.get("/").text
    assert "Request beta access" not in home and "Registration opens after" in home
    assert "invite-only" in c.get("/login").text
    c2, _ = make(db_url, tmp_path, Beta(registration_open=True, policy_reviewed=i18n.POLICY_VERSION))
    assert "Request beta access" in c2.get("/").text and "invite-only" not in c2.get("/login").text


def test_request_access_is_a_two_step_flow_that_ends_on_the_form(world, db_url, tmp_path):
    c, mailer = make(db_url, tmp_path, Beta(registration_open=True, policy_reviewed=i18n.POLICY_VERSION))
    assert 'href="/login?next=/app/register"' in c.get("/").text
    page = c.get("/login?next=/app/register").text
    assert "Step 1 of 2" in page and 'name="next" value="/app/register"' in page
    r = c.post("/login", data={"csrf": form_csrf(page), "email": "new@example.com", "next": "/app/register"})
    assert "Step 1 of 2" in r.text
    r = c.post("/login/code", data={"csrf": form_csrf(c.get("/login").text), "email": "new@example.com",
                                    "code": mailer.sent[-1][1], "next": "/app/register"}, follow_redirects=False)
    assert r.headers["location"] == "/app/register"
    assert "Step 2 of 2" in c.get("/app/register").text


def test_email_code_uses_saku_branded_html_in_both_languages():
    for lang, phrase, duration in (("en", "Your sign-in code", "10 minutes"),
                                   ("id", "Kode masuk kamu", "10 menit")):
        subject, html = email_message(lang, "246810")
        assert "246810" in subject and "246810" in html
        assert phrase in html and duration in html
        assert "#0F6B4F" in html and "#F7F3EA" in html and "#F0BE63" in html
        assert "Saku" in html and "password" in html and "PIN" in html and "OTP" in html
