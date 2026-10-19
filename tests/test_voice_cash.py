"""Voice notes and cash entries in the Telegram bot.

Voice: the bot downloads the note, transcribes it locally, shows the transcript, then runs the same extraction and
validation as typed text. Audio is never stored and never sent to a third-party speech service.
Cash: a message that says cash/tunai, or an expense with no account when the tenant has exactly one cash account,
goes to that cash account instead of ending in a dead end.
"""
from datetime import date
from decimal import Decimal

import pytest

from saku import bot
from saku.normalise import RawTxn

ALICE_TG = 111_000_111


class FixedAgent:
    def __init__(self, result):
        self.result = result
        self.seen = []

    def extract(self, text, accounts, today):
        self.seen.append(text)
        return self.result


class FakeTranscriber:
    def __init__(self, text="bayar parkir mobil 5 ribu", error=None):
        self.text, self.error, self.calls = text, error, []

    def transcribe(self, audio: bytes) -> str:
        self.calls.append(len(audio))
        if self.error:
            raise self.error
        return self.text


class FakeFiles:
    """Stands in for Telegram getFile + download. Records what was asked for."""
    def __init__(self, data=b"OggS fake opus", fail=False):
        self.data, self.fail, self.asked = data, fail, []

    def download(self, file_id: str, max_bytes: int) -> bytes:
        self.asked.append((file_id, max_bytes))
        if self.fail:
            raise OSError("network")
        return self.data


def voice_upd(uid, duration=3, size=4_000, mime="audio/ogg", kind="voice"):
    media = {"file_id": "VOICE-FILE-ID", "file_unique_id": "u1", "duration": duration,
             "mime_type": mime, "file_size": size}
    return {"update_id": 7, "message": {"message_id": 7, kind: media,
            "from": {"id": uid, "is_bot": False}, "chat": {"id": uid, "type": "private"}}}


def text_upd(uid, text):
    return {"update_id": 8, "message": {"message_id": 8, "text": text,
            "from": {"id": uid, "is_bot": False}, "chat": {"id": uid, "type": "private"}}}


PARKIR = {"intent": "record", "transactions": [
    {"description": "Parkir mobil", "amount_idr": 5000, "direction": "expense", "date": None, "account_hint": None}]}
PARKIR_CASH = {"intent": "record", "transactions": [
    {"description": "Parkir mobil", "amount_idr": 5000, "direction": "expense", "date": None, "account_hint": "cash"}]}


@pytest.fixture
def alice(repo):
    repo.add_member("alice@example.com", "alice")
    main = repo.add_account("alice", "bank", "bca", "Alice BCA")
    repo.add_account("alice", "ewallet", "gopay", "Alice GoPay")
    repo.import_rows("alice", main, [RawTxn(date(2026, 9, 1), Decimal(5_000_000), "SALARY")], "csv", "t")
    bot.handle(repo, text_upd(ALICE_TG, f"/start {repo.create_link_code('alice@example.com')}"))
    return repo


def count(conn, account_kind=None):
    if account_kind:
        return conn.execute("SELECT count(*) FROM txn t JOIN account a ON a.id=t.account_id "
                            "WHERE t.tenant_id='alice' AND a.kind=%s", (account_kind,)).fetchone()[0]
    return conn.execute("SELECT count(*) FROM txn WHERE tenant_id='alice'").fetchone()[0]


# ---- voice ------------------------------------------------------------------------------------------
def test_voice_note_is_transcribed_then_saved_like_text(alice, conn):
    alice.add_account("alice", "cash", "cash", "Cash")
    agent, stt, files = FixedAgent(PARKIR_CASH), FakeTranscriber("bayar parkir mobil 5 ribu cash"), FakeFiles()
    before = count(conn, "cash")
    reply = bot.handle(alice, voice_upd(ALICE_TG), agent=agent, transcriber=stt, files=files)[0].text
    assert files.asked == [("VOICE-FILE-ID", bot.MAX_VOICE_BYTES)]
    assert agent.seen == ["bayar parkir mobil 5 ribu cash"]
    assert reply.startswith('Saya dengar: "bayar parkir mobil 5 ribu cash"')
    assert "Berhasil disimpan" in reply and "Cash" in reply and "Rp5.000" in reply
    assert count(conn, "cash") == before + 1


def test_audio_file_message_is_also_accepted(alice):
    agent = FixedAgent(PARKIR_CASH)
    alice.add_account("alice", "cash", "cash", "Cash")
    reply = bot.handle(alice, voice_upd(ALICE_TG, kind="audio", mime="audio/mpeg"), agent=agent,
                       transcriber=FakeTranscriber(), files=FakeFiles())[0].text
    assert "Berhasil disimpan" in reply


def test_long_or_large_voice_is_refused_before_download(alice, conn):
    files, stt = FakeFiles(), FakeTranscriber()
    before = count(conn)
    long_reply = bot.handle(alice, voice_upd(ALICE_TG, duration=bot.MAX_VOICE_SECONDS + 1),
                            agent=FixedAgent(PARKIR), transcriber=stt, files=files)[0].text
    big_reply = bot.handle(alice, voice_upd(ALICE_TG, size=bot.MAX_VOICE_BYTES + 1),
                           agent=FixedAgent(PARKIR), transcriber=stt, files=files)[0].text
    assert files.asked == [] and stt.calls == []
    assert "Belum disimpan" in long_reply and str(bot.MAX_VOICE_SECONDS) in long_reply
    assert "Belum disimpan" in big_reply
    assert count(conn) == before


def test_voice_from_unlinked_sender_is_not_downloaded(repo):
    files, stt = FakeFiles(), FakeTranscriber()
    reply = bot.handle(repo, voice_upd(999_000_999), agent=FixedAgent(PARKIR), transcriber=stt, files=files)[0].text
    assert "not connected" in reply
    assert files.asked == [] and stt.calls == []


def test_transcription_failure_saves_nothing_and_says_so(alice, conn):
    before = count(conn)
    for stt, files in ((FakeTranscriber(error=RuntimeError("boom")), FakeFiles()),
                       (FakeTranscriber(), FakeFiles(fail=True)),
                       (FakeTranscriber(text="   "), FakeFiles())):
        reply = bot.handle(alice, voice_upd(ALICE_TG), agent=FixedAgent(PARKIR), transcriber=stt, files=files)[0].text
        assert "Belum disimpan" in reply and "voice" in reply.lower()
    assert count(conn) == before


def test_voice_without_transcriber_explains_instead_of_asking_for_text(alice):
    reply = bot.handle(alice, voice_upd(ALICE_TG), agent=FixedAgent(PARKIR), transcriber=None, files=FakeFiles())[0].text
    assert "voice" in reply.lower() and "Belum disimpan" in reply
    assert "dalam bentuk teks" not in reply


def test_voice_transcript_answers_a_pending_account_question(alice, conn):
    agent = FixedAgent(PARKIR)
    ask = bot.handle(alice, text_upd(ALICE_TG, "bayar parkir mobil 5 ribu"), agent=agent)[0].text
    assert "Account yang dipakai belum jelas" in ask
    before = count(conn)
    reply = bot.handle(alice, voice_upd(ALICE_TG), agent=agent, transcriber=FakeTranscriber("Alice GoPay"),
                       files=FakeFiles())[0].text
    assert "Berhasil disimpan" in reply and "Alice GoPay" in reply
    assert count(conn) == before + 1


def test_transcript_is_not_logged(alice, caplog):
    alice.add_account("alice", "cash", "cash", "Cash")
    with caplog.at_level("DEBUG"):
        bot.handle(alice, voice_upd(ALICE_TG), agent=FixedAgent(PARKIR_CASH),
                   transcriber=FakeTranscriber("parkir rahasia 5 ribu"), files=FakeFiles())
        bot.handle(alice, voice_upd(ALICE_TG), agent=FixedAgent(PARKIR_CASH),
                   transcriber=FakeTranscriber(error=RuntimeError("parkir rahasia")), files=FakeFiles())
    assert "rahasia" not in caplog.text


# ---- cash -------------------------------------------------------------------------------------------
def test_expense_without_account_still_asks_with_cash_first(alice, conn):
    alice.add_account("alice", "cash", "cash", "Dompet")
    before = count(conn)
    reply = bot.handle(alice, text_upd(ALICE_TG, "bayar parkir mobil 5 ribu"), agent=FixedAgent(PARKIR))[0].text
    assert "Belum disimpan" in reply and "Account yang dipakai belum jelas" in reply
    assert reply.index("Dompet") < reply.index("Alice BCA")      # the cash account is offered first
    assert count(conn) == before


@pytest.mark.parametrize("answer", ["cash", "Tunai", "Pakai cash.", "pake tunai aja", "Dompet"])
def test_cash_reply_answers_the_account_question(alice, conn, answer):
    alice.add_account("alice", "cash", "cash", "Dompet")
    bot.handle(alice, text_upd(ALICE_TG, "bayar parkir mobil 5 ribu"), agent=FixedAgent(PARKIR))
    before = count(conn, "cash")
    reply = bot.handle(alice, text_upd(ALICE_TG, answer), agent=FixedAgent(PARKIR))[0].text
    assert "Berhasil disimpan" in reply and "Dompet" in reply
    assert count(conn, "cash") == before + 1


def test_spoken_cash_answer_resolves_pending_question(alice, conn):
    alice.add_account("alice", "cash", "cash", "Cash")
    bot.handle(alice, text_upd(ALICE_TG, "bayar parkir mobil 5 ribu"), agent=FixedAgent(PARKIR))
    reply = bot.handle(alice, voice_upd(ALICE_TG), agent=FixedAgent(PARKIR),
                       transcriber=FakeTranscriber("Pakai cash."), files=FakeFiles())[0].text
    assert reply.startswith('Saya dengar: "Pakai cash."') and "Berhasil disimpan" in reply
    assert count(conn, "cash") == 1


def test_ambiguous_reply_is_not_guessed(alice, conn):
    alice.add_account("alice", "cash", "cash", "Cash")
    bot.handle(alice, text_upd(ALICE_TG, "bayar parkir mobil 5 ribu"), agent=FixedAgent(PARKIR))
    before = count(conn)
    nothing = {"intent": "unknown", "transactions": []}
    reply = bot.handle(alice, text_upd(ALICE_TG, "gopay atau cash"), agent=FixedAgent(nothing))[0].text
    assert "Berhasil disimpan" not in reply
    assert count(conn) == before


@pytest.mark.parametrize("word", ["cash", "tunai", "Cash", "TUNAI", "uang cash", "duit tunai"])
def test_cash_words_pick_the_cash_account_even_if_named_differently(alice, conn, word):
    alice.add_account("alice", "cash", "cash", "Dompet")
    result = {"intent": "record", "transactions": [
        {"description": "Parkir mobil", "amount_idr": 5000, "direction": "expense", "date": None, "account_hint": word}]}
    before = count(conn, "cash")
    reply = bot.handle(alice, text_upd(ALICE_TG, f"parkir 5rb {word}"), agent=FixedAgent(result))[0].text
    assert "Berhasil disimpan" in reply and "Dompet" in reply
    assert count(conn, "cash") == before + 1


def test_cash_word_without_cash_account_offers_to_create_it(alice, conn):
    result = {"intent": "record", "transactions": [
        {"description": "Parkir mobil", "amount_idr": 5000, "direction": "expense", "date": None, "account_hint": "cash"}]}
    before = count(conn)
    ask = bot.handle(alice, text_upd(ALICE_TG, "parkir 5rb cash"), agent=FixedAgent(result))[0].text
    assert "Belum disimpan" in ask and "Cash" in ask and "membuat account" in ask
    assert count(conn) == before
    assert alice.c.execute("SELECT count(*) FROM account WHERE tenant_id='alice' AND kind='cash'").fetchone()[0] == 0
    done = bot.handle(alice, text_upd(ALICE_TG, "Cash"), agent=FixedAgent(result))[0].text
    assert "Berhasil disimpan" in done and "Cash" in done
    assert alice.c.execute("SELECT count(*) FROM account WHERE tenant_id='alice' AND kind='cash'").fetchone()[0] == 1
    assert count(conn, "cash") == 1


def test_unrelated_reply_never_creates_a_cash_account(alice, conn):
    bot.handle(alice, text_upd(ALICE_TG, "bayar parkir mobil 5 ribu"), agent=FixedAgent(PARKIR))
    nothing = {"intent": "unknown", "transactions": []}
    bot.handle(alice, text_upd(ALICE_TG, "nanti saja"), agent=FixedAgent(nothing))
    assert alice.c.execute("SELECT count(*) FROM account WHERE tenant_id='alice' AND kind='cash'").fetchone()[0] == 0


GOPAY_20 = {"intent": "record", "transactions": [
    {"description": "Kopi", "amount_idr": 20000, "direction": "expense", "date": None, "account_hint": "GoPay"}]}


@pytest.mark.parametrize("message", ["gopay 20rb", "makan 45rb dari GoPay", "kopi 20 ribu gopay",
                                     "gopay 20k", "GoPay 1,5jt", "jangan gopay", "bukan gopay", "gopay 20.000"])
def test_new_transaction_while_draft_pending_is_not_taken_as_the_answer(alice, conn, message):
    bot.handle(alice, text_upd(ALICE_TG, "bayar parkir mobil 5 ribu"), agent=FixedAgent(PARKIR))
    agent = FixedAgent(GOPAY_20)
    bot.handle(alice, text_upd(ALICE_TG, message), agent=agent)
    assert agent.seen == [message]                       # the message went to the agent as a new message
    parkir = conn.execute("SELECT count(*) FROM txn WHERE tenant_id='alice' AND description LIKE 'PARKIR%'").fetchone()[0]
    assert parkir == 0                                   # the pending draft was not saved to GoPay


def test_cash_word_never_matches_a_non_cash_account_label(repo, conn):
    repo.add_member("carol@example.com", "carol")
    repo.add_account("carol", "ewallet", "ovo", "OVO Cashback")
    repo.add_account("carol", "bank", "bca", "BCA Kasir")
    bot.handle(repo, text_upd(ALICE_TG, f"/start {repo.create_link_code('carol@example.com')}"))
    for word in ("cash", "kas"):
        result = {"intent": "record", "transactions": [
            {"description": "Parkir", "amount_idr": 5000, "direction": "expense", "date": None, "account_hint": word}]}
        reply = bot.handle(repo, text_upd(ALICE_TG, f"parkir 5rb {word}"), agent=FixedAgent(result))[0].text
        assert "Berhasil disimpan" not in reply and "belum punya account cash" in reply
    assert conn.execute("SELECT count(*) FROM txn WHERE tenant_id='carol'").fetchone()[0] == 0


def test_cash_is_only_offered_when_the_message_asked_for_cash(alice, conn):
    bot.handle(alice, text_upd(ALICE_TG, "bayar parkir mobil 5 ribu"), agent=FixedAgent(PARKIR))   # no cash word
    nothing = {"intent": "unknown", "transactions": []}
    bot.handle(alice, text_upd(ALICE_TG, "cash"), agent=FixedAgent(nothing))
    assert alice.c.execute("SELECT count(*) FROM account WHERE tenant_id='alice' AND kind='cash'").fetchone()[0] == 0


def test_cash_account_is_not_left_behind_when_the_save_is_refused(alice, conn):
    future = {"intent": "record", "transactions": [
        {"description": "Parkir", "amount_idr": 5000, "direction": "expense", "date": "2099-01-01", "account_hint": "cash"}]}
    bot.handle(alice, text_upd(ALICE_TG, "parkir 5rb cash"), agent=FixedAgent(future))
    reply = bot.handle(alice, text_upd(ALICE_TG, "cash"), agent=FixedAgent(future))[0].text
    assert "Belum disimpan" in reply
    assert alice.c.execute("SELECT count(*) FROM account WHERE tenant_id='alice' AND kind='cash'").fetchone()[0] == 0


KOPI_BNI_PARKIR = {"intent": "record", "transactions": [
    {"description": "Kopi", "amount_idr": 20000, "direction": "expense", "date": None, "account_hint": "BNI"},
    {"description": "Parkir", "amount_idr": 5000, "direction": "expense", "date": None, "account_hint": None}]}


def _saved_to(conn):
    return dict(conn.execute("SELECT t.description, a.label FROM txn t JOIN account a ON a.id=t.account_id "
                             "WHERE t.tenant_id='alice' AND t.amount < 0").fetchall())


def test_reply_never_moves_a_row_whose_account_was_named(alice, conn):
    """User said BNI for kopi (no such account). Saku says so at once and keeps no draft a reply could redirect."""
    first = bot.handle(alice, text_upd(ALICE_TG, "kopi 20rb pakai BNI, parkir 5rb"),
                       agent=FixedAgent(KOPI_BNI_PARKIR))[0].text
    assert "Belum disimpan" in first and "BNI" in first
    bot.handle(alice, text_upd(ALICE_TG, "gopay"), agent=FixedAgent({"intent": "unknown", "transactions": []}))
    assert _saved_to(conn) == {}


def test_new_cash_account_takes_only_the_rows_that_said_cash(alice, conn):
    mixed = {"intent": "record", "transactions": [
        {"description": "Kopi", "amount_idr": 20000, "direction": "expense", "date": None, "account_hint": "BNI"},
        {"description": "Parkir", "amount_idr": 5000, "direction": "expense", "date": None, "account_hint": "cash"}]}
    first = bot.handle(alice, text_upd(ALICE_TG, "kopi 20rb BNI, parkir 5rb cash"), agent=FixedAgent(mixed))[0].text
    assert "BNI" in first
    bot.handle(alice, text_upd(ALICE_TG, "Cash"), agent=FixedAgent({"intent": "unknown", "transactions": []}))
    assert _saved_to(conn) == {}
    assert alice.c.execute("SELECT count(*) FROM account WHERE tenant_id='alice' AND kind='cash'").fetchone()[0] == 0


def test_cash_reply_creates_cash_only_for_rows_that_said_cash(alice, conn):
    mixed = {"intent": "record", "transactions": [
        {"description": "Kopi", "amount_idr": 20000, "direction": "expense", "date": None, "account_hint": "gopay"},
        {"description": "Parkir", "amount_idr": 5000, "direction": "expense", "date": None, "account_hint": "cash"}]}
    bot.handle(alice, text_upd(ALICE_TG, "kopi 20rb gopay, parkir 5rb cash"), agent=FixedAgent(mixed))
    bot.handle(alice, text_upd(ALICE_TG, "Cash"), agent=FixedAgent({"intent": "unknown", "transactions": []}))
    assert _saved_to(conn) == {"KOPI": "Alice GoPay", "PARKIR": "Cash"}


def test_answer_fills_rows_without_an_account(alice, conn):
    two = {"intent": "record", "transactions": [
        {"description": "Kopi", "amount_idr": 20000, "direction": "expense", "date": None, "account_hint": None},
        {"description": "Parkir", "amount_idr": 5000, "direction": "expense", "date": None, "account_hint": None}]}
    bot.handle(alice, text_upd(ALICE_TG, "kopi 20rb, parkir 5rb"), agent=FixedAgent(two))
    bot.handle(alice, text_upd(ALICE_TG, "gopay"), agent=FixedAgent({"intent": "unknown", "transactions": []}))
    assert _saved_to(conn) == {"KOPI": "Alice GoPay", "PARKIR": "Alice GoPay"}


@pytest.mark.parametrize("fragment", ["se", "pay", "an", "ba"])
def test_tiny_fragments_do_not_pick_an_account(alice, conn, fragment):
    bot.handle(alice, text_upd(ALICE_TG, "bayar parkir mobil 5 ribu"), agent=FixedAgent(PARKIR))
    bot.handle(alice, text_upd(ALICE_TG, fragment), agent=FixedAgent({"intent": "unknown", "transactions": []}))
    assert _saved_to(conn) == {}


def test_only_voice_notes_and_known_audio_types_are_downloaded(alice):
    files, stt = FakeFiles(), FakeTranscriber()
    for kind, mime in (("audio", "video/mp4"), ("audio", "application/octet-stream"), ("audio", None)):
        reply = bot.handle(alice, voice_upd(ALICE_TG, kind=kind, mime=mime), agent=FixedAgent(PARKIR_CASH),
                           transcriber=stt, files=files)[0].text
        assert "Belum disimpan" in reply
    assert files.asked == [] and stt.calls == []


# ---- Telegram file download -------------------------------------------------------------------------
def test_download_builds_file_url_and_enforces_size(monkeypatch):
    import io

    calls = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if "/getFile" in request.full_url:
            return Response(b'{"ok": true, "result": {"file_path": "voice/file_9.oga", "file_size": 10}}')
        return Response(b"x" * 10)

    monkeypatch.setattr(bot.urllib.request, "urlopen", fake_urlopen)
    api = bot.TelegramAPI("123:SYNTHETIC")
    assert api.download("F1", 10) == b"x" * 10
    assert calls[-1] == "https://api.telegram.org/file/bot123:SYNTHETIC/voice/file_9.oga"
    with pytest.raises(ValueError) as e:
        api.download("F1", 9)
    assert "SYNTHETIC" not in str(e.value)


@pytest.mark.parametrize("bad", ["../../bot123/getMe", "/etc/passwd", "voice/../../x", "http://evil/x"])
def test_download_refuses_suspicious_file_paths(monkeypatch, bad):
    import io
    import json as _json

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    def fake_urlopen(request, timeout):
        assert "/getFile" in request.full_url, "must not fetch a suspicious path"
        return Response(_json.dumps({"ok": True, "result": {"file_path": bad, "file_size": 1}}).encode())

    monkeypatch.setattr(bot.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ValueError):
        bot.TelegramAPI("123:SYNTHETIC").download("F1", 10)


def test_typing_indicator_is_not_sent_to_unlinked_or_group_chats(repo):
    sent = []

    class API:
        def call(self, method, **params):
            sent.append(method)
            if method == "getUpdates":
                return [voice_upd(999_000_999)]
            return {}

    def conn_factory():
        return repo.c

    class NoClose:
        def __init__(self, c):
            self.c = c

        def __getattr__(self, name):
            return getattr(self.c, name)

        def close(self):
            pass

    bot.poll_once(lambda: NoClose(repo.c), API(), None, agent=None, transcriber=FakeTranscriber())
    assert "sendChatAction" not in sent and "sendMessage" in sent

    sent.clear()
    group = voice_upd(999_000_999)
    group["message"]["chat"] = {"id": -100, "type": "group"}
    API.call = lambda self, method, **params: (sent.append(method), [group] if method == "getUpdates" else {})[1]
    bot.poll_once(lambda: NoClose(repo.c), API(), None, agent=None, transcriber=FakeTranscriber())
    assert "sendChatAction" not in sent


def test_typing_indicator_is_sent_to_a_linked_private_chat(alice):
    sent = []

    class API:
        def call(self, method, **params):
            sent.append(method)
            return [voice_upd(ALICE_TG)] if method == "getUpdates" else {}

        def download(self, file_id, max_bytes):
            return b"OggS fake opus"

    class NoClose:
        def __init__(self, c):
            self.c = c

        def __getattr__(self, name):
            return getattr(self.c, name)

        def close(self):
            pass

    bot.poll_once(lambda: NoClose(alice.c), API(), None, agent=None, transcriber=FakeTranscriber())
    assert "sendChatAction" in sent


def test_unlabelled_income_is_not_assumed_to_be_cash(alice, conn):
    alice.add_account("alice", "cash", "cash", "Cash")
    result = {"intent": "record", "transactions": [
        {"description": "Gaji", "amount_idr": 8_000_000, "direction": "income", "date": None, "account_hint": None}]}
    before = count(conn)
    reply = bot.handle(alice, text_upd(ALICE_TG, "gaji 8jt"), agent=FixedAgent(result))[0].text
    assert "Belum disimpan" in reply and "Account yang dipakai belum jelas" in reply
    assert count(conn) == before


def test_two_cash_accounts_still_ask(alice, conn):
    alice.add_account("alice", "cash", "cash", "Dompet")
    alice.add_account("alice", "cash", "cash", "Kas Toko")
    before = count(conn)
    reply = bot.handle(alice, text_upd(ALICE_TG, "parkir 5rb"), agent=FixedAgent(PARKIR))[0].text
    assert "Belum disimpan" in reply and "Dompet" in reply and "Kas Toko" in reply
    assert count(conn) == before


def test_named_non_cash_account_is_never_overridden_by_cash_default(alice, conn):
    alice.add_account("alice", "cash", "cash", "Cash")
    result = {"intent": "record", "transactions": [
        {"description": "Parkir", "amount_idr": 5000, "direction": "expense", "date": None, "account_hint": "GoPay"}]}
    reply = bot.handle(alice, text_upd(ALICE_TG, "parkir 5rb gopay"), agent=FixedAgent(result))[0].text
    assert "Berhasil disimpan" in reply and "Alice GoPay" in reply
    assert count(conn, "cash") == 0


def test_help_mentions_voice_and_cash(alice):
    text = bot.handle(alice, text_upd(ALICE_TG, "/help"))[0].text
    assert "voice" in text.lower() and "cash" in text.lower()
