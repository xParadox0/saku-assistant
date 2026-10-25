from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Mapping


def advance_schedule_date(current: date, cadence: str, *, anchor_day: int | None = None) -> date | None:
    """Return the next weekly/monthly date, or None at the calendar ceiling."""
    if cadence == "weekly":
        try:
            return current + timedelta(days=7)
        except OverflowError:
            return None
    if cadence != "monthly":
        raise ValueError(f"Unsupported schedule cadence: {cadence}")

    year = current.year + (current.month // 12)
    month = current.month % 12 + 1
    if year > date.max.year:
        return None
    day = min(anchor_day or current.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _installments_until(start: date, through: date, cadence: str, anchor_day: int) -> int:
    if start > through:
        return 0
    if cadence == "weekly":
        return (through - start).days // 7 + 1
    count = 0
    current = start
    while current <= through:
        count += 1
        next_date = advance_schedule_date(current, cadence, anchor_day=anchor_day)
        if next_date is None:
            break
        current = next_date
    return count


def project_goal(goal: Mapping[str, Any], today: date) -> dict[str, Any]:
    """Build a transparent, schedule-based summary for a virtual financial plan."""
    target = Decimal(goal["target"])
    saved = Decimal(goal["saved"])
    remaining = max(target - saved, Decimal("0.00"))
    status = str(goal["status"])
    next_contribution = goal.get("next_contribution_on")
    review_on = goal.get("review_on")
    next_actions = [day for day in (next_contribution, review_on) if day is not None]
    next_action = min(next_actions, default=None)

    if status == "paused":
        reminder_status = "paused"
    elif status == "completed":
        reminder_status = "completed"
    elif next_action is None:
        reminder_status = "none"
    elif next_action < today:
        reminder_status = "overdue"
    elif next_action == today:
        reminder_status = "due"
    elif next_action <= today + timedelta(days=7):
        reminder_status = "upcoming"
    else:
        reminder_status = "scheduled"

    projection_status = "no_target_date"
    projected_amount: Decimal | None = None
    contributions_remaining = 0
    due_on = goal.get("due_on")
    schedule_amount = goal.get("schedule_amount")
    cadence = goal.get("schedule_cadence")
    anchor_day = goal.get("schedule_day")

    if target <= 0:
        projection_status = "invalid_target"
    elif status == "paused":
        projection_status = "paused"
    elif status == "completed" or remaining == 0:
        projection_status = "complete"
        projected_amount = saved
    elif due_on is not None:
        if schedule_amount is None or cadence is None or next_contribution is None:
            projection_status = "no_schedule"
        else:
            projected_amount = saved
            if due_on >= today:
                start = next_contribution
                while start is not None and start < today:
                    start = advance_schedule_date(start, cadence, anchor_day=anchor_day)
                if start is not None:
                    contributions_remaining = _installments_until(
                        start, due_on, cadence, anchor_day or start.day
                    )
                projected_amount += Decimal(schedule_amount) * contributions_remaining
            projection_status = "on_track" if projected_amount >= target else "behind"

    return {
        "remaining": remaining,
        "projected_amount": projected_amount,
        "contributions_remaining": contributions_remaining,
        "projection_status": projection_status,
        "reminder_status": reminder_status,
        "next_action_on": next_action,
    }
