"""Strings for the public pages and the registration flow (HLD 3.10). The dashboard under /app stays English.

English is the source. Indonesian lives in i18n_id.py and must have exactly the same keys (a test checks it).
Templates call t('key'); placeholders use str.format with named fields.
"""
from __future__ import annotations

POLICY_VERSION = "2026-10-18-draft"
LANGS = ("en", "id")
DEFAULT_LANG = "en"

EN: dict[str, str] = {
    # navigation and chrome
    "nav_demo": "Demo",
    "nav_privacy": "Privacy",
    "nav_terms": "Terms",
    "nav_signin": "Sign in",
    "lang_other": "Bahasa Indonesia",
    "footer_by": "Saku is a personal R&D project by Yona Surya.",
    "footer_repo": "Source code",
    "footer_not_advice": "Saku does not give financial advice.",

    # landing
    "hero_title": "All your accounts, one honest monthly picture.",
    "hero_lead": "Saku brings your bank and e-wallet statements together. It shows what came in, what went out and where it went, and it points out money that leaks. It never asks for your bank login.",
    "cta_register": "Request beta access",
    "cta_demo": "See the demo",
    "beta_note": "Closed beta. Every request is reviewed by hand.",
    "feat1_title": "Every account in one place",
    "feat1_body": "Upload a statement from each bank and e-wallet. Transfers between your own accounts are matched, so nothing is counted twice.",
    "feat2_title": "Numbers you can check",
    "feat2_body": "Every figure is computed from your own data. An AI model never guesses an amount.",
    "feat3_title": "On Telegram too",
    "feat3_body": "Connect your Telegram account and check your month from your phone.",
    "never_title": "What Saku will never do",
    "never_1": "Ask for your internet banking password, PIN or OTP.",
    "never_2": "Move money, pay or top up on your behalf.",
    "never_3": "Give investment, loan or insurance advice.",
    "never_4": "Sell or share your data.",
    "how_title": "How the beta works",
    "how_1": "Sign in with your email. You get a one-time code, so there is no password to remember.",
    "how_2": "Tell us your name and accept the privacy policy and the terms.",
    "how_3": "The owner reviews your request. You can use Saku once it is approved.",

    # demo
    "demo_title": "Demo",
    "demo_banner": "This demo uses made-up data. No real person or account is shown. Nothing here can be changed.",
    "demo_missing": "The demo data is not loaded on this server.",
    "demo_greeting": "Welcome to the demo",

    # policy pages
    "privacy_title": "Privacy policy",
    "terms_title": "Terms of use",
    "policy_version": "Version {version}",
    "policy_draft": "Draft under legal review. This text is not final. The beta does not accept other users until the reviewed version is published.",

    # registration
    "signup_title": "Request beta access",
    "signup_lead": "You are signed in as {email}. Sending this form creates a request, not an account. Nothing about your finances is stored unless the request is approved and you upload something yourself.",
    "signup_name": "Name to show",
    "signup_note": "Why would you like to try Saku? (optional)",
    "signup_consent_pre": "I have read and accept the",
    "signup_consent_and": "and the",
    "signup_privacy_link": "privacy policy",
    "signup_terms_link": "terms of use",
    "signup_submit": "Send request",
    "signup_invalid": "Please enter a name of up to 60 characters and accept the privacy policy and the terms.",
    "signup_closed_title": "Registration is not open yet",
    "signup_closed_body": "The closed beta opens after the privacy policy has been reviewed. Nothing has been stored about you.",
    "signup_full_title": "The waiting list is full",
    "signup_full_body": "Please try again later. Nothing has been stored about you.",
    "waiting_title": "Your request is waiting for review",
    "waiting_body": "You asked on {date}. You can use Saku once the owner approves the request. Until then nothing else is stored.",
    "rejected_title": "Your request was not approved",
    "rejected_body": "Thank you for your interest. To have your request deleted, use the contact in the privacy policy.",
    "signout": "Sign out",
}


def strings(lang: str) -> dict[str, str]:
    from .i18n_id import ID
    return ID if lang == "id" else EN


def pick_lang(cookie: str | None) -> str:
    return cookie if cookie in LANGS else DEFAULT_LANG
