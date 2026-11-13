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
