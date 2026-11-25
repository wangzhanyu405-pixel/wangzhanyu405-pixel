from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class TermDates:
    due_date: date
    grace_deadline: date


def add_months(value: date, months: int) -> date:
    """Add calendar months and clamp overflow days to the destination month end."""
    month_index = value.month - 1 + int(months)
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(value.day, last_day))


def month_end(value: date) -> date:
    last_day = calendar.monthrange(value.year, value.month)[1]
    return date(value.year, value.month, last_day)


def calculate_terms(
    invoice_date: date,
    term_days: int | None = None,
    *,
    term_months: int | None = None,
    grace_months: int | None = None,
) -> TermDates:
    if term_days is None:
        term_days = int(term_months or 0) * 30
    due_date = invoice_date + timedelta(days=max(0, int(term_days or 0)))
    return TermDates(due_date=due_date, grace_deadline=due_date)
