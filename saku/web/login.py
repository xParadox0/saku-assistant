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
        member = repo.member(email)
        known_identity = member is not None or repo.may_sign_in(email)
        repo.login_event_add("request", et, it, email=email,
                             expected_tenant=member["tenant_id"] if member else None)
        if not (registration_open or known_identity):
            return "silent"
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(8)
        created = repo.login_code_new(
            email, self._h("code", salt, email, code), salt, CODE_TTL_S,
            registration_open=registration_open,
            expected_tenant=member["tenant_id"] if member else None,
            expected_signup=known_identity and member is None,
        )
        if not created:
            return "silent"
        self.mailer.send_code(email, code, lang)
        return "sent"

    # ---- step 2: redeem it ------------------------------------------------------------------------------
    def verify_code(self, repo: Repo, email: str, code: str, ip: str, *, allow_registration: bool = False) -> str | None:
        """Returns a new session token on success, else None. Wrong tries are counted per code, email and IP."""
        et, it = self.email_tag(email), self.ip_tag(ip)
        member = repo.member(email)
        fails_email, fails_ip = repo.login_events("fail", et, it, HOUR)
        if fails_email >= self.limits.fails_per_email or fails_ip >= self.limits.fails_per_ip:
            return None
        code = re.sub(r"\D", "", code or "")
        live = repo.login_code_live(email)
        ok = (live is not None and len(code) == 6
              and hmac.compare_digest(live["code_hmac"], self._h("code", live["salt"], email, code)))
        if live is None or not ok:
            repo.login_event_add("fail", et, it, email=email,
                                 expected_tenant=member["tenant_id"] if member else None)
            if live is not None:
                repo.login_code_fail(live["id"], MAX_CODE_ATTEMPTS)
            return None
        token = secrets.token_urlsafe(32)
        if not repo.redeem_code_session(live["id"], self.token_hash(token), email, SESSION_TTL_S,
                                        allow_unregistered=allow_registration,
                                        expected_tenant=member["tenant_id"] if member else None):
            return None
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
EMAIL_COPY = {
    "en": {
        "eyebrow": "SECURE SIGN-IN", "title": "Your sign-in code", "duration": "Use it within 10 minutes.",
        "one_use": "This code works once. Keep it private.",
        "ignore": "Didn't request this? You can safely ignore this email. Nobody can sign in without the code.",
        "safety": "Saku will never ask for your bank password, PIN or OTP.",
    },
    "id": {
        "eyebrow": "MASUK DENGAN AMAN", "title": "Kode masuk kamu", "duration": "Gunakan dalam 10 menit.",
        "one_use": "Kode ini hanya bisa dipakai sekali. Jangan bagikan ke siapa pun.",
        "ignore": "Tidak merasa meminta kode? Abaikan email ini. Tidak ada yang bisa masuk tanpa kodenya.",
        "safety": "Saku tidak pernah meminta password bank, PIN, atau OTP kamu.",
    },
}


def email_message(lang: str, code: str) -> tuple[str, str]:
    """Return a branded, self-contained email. Inline styles keep it reliable in Outlook and Gmail."""
    lang = lang if lang in EMAIL_COPY else "en"
    c = EMAIL_COPY[lang]
    subject = SUBJECT[lang].format(code=code)
    html = f'''<!doctype html><html lang="{lang}"><body style="margin:0;background:#F7F3EA;color:#1F2D28;font-family:Arial,sans-serif">
<div style="display:none;max-height:0;overflow:hidden">{c['title']}: {code}. {c['duration']}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#F7F3EA"><tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px;background:#FFFFFF;border-radius:24px;overflow:hidden">
<tr><td style="background:#0F6B4F;padding:24px 28px;color:#FFFFFF">
  <table role="presentation" cellspacing="0" cellpadding="0"><tr>
    <td style="font-size:25px;font-weight:800;letter-spacing:-1px">saku</td>
    <td style="padding-left:12px"><table role="presentation" cellspacing="3" cellpadding="0"><tr><td width="10" height="10" style="background:#A7D7B5"></td><td width="10" height="10" style="background:#E67D63"></td></tr><tr><td width="10" height="10" style="background:#F0BE63"></td><td width="10" height="10" style="background:#FFFFFF"></td></tr></table></td>
  </tr></table>
</td></tr>
<tr><td style="padding:36px 32px 12px">
  <p style="margin:0 0 10px;color:#0F6B4F;font-size:12px;font-weight:700;letter-spacing:1.2px">{c['eyebrow']}</p>
  <h1 style="margin:0 0 12px;font-size:28px;line-height:1.2">{c['title']}</h1>
  <p style="margin:0;color:#65716C;font-size:16px;line-height:1.6">{c['duration']}</p>
</td></tr>
<tr><td style="padding:20px 32px">
  <div style="background:#F7F3EA;border-left:6px solid #F0BE63;border-radius:16px;padding:24px;text-align:center">
    <div style="font-size:38px;font-weight:800;letter-spacing:9px;color:#0F6B4F">{code}</div>
  </div>
</td></tr>
<tr><td style="padding:0 32px 34px;color:#65716C;font-size:14px;line-height:1.65">
  <p style="margin:0 0 14px">{c['one_use']}</p><p style="margin:0 0 22px">{c['ignore']}</p>
  <div style="background:#E8F4EC;border-radius:14px;padding:16px;color:#1F2D28"><strong style="color:#0F6B4F">Saku</strong><br>{c['safety']}</div>
</td></tr></table>
<p style="margin:18px 0 0;color:#7D8782;font-size:12px">Saku · Personal finance, one honest pocket.</p>
</td></tr></table></body></html>'''
    return subject, html


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
        subject, html = email_message(lang, code)
        msg = {"message": {"subject": subject,
                           "body": {"contentType": "HTML", "content": html},
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
