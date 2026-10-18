"""Saku's own sign-in (HLD 3.11): 6-digit email codes, server-side sessions, rate limits.

Nothing secret is stored in plain form. Codes are kept as HMAC(secret, salt|email|code); session tokens as SHA-256;
rate-limit records as HMACs of the email and of the client IP. The mailer is injected so tests never send mail.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from ..repo import Repo

EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]{1,64}@[a-z0-9.\-]{1,190}\.[a-z]{2,24}$")
CODE_TTL_S = 10 * 60
MAX_CODE_ATTEMPTS = 5
SESSION_TTL_S = 30 * 24 * 3600
SESSION_IDLE_S = 7 * 24 * 3600
HOUR = 3600


@dataclass(frozen=True)
class Limits:
    requests_per_email: int = 5
    requests_per_ip: int = 20
    fails_per_email: int = 10
    fails_per_ip: int = 50


class Mailer(Protocol):
    def send_code(self, to: str, code: str, lang: str) -> None: ...


class MailError(Exception):
    """The code could not be sent. The user is told; no session is created."""


def normalise_email(raw: str) -> str | None:
    e = (raw or "").strip().lower()
    return e if len(e) <= 254 and EMAIL_RE.fullmatch(e) else None


class Login:
    def __init__(self, secret: bytes, mailer: Mailer, limits: Limits | None = None):
        self.key = hashlib.sha256(b"saku-login|" + secret).digest()  # separate from the CSRF key
        self.mailer = mailer
        self.limits = limits or Limits()

    # ---- hashing helpers ----------------------------------------------------------------------------
    def _h(self, *parts: str) -> str:
        return hmac.new(self.key, "|".join(parts).encode(), hashlib.sha256).hexdigest()

    def email_tag(self, email: str) -> str:
        return self._h("email", email)

    def ip_tag(self, ip: str) -> str:
        return self._h("ip", ip or "unknown")

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    # ---- step 1: ask for a code -----------------------------------------------------------------------
    def request_code(self, repo: Repo, email: str, ip: str, lang: str, registration_open: bool) -> str:
        """Returns 'sent', 'silent' (not eligible: nothing sent, nothing stored but the rate event) or 'limited'.
        The caller shows the same page for 'sent' and 'silent'. Raises MailError if sending failed."""
        et, it = self.email_tag(email), self.ip_tag(ip)
        by_email, by_ip = repo.login_events("request", et, it, HOUR)
        if by_email >= self.limits.requests_per_email or by_ip >= self.limits.requests_per_ip:
            return "limited"
        repo.login_event_add("request", et, it)  # counted for every address, eligible or not
        if not (registration_open or repo.may_sign_in(email)):
            return "silent"
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(8)
        repo.login_code_new(email, self._h("code", salt, email, code), salt, CODE_TTL_S)
        self.mailer.send_code(email, code, lang)
        return "sent"

    # ---- step 2: redeem it ------------------------------------------------------------------------------
    def verify_code(self, repo: Repo, email: str, code: str, ip: str) -> str | None:
        """Returns a new session token on success, else None. Wrong tries are counted per code, email and IP."""
        et, it = self.email_tag(email), self.ip_tag(ip)
        fails_email, fails_ip = repo.login_events("fail", et, it, HOUR)
        if fails_email >= self.limits.fails_per_email or fails_ip >= self.limits.fails_per_ip:
            return None
        code = re.sub(r"\D", "", code or "")
        live = repo.login_code_live(email)
        ok = (live is not None and len(code) == 6
              and hmac.compare_digest(live["code_hmac"], self._h("code", live["salt"], email, code)))
        if live is None or not ok:
            repo.login_event_add("fail", et, it)
            if live is not None:
                repo.login_code_fail(live["id"], MAX_CODE_ATTEMPTS)
            return None
        if not repo.login_code_use(live["id"]):  # lost a race with a parallel request
            return None
        token = secrets.token_urlsafe(32)
        repo.session_new(self.token_hash(token), email, SESSION_TTL_S)
        return token

    def session_email(self, repo: Repo, token: str | None) -> str | None:
        if not token or len(token) > 100:
            return None
        return repo.session_email(self.token_hash(token), SESSION_IDLE_S)

    def end_session(self, repo: Repo, token: str | None) -> None:
        if token and len(token) <= 100:
            repo.session_revoke(self.token_hash(token))


# ---- mail ------------------------------------------------------------------------------------------------
SUBJECT = {"en": "Your Saku sign-in code: {code}", "id": "Kode masuk Saku kamu: {code}"}
BODY = {
    "en": ("Your sign-in code for Saku is\n\n    {code}\n\nIt works once, for 10 minutes.\n\n"
           "If you did not try to sign in, ignore this email. Nobody can sign in without this code.\n\n"
           "Saku will never ask for your bank password, PIN or OTP.\n"),
    "id": ("Kode masuk Saku kamu:\n\n    {code}\n\nKode ini hanya bisa dipakai sekali dan berlaku 10 menit.\n\n"
           "Kalau kamu tidak sedang masuk ke Saku, abaikan email ini. Tanpa kode ini, tidak ada yang bisa masuk.\n\n"
           "Saku tidak pernah meminta password bank, PIN, atau OTP kamu.\n"),
}


class GraphMailer:
    """Microsoft Graph sendMail, app-only (client credentials), from one mailbox.
    The app registration should be limited to that mailbox with an Exchange application access policy."""

    def __init__(self, tenant: str, client_id: str, client_secret: str, sender: str):
        if not (tenant and client_id and client_secret and normalise_email(sender)):
            raise ValueError("Graph mailer needs tenant, client id, client secret and a sender address")
        self.tenant, self.client_id, self.client_secret, self.sender = tenant, client_id, client_secret, sender
        self._token, self._token_exp = "", 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 120:
            return self._token
        data = urllib.parse.urlencode({"client_id": self.client_id, "client_secret": self.client_secret,
                                       "scope": "https://graph.microsoft.com/.default",
                                       "grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token", data=data)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read())
        except (OSError, ValueError) as e:
            raise MailError(f"token request failed: {type(e).__name__}") from None
        self._token, self._token_exp = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        return self._token

    def send_code(self, to: str, code: str, lang: str) -> None:
        lang = lang if lang in BODY else "en"
        msg = {"message": {"subject": SUBJECT[lang].format(code=code),
                           "body": {"contentType": "Text", "content": BODY[lang].format(code=code)},
                           "toRecipients": [{"emailAddress": {"address": to}}]},
               "saveToSentItems": False}
        req = urllib.request.Request(
            f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(self.sender)}/sendMail",
            data=json.dumps(msg).encode(), method="POST",
            headers={"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status != 202:
                    raise MailError(f"sendMail answered {r.status}")
        except OSError as e:
            raise MailError(f"sendMail failed: {type(e).__name__}") from None


class OutboxMailer:
    """Keeps messages in memory. Used by tests and by the local demo server, where the code is printed."""

    def __init__(self, echo: bool = False):
        self.sent: list[tuple[str, str, str]] = []
        self.echo = echo

    def send_code(self, to: str, code: str, lang: str) -> None:
        self.sent.append((to, code, lang))
        if self.echo:
            print(f"[dev mail] sign-in code for {to}: {code}", flush=True)
