"""Time helpers for outbound rules. They use the outbound `timezone` setting (default Africa/Lagos), not the server clock."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..models import utcnow
from . import settings


def _tz():
    return ZoneInfo(settings.get("timezone"))


def local(now=None):
    return (now or utcnow()).replace(tzinfo=timezone.utc).astimezone(_tz())


def day_start_utc(now=None):
    """Start of the current local day, as naive UTC (the form the database stores)."""
    start = local(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def month_start_utc(now=None):
    start = local(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def in_send_hours(now=None):
    """True on a send day, between send_start and send_end (start inclusive, end exclusive), in the outbound time zone."""
    here = local(now)
    days = {int(d) for d in settings.get("send_days").replace(" ", "").split(",") if d != ""}
    if here.weekday() not in days:
        return False
    return settings.get("send_start") <= here.strftime("%H:%M") < settings.get("send_end")
