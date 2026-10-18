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
    "hero_title_a": "All your accounts.",
    "hero_title_b": "One honest",
    "hero_title_em": "pocket.",
    "hero_lead": "Saku puts your bank and e-wallet statements in one place. See what came in, what went out, and where money quietly leaks. It never asks for your bank login.",
    "cta_register": "Request beta access",
    "cta_demo": "See the demo",
    "beta_note": "Closed beta. Every request is reviewed by hand.",
    "trust_1": "No bank login",
    "trust_2": "Never moves money",
    "trust_3": "Numbers you can check",
    "stage_c1_t": "Money in",
    "stage_c1_s": "Across all accounts",
    "stage_c2_t": "Food and drink",
    "stage_c2_s": "The biggest category",
    "stage_c3_t": "Repeating bills",
    "stage_c3_s": "{n} found, every month",
    "stage_sum_k": "Example month, made-up data",
    "stage_sum_p": "You kept {pct}% of what came in.",
    "steps_title": "Three steps, no bank password",
    "steps_sub": "Saku reads the files you give it and nothing else.",
    "step1_t": "Add your accounts",
    "step1_b": "Give each bank and e-wallet a name. Saku keeps at most the last 4 digits of an account number.",
    "step2_t": "Upload statements",
    "step2_b": "Transfers between your own accounts are matched, so nothing is counted twice.",
    "step3_t": "See the month",
    "step3_b": "What came in, what went out, what repeats, and what changed since last month.",
    "tg_note": "Telegram: connect your account once from the dashboard. Recording spending by chat comes next.",
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
    "login_title": "Sign in to Saku",
    "login_lead": "Enter your email. We send you a 6-digit code. There is no password to remember.",
    "login_email": "Email",
    "login_send": "Send code",
    "login_no_password": "Saku never asks for a password, and never for your bank login, PIN or OTP.",
    "login_code_title": "Check your email",
    "login_code_lead": "If {email} can sign in to Saku, a 6-digit code is on its way. It works once, for 10 minutes.",
    "login_code": "6-digit code",
    "login_verify": "Sign in",
    "login_other_email": "Use a different email or send a new code",
    "login_err_email": "That does not look like an email address.",
    "login_err_code": "That code is wrong or has expired. Check the latest email, or ask for a new code.",
    "login_err_limited": "Too many tries. Wait an hour and try again.",
    "login_err_mail": "We could not send the email just now. Try again in a few minutes.",
    "reg_closed_note": "Registration opens after the privacy policy has been reviewed. Until then only invited members can sign in.",
    "login_members_only": "Saku is invite-only for now. Only approved members receive a code. Registration opens after the privacy review.",
    "login_code_members_hint": "No email after a few minutes? Check the spelling. Right now codes go only to approved members.",
    "reg_step1": "Step 1 of 2 · Confirm your email",
    "reg_step1_lead": "First we check that the email is yours. We send a 6-digit code, then you fill in a short form.",
    "reg_step2": "Step 2 of 2 · Your request",
    "signout": "Sign out",
}


def strings(lang: str) -> dict[str, str]:
    from .i18n_id import ID
    return ID if lang == "id" else EN


def pick_lang(cookie: str | None) -> str:
    return cookie if cookie in LANGS else DEFAULT_LANG
