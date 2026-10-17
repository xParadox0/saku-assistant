#!/usr/bin/env python3
"""Provision Cloudflare for the Saku dashboard: dedicated tunnel, Access app + policy, DNS record.

Default is a DRY RUN that only reads. --apply makes changes, in a safe order:
  1. Access application + allow policy for the given emails   (the hostname is protected before it exists)
  2. Tunnel (remotely managed) with one ingress rule -> http://web:8090, everything else 404
  3. Proxied CNAME <host> -> <tunnel-id>.cfargotunnel.com
Writes TUNNEL_TOKEN, SAKU_CF_TEAM and SAKU_CF_AUD into the server .env (mode 600). Prints no secrets.
Rollback: --rollback deletes the DNS record, the tunnel and the Access app it created (matched by name).

Credentials: ~/.config/cloudflare/api.env (CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_ZONE_ID).
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


class CF:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            return json.load(e)


def must(res: dict, what: str):
    if not res.get("success"):
        raise SystemExit(f"{what} failed: {[(e.get('code'), e.get('message')) for e in res.get('errors', [])]}")
    return res["result"]


def set_env(path: Path, updates: dict) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    keys = set()
    for i, l in enumerate(lines):
        k = l.split("=", 1)[0]
        if k in updates:
            lines[i] = f"{k}={updates[k]}"; keys.add(k)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in keys]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="saku.chronyxlabs.com")
    ap.add_argument("--email", action="append", required=True)
    ap.add_argument("--name", default="saku")
    ap.add_argument("--service", default="http://web:8090")
    ap.add_argument("--server-env", default="/srv/saku/.env")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    a = ap.parse_args()
    env = load_env(Path.home() / ".config/cloudflare/api.env")
    cf, acct, zone = CF(env["CLOUDFLARE_API_TOKEN"]), env["CLOUDFLARE_ACCOUNT_ID"], env["CLOUDFLARE_ZONE_ID"]

    org = must(cf.call("GET", f"/accounts/{acct}/access/organizations"), "read Access organization")
    team = f"https://{org['auth_domain']}"
    apps = [x for x in must(cf.call("GET", f"/accounts/{acct}/access/apps"), "list Access apps") if x.get("domain") == a.host]
    tunnels = must(cf.call("GET", f"/accounts/{acct}/cfd_tunnel?is_deleted=false&name={a.name}"), "list tunnels")
    dns = cf.call("GET", f"/zones/{zone}/dns_records?name={a.host}")
    dns_ok = dns.get("success", False)
    print(f"team domain      {team}")
    print(f"access app       {'exists' if apps else 'missing'} for {a.host}")
    print(f"tunnel '{a.name}'    {'exists ' + tunnels[0]['id'][:8] + '…' if tunnels else 'missing'}")
    print(f"dns permission   {'ok' if dns_ok else 'MISSING: ' + str([e.get('message') for e in dns.get('errors', [])])}")
    if dns_ok:
        print(f"dns record       {'exists' if dns['result'] else 'missing'} for {a.host}")

    if a.rollback:
        if dns_ok:
            for r in dns["result"]:
                must(cf.call("DELETE", f"/zones/{zone}/dns_records/{r['id']}"), "delete DNS"); print("deleted DNS", r["name"])
        for t in tunnels:
            cf.call("DELETE", f"/accounts/{acct}/cfd_tunnel/{t['id']}/connections")
            must(cf.call("DELETE", f"/accounts/{acct}/cfd_tunnel/{t['id']}"), "delete tunnel"); print("deleted tunnel", t["name"])
        for x in apps:
            must(cf.call("DELETE", f"/accounts/{acct}/access/apps/{x['id']}"), "delete Access app"); print("deleted Access app", x["name"])
        return
    if not a.apply:
        print("\ndry run: nothing changed. Add --apply to provision.")
        return
    if not dns_ok:
        raise SystemExit("stopping before any change: the API token cannot manage DNS for this zone")

    # 1. Access first, so the hostname is never reachable unprotected.
    if apps:
        app = apps[0]
    else:
        app = must(cf.call("POST", f"/accounts/{acct}/access/apps", {
            "name": a.name, "domain": a.host, "type": "self_hosted", "session_duration": "24h",
            "app_launcher_visible": False, "auto_redirect_to_identity": False,
            "policies": [{"name": f"{a.name} members", "decision": "allow", "precedence": 1,
                          "include": [{"email": {"email": e.lower()}} for e in a.email]}],
        }), "create Access app")
        print("created Access app", app["name"])
    # 2. Tunnel, remotely managed, one hostname only.
    if tunnels:
        tun = tunnels[0]
    else:
        tun = must(cf.call("POST", f"/accounts/{acct}/cfd_tunnel", {"name": a.name, "config_src": "cloudflare"}), "create tunnel")
        print("created tunnel", tun["name"])
    must(cf.call("PUT", f"/accounts/{acct}/cfd_tunnel/{tun['id']}/configurations", {"config": {"ingress": [
        {"hostname": a.host, "service": a.service, "originRequest": {}},
        {"service": "http_status:404"}]}}), "set tunnel ingress")
    token = must(cf.call("GET", f"/accounts/{acct}/cfd_tunnel/{tun['id']}/token"), "get tunnel token")
    # 3. DNS last.
    if not dns["result"]:
        must(cf.call("POST", f"/zones/{zone}/dns_records", {"type": "CNAME", "name": a.host, "proxied": True,
             "content": f"{tun['id']}.cfargotunnel.com", "comment": "saku dashboard tunnel"}), "create DNS")
        print("created DNS", a.host)
    set_env(Path(a.server_env), {"TUNNEL_TOKEN": token, "SAKU_CF_TEAM": team, "SAKU_CF_AUD": app["aud"]})
    print("wrote TUNNEL_TOKEN, SAKU_CF_TEAM, SAKU_CF_AUD to", a.server_env, "(mode 600)")


if __name__ == "__main__":
    main()
