"""Demo scheduling: defensive parsing of "Tuesday 10am", validation against business hours, notice and confirmed
demos, and alternative slots. Times here are naive local clock times in the business time zone (Africa/Lagos)."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import business_facts as bf
from .models import DemoRequest

DEFAULT_DURATION = 30  # minutes, while DEMO_DURATION_MINUTES is still TODO
DEFAULT_NOTICE_HOURS = 2  # while DEMO_NOTICE_HOURS is still TODO
ASK_TIME = "Which day and time would suit you for the demo? For example Tuesday at 10am."

_WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2, "weds": 2,
             "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
             "sunday": 6, "sun": 6}
_DAY_RE = re.compile(r"\b(?:(next)\s+)?(%s)\b" % "|".join(sorted(_WEEKDAYS, key=len, reverse=True)), re.I)
_TIME_RE = re.compile(r"(?:\bat\s+|\b)(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)(?![a-z])"
                      r"|\b([01]?\d|2[0-3]):([0-5]\d)\b|\bat\s+(\d{1,2})\b(?![:\d]|\s*(?:am|pm|staff|people|users|patients))"
                      r"|\b(noon|midday)\b", re.I)


def tz():
    return ZoneInfo(bf.BUSINESS_HOURS["tz"])


def now_local(now_utc):
    return now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz()).replace(tzinfo=None)


def duration():
    return bf.number("DEMO_DURATION_MINUTES", DEFAULT_DURATION)


def notice_hours():
    return bf.number("DEMO_NOTICE_HOURS", DEFAULT_NOTICE_HOURS)


def _hm(s):
    h, m = s.split(":")
    return int(h), int(m)


def fmt(dt):
    return f"{dt:%A} {dt.day} {dt:%B} at {dt:%H:%M} (Lagos time)"


def _time_of(text):
    """(hour, minute) from the first clear time in the text, else None."""
    m = _TIME_RE.search(text)
    if not m:
        return None
    if m.group(1):
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()[0]
        if not 1 <= h <= 12:
            return None
        return (h % 12) + (12 if ap == "p" else 0), mi
    if m.group(4):
        return int(m.group(4)), int(m.group(5))
    if m.group(6):  # "at 10": read it inside business hours (7-11 am, 12-6 pm)
        h = int(m.group(6))
        if not 1 <= h <= 12:
            return None
        return (h if 7 <= h <= 11 else (h % 12) + 12), 0
    return 12, 0


def parse(text, now_utc):
    """Returns {"kind": "none"} when the text names no day or time, {"kind": "vague", "ask": ...} when it is too
    unclear to book, or {"kind": "slot", "start": datetime}. Never raises."""
    try:
        text = (text or "")[:500]
        low = text.lower()
        now = now_local(now_utc)
        day, tm = None, _time_of(low)
        if re.search(r"\btoday\b", low):
            day = now.date()
        elif re.search(r"\btomorrow\b", low):
            day = now.date() + timedelta(days=1)
        else:
            m = _DAY_RE.search(low)
            if m:
                target = _WEEKDAYS[m.group(2).lower()]
                ahead = (target - now.weekday()) % 7
                if ahead == 0 and (tm is None or (tm[0], tm[1]) <= (now.hour, now.minute)):
                    ahead = 7
                day = now.date() + timedelta(days=ahead)
                if m.group(1) and day.isocalendar()[:2] == now.date().isocalendar()[:2]:
                    day += timedelta(days=7)
        vague_words = re.search(r"\b(next week|this week|later|morning|afternoon|evening|sometime|any ?time)\b", low)
        if day is None and tm is None:
            return {"kind": "vague", "ask": ASK_TIME} if vague_words else {"kind": "none"}
        if day is None or tm is None:
            return {"kind": "vague", "ask": ASK_TIME}
        return {"kind": "slot", "start": datetime(day.year, day.month, day.day, tm[0], tm[1])}
    except Exception:
        return {"kind": "vague", "ask": ASK_TIME}


def _overlaps(start, minutes, exclude_id=None):
    end = start + timedelta(minutes=minutes)
    for d in DemoRequest.query.filter_by(status="confirmed").all():
        if d.id != exclude_id and d.start_at and d.start_at < end and start < d.start_at + timedelta(minutes=d.duration):
            return True
    return False


def validate(start, now_utc, exclude_id=None):
    """(ok, reason) for a demo start (naive Lagos time)."""
    h = bf.BUSINESS_HOURS
    mins = duration()
    (sh, sm), (eh, em) = _hm(h["start"]), _hm(h["end"])
    day_start = start.replace(hour=sh, minute=sm)
    day_end = start.replace(hour=eh, minute=em)
    if start.weekday() not in h["days"] or start < day_start or start + timedelta(minutes=mins) > day_end:
        return False, "outside demo hours"
    if start < now_local(now_utc) + timedelta(hours=notice_hours()):
        return False, "too soon"
    if _overlaps(start, mins, exclude_id):
        return False, "already taken"
    return True, ""


def next_slots(now_utc, count=3):
    """The next valid slots, one or two per day at fixed times, earliest first."""
    out, day = [], now_local(now_utc).date()
    for _ in range(21):
        for hour in (10, 14):
            start = datetime(day.year, day.month, day.day, hour, 0)
            if validate(start, now_utc)[0]:
                out.append(start)
        day += timedelta(days=1)
        if len(out) >= count:
            break
    return out[:count]


def join_slots(slots):
    parts = [f"{s:%A} {s.day} {s:%B} at {s:%H:%M}" for s in slots]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " or " + parts[-1]
