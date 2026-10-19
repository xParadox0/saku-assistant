"""Web <-> Telegram linking. The negatives matter most: unlinked senders get nothing, codes are single-use,
expire, are stored only as hashes, and one member's code can never attach to another member's tenant."""
from datetime import date
from decimal import Decimal

import psycopg
import pytest
from fastapi.testclient import TestClient

from saku import bot
from saku.normalise import RawTxn
from saku.repo import Repo, _sha
from saku.web.app import create_app

SECRET = "y" * 40
ALICE_TG, BOB_TG, STRANGER_TG = 111_000_111, 222_000_222, 999_000_999


class FixedAgent:
    def __init__(self, result):
        self.result = result

    def extract(self, text, accounts, today):
        return self.result


def upd(uid, text, chat_type="private", is_bot=False):
    return {"update_id": 1, "message": {"message_id": 1, "text": text,
            "from": {"id": uid, "is_bot": is_bot}, "chat": {"id": uid if chat_type == "private" else -100, "type": chat_type}}}


@pytest.fixture
def two(repo):
    repo.add_member("alice@example.com", "alice")
    repo.add_member("bob@example.com", "bob")
    a = repo.add_account("alice", "bank", "bank_a", "Alice Main")
    repo.import_rows("alice", a, [RawTxn(date(2026, 9, 1), Decimal(5_000_000), "ALICE SALARY")], "csv", "t")
    return repo


def test_unlinked_sender_is_refused(two):
    r = bot.handle(two, upd(STRANGER_TG, "makan 45rb gopay"))
    assert len(r) == 1 and "not connected" in r[0].text
    assert two.telegram_identity(STRANGER_TG) is None


def test_link_then_identity_resolves_to_right_tenant(two):
    code = two.create_link_code("alice@example.com")
    r = bot.handle(two, upd(ALICE_TG, f"/start {code}"))
    assert "Saku berhasil terhubung sebagai alice@example.com" in r[0].text
    assert two.telegram_identity(ALICE_TG) == {"email": "alice@example.com", "tenant_id": "alice", "role": "user"}
    assert "alice@example.com" in bot.handle(two, upd(ALICE_TG, "/status"))[0].text


def test_first_start_explains_onboarding_safety_linking_commands_and_next_step(two):
    unlinked = bot.handle(two, upd(STRANGER_TG, "/start"))[0].text
    assert "Saku" in unlinked
    assert "password bank, PIN, atau OTP" in unlinked
    assert "Settings" in unlinked and "Connect Telegram" in unlinked
    assert "/status" in unlinked and "/help" in unlinked and "/unlink" in unlinked

    linked = bot.handle(two, upd(ALICE_TG, f"/start {two.create_link_code('alice@example.com')}"))[0].text
    assert "berhasil terhubung" in linked.lower()
    assert "makan 45rb" in linked.lower()
    assert "account" in linked.lower()


def test_code_is_single_use(two):
    code = two.create_link_code("alice@example.com")
    bot.handle(two, upd(ALICE_TG, f"/start {code}"))
    r = bot.handle(two, upd(STRANGER_TG, f"/start {code}"))
    assert "kedaluwarsa atau pernah dipakai" in r[0].text
    assert two.telegram_identity(STRANGER_TG) is None


def test_expired_code_is_refused(two, conn):
    code = two.create_link_code("alice@example.com")
    conn.execute("UPDATE link_code SET expires_at = now() - interval '1 second'")
    assert two.redeem_link_code(code, ALICE_TG) == ("invalid", None)


def test_new_code_cancels_older_code(two):
    old = two.create_link_code("alice@example.com")
    new = two.create_link_code("alice@example.com")
    assert two.redeem_link_code(old, ALICE_TG)[0] == "invalid"
    assert two.redeem_link_code(new, ALICE_TG)[0] == "linked"


def test_code_stored_only_as_hash(two, conn):
    code = two.create_link_code("alice@example.com")
    rows = conn.execute("SELECT code_hash FROM link_code").fetchall()
    assert rows == [(_sha(code),)] and code not in str(rows)


def test_guessing_codes_reveals_nothing(two):
    for guess in ("", "x", "a" * 64, "a" * 200, "../../etc", "' OR 1=1 --"):
        assert two.redeem_link_code(guess, STRANGER_TG) == ("invalid", None)


def test_telegram_account_cannot_be_taken_over(two):
    bot.handle(two, upd(ALICE_TG, f"/start {two.create_link_code('alice@example.com')}"))
    bob_code = two.create_link_code("bob@example.com")
    r = bot.handle(two, upd(ALICE_TG, f"/start {bob_code}"))
    assert "sudah terhubung ke akun Saku lain" in r[0].text
    assert two.telegram_identity(ALICE_TG)["tenant_id"] == "alice"
    assert two.redeem_link_code(bob_code, BOB_TG)[0] == "linked"  # bob's code was not burned


def test_relink_replaces_old_device(two):
    bot.handle(two, upd(ALICE_TG, f"/start {two.create_link_code('alice@example.com')}"))
    new_phone = 333_000_333
    r = bot.handle(two, upd(new_phone, f"/start {two.create_link_code('alice@example.com')}"))
    assert "Telegram sebelumnya sudah diputus" in r[0].text
    assert two.telegram_identity(ALICE_TG) is None
    assert two.telegram_identity(new_phone)["email"] == "alice@example.com"


def test_unlink_from_bot(two):
    bot.handle(two, upd(ALICE_TG, f"/start {two.create_link_code('alice@example.com')}"))
    bot.handle(two, upd(ALICE_TG, "/unlink"))
    assert two.telegram_identity(ALICE_TG) is None
    assert "not connected" in bot.handle(two, upd(ALICE_TG, "/status"))[0].text


def test_chat_records_two_expenses_and_confirms_what_was_saved(two, conn):
    bot.handle(two, upd(ALICE_TG, f"/start {two.create_link_code('alice@example.com')}"))
    before = conn.execute("SELECT count(*) FROM txn WHERE tenant_id = 'alice'").fetchone()[0]
    agent = FixedAgent({
        "intent": "record",
        "transactions": [
            {"description": "Gorengan", "amount_idr": 5000, "direction": "expense", "date": None,
             "account_hint": "Alice Main"},
            {"description": "Air mineral", "amount_idr": 5000, "direction": "expense", "date": None,
             "account_hint": "Alice Main"},
        ],
    })

    reply = bot.handle(two, upd(ALICE_TG, "Beli gorengan 5rb dan air mineral 5rb"), agent=agent)[0].text

    rows = conn.execute("SELECT amount, description FROM txn WHERE tenant_id = 'alice' ORDER BY id DESC LIMIT 2").fetchall()
    assert conn.execute("SELECT count(*) FROM txn WHERE tenant_id = 'alice'").fetchone()[0] == before + 2
    assert rows == [(Decimal("-5000.00"), "AIR MINERAL"), (Decimal("-5000.00"), "GORENGAN")]
    assert "Berhasil disimpan" in reply
    assert "2 transaksi" in reply
    assert "Rp10.000" in reply
    assert "Alice Main" in reply
    assert "hari ini" in reply


def test_chat_asks_for_account_then_saves_after_the_follow_up(two, conn):
    two.add_account("alice", "cash", "cash", "Cash")
    bot.handle(two, upd(ALICE_TG, f"/start {two.create_link_code('alice@example.com')}"))
    before = conn.execute("SELECT count(*) FROM txn WHERE tenant_id = 'alice'").fetchone()[0]
    agent = FixedAgent({
        "intent": "record",
        "transactions": [
            {"description": "Gorengan", "amount_idr": 5000, "direction": "expense", "date": None,
             "account_hint": None},
            {"description": "Air mineral", "amount_idr": 5000, "direction": "expense", "date": None,
             "account_hint": None},
        ],
    })

    missing = bot.handle(two, upd(ALICE_TG, "Beli gorengan 5rb dan air mineral 5rb"), agent=agent)[0].text
    assert conn.execute("SELECT count(*) FROM txn WHERE tenant_id = 'alice'").fetchone()[0] == before
    assert "Belum disimpan" in missing
    assert "account" in missing.lower()
    assert "Alice Main" in missing and "Cash" in missing

    saved = bot.handle(two, upd(ALICE_TG, "Alice Main"), agent=agent)[0].text
    assert conn.execute("SELECT count(*) FROM txn WHERE tenant_id = 'alice'").fetchone()[0] == before + 2
    assert "Berhasil disimpan" in saved


def test_chat_refuses_incomplete_or_unavailable_agent_without_silent_loss(two, conn):
    bot.handle(two, upd(ALICE_TG, f"/start {two.create_link_code('alice@example.com')}"))
    before = conn.execute("SELECT count(*) FROM txn WHERE tenant_id = 'alice'").fetchone()[0]
    incomplete = FixedAgent({
        "intent": "record",
        "transactions": [{"description": "Gorengan", "amount_idr": None, "direction": "expense",
                          "date": None, "account_hint": "Alice Main"}],
    })

    missing = bot.handle(two, upd(ALICE_TG, "Beli gorengan"), agent=incomplete)[0].text
    unavailable = bot.handle(two, upd(ALICE_TG, "Beli gorengan 5rb"), agent=None)[0].text

    assert conn.execute("SELECT count(*) FROM txn WHERE tenant_id = 'alice'").fetchone()[0] == before
    assert "Belum disimpan" in missing and "nominal" in missing.lower()
    assert "tidak ada data yang disimpan" in unavailable.lower()


def test_group_chats_and_bots_are_ignored(two):
    bot.handle(two, upd(ALICE_TG, f"/start {two.create_link_code('alice@example.com')}"))
    assert "private chat" in bot.handle(two, upd(ALICE_TG, "/status", chat_type="group"))[0].text
    assert bot.handle(two, upd(ALICE_TG, "/status", is_bot=True)) == []


def test_unknown_member_cannot_get_code(repo):
    with pytest.raises(ValueError):
        repo.create_link_code("nobody@example.com")


# ---- dashboard side ---------------------------------------------------------------------------------
class HeaderAuth:
    def identify(self, headers):
        from saku.web.auth import Identity
        return Identity(headers.get("x-test-email", ""))


@pytest.fixture
def web(two, db_url, tmp_path):
    app = create_app(lambda: psycopg.connect(db_url, autocommit=True), HeaderAuth(), SECRET, str(tmp_path),
                     bot_username="Saku_Test_Bot")
    return TestClient(app)


def _csrf(c, email):
    page = c.get("/app/settings", headers={"x-test-email": email}).text
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def test_settings_makes_deep_link_and_bot_redeems_it(web, two):
    h = {"x-test-email": "alice@example.com"}
    page = web.post("/app/settings/telegram/code", data={"csrf": _csrf(web, "alice@example.com")}, headers=h).text
    url = page.split('href="https://t.me/Saku_Test_Bot?start=', 1)[1].split('"', 1)[0]
    assert bot.handle(two, upd(ALICE_TG, f"/start {url}"))[0].text.startswith("Saku berhasil terhubung sebagai alice@example.com")
    assert "Connected since" in web.get("/app/settings", headers=h).text


def test_code_endpoint_requires_csrf(web):
    r = web.post("/app/settings/telegram/code", data={"csrf": "nope"}, headers={"x-test-email": "alice@example.com"})
    assert r.status_code == 403 and "t.me/" not in r.text


def test_web_unlink(web, two):
    bot.handle(two, upd(ALICE_TG, f"/start {two.create_link_code('alice@example.com')}"))
    h = {"x-test-email": "alice@example.com"}
    r = web.post("/app/settings/telegram/unlink", data={"csrf": _csrf(web, "alice@example.com")}, headers=h)
    assert "Telegram disconnected" in r.text and two.telegram_identity(ALICE_TG) is None


def test_admin_sees_link_count_not_telegram_ids(two, db_url, tmp_path):
    two.add_member("ops@example.com", "ops", "admin")
    bot.handle(two, upd(ALICE_TG, f"/start {two.create_link_code('alice@example.com')}"))
    app = create_app(lambda: psycopg.connect(db_url, autocommit=True), HeaderAuth(), SECRET, str(tmp_path))
    page = TestClient(app).get("/app/admin", headers={"x-test-email": "ops@example.com"}).text
    assert str(ALICE_TG) not in page


def test_bad_bot_username_refused_at_startup(db_url, tmp_path):
    with pytest.raises(SystemExit):
        create_app(lambda: psycopg.connect(db_url, autocommit=True), HeaderAuth(), SECRET, str(tmp_path),
                   bot_username="bad name\"><script>")
