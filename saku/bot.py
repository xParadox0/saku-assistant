"""Telegram bot with link-first identity and explicit transaction-ingest outcomes.

Unlinked senders are never stored. Linked free text is extracted by a narrowly scoped local agent, validated by
this process, then written through Repo. Every path replies with saved, missing-data, duplicate, or unavailable;
there is no silent acceptance.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .agent import AgentError, UnixAgentClient, validate_extraction
from .format import rupiah
from .normalise import RawTxn
from .repo import Repo

log = logging.getLogger("saku.bot")

NOT_LINKED = (
    "This Telegram account is not connected to Saku yet.\n\n"
    "Saku membantu mencatat pemasukan dan pengeluaran lewat chat, lalu menampilkannya di Dashboard. "
    "Saku tidak meminta password bank, PIN, atau OTP dan tidak bisa memindahkan uang.\n\n"
    "Untuk menghubungkan akun: buka Saku → Settings → Connect Telegram, lalu tekan link yang dibuat. "
    "Tidak ada pesan yang disimpan sebelum akun terhubung.\n\n"
    "Commands: /start, /help, /status, /unlink."
)
PRIVATE_ONLY = "Saku hanya bekerja lewat private chat. Kirim pesan langsung ke bot ini."


@dataclass
class Reply:
    chat_id: int
    text: str


def _connected_help(email: str) -> str:
    return (
        f"Saku terhubung sebagai {email}.\n\n"
        "Kirim transaksi dengan nominal dan account, misalnya:\n"
        "• makan 45rb dari GoPay\n"
        "• gaji 8jt masuk BCA\n\n"
        "Kalau tanggal tidak disebut, Saku memakai hari ini. Kalau ada data yang kurang, Saku akan bertanya dan "
        "belum menyimpan transaksi.\n\n"
        "Saku tidak meminta password bank, PIN, atau OTP dan tidak bisa memindahkan uang.\n"
        "Commands: /status, /help, /unlink."
    )


def _key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


def _match_account(accounts: list[dict], hint: str | None) -> dict | None:
    if not hint:
        return accounts[0] if len(accounts) == 1 else None
    wanted = _key(hint)
    exact = [a for a in accounts if wanted in {_key(a["label"]), _key(a["provider"])}]
    if len(exact) == 1:
        return exact[0]
    partial = [a for a in accounts if wanted and (wanted in _key(a["label"]) or wanted in _key(a["provider"]))]
    return partial[0] if len(partial) == 1 else None


def _today(repo: Repo, tenant_id: str) -> date:
    try:
        tz = ZoneInfo(repo.tenant_timezone(tenant_id))
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("Asia/Jakarta")
    return datetime.now(tz).date()


def _record(repo: Repo, who: dict, uid: int, parsed: dict, accounts: list[dict], today: date,
            source_ref: str) -> str:
    transactions = parsed["transactions"]
    if not transactions:
        return "Belum disimpan. Saya belum menemukan transaksi. Tulis nominal dan account, misalnya: makan 45rb dari GoPay."
    missing = []
    if any(not row.get("description") for row in transactions):
        missing.append("keterangan")
    if any(row.get("amount_idr") is None for row in transactions):
        missing.append("nominal")
    if missing:
        return "Belum disimpan. Data yang masih kurang: " + ", ".join(missing) + ". Kirim ulang transaksi dengan data tersebut."
    if not accounts:
        return "Belum disimpan. Kamu belum punya account. Tambahkan dulu lewat Dashboard → Accounts."

    resolved = [_match_account(accounts, row.get("account_hint")) for row in transactions]
    if any(account is None for account in resolved):
        repo.telegram_draft_set(uid, who["tenant_id"], {"transactions": transactions, "source_ref": source_ref})
        choices = " / ".join(account["label"] for account in accounts)
        return f"Belum disimpan. Account yang dipakai belum jelas. Balas salah satu nama account ini: {choices}."

    transfer_indexes = [i for i, row in enumerate(transactions) if row.get("kind") == "own_transfer"]
    transfer_rows = [transactions[i] for i in transfer_indexes]
    if transfer_rows and (len(transfer_rows) != 2 or resolved[transfer_indexes[0]] == resolved[transfer_indexes[1]]):
        return "Belum disimpan. Transfer harus menyebutkan satu account asal dan satu account tujuan."

    rows_by_account: dict[int, tuple[dict, list[RawTxn]]] = {}
    assumed_today = False
    total_in = Decimal(0)
    total_out = Decimal(0)
    transfer_amount = Decimal(0)
    saved_details = []
    for item, account in zip(transactions, resolved):
        booked_on = date.fromisoformat(item["date"]) if item.get("date") else today
        assumed_today |= item.get("date") is None
        if booked_on > today or booked_on.year < 2000:
            return "Belum disimpan. Tanggal transaksi tidak valid. Kirim tanggal yang sudah terjadi dalam format YYYY-MM-DD."
        amount = Decimal(item["amount_idr"])
        if item["direction"] == "expense":
            amount = -amount
        if item.get("kind") == "own_transfer":
            transfer_amount += abs(amount)
            description = "TRANSFER " + item["description"]
        elif amount > 0:
            total_in += amount
            description = item["description"]
        else:
            total_out += abs(amount)
            description = item["description"]
        rows_by_account.setdefault(account["id"], (account, []))[1].append(
            RawTxn(booked_on, amount, description, item.get("kind", "normal"))
        )
        sign = "+" if amount > 0 else "-"
        saved_details.append(f"{item['description']} {sign}{rupiah(abs(amount))} ({account['label']})")

    stored = duplicate = 0
    labels = []
    for account_id, (account, rows) in rows_by_account.items():
        result = repo.import_manual_rows(who["tenant_id"], account_id, rows, f"{source_ref}:{account_id}")
        stored += result.rows_new
        duplicate += result.rows_dup
        labels.append(account["label"])
    repo.detect_own_transfers(who["tenant_id"])
    repo.apply_categories(who["tenant_id"])
    repo.telegram_draft_clear(uid, who["tenant_id"])

    if stored == 0 and duplicate:
        return "Pesan ini sudah pernah diproses. Tidak ada transaksi duplikat yang disimpan."
    when = " untuk hari ini" if assumed_today else ""
    account_text = ", ".join(dict.fromkeys(labels))
    detail = f" ({duplicate} duplikat dilewati)" if duplicate else ""
    if transfer_rows:
        transfer_total = transfer_amount / 2
        flow = f"transfer internal {rupiah(transfer_total)}"
    else:
        flow = f"money in {rupiah(total_in)}, money out {rupiah(total_out)}"
    details_text = "; ".join(saved_details)
    return (f"Berhasil disimpan: {stored} detail, {flow}, account {account_text}{when}{detail}. "
            f"Detail: {details_text}. "
            "Transfer internal tidak dihitung sebagai income atau spending. Datanya sudah masuk ke Dashboard." if transfer_rows else
            f"Berhasil disimpan: {stored} transaksi, total {rupiah(total_in + total_out)}, {flow}, "
            f"account {account_text}{when}{detail}. Detail: {details_text}. Datanya sudah masuk ke Dashboard.")


def handle(repo: Repo, update: dict, agent=None) -> list[Reply]:
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
            extra = " Telegram sebelumnya sudah diputus." if outcome == "relinked" else ""
            return [Reply(chat["id"], f"Saku berhasil terhubung sebagai {email}.{extra}\n\n"
                                      + _connected_help(email))]
        if outcome == "taken":
            return [Reply(chat["id"], "Telegram ini sudah terhubung ke akun Saku lain. Putuskan koneksinya lebih dulu.")]
        return [Reply(chat["id"], "Link sudah kedaluwarsa atau pernah dipakai. Buat link baru lewat Settings → Connect Telegram.")]

    who = repo.telegram_identity(uid)
    if who is None:
        return [Reply(chat["id"], NOT_LINKED)]

    if cmd == "/status":
        return [Reply(chat["id"], f"Connected as {who['email']}.")]
    if cmd == "/unlink":
        repo.unlink_telegram(telegram_user_id=uid)
        return [Reply(chat["id"], "Disconnected. Saku tidak akan membaca pesan dari Telegram ini lagi.")]
    if cmd in ("/start", "/help"):
        return [Reply(chat["id"], _connected_help(who["email"]))]
    if text.startswith("/"):
        return [Reply(chat["id"], "Command tidak dikenal. Gunakan /help untuk melihat cara memakai Saku.")]
    if not text:
        return [Reply(chat["id"], "Belum disimpan. Kirim transaksi dalam bentuk teks, misalnya: makan 45rb dari GoPay.")]

    accounts = repo.accounts(who["tenant_id"])
    today = _today(repo, who["tenant_id"])
    pending = repo.telegram_draft_get(uid, who["tenant_id"])
    if pending:
        selected = _match_account(accounts, text)
        if selected:
            for row in pending["transactions"]:
                if not row.get("account_hint"):
                    row["account_hint"] = selected["label"]
            parsed = validate_extraction({"intent": "record", "transactions": pending["transactions"]})
            reply = _record(repo, who, uid, parsed, accounts, today, pending["source_ref"])
            return [Reply(chat["id"], reply)]

    if agent is None:
        return [Reply(chat["id"], "Saku belum bisa memproses pesan ini sekarang. Tidak ada data yang disimpan. Coba lagi sebentar.")]
    try:
        parsed = validate_extraction(agent.extract(text, accounts, today))
    except (AgentError, OSError, ValueError, TypeError):
        return [Reply(chat["id"], "Saku gagal memahami pesan ini. Tidak ada data yang disimpan. Coba tulis nominal dan account dengan jelas.")]
    if parsed["intent"] != "record":
        return [Reply(chat["id"], "Belum disimpan. Saat ini Saku baru mencatat transaksi. Contoh: makan 45rb dari GoPay.")]
    source_ref = f"telegram:{uid}:{msg.get('message_id', update.get('update_id', 0))}"
    return [Reply(chat["id"], _record(repo, who, uid, parsed, accounts, today, source_ref))]


class TelegramAPI:
    def __init__(self, token: str):
        if not token or ":" not in token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
        self._base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, request_timeout: int = 20, **params) -> dict:
        data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                                       for k, v in params.items()}).encode()
        req = urllib.request.Request(self._base + method, data=data)
        with urllib.request.urlopen(req, timeout=request_timeout) as response:
            body = json.load(response)
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {body.get('description')}")
        return body["result"]


def poll_once(conn_factory, api: TelegramAPI, offset: int | None, agent=None) -> int | None:
    updates = api.call(
        "getUpdates", request_timeout=40, timeout=25, offset=offset or "", allowed_updates=["message"]
    )
    next_offset = offset
    for update in updates:
        conn = conn_factory()
        try:
            repo = Repo(conn)
            for reply in handle(repo, update, agent=agent):
                api.call("sendMessage", chat_id=reply.chat_id, text=reply.text)
            next_offset = update["update_id"] + 1  # advance only after every reply was delivered
        finally:
            conn.close()

    heartbeat_conn = conn_factory()
    try:
        Repo(heartbeat_conn).beat("_system", "telegram_bot")
    finally:
        heartbeat_conn.close()
    return next_offset


def run_polling(conn_factory, token: str | None = None, agent=None) -> None:
    api = TelegramAPI(token or os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    socket_path = os.environ.get("SAKU_AGENT_SOCKET", "")
    agent = agent or (UnixAgentClient(socket_path) if socket_path else None)
    me = api.call("getMe")
    log.info("bot @%s started (long polling)", me.get("username"))
    api.call("setMyCommands", commands=[{"command": "start", "description": "Mulai onboarding Saku"},
                                        {"command": "status", "description": "Lihat akun Saku yang terhubung"},
                                        {"command": "help", "description": "Cara mencatat transaksi"},
                                        {"command": "unlink", "description": "Putuskan akun Telegram"}])
    offset = None
    while True:
        try:
            offset = poll_once(conn_factory, api, offset, agent=agent)
        except Exception as exc:  # noqa: BLE001  keep polling; never log message contents
            log.warning("poll error: %s", type(exc).__name__)
            time.sleep(5)
