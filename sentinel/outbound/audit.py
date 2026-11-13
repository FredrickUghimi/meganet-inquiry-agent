"""Audit log for every outbound action. Ids, settings names and reasons only: never message text or personal data."""
import json

from ..models import db
from .models import AuditEvent

ACTORS = ("owner", "system")
FORBIDDEN_KEYS = {"body", "text", "message", "phone", "email", "name", "contact_name", "business_phone", "business_email",
                  "subject", "note", "token", "password"}


def record(action, actor="system", entity=None, entity_id=None, detail=None, commit=True):
    """Append one audit event. Raises ValueError if the detail could carry message text or personal data."""
    if actor not in ACTORS:
        raise ValueError(f"unknown actor {actor!r}")
    detail = dict(detail or {})
    bad = FORBIDDEN_KEYS & {str(k).lower() for k in detail}
    if bad:
        raise ValueError(f"audit detail must not contain {sorted(bad)}")
    event = AuditEvent(actor=actor, action=action, entity=entity,
                       entity_id=None if entity_id is None else str(entity_id), detail=json.dumps(detail, default=str))
    db.session.add(event)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return event


def record_safe(action, actor="system", entity=None, entity_id=None, detail=None):
    """Like record(), but never raises (a broken database, a bad detail): returns the event, or None if it could not be written.
    Used where the audit entry is a bonus and must not turn a refusal into a crash."""
    try:
        return record(action, actor, entity, entity_id, detail)
    except Exception as exc:
        from . import safety
        safety.log_failure("audit write", exc)
        safety.reset_session()
        return None
