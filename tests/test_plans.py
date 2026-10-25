from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from saku.db import init_schema
from saku.normalise import MAX_LEDGER_AMOUNT
from saku.repo import Repo
from test_web import as_, client, csrf, seeded, upload_dir


def test_invalid_plan_post_renders_html_error(client, seeded, conn):
    headers = as_("alice@example.com")
    token = csrf(client, "alice@example.com")
    invalid = client.post("/app/plans", headers=headers, follow_redirects=False, data={
        "csrf": token, "name": "Bad plan", "target": "not-a-number",
    })
    assert invalid.status_code == 400
    assert invalid.headers["content-type"].startswith("text/html")
    assert "Plan request rejected" in invalid.text
    assert "Enter valid amounts and dates." in invalid.text

    created = client.post("/app/plans", headers=headers, follow_redirects=False, data={
        "csrf": token, "name": "Good plan", "target": "100",
    })
    assert created.status_code == 303
    goal_id = conn.execute("SELECT id FROM goal WHERE tenant_id='alice' AND name='Good plan'").fetchone()[0]
    bad_entry = client.post(f"/app/plans/{goal_id}/entries", headers=headers, follow_redirects=False, data={
        "csrf": token, "entry_type": "contribution", "amount": "bad",
        "booked_on": Repo(conn).tenant_today("alice").isoformat(),
    })
    assert bad_entry.status_code == 400
    assert bad_entry.headers["content-type"].startswith("text/html")
    assert "Plan request rejected" in bad_entry.text
    assert "Enter a valid amount and date." in bad_entry.text
    assert "ConversionSyntax" not in bad_entry.text
    assert conn.execute("SELECT saved FROM goal WHERE id=%s", (goal_id,)).fetchone()[0] == Decimal("0.00")


def test_plan_create_and_entries_do_not_write_to_transaction_ledger(client, seeded, conn):
    headers = as_("alice@example.com")
    token = csrf(client, "alice@example.com")
    repo = Repo(conn)
    today = repo.tenant_today("alice")
    due_on = today + timedelta(days=90)
    next_on = today + timedelta(days=30)
    txn_count = repo.txn_count("alice")
    response = client.post("/app/plans", headers=headers, follow_redirects=False, data={
        "csrf": token,
        "name": "Laptop reserve",
        "target": "1500",
        "due_on": due_on.isoformat(),
        "schedule_amount": "250",
        "schedule_cadence": "monthly",
        "next_contribution_on": next_on.isoformat(),
        "review_on": "",
    })
    assert response.status_code == 303
    goal_id = conn.execute("SELECT id FROM goal WHERE tenant_id='alice' AND name='Laptop reserve'").fetchone()[0]

    for kind, amount in (("contribution", "1000"), ("use", "100")):
        response = client.post(f"/app/plans/{goal_id}/entries", headers=headers, follow_redirects=False, data={
            "csrf": token, "entry_type": kind, "amount": amount,
            "booked_on": today.isoformat(), "note": f"{kind} note",
        })
        assert response.status_code == 303

    saved = conn.execute("SELECT saved FROM goal WHERE id=%s AND tenant_id='alice'", (goal_id,)).fetchone()[0]
    assert saved == Decimal("900.00")
    edited_due = today + timedelta(days=120)
    edited = client.post(f"/app/plans/{goal_id}/edit", headers=headers, follow_redirects=False, data={
        "csrf": token, "name": "Laptop and travel reserve", "target": "2000",
        "due_on": edited_due.isoformat(), "schedule_amount": "300",
        "schedule_cadence": "monthly", "next_contribution_on": next_on.isoformat(), "review_on": "",
    })
    assert edited.status_code == 303
    changed = conn.execute("SELECT name, target, saved FROM goal WHERE id=%s", (goal_id,)).fetchone()
    assert changed == ("Laptop and travel reserve", Decimal("2000.00"), Decimal("900.00"))
    assert repo.txn_count("alice") == txn_count
    assert conn.execute("SELECT COUNT(*) FROM goal_entry WHERE tenant_id='alice' AND goal_id=%s", (goal_id,)).fetchone()[0] == 2
    page = client.get("/app/plans", headers=headers).text
    assert "Laptop and travel reserve" in page and "Rp900" in page and "Rp1.100" in page
    exported = repo.export_tenant_data("alice", "alice@example.com")
    assert exported is not None
    assert any(g["id"] == goal_id for g in exported["goals"])
    assert len([e for e in exported["goal_entries"] if e["goal_id"] == goal_id]) == 2
    web_export = client.get("/app/settings/export.json", headers=as_("alice@example.com"))
    assert web_export.status_code == 200
    exported_rows = web_export.json()["data"]
    assert any(g["id"] == goal_id for g in exported_rows["goals"])
    assert len([e for e in exported_rows["goal_entries"] if e["goal_id"] == goal_id]) == 2


def test_plan_status_changes_and_tenant_boundary(client, seeded, conn):
    headers = as_("alice@example.com")
    token = csrf(client, "alice@example.com")
    created = client.post("/app/plans", headers=headers, follow_redirects=False, data={
        "csrf": token, "name": "Rainy day", "target": "100",
    })
    assert created.status_code == 303
    goal_id = conn.execute("SELECT id FROM goal WHERE tenant_id='alice' AND name='Rainy day'").fetchone()[0]
    for status in ("paused", "active", "completed"):
        response = client.post(f"/app/plans/{goal_id}/status", headers=headers, follow_redirects=False,
                               data={"csrf": token, "status": status})
        assert response.status_code == 303
        assert conn.execute("SELECT status FROM goal WHERE id=%s", (goal_id,)).fetchone()[0] == status
        if status == "paused":
            blocked = client.post(f"/app/plans/{goal_id}/entries", headers=headers, follow_redirects=False,
                                  data={"csrf": token, "entry_type": "contribution", "amount": "1",
                                        "booked_on": Repo(conn).tenant_today("alice").isoformat()})
            assert blocked.status_code == 400
            assert conn.execute("SELECT saved FROM goal WHERE id=%s", (goal_id,)).fetchone()[0] == Decimal("0.00")
        if status == "completed":
            page = client.get("/app/plans", headers=headers).text
            assert "This plan is marked complete. Reopen it to resume tracking activity." in page
            assert "Target reached. You can mark this plan complete when ready." not in page

    bob_id = conn.execute(
        "INSERT INTO goal (tenant_id, name, target, saved) VALUES ('bob', 'Private plan', 500, 0) RETURNING id"
    ).fetchone()[0]
    page = client.get("/app/plans", headers=headers).text
    assert "Private plan" not in page
    response = client.post(f"/app/plans/{bob_id}/entries", headers=headers, follow_redirects=False, data={
        "csrf": token, "entry_type": "contribution", "amount": "1",
        "booked_on": Repo(conn).tenant_today("alice").isoformat(),
    })
    assert response.status_code == 404


def test_plan_refuses_overdraw_and_missing_csrf(client, seeded, conn):
    missing_csrf = client.post("/app/plans", headers=as_("alice@example.com"), follow_redirects=False, data={
        "name": "No CSRF", "target": "100",
    })
    assert missing_csrf.status_code == 403
    token = csrf(client, "alice@example.com")
    created = client.post("/app/plans", headers=as_("alice@example.com"), follow_redirects=False, data={
        "csrf": token, "name": "Rainy day", "target": "100",
    })
    goal_id = conn.execute("SELECT id FROM goal WHERE tenant_id='alice' AND name='Rainy day'").fetchone()[0]
    response = client.post(f"/app/plans/{goal_id}/entries", headers=as_("alice@example.com"), follow_redirects=False, data={
        "csrf": token, "entry_type": "use", "amount": "1",
        "booked_on": Repo(conn).tenant_today("alice").isoformat(),
    })
    assert response.status_code == 400
    assert conn.execute("SELECT saved FROM goal WHERE id=%s", (goal_id,)).fetchone()[0] == Decimal("0.00")


def test_plan_activity_rejects_amount_above_numeric_limit(client, seeded, conn):
    repo = Repo(conn)
    today = repo.tenant_today("alice")
    goal_id = repo.create_goal("alice", "Bounded plan", Decimal("500"))
    token = csrf(client, "alice@example.com")

    response = client.post(
        f"/app/plans/{goal_id}/entries", headers=as_("alice@example.com"), follow_redirects=False,
        data={"csrf": token, "entry_type": "contribution", "amount": "10000000000000000.00",
              "booked_on": today.isoformat(), "note": "oversized"},
    )

    assert response.status_code == 400
    assert conn.execute("SELECT saved FROM goal WHERE id=%s", (goal_id,)).fetchone()[0] == Decimal("0.00")
    assert conn.execute("SELECT COUNT(*) FROM goal_entry WHERE goal_id=%s", (goal_id,)).fetchone()[0] == 0


def test_plan_activity_rejects_balance_above_numeric_limit(client, seeded, conn):
    repo = Repo(conn)
    today = repo.tenant_today("alice")
    goal_id = repo.create_goal("alice", "Balance boundary", Decimal("9999999999999999.99"))
    initial_saved = Decimal("9999999999999989.99")
    assert repo.record_goal_entry("alice", goal_id, "contribution", initial_saved, today, "initial")
    token = csrf(client, "alice@example.com")

    response = client.post(
        f"/app/plans/{goal_id}/entries", headers=as_("alice@example.com"), follow_redirects=False,
        data={"csrf": token, "entry_type": "contribution", "amount": "20.00",
              "booked_on": today.isoformat(), "note": "over balance"},
    )

    assert response.status_code == 400
    assert conn.execute("SELECT saved FROM goal WHERE id=%s", (goal_id,)).fetchone()[0] == initial_saved
    assert conn.execute("SELECT COUNT(*) FROM goal_entry WHERE goal_id=%s", (goal_id,)).fetchone()[0] == 1


def test_recording_last_calendar_installment_clears_next_date(conn, seeded):
    repo = Repo(conn)
    goal_id = repo.create_goal(
        "alice", "Calendar ceiling", Decimal("100.00"), due_on=date.max,
        schedule_amount=Decimal("20.00"), schedule_cadence="monthly", next_contribution_on=date.max,
    )

    with patch.object(repo, "tenant_today", return_value=date.max):
        assert repo.record_goal_entry("alice", goal_id, "contribution", Decimal("20.00"), date.max, "final")
    schedule = conn.execute(
        "SELECT schedule_amount, schedule_cadence, schedule_day, next_contribution_on FROM goal WHERE id=%s",
        (goal_id,),
    ).fetchone()
    assert schedule == (None, None, None, None)


def test_edit_keeps_monthly_anchor_after_short_month_clamping(conn, seeded):
    repo = Repo(conn)
    january = date(2027, 1, 31)
    goal_id = repo.create_goal(
        "alice", "Anchor test", Decimal("500.00"), schedule_amount=Decimal("50.00"),
        schedule_cadence="monthly", next_contribution_on=january,
    )

    with patch.object(repo, "tenant_today", return_value=january):
        assert repo.record_goal_entry("alice", goal_id, "contribution", Decimal("50.00"), january, "scheduled")

    february = date(2027, 2, 28)
    assert conn.execute("SELECT schedule_day, next_contribution_on FROM goal WHERE id=%s", (goal_id,)).fetchone() == (31, february)
    assert repo.update_goal(
        "alice", goal_id, "Renamed plan", Decimal("500.00"), schedule_amount=Decimal("50.00"),
        schedule_cadence="monthly", next_contribution_on=february,
    )
    assert conn.execute("SELECT schedule_day FROM goal WHERE id=%s", (goal_id,)).fetchone()[0] == 31


@pytest.mark.parametrize("date_field", ("due_on", "next_contribution_on", "review_on"))
@pytest.mark.parametrize("date_value", ("0001-01-01", "9999-12-31"))
def test_plan_form_rejects_dates_outside_supported_horizon(client, seeded, date_field, date_value):
    schedule = {"schedule_amount": "10", "schedule_cadence": "weekly"} if date_field == "next_contribution_on" else {}
    response = client.post("/app/plans", headers=as_("alice@example.com"), follow_redirects=False, data={
        "csrf": csrf(client, "alice@example.com"), "name": "Far date", "target": "100",
        date_field: date_value, **schedule,
    })
    assert response.status_code == 400


def test_plan_repository_rejects_amounts_above_numeric_limit(conn, seeded):
    repo = Repo(conn)
    oversized = MAX_LEDGER_AMOUNT + Decimal("0.01")
    goal_id = repo.create_goal("alice", "Bounded", Decimal("100.00"))

    with pytest.raises(ValueError, match="supported range"):
        repo.create_goal("alice", "Too large", oversized)
    with pytest.raises(ValueError, match="supported range"):
        repo.create_goal("alice", "Schedule too large", Decimal("100"),
                         schedule_amount=oversized, schedule_cadence="weekly",
                         next_contribution_on=date(2026, 10, 26))
    with pytest.raises(ValueError, match="supported range"):
        repo.update_goal("alice", goal_id, "Bounded", oversized)
    with pytest.raises(ValueError, match="supported range"):
        repo.update_goal("alice", goal_id, "Bounded", Decimal("100"),
                         schedule_amount=oversized, schedule_cadence="weekly",
                         next_contribution_on=date(2026, 10, 26))


def test_plan_page_requires_membership(client):
    response = client.get("/app/plans", headers=as_("stranger@example.com"), follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/app/register"


def test_plan_page_handles_legacy_zero_target(client, seeded, conn):
    conn.execute(
        "INSERT INTO goal (tenant_id, name, target, saved) VALUES ('alice', 'Legacy plan', 0, 0)"
    )

    response = client.get("/app/plans", headers=as_("alice@example.com"))

    assert response.status_code == 200
    assert "Needs review." in response.text


def test_goal_schema_backfills_legacy_balance_once(conn, seeded):
    with conn.transaction():
        conn.execute("SET LOCAL TIME ZONE 'Pacific/Kiritimati'")
        conn.execute("UPDATE tenant SET tz='Pacific/Pago_Pago' WHERE id='alice'")
        tenant_today = conn.execute(
            "SELECT (now() AT TIME ZONE 'Pacific/Pago_Pago')::date"
        ).fetchone()[0]
        legacy_id = conn.execute(
            "INSERT INTO goal (tenant_id, name, target, saved) VALUES ('alice', 'Existing goal', 1000, 275) RETURNING id"
        ).fetchone()[0]
        init_schema(conn)
        entries = conn.execute(
            "SELECT entry_type, amount, booked_on FROM goal_entry WHERE goal_id=%s AND tenant_id='alice'",
            (legacy_id,),
        ).fetchall()
        assert entries == [("opening", Decimal("275.00"), tenant_today)]
        init_schema(conn)
        count = conn.execute("SELECT COUNT(*) FROM goal_entry WHERE goal_id=%s", (legacy_id,)).fetchone()[0]
        assert count == 1


def test_schema_restart_does_not_backfill_new_plan_activity_as_opening(conn, seeded):
    repo = Repo(conn)
    today = repo.tenant_today("alice")
    goal_id = repo.create_goal("alice", "Post-migration plan", Decimal("500"))
    assert repo.record_goal_entry("alice", goal_id, "contribution", Decimal("125"), today, "first")

    init_schema(conn)

    entries = conn.execute(
        "SELECT entry_type, amount FROM goal_entry WHERE goal_id=%s AND tenant_id='alice' ORDER BY id",
        (goal_id,),
    ).fetchall()
    assert entries == [("contribution", Decimal("125.00"))]
    saved = conn.execute("SELECT saved FROM goal WHERE id=%s AND tenant_id='alice'", (goal_id,)).fetchone()[0]
    assert saved == Decimal("125.00")


def test_tenant_delete_removes_plan_and_activity_rows(conn, seeded):
    repo = Repo(conn)
    today = repo.tenant_today("alice")
    goal_id = repo.create_goal("alice", "Delete with tenant", Decimal("500"))
    assert repo.record_goal_entry("alice", goal_id, "contribution", Decimal("50"), today, "seed")
    assert conn.execute("SELECT COUNT(*) FROM goal_entry WHERE goal_id=%s", (goal_id,)).fetchone()[0] == 1
    assert repo.delete_tenant_data("alice", "alice@example.com") is True
    assert conn.execute("SELECT COUNT(*) FROM goal WHERE tenant_id='alice'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM goal_entry WHERE tenant_id='alice'").fetchone()[0] == 0
