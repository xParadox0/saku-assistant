#!/usr/bin/env python3
"""Closed-beta Access layout for the Saku hostname (HLD 3.10).

Before:  one Access app on  <host>             (whole site behind login)
After:   app '<name>'       on  <host>/app       (login; policy = owner only while registration is closed)
         app '<name>-admin' on  <host>/app/admin (owner only, more specific path wins)
         <host>/, /demo, /privacy, /terms are public.

Order is safe: the stricter admin app is created first, then the main app is narrowed to /app.
Default is a dry run. --apply makes the change and writes SAKU_CF_AUD (both tags) into the server .env.
--rollback puts the main app back on the whole host and deletes the admin app.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.cloudflare.com/client/v4"


def load_env(path: Path) -> dict:
    return dict(l.split("=", 1) for l in path.read_text().splitlines() if "=" in l and not l.startswith("#"))


def call(token, method, path, body=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def must(res, what):
    if not res.get("success"):
        raise SystemExit(f"{what} failed: {[(e.get('code'), e.get('message')) for e in res.get('errors', [])]}")
    return res["result"]


def set_env(path: Path, updates: dict) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    for i, l in enumerate(lines):
        k = l.split("=", 1)[0]
        if k in updates:
            lines[i] = f"{k}={updates[k]}"; seen.add(k)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def app_body(app: dict, domain: str) -> dict:
    keep = ("name", "type", "session_duration", "app_launcher_visible", "auto_redirect_to_identity", "allowed_idps")
    body = {k: app[k] for k in keep if k in app}
    body["domain"] = domain
    body["policies"] = [{"id": p["id"], "precedence": p.get("precedence", i + 1)} for i, p in enumerate(app.get("policies") or [])]
    return body


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="saku.chronyxlabs.com")
    ap.add_argument("--name", default="saku")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--server-env", required=True)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true")
    g.add_argument("--rollback", action="store_true")
    a = ap.parse_args()
    env = load_env(Path.home() / ".config/cloudflare/api.env")
    tok, acct = env["CLOUDFLARE_API_TOKEN"], env["CLOUDFLARE_ACCOUNT_ID"]

    apps = must(call(tok, "GET", f"/accounts/{acct}/access/apps"), "list Access apps")
    main_app = next((x for x in apps if x.get("name") == a.name and x.get("domain", "").startswith(a.host)), None)
    admin_app = next((x for x in apps if x.get("name") == f"{a.name}-admin"), None)
    if not main_app:
        raise SystemExit(f"no Access app named {a.name!r} on {a.host}")
    main_app = must(call(tok, "GET", f"/accounts/{acct}/access/apps/{main_app['id']}"), "read main app")
    pols = must(call(tok, "GET", f"/accounts/{acct}/access/apps/{main_app['id']}/policies"), "read main app policies")
    main_app["policies"] = pols
    print(f"main app   {main_app['name']:<12} domain={main_app['domain']} policies={[p['name'] for p in pols]}")
    print(f"admin app  {'exists on ' + admin_app['domain'] if admin_app else 'missing'}")

    if a.rollback:
        if main_app["domain"] != a.host:
            must(call(tok, "PUT", f"/accounts/{acct}/access/apps/{main_app['id']}", app_body(main_app, a.host)), "restore main app")
            print("main app restored to", a.host)
        if admin_app:
            must(call(tok, "DELETE", f"/accounts/{acct}/access/apps/{admin_app['id']}"), "delete admin app")
            print("admin app deleted")
        set_env(Path(a.server_env), {"SAKU_CF_AUD": main_app["aud"]})
        print("SAKU_CF_AUD reset to the main app tag")
        return
    if not a.apply:
        print("\ndry run: nothing changed. Add --apply.")
        return

    if not admin_app:
        admin_app = must(call(tok, "POST", f"/accounts/{acct}/access/apps", {
            "name": f"{a.name}-admin", "domain": f"{a.host}/app/admin", "type": "self_hosted", "session_duration": "24h",
            "app_launcher_visible": False, "auto_redirect_to_identity": False,
            "policies": [{"name": f"{a.name} admin", "decision": "allow", "precedence": 1,
                          "include": [{"email": {"email": a.owner.lower()}}]}],
        }), "create admin app")
        print("created admin app on", admin_app["domain"])
    if main_app["domain"] != f"{a.host}/app":
        res = must(call(tok, "PUT", f"/accounts/{acct}/access/apps/{main_app['id']}", app_body(main_app, f"{a.host}/app")),
                   "narrow main app to /app")
        print("main app now on", res["domain"])
    after = must(call(tok, "GET", f"/accounts/{acct}/access/apps/{main_app['id']}/policies"), "re-read main policies")
    if [p["id"] for p in after] != [p["id"] for p in pols]:
        raise SystemExit("policies on the main app changed unexpectedly; run --rollback")
    set_env(Path(a.server_env), {"SAKU_CF_AUD": f"{main_app['aud']},{admin_app['aud']}"})
    print("main app policies unchanged:", [p["name"] for p in after])
    print("wrote SAKU_CF_AUD (2 tags) to", a.server_env)


if __name__ == "__main__":
    main()
