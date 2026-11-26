"""IST office-hour windows for staff reply-time metrics (Asia/Kolkata)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Mon–Fri 09:30–17:30 IST; Saturday 09:30–17:00 IST; Sunday = all off.


def office_window_for_date(d: date) -> tuple[datetime, datetime] | None:
    wd = d.weekday()
    if wd == 6:
        return None
    if wd == 5:
        end_t = time(17, 0)
    else:
        end_t = time(17, 30)
    start = datetime.combine(d, time(9, 30), tzinfo=IST)
    end = datetime.combine(d, end_t, tzinfo=IST)
    return start, end


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def split_office_off_seconds(awaiting_at: datetime, replied_at: datetime) -> tuple[int, int, int]:
    """Wall-clock total plus seconds that fell inside vs outside office windows (IST)."""
    start = _utc(awaiting_at)
    end = _utc(replied_at)
    total = int(max(0, (end - start).total_seconds()))
    if total == 0:
        return 0, 0, 0

    office = 0
    ist_start = start.astimezone(IST)
    ist_end = end.astimezone(IST)
    day = ist_start.date()
    end_day = ist_end.date()
    one = timedelta(days=1)

    while day <= end_day:
        window = office_window_for_date(day)
        if window:
            w0, w1 = window
            seg0 = max(start, w0)
            seg1 = min(end, w1)
            if seg1 > seg0:
                office += int((seg1 - seg0).total_seconds())
        day += one

    off = max(0, total - office)
    return total, office, off