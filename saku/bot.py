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
import urllib.error
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
from .stt import UnixTranscriberClient

log = logging.getLogger("saku.bot")
_SAFE_FILE_PATH = re.compile(r"[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-.]+)*")   # relative, no scheme, no leading slash

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
        "• gaji 8jt masuk BCA\n"
        "• parkir 5rb cash\n\n"
        "Bisa juga lewat voice note, maksimal 60 detik. Saku membalas dengan teks yang ia dengar, "
        "lalu menyimpan seperti pesan biasa. Audio tidak disimpan.\n\n"
        "Untuk uang tunai, sebut cash atau tunai. Kalau account tidak disebut, Saku bertanya dulu.\n\n"
        "Kalau tanggal tidak disebut, Saku memakai hari ini. Kalau ada data yang kurang, Saku akan bertanya dan "
        "belum menyimpan transaksi.\n\n"
        "Saku tidak meminta password bank, PIN, atau OTP dan tidak bisa memindahkan uang.\n"
        "Commands: /status, /help, /unlink."
    )


def _key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


CASH_WORDS = {"cash", "tunai", "uangcash", "uangtunai", "duitcash", "duittunai", "kas", "kontan"}
CASH_LABEL = "Cash"
_FILLER = {"pakai", "pake", "dari", "lewat", "via", "ke", "masuk", "di", "yang", "aja", "saja", "uang", "duit",
           "account", "akun", "rekening", "dong", "ya", "yg", "dengan", "pakaiin"}
# A follow-up containing any of these is a new message, never an account answer.
_NEGATION = {"jangan", "bukan", "tidak", "gak", "nggak", "ngga", "enggak", "ga", "tdk", "no", "batal"}
_AMOUNT_WORDS = {"rb", "ribu", "jt", "juta", "k", "rp", "rupiah", "ratus", "puluh", "belas", "seribu", "sejuta",
                 "seratus", "sepuluh", "setengah", "ceban", "goceng", "gocap", "noban"}


def _is_cash_hint(hint: str | None) -> bool:
    return _key(hint) in CASH_WORDS


def _cash_accounts(accounts: list[dict]) -> list[dict]:
    return [a for a in accounts if a.get("kind") == "cash"]


def _match_account(accounts: list[dict], hint: str | None) -> dict | None:
    if not hint:
        return accounts[0] if len(accounts) == 1 else None
    if _is_cash_hint(hint):
        # a cash word only ever means a cash account, never a label that happens to contain it ("OVO Cashback")
        cash = _cash_accounts(accounts)
        return cash[0] if len(cash) == 1 else None
    wanted = _key(hint)
    exact = [a for a in accounts if wanted in {_key(a["label"]), _key(a["provider"])}]
    if len(exact) == 1:
        return exact[0]
    if len(wanted) < 4:   # "se", "pay", "ba" are fragments, not account names
        return None
    partial = [a for a in accounts if wanted and (wanted in _key(a["label"]) or wanted in _key(a["provider"]))]
    return partial[0] if len(partial) == 1 else None


def _answer_words(reply: str) -> str | None:
    """The account name in a short follow-up ("Pakai cash.", "GoPay aja"), or None if it is a new message.

    Anything with a number, an amount word or a negation is a new message. What remains after filler words
    must be the account name itself, at most three words.
    """
    words = [w for w in re.split(r"[^\w]+", reply.casefold()) if w]
    if not words or any(ch.isdigit() for w in words for ch in w):
        return None
    if any(w in _NEGATION or w in _AMOUNT_WORDS for w in words):
        return None
    rest = [w for w in words if w not in _FILLER]
    return " ".join(rest) if 1 <= len(rest) <= 3 else None


def _match_reply(accounts: list[dict], reply: str) -> dict | None:
    name = _answer_words(reply)
    return _match_account(accounts, name) if name else None


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
        wants_cash = any(account is None and _is_cash_hint(row.get("account_hint"))
                         for row, account in zip(transactions, resolved))
        has_cash = bool(_cash_accounts(accounts))
        unknown = [row["account_hint"] for row, account in zip(transactions, resolved)
                   if account is None and row.get("account_hint") and not _is_cash_hint(row["account_hint"])]
        if unknown:
            # The user named an account Saku does not have. Never move that row elsewhere; ask for a resend.
            repo.telegram_draft_clear(uid, who["tenant_id"])
            names = ", ".join(dict.fromkeys(unknown))
            return (f"Belum disimpan. Account {names} belum ada di Saku. Tambahkan dulu lewat Dashboard → Accounts, "
                    f"atau kirim ulang transaksinya dengan account yang sudah ada: "
                    + " / ".join(a["label"] for a in accounts) + ".")
        repo.telegram_draft_set(uid, who["tenant_id"], {"transactions": transactions, "source_ref": source_ref,
                                                         "offer_cash": wants_cash and not has_cash})
        if wants_cash and not has_cash:
            choices = " / ".join([CASH_LABEL] + [account["label"] for account in accounts])
            return (f"Belum disimpan. Kamu belum punya account cash. Balas {CASH_LABEL} untuk membuat account "
                    f"cash dan menyimpan transaksi ini, atau pilih account lain: {choices}.")
        ordered = _cash_accounts(accounts) + [a for a in accounts if a.get("kind") != "cash"]
        choices = " / ".join(account["label"] for account in ordered)
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


MAX_VOICE_SECONDS = 60
MAX_VOICE_BYTES = 1_000_000
VOICE_UNAVAILABLE = ("Belum disimpan. Voice note belum bisa diproses sekarang. Coba lagi sebentar, "
                     "atau ketik transaksinya, misalnya: parkir 5rb cash.")
# Telegram voice notes are OGG/Opus. Audio files are accepted only in common formats; the bridge re-checks bytes.
_AUDIO_MIME = {"audio/ogg", "audio/opus", "audio/mpeg", "audio/mp3", "audio/mp4", "audio/m4a", "audio/x-m4a",
               "audio/aac", "audio/wav", "audio/x-wav", "audio/webm"}


class _Refused(Exception):
    """Rolls back a cash account created for a save that was then refused."""


def _record_with_new_cash(repo: Repo, who: dict, uid: int, pending: dict, today: date) -> str:
    """Create the Cash account and save the draft in one transaction, so a refused save leaves no account."""
    try:
        with repo.c.transaction():
            repo.add_account(who["tenant_id"], "cash", "cash", CASH_LABEL)
            accounts = repo.accounts(who["tenant_id"])
            cash = _cash_accounts(accounts)[0]
            for row in pending["transactions"]:
                # The user answered the "Balas Cash … menyimpan transaksi ini" offer, so Cash is their choice for
                # every row that said cash or named no account. Rows that named another account stay untouched.
                if not row.get("account_hint") or _is_cash_hint(row.get("account_hint")):
                    row["account_hint"] = cash["label"]
            parsed = validate_extraction({"intent": "record", "transactions": pending["transactions"]})
            out = _record(repo, who, uid, parsed, accounts, today, pending["source_ref"])
            if not out.startswith("Berhasil disimpan"):
                raise _Refused(out)
            return f"Account {CASH_LABEL} dibuat. " + out
    except _Refused as refused:
        return str(refused)


def _voice_media(msg: dict) -> dict | None:
    if isinstance(msg.get("voice"), dict):
        media = msg["voice"]
        ok = (media.get("mime_type") or "audio/ogg") in ("audio/ogg", "audio/opus")
    elif isinstance(msg.get("audio"), dict):
        media = msg["audio"]
        ok = (media.get("mime_type") or "") in _AUDIO_MIME
    else:
        return None
    if not media.get("file_id"):
        return None
    return media if ok else {"unsupported": True}


def _error_kind(exc: BaseException) -> str:
    """Exception type plus the underlying cause type, never the message (it can hold the token or a URL)."""
    cause = getattr(exc, "reason", None) or exc.__cause__
    if isinstance(cause, BaseException):
        return f"{type(exc).__name__} ({type(cause).__name__})"
    return type(exc).__name__


def _transcribe_voice(media: dict, transcriber, files) -> tuple[str | None, str | None]:
    """Return (transcript, None) or (None, reply). Size and length are checked before anything is downloaded."""
    if media.get("unsupported"):
        return None, ("Belum disimpan. Format audio ini belum didukung. Kirim lewat tombol mikrofon "
                      "(voice note) di Telegram.")
    if transcriber is None or files is None:
        return None, VOICE_UNAVAILABLE
    duration = media.get("duration") or 0
    size = media.get("file_size") or 0
    if duration > MAX_VOICE_SECONDS or size > MAX_VOICE_BYTES:
        return None, (f"Belum disimpan. Voice note maksimal {MAX_VOICE_SECONDS} detik. "
                      "Kirim satu atau beberapa transaksi singkat per voice note.")
    try:
        audio = files.download(media["file_id"], MAX_VOICE_BYTES)
    except Exception as exc:  # noqa: BLE001  log only types: URLs carry the bot token
        log.warning("voice download failed: %s", _error_kind(exc))
        return None, VOICE_UNAVAILABLE
    try:
        transcript = (transcriber.transcribe(audio) or "").strip()
    except Exception as exc:  # noqa: BLE001  never log audio or transcript
        log.warning("voice transcription failed: %s", _error_kind(exc))
        return None, VOICE_UNAVAILABLE
    if not transcript:
        return None, ("Belum disimpan. Saya tidak menangkap kata apa pun di voice note ini. "
                      "Coba rekam ulang dengan lebih dekat ke mikrofon.")
    return transcript, None


def handle(repo: Repo, update: dict, agent=None, transcriber=None, files=None) -> list[Reply]:
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
        return [Reply(chat["id"], NOT_LINKED)]   # unlinked voice is never downloaded

    if cmd == "/status":
        return [Reply(chat["id"], f"Connected as {who['email']}.")]
    if cmd == "/unlink":
        repo.unlink_telegram(telegram_user_id=uid)
        return [Reply(chat["id"], "Disconnected. Saku tidak akan membaca pesan dari Telegram ini lagi.")]
    if cmd in ("/start", "/help"):
        return [Reply(chat["id"], _connected_help(who["email"]))]
    if text.startswith("/"):
        return [Reply(chat["id"], "Command tidak dikenal. Gunakan /help untuk melihat cara memakai Saku.")]

    heard = ""
    media = None if text else _voice_media(msg)
    if media is not None:
        transcript, refusal = _transcribe_voice(media, transcriber, files)
        if refusal:
            return [Reply(chat["id"], refusal)]
        text = transcript
        heard = f'Saya dengar: "{transcript}"\n\n'
    if not text:
        return [Reply(chat["id"], "Belum disimpan. Kirim transaksi lewat teks atau voice note, "
                                  "misalnya: makan 45rb dari GoPay.")]

    def reply(body: str) -> list[Reply]:
        return [Reply(chat["id"], heard + body)]

    accounts = repo.accounts(who["tenant_id"])
    today = _today(repo, who["tenant_id"])
    pending = repo.telegram_draft_get(uid, who["tenant_id"])
    if pending:
        selected = _match_reply(accounts, text)
        answer = _answer_words(text)
        if selected is None and answer and _is_cash_hint(answer) and pending.get("offer_cash") \
                and not _cash_accounts(accounts):
            return reply(_record_with_new_cash(repo, who, uid, pending, today))
        if selected:
            for row in pending["transactions"]:
                hint = row.get("account_hint")
                # A cash-hint row moves only to a cash account, or to the account the user picked after Saku said
                # they have no cash account and offered "atau pilih account lain".
                if not hint or (_is_cash_hint(hint) and (selected.get("kind") == "cash" or pending.get("offer_cash"))):
                    row["account_hint"] = selected["label"]
            parsed = validate_extraction({"intent": "record", "transactions": pending["transactions"]})
            return reply(_record(repo, who, uid, parsed, accounts, today, pending["source_ref"]))

    if agent is None:
        return reply("Saku belum bisa memproses pesan ini sekarang. Tidak ada data yang disimpan. Coba lagi sebentar.")
    try:
        parsed = validate_extraction(agent.extract(text, accounts, today))
    except (AgentError, OSError, ValueError, TypeError):
        return reply("Saku gagal memahami pesan ini. Tidak ada data yang disimpan. "
                     "Coba tulis nominal dan account dengan jelas.")
    if parsed["intent"] != "record":
        return reply("Belum disimpan. Saat ini Saku baru mencatat transaksi. Contoh: makan 45rb dari GoPay.")
    source_ref = f"telegram:{uid}:{msg.get('message_id', update.get('update_id', 0))}"
    return reply(_record(repo, who, uid, parsed, accounts, today, source_ref))


class TelegramAPI:
    def __init__(self, token: str):
        if not token or ":" not in token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
        self._base = f"https://api.telegram.org/bot{token}/"

    def _open(self, request: urllib.request.Request, timeout: int):
        """urlopen with two quick retries for network-level failures (reset, DNS blip, timeout).

        HTTP errors (4xx/5xx) are answers from Telegram and are not retried.
        """
        for attempt in range(3):
            try:
                return urllib.request.urlopen(request, timeout=timeout)
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == 2:
                    raise
                time.sleep(1 + attempt)

    def call(self, method: str, request_timeout: int = 20, **params) -> dict:
        data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                                       for k, v in params.items()}).encode()
        req = urllib.request.Request(self._base + method, data=data)
        with urllib.request.urlopen(req, timeout=request_timeout) as response:
            body = json.load(response)
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {body.get('description')}")
        return body["result"]

    def download(self, file_id: str, max_bytes: int) -> bytes:
        """Fetch a file the user sent, refusing anything larger than max_bytes. Nothing is written to disk."""
        req = urllib.request.Request(self._base + "getFile",
                                     data=urllib.parse.urlencode({"file_id": file_id}).encode())
        with self._open(req, 20) as response:
            body = json.load(response)
        if not body.get("ok"):
            raise RuntimeError("telegram getFile failed")
        info = body["result"]
        path = info.get("file_path") or ""
        if (info.get("file_size") or 0) > max_bytes or not _SAFE_FILE_PATH.fullmatch(path) \
                or ".." in path.split("/"):
            raise ValueError("file too large or unavailable")
        url = self._base.replace("/bot", "/file/bot", 1) + path
        with self._open(urllib.request.Request(url), 30) as response:
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("file too large")
        return data


def poll_once(conn_factory, api: TelegramAPI, offset: int | None, agent=None, transcriber=None) -> int | None:
    updates = api.call(
        "getUpdates", request_timeout=40, timeout=25, offset=offset or "", allowed_updates=["message"]
    )
    next_offset = offset
    for update in updates:
        conn = conn_factory()
        try:
            repo = Repo(conn)
            msg = update.get("message") or {}
            chat = msg.get("chat") or {}
            if (msg.get("voice") or msg.get("audio")) and transcriber is not None and chat.get("type") == "private" \
                    and repo.telegram_identity(int((msg.get("from") or {}).get("id") or 0)) is not None:
                try:
                    api.call("sendChatAction", chat_id=msg["chat"]["id"], action="typing")
                except Exception:  # noqa: BLE001  a missing typing indicator must not drop the message
                    pass
            for reply in handle(repo, update, agent=agent, transcriber=transcriber, files=api):
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


def run_polling(conn_factory, token: str | None = None, agent=None, transcriber=None) -> None:
    api = TelegramAPI(token or os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    socket_path = os.environ.get("SAKU_AGENT_SOCKET", "")
    agent = agent or (UnixAgentClient(socket_path) if socket_path else None)
    stt_path = os.environ.get("SAKU_STT_SOCKET", "")
    transcriber = transcriber or (UnixTranscriberClient(stt_path) if stt_path else None)
    me = api.call("getMe")
    log.info("bot @%s started (long polling, voice %s)", me.get("username"), "on" if transcriber else "off")
    api.call("setMyCommands", commands=[{"command": "start", "description": "Mulai onboarding Saku"},
                                        {"command": "status", "description": "Lihat akun Saku yang terhubung"},
                                        {"command": "help", "description": "Cara mencatat transaksi"},
                                        {"command": "unlink", "description": "Putuskan akun Telegram"}])
    offset = None
    while True:
        try:
            offset = poll_once(conn_factory, api, offset, agent=agent, transcriber=transcriber)
        except Exception as exc:  # noqa: BLE001  keep polling; never log message contents
            log.warning("poll error: %s", _error_kind(exc))
            time.sleep(5)
