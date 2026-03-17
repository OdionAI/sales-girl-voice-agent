"""
Shared business logic for the salon agents.
Pure helpers - no user-facing strings, no tools.
"""
from datetime import date, datetime, time, timedelta

OPEN_HOUR = 9
CLOSE_HOUR = 18
SLOT_MINUTES = 60


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def parse_time(value: str) -> time:
    return time.fromisoformat(value)


def business_hours_for(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time(hour=OPEN_HOUR, minute=0))
    end = datetime.combine(d, time(hour=CLOSE_HOUR, minute=0))
    return start, end


def slot_range_dt(
    d: date,
    t: time,
    duration_minutes: int,
) -> tuple[datetime, datetime]:
    start = datetime.combine(d, t)
    return start, start + timedelta(minutes=duration_minutes)


def overlaps_dt(
    a_start: datetime,
    a_end: datetime,
    b_start: datetime,
    b_end: datetime,
) -> bool:
    return a_start < b_end and b_start < a_end
