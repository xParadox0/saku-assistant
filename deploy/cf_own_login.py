#!/usr/bin/env python3
"""Switch the Saku hostname to its own sign-in (HLD 3.11): Cloudflare Access stays only on /app/admin.

--apply   back up the '<name>' Access app (on <host>/app) to a JSON file, delete it, and set SAKU_CF_AUD in the
          server .env to the admin app's tag only. '<name>-admin' on <host>/app/admin is left untouched.
--rollback  recreate '<name>' on <host>/app from the backup (owner email allow policy) and restore both tags.
Default is a dry run.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from cf_beta_paths import call, load_env, must, set_env

BACKUP = Path.home() / ".config/saku-assistant/cf_app_backup.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="saku.chronyxlabs.com")
    ap.add_argument("--name", default="saku")
    ap.add_argument("--server-env", required=True)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true")
    g.add_argument("--rollback", action="store_true")
    a = ap.parse_args()
    env = load_env(Path.home() / ".config/cloudflare/api.env")
    tok, acct = env["CLOUDFLARE_API_TOKEN"], env["CLOUDFLARE_ACCOUNT_ID"]
    apps = must(call(tok, "GET", f"/accounts/{acct}/access/apps"), "list Access apps")
    main_app = next((x for x in apps if x.get("name") == a.name), None)
    admin_app = next((x for x in apps if x.get("name") == f"{a.name}-admin"), None)
    print("main app :", f"{main_app['domain']}" if main_app else "absent")
    print("admin app:", f"{admin_app['domain']}" if admin_app else "absent")
    if not admin_app or admin_app["domain"] != f"{a.host}/app/admin":
        raise SystemExit("admin app missing or not on /app/admin; refusing to touch anything")

    if a.rollback:
        if main_app:
            print("main app already present; nothing to do")
            return
        b = json.loads(BACKUP.read_text())
        created = must(call(tok, "POST", f"/accounts/{acct}/access/apps", {
            "name": a.name, "domain": f"{a.host}/app", "type": "self_hosted", "session_duration": b.get("session_duration", "24h"),
            "policies": [{"name": "saku members", "decision": "allow", "precedence": 1,
                          "include": [{"email": {"email": e}} for e in b["emails"]]}]}), "recreate main app")
        set_env(Path(a.server_env), {"SAKU_CF_AUD": f"{created['aud']},{admin_app['aud']}", "SAKU_AUTH_MODE": "cloudflare"})
        print("recreated", created["domain"], "and restored SAKU_CF_AUD + SAKU_AUTH_MODE=cloudflare")
        return

    if not main_app:
        print("main app already removed")
    else:
        full = must(call(tok, "GET", f"/accounts/{acct}/access/apps/{main_app['id']}"), "read main app")
        pols = must(call(tok, "GET", f"/accounts/{acct}/access/apps/{main_app['id']}/policies"), "read policies")
        emails = sorted({i["email"]["email"] for p in pols for i in p.get("include", []) if "email" in i})
        print("main app policy emails:", emails)
        if not a.apply:
            print("\ndry run: nothing changed. Add --apply.")
            return
        fd = os.open(BACKUP, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "domain": full["domain"],
                       "session_duration": full.get("session_duration"), "emails": emails}, f, indent=1)
        must(call(tok, "DELETE", f"/accounts/{acct}/access/apps/{main_app['id']}"), "delete main app")
        print("deleted main app (backup:", BACKUP, ")")
    if a.apply:
        set_env(Path(a.server_env), {"SAKU_CF_AUD": admin_app["aud"], "SAKU_AUTH_MODE": "own", "SAKU_MAIL": "graph"})
        print("SAKU_CF_AUD = admin tag only; SAKU_AUTH_MODE=own")


if __name__ == "__main__":
    main()
