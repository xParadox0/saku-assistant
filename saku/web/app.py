"""Saku dashboard: the user's money picture, account management, statement upload, and an ops page.

Security model (see docs/HLD.md 3.9):
- identity from a verified Cloudflare Access JWT only;
- every tenant query goes through Repo with the member's tenant_id;
- admin pages show operational metadata only, never another tenant's transactions;
- POSTs carry an HMAC CSRF token; uploads are size-limited, parsed in a private temp dir, then deleted.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import shutil
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

import psycopg
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from dataclasses import dataclass, field
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import parsers
from ..db import connect, init_schema
from .. import insights
from ..categorise import CATEGORIES, label as cat_label
from ..format import rupiah, rupiah_short, signed_rupiah
from ..repo import ACCOUNT_KINDS, Repo
from . import i18n
from .auth import AuthError, AuthUnavailable, CloudflareAccessAuth, DevAuth, Identity

HERE = Path(__file__).parent
DEMO_TENANT = "demo"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
PROVIDER_RE = re.compile(r"^[a-z0-9_]{2,30}$")
PROVIDER_SUGGESTIONS = ["bca", "bri", "bni", "mandiri", "btn", "cimb", "permata", "danamon", "ocbc", "uob",
                        "jago", "seabank", "blu", "jenius", "gopay", "ovo", "dana", "shopeepay", "linkaja", "cash", "other"]
CSV_TEMPLATE = "date,amount,description,kind\n2026-07-01,15000000,SALARY,normal\n2026-07-02,-45000,LUNCH,normal\n2026-07-28,-15000,ADMIN FEE,fee\n"


class Forbidden(Exception):
    pass


class NotFound(Exception):
    pass


class NotMember(Exception):
    """A verified email without a member row: it may only see the registration flow."""


@dataclass
class Beta:
    registration_open: bool = False          # SAKU_REGISTRATION=open
    pending_cap: int = 50                    # SAKU_PENDING_CAP
    approved_cap: int = 20                   # SAKU_APPROVED_CAP
    policy_reviewed: str = ""                # SAKU_POLICY_REVIEWED; approve works only when it equals POLICY_VERSION
    contact: str = ""                        # SAKU_CONTACT, shown in the privacy policy

    @property
    def approvals_allowed(self) -> bool:
        return self.policy_reviewed == i18n.POLICY_VERSION


def _chart(series: list[dict]) -> dict:
    w, h, pad, base = 720, 250, 24, 214
    top = max([max(s["income"], s["spending"]) for s in series] + [Decimal(1)])
    slot = (w - 2 * pad) / max(len(series), 1)
    bw = slot * 0.3
    bars = []
    for i, s in enumerate(series):
        x = pad + i * slot + slot * 0.17
        hi = float(s["income"] / top) * (base - 34)
        ho = float(s["spending"] / top) * (base - 34)
        bars.append({"x_in": round(x, 1), "x_out": round(x + bw + 3, 1), "w": round(bw, 1),
                     "y_in": round(base - hi, 1), "h_in": round(hi, 1), "y_out": round(base - ho, 1), "h_out": round(ho, 1),
                     "lx": round(x + bw + 1.5, 1), "label": s["start"].strftime("%b %Y"),
                     "v_in": rupiah_short(s["income"]), "v_out": rupiah_short(s["spending"]),
                     "title": f"{s['start']:%B %Y}: in {rupiah(s['income'])}, out {rupiah(s['spending'])}"})
    return {"w": w, "h": h, "base": base, "bars": bars}


def _donut(cats: list[dict], total: Decimal) -> list[dict]:
    """Ring segments for an SVG donut: circumference 100 on r=15.9155, so dash lengths are percentages."""
    segs, offset = [], 25.0  # start at 12 o'clock
    for c in cats:
        share = float(c["amount"] / total * 100) if total > 0 else 0.0
        name, icon, colour = cat_label(c["category"])
        segs.append({"key": c["category"], "name": name, "icon": icon, "colour": colour, "amount": c["amount"],
                     "count": c["count"], "pct": round(share), "dash": round(max(share - 0.6, 0.0), 2),
                     "offset": round(offset, 2), "bar": max(round(share), 2)})
        offset -= share
    return segs


def create_app(conn_factory: Callable[[], psycopg.Connection], authenticator, secret: str,
               upload_root: str | None = None, bot_username: str | None = None,
               beta: Beta | None = None) -> FastAPI:
    beta = beta or Beta()
    bot_username = (bot_username or "").lstrip("@") or None
    if bot_username and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot_username):
        raise SystemExit("SAKU_BOT_USERNAME is not a valid Telegram username")
    app = FastAPI(title="Saku", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["rupiah"] = rupiah
    templates.env.filters["signed_rupiah"] = signed_rupiah
    templates.env.filters["rupiah_short"] = rupiah_short
    templates.env.globals["cat"] = cat_label
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    key = secret.encode()
    upload_root = upload_root or tempfile.gettempdir()

    @contextmanager
    def repo_ctx():
        conn = conn_factory()
        try:
            yield Repo(conn)
        finally:
            conn.close()

    def csrf_token(email: str) -> str:
        return hmac.new(key, f"csrf|{email}".encode(), hashlib.sha256).hexdigest()

    def who(request: Request, repo: Repo) -> tuple[Identity, dict]:
        ident = authenticator.identify(request.headers)
        member = repo.member(ident.email)
        if member is None:
            raise NotMember(ident.email)
        return ident, member

    def check_csrf(ident: Identity, token: str) -> None:
        if not hmac.compare_digest(token or "", csrf_token(ident.email)):
            raise Forbidden("The form expired. Reload the page and try again.")

    def render(request: Request, name: str, ident: Identity, member: dict, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"email": ident.email, "role": member["role"],
                                                          "tenant": member["tenant_id"], "csrf": csrf_token(ident.email), **ctx})

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp: Response = await call_next(request)
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
                                                   "script-src 'none'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        if not request.url.path.startswith("/static"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(AuthError)
    async def _auth(request: Request, exc: AuthError):
        return templates.TemplateResponse(request, "error.html", {"title": "Sign-in required", "message": str(exc)}, status_code=401)

    @app.exception_handler(AuthUnavailable)
    async def _auth_down(request: Request, exc: AuthUnavailable):
        return templates.TemplateResponse(request, "error.html", {"title": "Sign-in check unavailable",
            "message": "Saku could not reach Cloudflare to verify your sign-in. Try again in a minute."}, status_code=503)

    @app.exception_handler(NotMember)
    async def _not_member(request: Request, exc: NotMember):
        if request.method == "GET":
            return RedirectResponse("/app/register", status_code=303)
        return templates.TemplateResponse(request, "error.html", {"title": "Not allowed",
            "message": "This email has no Saku account yet."}, status_code=403)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        return templates.TemplateResponse(request, "error.html", {"title": "Not allowed", "message": str(exc)}, status_code=403)

    @app.exception_handler(NotFound)
    async def _notfound(request: Request, exc: NotFound):
        return templates.TemplateResponse(request, "error.html", {"title": "Not found", "message": str(exc)}, status_code=404)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        with repo_ctx() as repo:
            repo.ping()
        return "ok"

    def overview_ctx(repo: Repo, tenant: str, first_name: str | None) -> dict:
        month = repo.latest_month(tenant)
        ctx = {"page": "overview", "month": month, "accounts": repo.account_stats(tenant)}
        if month:
            repo.apply_categories(tenant)
            series = repo.monthly_series(tenant, month, 6)
            cf = series[-1]
            prev = series[-2] if len(series) > 1 else None
            saved_pct = int((cf["net"] / cf["income"] * 100).to_integral_value()) if cf["income"] > 0 else None
            cats = repo.spending_by_category(tenant, cf["start"], cf["end"])
            prev_cats = repo.spending_by_category(tenant, prev["start"], prev["end"]) if prev else []
            daily = repo.daily_spending(tenant, cf["start"], cf["end"])
            recurring = repo.recurring_charges(tenant, cf["start"])
            covered, days = insights.month_progress(cf["start"], max((d["day"] for d in daily), default=None))
            now = datetime.now(timezone(timedelta(hours=7)))
            ctx.update(cf=cf, prev=prev, series=series, saved_pct=saved_pct, chart=_chart(series),
                       recent=repo.recent_txns(tenant, 8), donut=_donut(cats, cf["spending"] + cf["refunds"]),
                       notes=insights.build(cf, prev, cats, prev_cats, recurring, daily),
                       recurring=recurring, covered=covered, days=days,
                       kept_bar=max(min(saved_pct or 0, 100), 0),
                       hello=insights.greeting(now.hour), first_name=first_name)
        return ctx

    @app.get("/app", response_class=HTMLResponse)
    def overview(request: Request):
        with repo_ctx() as repo:
            ident, member = who(request, repo)
            ctx = overview_ctx(repo, member["tenant_id"], ident.email.split("@")[0].split(".")[0].title())
            return render(request, "overview.html", ident, member, **ctx)

    @app.get("/app/accounts", response_class=HTMLResponse)
    def accounts(request: Request, batch: int | None = None, notice: str | None = None):
        with repo_ctx() as repo:
            ident, member = who(request, repo)
            t = member["tenant_id"]
            result = repo.import_batch(t, batch) if batch else None
            notices = {"deleted": "Account deleted.", "renamed": "Account renamed.", "added": "Account added.",
                       "has_transactions": "This account has transactions, so it was not deleted."}
            return render(request, "accounts.html", ident, member, page="accounts", accounts=repo.account_stats(t),
                          result=result, notice=notices.get(notice or ""), kinds=sorted(ACCOUNT_KINDS),
                          providers=PROVIDER_SUGGESTIONS, max_mb=MAX_UPLOAD_BYTES // (1024 * 1024))

    @app.post("/app/accounts")
    def add_account(request: Request, csrf: str = Form(""), kind: str = Form(...), provider: str = Form(...),
                    label: str = Form(...), last4: str = Form("")):
        with repo_ctx() as repo:
            ident, member = who(request, repo)
            check_csrf(ident, csrf)
            provider = provider.strip().lower()
            if kind not in ACCOUNT_KINDS or not PROVIDER_RE.match(provider) or not 1 <= len(label.strip()) <= 60 \
                    or (last4 and not re.fullmatch(r"\d{4}", last4)):
                raise Forbidden("Check the account fields: provider is a short slug, label 1-60 characters, last 4 digits only.")
            repo.add_account(member["tenant_id"], kind, provider, label.strip(), last4 or None)
        return RedirectResponse("/app/accounts?notice=added", status_code=303)

    @app.post("/app/accounts/{account_id}/rename")
    def rename(request: Request, account_id: int, csrf: str = Form(""), label: str = Form(...)):
        with repo_ctx() as repo:
            ident, member = who(request, repo)
            check_csrf(ident, csrf)
            try:
                ok = repo.rename_account(member["tenant_id"], account_id, label)
            except ValueError as e:
                raise Forbidden(str(e)) from None
            if not ok:
                raise NotFound("Account not found.")
        return RedirectResponse("/app/accounts?notice=renamed", status_code=303)

    @app.post("/app/accounts/{account_id}/delete")
    def delete(request: Request, account_id: int, csrf: str = Form("")):
        with repo_ctx() as repo:
            ident, member = who(request, repo)
            check_csrf(ident, csrf)
            outcome = repo.delete_account(member["tenant_id"], account_id)
            if outcome == "not_found":
                raise NotFound("Account not found.")
        return RedirectResponse(f"/app/accounts?notice={outcome}", status_code=303)

    async def _import(request: Request, account_id: int, csrf: str, file: UploadFile):
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        with repo_ctx() as repo:
            ident, member = who(request, repo)
            check_csrf(ident, csrf)
            t = member["tenant_id"]
            acct = next((a for a in repo.accounts(t) if a["id"] == account_id), None)
            if acct is None:
                raise NotFound("Account not found.")
            if len(data) > MAX_UPLOAD_BYTES:
                return templates.TemplateResponse(request, "error.html", {"title": "File too large",
                    "message": f"The limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."}, status_code=413)
            sha = hashlib.sha256(data).hexdigest()
            suffix = Path(file.filename or "").suffix.lower()[:6] or ".bin"
            workdir = tempfile.mkdtemp(prefix="saku-upload-", dir=upload_root)
            try:
                path = Path(workdir) / f"statement{suffix}"
                path.write_bytes(data)
                parser = parsers.pick(path, acct["provider"])
                if parser is None:
                    batch_id = repo.record_failed_import(t, account_id, suffix.lstrip(".") or "file", None, sha,
                        "format not supported yet; download the Saku CSV template on this page")
                else:
                    try:
                        rows = parser.parse(path)
                    except parsers.ParseError as e:
                        batch_id = repo.record_failed_import(t, account_id, "csv", parser.name, sha, str(e))
                    else:
                        batch_id = repo.import_rows(t, account_id, rows, "csv", parser.name, sha).batch_id
                        repo.detect_own_transfers(t)
                        repo.apply_categories(t)
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
        return RedirectResponse(f"/app/accounts?batch={batch_id}", status_code=303)

    @app.post("/app/accounts/{account_id}/import")
    async def upload(request: Request, account_id: int, csrf: str = Form(""), file: UploadFile = File(...)):
        return await _import(request, account_id, csrf, file)

    @app.post("/app/imports")
    async def upload_picked(request: Request, csrf: str = Form(""), account_id: int = Form(...), file: UploadFile = File(...)):
        return await _import(request, account_id, csrf, file)

    @app.get("/app/accounts/template.csv")
    def template_csv(request: Request):
        with repo_ctx() as repo:
            who(request, repo)
        return Response(CSV_TEMPLATE, media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=saku-template.csv"})

    @app.get("/app/imports", response_class=HTMLResponse)
    def imports(request: Request):
        with repo_ctx() as repo:
            ident, member = who(request, repo)
            return render(request, "imports.html", ident, member, page="imports",
                          batches=repo.import_batches(member["tenant_id"], 50))

    @app.get("/app/settings", response_class=HTMLResponse)
    def settings(request: Request, notice: str | None = None):
        with repo_ctx() as repo:
            ident, member = who(request, repo)
            notices = {"unlinked": "Telegram disconnected. The bot no longer reads messages from that account."}
            return render(request, "settings.html", ident, member, page="settings",
                          telegram=repo.telegram_status(ident.email), notice=notices.get(notice or ""),
                          bot_username=bot_username, link=None)

    @app.post("/app/settings/telegram/code", response_class=HTMLResponse)
    def telegram_code(request: Request, csrf: str = Form("")):
        with repo_ctx() as repo:
            ident, member = who(request, repo)
            check_csrf(ident, csrf)
            if not bot_username:
                raise Forbidden("The Telegram bot is not configured on this server yet.")
            code = repo.create_link_code(ident.email)
            link = {"url": f"https://t.me/{bot_username}?start={code}", "code": code,
                    "minutes": Repo.LINK_TTL_MINUTES}
            return render(request, "settings.html", ident, member, page="settings",
                          telegram=repo.telegram_status(ident.email), notice=None,
                          bot_username=bot_username, link=link)

    @app.post("/app/settings/telegram/unlink")
    def telegram_unlink(request: Request, csrf: str = Form("")):
        with repo_ctx() as repo:
            ident, member = who(request, repo)
            check_csrf(ident, csrf)
            repo.unlink_telegram(email=ident.email)
        return RedirectResponse("/app/settings?notice=unlinked", status_code=303)


    # ---- public pages (no login; read only the demo tenant) -------------------------------------------------
    def lang_of(request: Request) -> str:
        return i18n.pick_lang(request.cookies.get("saku_lang"))

    def pub_render(request: Request, name: str, page: str, status_code: int = 200, **ctx) -> HTMLResponse:
        lang = lang_of(request)
        return templates.TemplateResponse(request, name, {"lang": lang, "t": i18n.strings(lang), "page": page,
                                                          "here": request.url.path, **ctx}, status_code=status_code)

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request):
        stage = None
        with repo_ctx() as repo:  # example figures come from the synthetic demo tenant only
            month = repo.latest_month(DEMO_TENANT)
            if month:
                cf = repo.monthly_series(DEMO_TENANT, month, 1)[-1]
                cats = repo.spending_by_category(DEMO_TENANT, cf["start"], cf["end"])
                top = cats[0] if cats else None
                pct = int((cf["net"] / cf["income"] * 100).to_integral_value()) if cf["income"] > 0 else 0
                stage = {"income": cf["income"], "net": cf["net"], "pct": max(min(pct, 100), 0),
                         "top_cat": top["category"] if top else None, "top_amount": top["amount"] if top else 0,
                         "repeats": len(repo.recurring_charges(DEMO_TENANT, cf["start"]))}
        return pub_render(request, "public/landing.html", "landing", stage=stage)

    @app.get("/demo", response_class=HTMLResponse)
    def demo_page(request: Request):
        with repo_ctx() as repo:
            ctx = overview_ctx(repo, DEMO_TENANT, None)  # the only tenant a public page ever reads
            ctx.pop("page", None)
            return pub_render(request, "public/demo.html", "demo", demo=True, **ctx)

    def policy(request: Request, kind: str):
        lang = lang_of(request)
        s = i18n.strings(lang)
        title = s["privacy_title"] if kind == "privacy" else s["terms_title"]
        return pub_render(request, "public/policy.html", kind, title=title, body=f"policy/{kind}_{lang}.html",
                          version=i18n.POLICY_VERSION, draft=not beta.approvals_allowed,
                          contact=beta.contact or "the address on the project page")

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request):
        return policy(request, "privacy")

    @app.get("/terms", response_class=HTMLResponse)
    def terms(request: Request):
        return policy(request, "terms")

    @app.post("/lang")
    def set_lang(lang: str = Form("en"), next: str = Form("/")):
        target = next if next.startswith("/") and not next.startswith("//") and "\\" not in next else "/"
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie("saku_lang", i18n.pick_lang(lang), max_age=365 * 24 * 3600, samesite="lax", secure=True, httponly=True)
        return resp

    # ---- registration (verified email, no member row) -------------------------------------------------------
    def signup_state(repo: Repo, email: str) -> tuple[str, dict | None]:
        s = repo.signup_get(email)
        if s:
            return s["status"] if s["status"] in ("pending", "rejected") else "pending", s
        if not beta.registration_open:
            return "closed", None
        if repo.signup_counts()["pending"] >= beta.pending_cap:
            return "full", None
        return "form", None

    @app.get("/app/register", response_class=HTMLResponse)
    def register_page(request: Request):
        with repo_ctx() as repo:
            ident = authenticator.identify(request.headers)
            if repo.member(ident.email) is not None:
                return RedirectResponse("/app", status_code=303)
            state, s = signup_state(repo, ident.email)
            return pub_render(request, "public/signup.html", "signup", state=state, email=ident.email,
                              csrf=csrf_token(ident.email), version=i18n.POLICY_VERSION,
                              asked_on=s["created_at"].strftime("%d %b %Y") if s else "")

    @app.post("/app/register", response_class=HTMLResponse)
    def register_submit(request: Request, csrf: str = Form(""), display_name: str = Form(""),
                        note: str = Form(""), consent: str = Form("")):
        with repo_ctx() as repo:
            ident = authenticator.identify(request.headers)
            check_csrf(ident, csrf)
            if repo.member(ident.email) is not None:
                return RedirectResponse("/app", status_code=303)
            state, _ = signup_state(repo, ident.email)
            if state != "form":
                return RedirectResponse("/app/register", status_code=303)
            if consent != "yes":
                return pub_render(request, "public/signup.html", "signup", status_code=400, state="form",
                                  error=True, email=ident.email, csrf=csrf_token(ident.email),
                                  version=i18n.POLICY_VERSION, form_name=display_name[:60], form_note=note[:300])
            outcome = repo.signup_create(ident.email, display_name, note, lang_of(request), i18n.POLICY_VERSION,
                                         beta.pending_cap)
            if outcome == "invalid":
                return pub_render(request, "public/signup.html", "signup", status_code=400, state="form",
                                  error=True, email=ident.email, csrf=csrf_token(ident.email),
                                  version=i18n.POLICY_VERSION, form_name=display_name[:60], form_note=note[:300])
        return RedirectResponse("/app/register", status_code=303)

    # ---- owner approval queue --------------------------------------------------------------------------------
    @app.post("/app/admin/signups")
    def decide(request: Request, csrf: str = Form(""), email: str = Form(""), action: str = Form("")):
        with repo_ctx() as repo:
            ident, member = who(request, repo)
            if member["role"] != "admin":
                raise Forbidden("Admin only.")
            check_csrf(ident, csrf)
            if action not in ("approve", "reject"):
                raise Forbidden("Unknown action.")
            if action == "approve" and not beta.approvals_allowed:
                return RedirectResponse("/app/admin?notice=policy", status_code=303)
            outcome = repo.signup_decide(email, action == "approve", ident.email, beta.approved_cap)
        return RedirectResponse(f"/app/admin?notice={outcome}", status_code=303)


    @app.get("/app/admin", response_class=HTMLResponse)
    def admin(request: Request):
        with repo_ctx() as repo:
            ident, member = who(request, repo)
            if member["role"] != "admin":
                raise Forbidden("Admin only.")
            notices = {"approved": "Approved. The account exists now.", "rejected": "Rejected.",
                       "not_pending": "That request is no longer pending.",
                       "full": f"The beta is full ({beta.approved_cap} approved users).",
                       "policy": "Approvals are blocked until the reviewed privacy policy and terms are configured."}
            return render(request, "admin.html", ident, member, page="admin", tenants=repo.admin_tenants(),
                          signups=repo.signups("pending"), signup_counts=repo.signup_counts(), beta=beta,
                          notice=notices.get(request.query_params.get("notice", "")),
                          heartbeats=repo.admin_heartbeats(), batches=repo.admin_recent_batches(25))

    return app


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def app_from_env(host: str = "127.0.0.1") -> FastAPI:
    """Build the app from environment variables.

    SAKU_AUTH_MODE   cloudflare (default) or dev
    SAKU_CF_TEAM     https://<team>.cloudflareaccess.com          (cloudflare mode)
    SAKU_CF_AUD      Access application AUD tag(s), comma separated (cloudflare mode)
    SAKU_REGISTRATION   open | closed (default closed)
    SAKU_PENDING_CAP / SAKU_APPROVED_CAP   beta caps (50 / 20)
    SAKU_POLICY_REVIEWED   the reviewed policy version; approvals stay blocked until it equals i18n.POLICY_VERSION
    SAKU_CONTACT     contact address shown in the privacy policy
    SAKU_SECRET      random string for CSRF tokens               (required in cloudflare mode)
    SAKU_DEV_EMAIL   identity used in dev mode
    SAKU_BOT_USERNAME  Telegram bot username for the Connect Telegram link (optional)
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    with connect(url) as c:
        init_schema(c)
    mode = os.environ.get("SAKU_AUTH_MODE", "cloudflare")
    if mode == "dev":
        if not _is_loopback(host) and os.environ.get("SAKU_DEV_CONTAINER") != "1":
            raise SystemExit("dev auth trusts everyone; it only runs on a loopback address")
        auth = DevAuth(os.environ.get("SAKU_DEV_EMAIL", "demo@saku.local"))
        secret = os.environ.get("SAKU_SECRET") or secrets.token_hex(32)
    elif mode == "cloudflare":
        auth = CloudflareAccessAuth(os.environ.get("SAKU_CF_TEAM", ""), os.environ.get("SAKU_CF_AUD", ""))
        secret = os.environ.get("SAKU_SECRET", "")
        if len(secret) < 32:
            raise SystemExit("SAKU_SECRET must be at least 32 characters in cloudflare mode")
    else:
        raise SystemExit(f"unknown SAKU_AUTH_MODE {mode!r}")
    beta = Beta(registration_open=os.environ.get("SAKU_REGISTRATION", "closed") == "open",
                pending_cap=int(os.environ.get("SAKU_PENDING_CAP", "50")),
                approved_cap=int(os.environ.get("SAKU_APPROVED_CAP", "20")),
                policy_reviewed=os.environ.get("SAKU_POLICY_REVIEWED", ""),
                contact=os.environ.get("SAKU_CONTACT", ""))
    return create_app(lambda: connect(url), auth, secret, bot_username=os.environ.get("SAKU_BOT_USERNAME"), beta=beta)
