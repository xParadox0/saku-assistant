"""Plain-language observations for the overview page.

Every number comes from Repo queries; this module only compares and phrases them. Rules are deterministic,
so the same data always gives the same sentences, and each sentence names the figure it is based on.
No advice about investments, loans or specific financial products.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .categorise import label
from .format import rupiah


@dataclass
class Insight:
    tone: str      # good | watch | info
    icon: str      # sprite id
    title: str
    body: str


def build(cf: dict, prev: dict | None, cats: list[dict], prev_cats: list[dict], recurring: list[dict],
          daily: list[dict]) -> list[Insight]:
    out: list[Insight] = []
    income, spending, net = cf["income"], cf["spending"], cf["net"]

    # 1. the month in one line
    if income > 0 and net >= 0:
        pct = int((net / income * 100).to_integral_value())
        out.append(Insight("good", "i-spark", f"You kept {pct}% of your income",
                           f"{rupiah(net)} left after {rupiah(spending)} of spending."))
    elif net < 0:
        out.append(Insight("watch", "i-alert", "Spending was higher than income",
                           f"The gap is {rupiah(-net)}. Check the biggest categories below."))

    # 2. the category that moved the most against last month
    if prev_cats:
        before = {c["category"]: c["amount"] for c in prev_cats}
        moves = [(c["amount"] - before.get(c["category"], Decimal(0)), c) for c in cats if c["category"] != "other"]
        moves = [m for m in moves if abs(m[0]) >= Decimal(50_000)]
        if moves:
            delta, c = max(moves, key=lambda m: abs(m[0]))
            name = label(c["category"])[0]
            if delta > 0:
                out.append(Insight("watch", label(c["category"])[1], f"{name} went up {rupiah(delta)}",
                                   f"{rupiah(c['amount'])} this month across {c['count']} payments."))
            else:
                out.append(Insight("good", label(c["category"])[1], f"{name} went down {rupiah(-delta)}",
                                   f"{rupiah(c['amount'])} this month, down from {rupiah(before[c['category']])}."))

    # 3. recurring charges
    if recurring:
        total = sum((r["average"] for r in recurring), Decimal(0))
        names = ", ".join(r["description"].title() for r in recurring[:3])
        out.append(Insight("info", "i-repeat", f"{len(recurring)} charges repeat every month",
                           f"About {rupiah(total)} a month: {names}. Still using all of them?"))

    # 4. small payments that add up
    small = [c for c in cats if c["category"] in ("food", "transport") and c["count"] >= 8]
    if small:
        c = max(small, key=lambda x: x["count"])
        avg = (c["amount"] / c["count"]).to_integral_value()
        out.append(Insight("info", label(c["category"])[1], f"{c['count']} small {label(c['category'])[0].lower()} payments",
                           f"Around {rupiah(avg)} each, {rupiah(c['amount'])} in total."))

    # 5. the heaviest day
    if daily:
        top = max(daily, key=lambda d: d["amount"])
        if spending > 0 and top["amount"] >= spending * Decimal("0.15"):
            out.append(Insight("info", "i-calendar", f"Biggest spending day: {top['day']:%-d %B}",
                               f"{rupiah(top['amount'])}, {int((top['amount'] / spending * 100).to_integral_value())}% of the month."))
    return out[:4]


def greeting(now_hour: int) -> str:
    if now_hour < 11:
        return "Good morning"
    if now_hour < 15:
        return "Good afternoon"
    if now_hour < 19:
        return "Good evening"
    return "Good night"


def month_progress(month_start: date, last_booked: date | None) -> tuple[int, int]:
    """(days covered by data, days in month). Uses the latest transaction date, not today's date."""
    days = calendar.monthrange(month_start.year, month_start.month)[1]
    covered = last_booked.day if last_booked and last_booked.month == month_start.month else days
    return covered, days
