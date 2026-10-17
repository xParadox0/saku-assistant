"""Telegram bot, link-first.

The bot never trusts a Telegram account on its own. A sender is resolved only through telegram_link, which is
filled when the member redeems a one-time code made in the dashboard (Settings > Connect Telegram).
Unlinked senders get instructions and nothing they send is stored or logged.

handle() is pure (repo in, replies out) so it is tested without the network. run_polling() is the thin
long-polling loop around it; the token comes from TELEGRAM_BOT_TOKEN and is never printed.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .repo import Repo

log = logging.getLogger("saku.bot")

NOT_LINKED = ("This Telegram account is not connected to Saku yet.\n\n"
              "Open the Saku dashboard, go to Settings, tap \"Connect Telegram\", then press the button it shows. "
              "Nothing you send here is saved until the account is connected.")
PRIVATE_ONLY = "Saku only works in a private chat. Please message me directly."


@dataclass
class Reply:
    chat_id: int
    text: str


def handle(repo: Repo, update: dict) -> list[Reply]:
    msg = update.get("message") or update.get("edited_message")
    if not msg or "from" not in msg or "chat" not in msg:
        return []
    chat, sender = msg["chat"], msg["from"]
    if chat.get("type") != "private":
        return [Reply(chat["id"], PRIVATE_ONLY)]
    if sender.get("is_bot"):
        return []
    uid = int(sender["id"])
    text = (msg.get("text") or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@", 1)[0].lower()

    if cmd == "/start" and arg.strip():
        outcome, email = repo.redeem_link_code(arg.strip(), uid)
        if outcome in ("linked", "relinked"):
            extra = " Your previous Telegram account was disconnected." if outcome == "relinked" else ""
            return [Reply(chat["id"], f"Connected to Saku as {email}.{extra}\n\n"
                                      "Send /status to check, /unlink to disconnect.")]
        if outcome == "taken":
            return [Reply(chat["id"], "This Telegram account is already connected to another Saku account. "
                                      "Disconnect it there first.")]
        return [Reply(chat["id"], "That link has expired or was already used. "
                                  "Make a new one in the dashboard: Settings, Connect Telegram.")]

    who = repo.telegram_identity(uid)
    if who is None:
        return [Reply(chat["id"], NOT_LINKED)]

    if cmd == "/status":
        return [Reply(chat["id"], f"Connected as {who['email']}.")]
    if cmd == "/unlink":
        repo.unlink_telegram(telegram_user_id=uid)
        return [Reply(chat["id"], "Disconnected. Saku will not read messages from this account any more.")]
    if cmd in ("/start", "/help"):
        return [Reply(chat["id"], f"Hi. You are connected as {who['email']}.\n"
                                  "/status shows the connection, /unlink disconnects.\n"
                                  "Recording spending by chat arrives in the next stage.")]
    return [Reply(chat["id"], "Got it. Recording spending by chat arrives in the next stage; "
                              "for now use the dashboard to upload statements.")]


# ---- network loop -------------------------------------------------------------------------------------
class TelegramAPI:
    def __init__(self, token: str):
        if not token or ":" not in token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
        self._base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, timeout: int = 40, **params) -> dict:
        data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                                       for k, v in params.items()}).encode()
        req = urllib.request.Request(self._base + method, data=data)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.load(r)
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {body.get('description')}")
        return body["result"]


def run_polling(conn_factory, token: str | None = None) -> None:
    api = TelegramAPI(token or os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    me = api.call("getMe")
    log.info("bot @%s started (long polling)", me.get("username"))
    api.call("setMyCommands", commands=[{"command": "status", "description": "Show the connected Saku account"},
                                        {"command": "unlink", "description": "Disconnect this Telegram account"},
                                        {"command": "help", "description": "What Saku can do"}])
    offset = None
    while True:
        try:
            updates = api.call("getUpdates", timeout=40, offset=offset or "", allowed_updates=["message"])
            for up in updates:
                offset = up["update_id"] + 1
                conn = conn_factory()
                try:
                    repo = Repo(conn)
                    for r in handle(repo, up):
                        api.call("sendMessage", chat_id=r.chat_id, text=r.text)
                    repo.beat("_system", "telegram_bot")
                finally:
                    conn.close()
        except Exception as e:  # noqa: BLE001  keep polling; never log message contents
            log.warning("poll error: %s", type(e).__name__)
            time.sleep(5)
