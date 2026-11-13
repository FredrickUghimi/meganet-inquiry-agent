"""Recorded MARKETING opt-ins for WhatsApp Cloud API outbound.

A message sent TO the official WhatsApp line is a SERVICE REPLY ONLY: it opens the 24-hour window so we can answer it, and it
is never a marketing opt-in. Nothing in the inbound path writes here. This module refuses to record an opt-in whose evidence is
an inbound message, and accepts only the explicit kinds listed below."""
from ..models import db, utcnow
from .. import service
from . import audit
from .models import ConsentRecord

# Explicit evidence only. An inbound WhatsApp message is NOT on this list, and unknown kinds are refused.
ALLOWED_EVIDENCE = ("form_tick", "explicit_yes_reply", "signed_form", "owner_entered_with_proof")
CHANNELS = ("whatsapp", "email")
INBOUND_MESSAGE_PURPOSE = "service_reply_only"  # what a message to the official line means


class ConsentError(ValueError):
    pass


def record_opt_in(phone, channel, evidence_kind, evidence_note, actor="owner", prospect_id=None, lead_id=None):
    if channel not in CHANNELS:
        raise ConsentError("unknown channel")
    if evidence_kind not in ALLOWED_EVIDENCE:
        raise ConsentError("A message to our WhatsApp line is a service reply only and is never a marketing opt-in. "
                           f"Evidence must be one of: {', '.join(ALLOWED_EVIDENCE)}.")
    if not (evidence_note or "").strip():
        raise ConsentError("Record where and how the opt-in was given.")
    key, review = service.phone_key(phone)
    if not key or review:
        raise ConsentError("The number's country is unclear; enter it with the country code (+234...).")
    if service.is_opted_out(phone):
        raise ConsentError("This number asked us to stop. It cannot be given an opt-in here.")
    row = ConsentRecord(contact_key=key, channel=channel, kind="opt_in", evidence_kind=evidence_kind,
                        evidence_note=evidence_note.strip()[:300], recorded_by=actor, recorded_at=utcnow(),
                        prospect_id=prospect_id, lead_id=lead_id)
    db.session.add(row)
    audit.record("consent.opt_in_recorded", actor, "consent", None, {"channel": channel, "evidence_kind": evidence_kind},
                 commit=False)
    db.session.commit()
    return row


def record_withdrawal(phone, channel, actor="owner"):
    """The person withdrew. Also goes onto the single do-not-contact list (opt_outs)."""
    key, _ = service.phone_key(phone)
    if not key:
        raise ConsentError("no number")
    db.session.add(ConsentRecord(contact_key=key, channel=channel, kind="withdrawal", evidence_kind="withdrawal",
                                 recorded_by=actor, recorded_at=utcnow()))
    service.record_opt_out(phone, source="withdrawal")
    audit.record("consent.withdrawn", actor, "consent", None, {"channel": channel}, commit=False)
    db.session.commit()


def has_marketing_opt_in(phone, channel="whatsapp"):
    """True only if the latest consent record for this number and channel is an opt-in AND the number is not on the
    do-not-contact list. A number with no record, or whose country is unclear, has none."""
    key, review = service.phone_key(phone)
    if not key or review or service.is_opted_out(phone):
        return False
    latest = (ConsentRecord.query.filter_by(contact_key=key, channel=channel)
              .order_by(ConsentRecord.recorded_at.desc(), ConsentRecord.id.desc()).first())
    return bool(latest and latest.kind == "opt_in")
