from datetime import date
from decimal import Decimal

from saku.planning import advance_schedule_date, project_goal


def test_monthly_schedule_keeps_the_original_day_after_short_months():
    february = advance_schedule_date(date(2026, 1, 31), "monthly", anchor_day=31)
    march = advance_schedule_date(february, "monthly", anchor_day=31)
    assert february == date(2026, 2, 28)
    assert march == date(2026, 3, 31)


def test_weekly_schedule_at_calendar_ceiling_has_no_next_date():
    assert advance_schedule_date(date(9999, 12, 31), "weekly") is None


def test_projection_counts_scheduled_contributions_through_target_date():
    plan = {
        "target": Decimal("100.00"),
        "saved": Decimal("25.00"),
        "due_on": date(2027, 1, 31),
        "status": "active",
        "schedule_amount": Decimal("25.00"),
        "schedule_cadence": "monthly",
        "schedule_day": 30,
        "next_contribution_on": date(2026, 11, 30),
        "review_on": None,
    }
    view = project_goal(plan, date(2026, 10, 25))
    assert view["projection_status"] == "on_track"
    assert view["projected_amount"] == Decimal("100.00")
    assert view["contributions_remaining"] == 3
    assert view["remaining"] == Decimal("75.00")


def test_projection_says_behind_when_schedule_cannot_reach_target():
    plan = {
        "target": Decimal("100.00"),
        "saved": Decimal("25.00"),
        "due_on": date(2026, 12, 31),
        "status": "active",
        "schedule_amount": Decimal("25.00"),
        "schedule_cadence": "monthly",
        "schedule_day": 30,
        "next_contribution_on": date(2026, 11, 30),
        "review_on": None,
    }
    view = project_goal(plan, date(2026, 10, 25))
    assert view["projection_status"] == "behind"
    assert view["projected_amount"] == Decimal("75.00")
    assert view["contributions_remaining"] == 2


def test_overdue_schedule_is_visible_but_pause_suppresses_reminder():
    plan = {
        "target": Decimal("100.00"),
        "saved": Decimal("0.00"),
        "due_on": None,
        "status": "active",
        "schedule_amount": Decimal("20.00"),
        "schedule_cadence": "weekly",
        "schedule_day": 1,
        "next_contribution_on": date(2026, 10, 18),
        "review_on": None,
    }
    assert project_goal(plan, date(2026, 10, 25))["reminder_status"] == "overdue"
    plan["status"] = "paused"
    assert project_goal(plan, date(2026, 10, 25))["reminder_status"] == "paused"


def test_earliest_review_date_is_visible_when_contribution_is_later():
    plan = {
        "target": Decimal("100.00"), "saved": Decimal("0.00"), "due_on": None,
        "status": "active", "schedule_amount": Decimal("20.00"),
        "schedule_cadence": "monthly", "schedule_day": 30,
        "next_contribution_on": date(2026, 11, 30), "review_on": date(2026, 10, 20),
    }
    view = project_goal(plan, date(2026, 10, 25))
    assert view["next_action_on"] == date(2026, 10, 20)
    assert view["reminder_status"] == "overdue"


def test_unscheduled_goal_has_no_false_projection_or_reminder():
    plan = {
        "target": Decimal("100.00"),
        "saved": Decimal("10.00"),
        "due_on": date(2027, 1, 1),
        "status": "active",
        "schedule_amount": None,
        "schedule_cadence": None,
        "schedule_day": None,
        "next_contribution_on": None,
        "review_on": None,
    }
    view = project_goal(plan, date(2026, 10, 25))
    assert view["projection_status"] == "no_schedule"
    assert view["projected_amount"] is None
    assert view["reminder_status"] == "none"


def test_projection_at_calendar_ceiling_stops_after_last_monthly_installment():
    plan = {
        "target": Decimal("100.00"),
        "saved": Decimal("0.00"),
        "due_on": date(9999, 12, 31),
        "status": "active",
        "schedule_amount": Decimal("20.00"),
        "schedule_cadence": "monthly",
        "schedule_day": 31,
        "next_contribution_on": date(9999, 11, 30),
        "review_on": None,
    }
    view = project_goal(plan, date(9999, 11, 30))
    assert view["contributions_remaining"] == 2
    assert view["projected_amount"] == Decimal("40.00")
