#!/usr/bin/env python3
"""Set TELEGRAM_BOT_TOKEN in the server .env without the token appearing on screen, in shell history or in chat.

    python3 ~/projects/finance-assistant/tools/set_bot_token.py

Paste the token from @BotFather when asked. The script checks it with Telegram (getMe) before saving.
"""
import getpass
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

ENV = Path.home() / ".config/saku-assistant/.env"

tok = getpass.getpass("New bot token (hidden): ").strip()
if not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,50}", tok):
    raise SystemExit("That does not look like a bot token (digits:letters). Nothing saved.")
try:
    with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/getMe", timeout=20) as r:
        me = json.load(r)
except urllib.error.HTTPError as e:
    me = json.load(e)
if not me.get("ok"):
    raise SystemExit(f"Telegram rejected the token ({me.get('description')}). Nothing saved.")

lines = ENV.read_text().splitlines() if ENV.exists() else []
lines = [l for l in lines if not l.startswith("TELEGRAM_BOT_TOKEN=")] + [f"TELEGRAM_BOT_TOKEN={tok}"]
fd = os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write("\n".join(lines) + "\n")
os.chmod(ENV, 0o600)
print(f"Saved. Token belongs to @{me['result']['username']}. File mode 600.")
