"""The one place that decides whether an outbound message may go. EVERY future outbound path (Cloud API, email, the manual
send links, calls) must call can_send(draft) immediately before acting. Phase 1 sends nothing: this only answers yes or no.

Order of checks (the first failure wins and is written to the audit log):
 1 emergency stop   2 master switch   3 channel switched on (email auto also needs recorded legal advice)
 4 do-not-contact list (the existing opt_outs register, the single suppression list)
 5 marketing opt-in for WhatsApp Cloud API   6 send/call hours (automated sends and calls)
 7 draft state and owner approval   8 review checkpoint (automated sends only)
 9 daily caps   10 monthly spend cap (automated sends)"""
from collections import namedtuple

from .. import service
from ..models import db, utcnow
from . import audit, clock, consent, counters, settings, spend
from .models import Draft, Prospect

Result = namedtuple("Result", "ok code reason")

AUTOMATED = ("whatsapp_cloud", "email")
PHONE_CHANNELS = ("whatsapp_cloud", "wa_manual", "call")
EMAIL_CHANNELS = ("email", "email_manual")
MANUAL = ("wa_manual", "email_manual", "call")
CHANNELS = AUTOMATED + MANUAL


def _block(draft, code, reason):
    audit.record("gate.blocked", "system", "draft", getattr(draft, "id", None),
                 {"code": code, "reason": reason, "channel": getattr(draft, "channel", None)})
    return Result(False, code, reason)


def can_send(draft, now=None):
    """Result(ok, code, reason). Never sends, never changes the draft. A refusal is audited."""
    now = now or utcnow()
    values = settings.all_values()
    channel = draft.channel
    automated = channel in AUTOMATED

    if settings.is_killed():
        return _block(draft, "killed", "The emergency stop is on.")
    if not values["enabled"]:
        return _block(draft, "disabled", "Outbound is switched off.")

    if channel not in CHANNELS:
        return _block(draft, "bad_channel", f"Unknown channel {channel!r}.")
    if channel in MANUAL or channel == "email_manual":
        if not values["channel_manual"]:
            return _block(draft, "channel_off", "Manual channels are switched off.")
    elif channel == "whatsapp_cloud":
        if not values["channel_wa_cloud"]:
            return _block(draft, "channel_off", "WhatsApp Cloud API outbound is switched off.")
    elif channel == "email":
        if not settings.legal_advice_recorded(values):
            return _block(draft, "email_locked", "Automated email is locked until legal advice is recorded.")
        if not values["email_auto"]:
            return _block(draft, "channel_off", "Automated email is switched off.")

    prospect = db.session.get(Prospect, draft.prospect_id)
    if channel in PHONE_CHANNELS:
        phone = prospect.business_phone if prospect else None
        if not phone:
            return _block(draft, "no_contact", "There is no phone number to check against the do-not-contact list.")
        if service.is_opted_out(phone):
            return _block(draft, "suppressed", "This number is on the do-not-contact list.")
        if channel == "whatsapp_cloud" and not consent.has_marketing_opt_in(phone, "whatsapp"):
            return _block(draft, "no_opt_in", "There is no recorded marketing opt-in for this number. A message to our "
                                              "WhatsApp line is a service reply only.")
    else:  # email channels: the do-not-contact list is phone-keyed today, so an email cannot be checked yet
        return _block(draft, "email_suppression_missing", "Email addresses cannot be checked against the do-not-contact "
                                                          "list yet, so email is refused.")

    if (automated or channel == "call") and not clock.in_send_hours(now):
        return _block(draft, "outside_hours", "Outside the send and call hours.")

    if draft.status in ("sent", "sending"):
        return _block(draft, "already_sent", "This draft was already sent.")
    if draft.expires_at and draft.expires_at < now:
        return _block(draft, "expired", "This draft has expired.")
    needs_approval = values["approval_required"] and (draft.kind == "first_touch" or values["followups_need_approval"])
    if needs_approval:
        if draft.status != "approved" or draft.approved_by not in settings.APPROVERS:
            return _block(draft, "not_approved", "The owner has not approved this draft.")
    elif draft.status not in ("draft", "approved"):
        return _block(draft, "bad_state", f"A {draft.status} draft cannot be sent.")

    if automated and values["pause_automated_after_gate"] and counters.review_pending():
        return _block(draft, "review_pending", f"{values['gate_count']} openers have been sent: automated sends are "
                                               "paused until you tap Reviewed.")

    start = clock.day_start_utc(now)
    today = Draft.query.filter(Draft.status == "sent", Draft.sent_at >= start)
    if today.count() >= values["daily_cap_total"]:
        return _block(draft, "cap_total", "The daily total cap is reached.")
    if draft.campaign_id and today.filter(Draft.campaign_id == draft.campaign_id).count() >= values["daily_cap_campaign"]:
        return _block(draft, "cap_campaign", "The daily cap for this campaign is reached.")

    if automated and not spend.spend_allowed(now=now):
        return _block(draft, "spend_cap", "The monthly spend cap is reached (hard stop).")
    return Result(True, "ok", "Allowed.")
