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
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable

import psycopg
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import parsers
from ..db import connect, init_schema
from ..format import rupiah, rupiah_short, signed_rupiah
from ..repo import ACCOUNT_KINDS, Repo
from .auth import AuthError, CloudflareAccessAuth, DevAuth, Identity

HERE = Path(__file__).parent
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
PROVIDER_RE = re.compile(r"^[a-z0-9_]{2,30}$")
PROVIDER_SUGGESTIONS = ["bca", "bri", "bni", "mandiri", "btn", "cimb", "permata", "danamon", "ocbc", "uob",
                        "jago", "seabank", "blu", "jenius", "gopay", "ovo", "dana", "shopeepay", "linkaja", "cash", "other"]
CSV_TEMPLATE = "date,amount,description,kind\n2026-07-01,15000000,SALARY,normal\n2026-07-02,-45000,LUNCH,normal\n2026-07-28,-15000,ADMIN FEE,fee\n"


class Forbidden(Exception):
    pass


class NotFound(Exception):
    pass


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


def create_app(conn_factory: Callable[[], psycopg.Connection], authenticator, secret: str,
               upload_root: str | None = None) -> FastAPI:
    app = FastAPI(title="Saku", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["rupiah"] = rupiah
    templates.env.filters["signed_rupiah"] = signed_rupiah
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
            raise Forbidden("This email is not registered for Saku.")
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

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        with repo_ctx() as repo:
            ident, member = who(request, repo)
            t = member["tenant_id"]
            month = repo.latest_month(t)
            ctx = {"page": "overview", "month": month, "accounts": repo.account_stats(t)}
            if month:
                series = repo.monthly_series(t, month, 6)
                cf = series[-1]
                saved_pct = int((cf["net"] / cf["income"] * 100).to_integral_value()) if cf["income"] > 0 else None
                ctx.update(cf=cf, prev=series[-2] if len(series) > 1 else None, series=series, saved_pct=saved_pct,
                           chart=_chart(series), recent=repo.recent_txns(t, 10))
            return render(request, "overview.html", ident, member, **ctx)

    @app.get("/accounts", response_class=HTMLResponse)
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

    @app.post("/accounts")
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
        return RedirectResponse("/accounts?notice=added", status_code=303)

    @app.post("/accounts/{account_id}/rename")
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
        return RedirectResponse("/accounts?notice=renamed", status_code=303)

    @app.post("/accounts/{account_id}/delete")
    def delete(request: Request, account_id: int, csrf: str = Form("")):
        with repo_ctx() as repo:
            ident, member = who(request, repo)
            check_csrf(ident, csrf)
            outcome = repo.delete_account(member["tenant_id"], account_id)
            if outcome == "not_found":
                raise NotFound("Account not found.")
        return RedirectResponse(f"/accounts?notice={outcome}", status_code=303)

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
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
        return RedirectResponse(f"/accounts?batch={batch_id}", status_code=303)

    @app.post("/accounts/{account_id}/import")
    async def upload(request: Request, account_id: int, csrf: str = Form(""), file: UploadFile = File(...)):
        return await _import(request, account_id, csrf, file)

    @app.post("/imports")
    async def upload_picked(request: Request, csrf: str = Form(""), account_id: int = Form(...), file: UploadFile = File(...)):
        return await _import(request, account_id, csrf, file)

    @app.get("/accounts/template.csv")
    def template_csv(request: Request):
        with repo_ctx() as repo:
            who(request, repo)
        return Response(CSV_TEMPLATE, media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=saku-template.csv"})

    @app.get("/imports", response_class=HTMLResponse)
    def imports(request: Request):
        with repo_ctx() as repo:
            ident, member = who(request, repo)
            return render(request, "imports.html", ident, member, page="imports",
                          batches=repo.import_batches(member["tenant_id"], 50))

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request):
        with repo_ctx() as repo:
            ident, member = who(request, repo)
            if member["role"] != "admin":
                raise Forbidden("Admin only.")
            return render(request, "admin.html", ident, member, page="admin", tenants=repo.admin_tenants(),
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
    SAKU_CF_AUD      Access application AUD tag                  (cloudflare mode)
    SAKU_SECRET      random string for CSRF tokens               (required in cloudflare mode)
    SAKU_DEV_EMAIL   identity used in dev mode
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
    return create_app(lambda: connect(url), auth, secret)
