"""Monthly Claude + WhatsApp spend for outbound work, with a hard stop at `spend_cap_usd`. Amounts are estimates in US$."""
from sqlalchemy import func

from ..models import db, utcnow
from . import audit, clock, settings
from .models import OutboundSpend


def record_spend(kind, usd, note=None, actor="system"):
    if kind not in ("claude", "whatsapp"):
        raise ValueError("kind must be claude or whatsapp")
    if usd < 0:
        raise ValueError("spend cannot be negative")
    row = OutboundSpend(kind=kind, usd=float(usd), note=(note or "")[:120] or None, at=utcnow())
    db.session.add(row)
    audit.record("spend.recorded", actor, "spend", None, {"kind": kind, "usd": round(float(usd), 4)}, commit=False)
    db.session.commit()
    return row


def month_total(now=None):
    total = db.session.query(func.coalesce(func.sum(OutboundSpend.usd), 0.0)).filter(
        OutboundSpend.at >= clock.month_start_utc(now)).scalar()
    return float(total or 0.0)


def spend_allowed(extra=0.0, now=None):
    """False once this month's spend has reached the cap, or would pass it with `extra`. A hard stop: no override here."""
    cap = settings.get("spend_cap_usd")
    spent = month_total(now)
    return spent < cap and spent + extra <= cap
