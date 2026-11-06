"""Demo scheduling. Live demos run on one weekday at one time (business_facts.DEMO_WEEKDAY / DEMO_START_TIME, WAT).
Defensive parsing of "Wednesday", "12 Nov", "tomorrow 10am"; validation against the demo day, notice and capacity;
the next free demo days. Times here are naive local clock times in the business time zone (Africa/Lagos)."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import business_facts as bf
from .models import DemoRequest

DEFAULT_DURATION = 30  # minutes, if DEMO_DURATION_MINUTES has no number
DEFAULT_NOTICE_HOURS = 2  # if DEMO_NOTICE_HOURS has no number
HOLDING = ("confirmed",)  # only CONFIRMED demos use up a slot; tentative requests never block anyone

_WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2, "weds": 2,
             "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
             "sunday": 6, "sun": 6}
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                        "dec"], 1)}
_DAY_RE = re.compile(r"\b(?:(next)\s+)?(%s)\b" % "|".join(sorted(_WEEKDAYS, key=len, reverse=True)), re.I)
_DATE_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)
_TIME_RE = re.compile(r"(?:\bat\s+|\b)(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)(?![a-z])"
                      r"|\b([01]?\d|2[0-3]):([0-5]\d)\b|\bat\s+(\d{1,2})\b(?![:\d]|\s*(?:am|pm|staff|people|users|patients))"
                      r"|\b(noon|midday)\b", re.I)
_PREFERS_TEAM = re.compile(
    r"\b(speak|talk|chat|call|meet)\b[^.?!]{0,40}\b(team|someone|somebody|person|human|fredrick|representative|"
    r"member|staff|sales)\b|\b(prefer|rather|instead)\b[^.?!]{0,40}\b(speak|talk|call|person|team)\b|"
    r"\b(without|skip|no need for|don'?t need)\b[^.?!]{0,20}\bvideo\b", re.I)


def tz():
    return ZoneInfo(bf.BUSINESS_HOURS["tz"])


def now_local(now_utc):
    return now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz()).replace(tzinfo=None)


def duration():
    return bf.number("DEMO_DURATION_MINUTES", DEFAULT_DURATION)


def notice_hours():
    return bf.number("DEMO_NOTICE_HOURS", DEFAULT_NOTICE_HOURS)


def slots_per_session():
    try:
        return max(1, int(bf.DEMO_SLOTS_PER_SESSION))
    except (TypeError, ValueError):
        return 1


def _hm(s):
    h, m = s.split(":")
    return int(h), int(m)


def clock(h, m=0):
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def fmt(dt):
    """'Wednesday 12 November at 10:00 AM WAT'"""
    return f"{dt:%A} {dt.day} {dt:%B} at {clock(dt.hour, dt.minute)} WAT"


def rule_text():
    h, m = bf.demo_hm()
    return f"{bf.WEEKDAY_NAMES[bf.demo_weekday()]}s at {clock(h, m)} WAT"


def hours_status(now_utc):
    """One prompt line: the time and whether the team is in business hours, so replies say when the team will answer."""
    now = now_local(now_utc)
    if in_business_hours(now):
        return f"Now: {now:%A %H:%M} WAT, inside business hours ({bf.display('BUSINESS_HOURS')})."
    return (f"Now: {now:%A %H:%M} WAT, OUTSIDE business hours ({bf.display('BUSINESS_HOURS')}). When you say the team "
            f"will reply or confirm, say they will do so from {fmt_open(next_opening(now))}.")


def in_business_hours(now):
    h = bf.BUSINESS_HOURS
    (sh, sm), (eh, em) = _hm(h["start"]), _hm(h["end"])
    return now.weekday() in h["days"] and (sh, sm) <= (now.hour, now.minute) < (eh, em)


def next_opening(now):
    h = bf.BUSINESS_HOURS
    sh, sm = _hm(h["start"])
    day = now.date()
    for _ in range(8):
        cand = datetime(day.year, day.month, day.day, sh, sm)
        if cand.weekday() in h["days"] and cand > now:
            return cand
        day += timedelta(days=1)
    return now


def fmt_open(dt):
    return f"{dt:%A} at {clock(dt.hour, dt.minute)} WAT"


def team_note(now_utc):
    """Sentence appended to fixed replies outside business hours, else ''."""
    now = now_local(now_utc)
    if in_business_hours(now):
        return ""
    h = bf.BUSINESS_HOURS
    return (f" Our team is outside working hours now ({bf.display('BUSINESS_HOURS')}) and will reply from "
            f"{fmt_open(next_opening(now))}.")


def _time_of(text):
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
    """{"kind": "none"} when the text names no day or time; {"kind": "vague"} when it is too unclear to pencil in;
    {"kind": "slot", "start": datetime}. A demo weekday with no time means DEMO_START_TIME. Never raises."""
    try:
        low = (text or "")[:500].lower()
        now = now_local(now_utc)
        day, tm = None, _time_of(low)
        dm = _DATE_RE.search(low)
        if re.search(r"\btoday\b", low):
            day = now.date()
        elif re.search(r"\btomorrow\b", low):
            day = now.date() + timedelta(days=1)
        elif dm:
            d, mo = int(dm.group(1)), _MONTHS[dm.group(2)[:3].lower()]
            for year in (now.year, now.year + 1):
                try:
                    cand = datetime(year, mo, d).date()
                except ValueError:
                    break
                if cand >= now.date():
                    day = cand
                    break
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
        vague = re.search(r"\b(next week|this week|later|morning|afternoon|evening|sometime|any ?time|soon)\b", low)
        if day is None and tm is None:
            return {"kind": "vague"} if vague else {"kind": "none"}
        if day is None:
            return {"kind": "vague"}
        if tm is None:
            if day.weekday() == bf.demo_weekday():
                tm = bf.demo_hm()
            else:
                return {"kind": "vague"}
        return {"kind": "slot", "start": datetime(day.year, day.month, day.day, tm[0], tm[1])}
    except Exception:
        return {"kind": "vague"}


def used(day, exclude_lead_id=None, exclude_id=None):
    """Confirmed demos already holding a slot on this date (other requests only). Tentative ones never count."""
    n = 0
    for d in DemoRequest.query.filter(DemoRequest.status.in_(HOLDING)).all():
        if d.start_at and d.start_at.date() == day and d.id != exclude_id and d.lead_id != exclude_lead_id:
            n += 1
    return n


def validate(start, now_utc, exclude_id=None, lead_id=None):
    """(ok, reason). Only the demo weekday at the demo start time, with notice, and a free slot, is valid."""
    if start.weekday() != bf.demo_weekday() or (start.hour, start.minute) != bf.demo_hm():
        return False, "not a demo day or time"
    if start < now_local(now_utc) + timedelta(hours=notice_hours()):
        return False, "too soon"
    if used(start.date(), exclude_lead_id=lead_id, exclude_id=exclude_id) >= slots_per_session():
        return False, "full"
    return True, ""


def next_slots(now_utc, count=2, lead_id=None):
    """The next demo days with a free slot, earliest first (a full day is skipped)."""
    out, day = [], now_local(now_utc).date()
    h, m = bf.demo_hm()
    for _ in range(26 * 7):
        start = datetime(day.year, day.month, day.day, h, m)
        if validate(start, now_utc, lead_id=lead_id)[0]:
            out.append(start)
            if len(out) >= count:
                break
        day += timedelta(days=1)
    return out


def join_slots(slots):
    parts = [fmt(s) for s in slots]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " or " + parts[-1]


def redirect_text(now_utc, lead_id=None, asked=None):
    """Explain the demo day and offer the next two free ones (used when a request does not fit the rules)."""
    slots = next_slots(now_utc, 2, lead_id)
    what = f"{fmt(asked).rsplit(' at ', 1)[0]} is not a demo day. " if asked else ""
    offer = (f"The next available are {join_slots(slots)}. Which would you prefer?" if slots
             else "I will ask the team for the next available date.")
    return f"{what}Live demos are held on {rule_text()}. {offer}"


# --- reading the lead's messages ---------------------------------------------------------------

def prefers_team(text):
    return bool(_PREFERS_TEAM.search(text or ""))


CHANNEL_NAMES = {"whatsapp": "WhatsApp video call", "meet": "Google Meet"}
