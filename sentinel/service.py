"""Core lead logic: intake, auto-reply, classification, escalation, follow-up sequence."""
import logging
import re
from datetime import timedelta

from flask import current_app
from sqlalchemy import func

from . import ai, templates_meta, whatsapp
from .models import Lead, Message, db, utcnow

log = logging.getLogger(__name__)

STAGES = (1, 3, 7)          # days after the lead's last message
ACTIVE = ("new", "engaged")  # statuses the follow-up sequence applies to
WINDOW = timedelta(hours=23, minutes=30)  # Meta's window is 24h; keep a margin
MAX_SEND_FAILURES = 3


def normalize_phone(raw):
    return re.sub(r"\D", "", raw or "") or None


# --- intake ---------------------------------------------------------------------------------

def find_or_create_lead(*, phone=None, email=None, name=None, source="whatsapp"):
    lead = Lead.query.filter_by(phone=phone).first() if phone else None
    if lead is None and email:
        lead = Lead.query.filter(func.lower(Lead.email) == email.lower()).first()
    if lead:
        lead.phone = lead.phone or phone
        lead.email = lead.email or email
        lead.name = lead.name or name
        return lead
    lead = Lead(phone=phone, email=email, name=name, source=source)
    db.session.add(lead)
    db.session.flush()
    return lead


def next_followup_time(lead):
    """When the next stage is due, or None if the sequence should not run."""
    if lead.status not in ACTIVE or lead.followup_paused or not lead.phone:
        return None
    nxt = next((s for s in STAGES if s > lead.followup_stage), None)
    if nxt is None:
        return None
    return (lead.last_inbound_at or lead.created_at) + timedelta(days=nxt)


def record_inbound(lead, body, channel, external_id=None, at=None):
    """Store a message from the lead and restart the follow-up clock. Returns None for duplicates."""
    if external_id and Message.query.filter_by(external_id=external_id).first():
        return None
    at = at or utcnow()
    msg = Message(lead=lead, direction="inbound", channel=channel, body=body, sent_at=at,
                  external_id=external_id, status="received", handled=False)
    db.session.add(msg)
    lead.last_contact_at = at
    lead.last_inbound_at = max(at, lead.last_inbound_at or at)
    lead.followup_stage = 0  # a reply always stops the running sequence
    lead.followup_failures = 0
    if lead.status == "cold":
        lead.status = "engaged"
    lead.next_followup_at = next_followup_time(lead)
    db.session.commit()
    return msg


# --- sending --------------------------------------------------------------------------------

def window_open(lead):
    last = (Message.query.filter_by(lead_id=lead.id, direction="inbound", channel="whatsapp")
            .order_by(Message.sent_at.desc()).first())
    return bool(last and utcnow() - last.sent_at < WINDOW)


def send_whatsapp(lead, *, text=None, template=None, params=(), is_followup=False, stage=None):
    """Send and log. Never raises: a failed send is stored as a failed Message. Returns the Message."""
    cfg = current_app.config
    external_id = error = None
    try:
        if template:
            params = [templates_meta.clean_param(p) for p in params]
            name = cfg[templates_meta.TEMPLATES[template]["env"]]
            external_id = whatsapp.send_template(lead.phone, name, cfg["WHATSAPP_TEMPLATE_LANG"], params)
            text = templates_meta.render(template, params)
        else:
            external_id = whatsapp.send_text(lead.phone, text)
    except Exception as exc:
        error = str(exc)[:500]
        log.error("WhatsApp send to lead %s failed: %s", lead.id, error)
        if template:
            text = templates_meta.render(template, params)
    msg = Message(lead_id=lead.id, direction="outbound", channel="whatsapp", body=text, sent_at=utcnow(),
                  is_followup=is_followup, followup_stage=stage, external_id=external_id,
                  status="failed" if error else "sent", error=error, handled=True)
    db.session.add(msg)
    if not error:
        lead.last_contact_at = msg.sent_at
    db.session.commit()
    return msg


def notify_owner(lead, summary, kind):
    """WhatsApp alert to Fredrick. Failures are logged and never propagate."""
    cfg = current_app.config
    owner = cfg["OWNER_WHATSAPP_NUMBER"]
    if not owner:
        log.warning("OWNER_WHATSAPP_NUMBER not set; cannot alert about lead %s", lead.id)
        return False
    url = f"{cfg['PUBLIC_BASE_URL']}/leads/{lead.id}"
    line = templates_meta.clean_param(
        f"{lead.display_name} ({ai.PRODUCT_LABELS[lead.product_interest]}) ready for {kind}. {summary}", 400)
    try:
        whatsapp.send_text(owner, f"{line}\n{url}")
        return True
    except Exception as exc:
        log.warning("Free-form owner alert failed (%s); trying template", exc)
    tmpl = cfg["WHATSAPP_TEMPLATE_ESCALATION"]
    if tmpl:
        try:
            whatsapp.send_template(owner, tmpl, cfg["WHATSAPP_TEMPLATE_LANG"],
                                   [line, url])
            return True
        except Exception:
            log.exception("Owner alert template failed for lead %s", lead.id)
    else:
        log.error("Owner alert for lead %s not delivered. Message the business number from your phone to "
                  "reopen the 24h window, or set WHATSAPP_TEMPLATE_ESCALATION.", lead.id)
    return False


# --- inbound handling -----------------------------------------------------------------------

STATUS_FOR_INTENT = {"exploring": "engaged", "ready_demo": "ready", "ready_pricing": "ready", "not_fit": "closed_lost"}


def handle_lead_reply(lead_id):
    """Classify the lead's unhandled message(s), update the record, reply, and alert Fredrick if ready.
    Multiple rapid messages are claimed together and answered once."""
    lead = db.session.get(Lead, lead_id)
    if lead is None:
        return
    pending = Message.query.filter_by(lead_id=lead_id, direction="inbound", handled=False).all()
    if not pending:
        return
    claimed = (Message.query.filter(Message.id.in_([m.id for m in pending]), Message.handled.is_(False))
               .update({"handled": True}, synchronize_session=False))
    db.session.commit()
    if not claimed:
        return  # another worker got there first

    if lead.status in ("closed_won", "closed_lost"):
        return  # logged only; Fredrick decides whether to reopen

    first_contact = not Message.query.filter(
        Message.lead_id == lead_id, Message.direction == "outbound", Message.status != "failed",
        Message.is_followup.is_(False)).count()
    inbound_count = Message.query.filter_by(lead_id=lead_id, direction="inbound").count()
    history = list(lead.messages)

    try:
        result = ai.classify(lead, history)
    except Exception:
        log.exception("Classification failed for lead %s; treating as exploring", lead_id)
        result = {"intent": "exploring", "product": "unsure", "name": None, "summary": ""}

    intent = result["intent"]
    was_ready = lead.status == "ready"
    if result["product"] != "unsure":
        lead.product_interest = result["product"]
    if result.get("name") and not lead.name:
        lead.name = result["name"][:120]
    lead.intent, lead.summary = intent, result["summary"]
    if intent == "exploring":
        lead.status = "engaged" if (inbound_count > 1 or lead.status != "new") else "new"
    else:
        lead.status = STATUS_FOR_INTENT[intent]
    lead.next_followup_at = next_followup_time(lead)
    db.session.commit()

    newly_ready = lead.status == "ready" and not was_ready
    if newly_ready:
        notify_owner(lead, result["summary"], "pricing" if intent == "ready_pricing" else "a demo")

    if lead.followup_paused or not lead.phone or (was_ready and lead.status == "ready"):
        return  # Fredrick is handling this lead; the bot stays quiet
    if first_contact:
        situation = "first_contact"
    elif newly_ready:
        situation = "ready"
    elif lead.status == "closed_lost":
        situation = "not_fit"
    else:
        situation = "continue"

    if window_open(lead):
        send_whatsapp(lead, text=ai.draft_reply(lead, history, situation))
    elif first_contact and current_app.config["WHATSAPP_TEMPLATE_ACK"]:
        send_whatsapp(lead, template="ack", params=[lead.name or "there", ai.PRODUCT_LABELS[lead.product_interest]])
    else:
        log.info("Lead %s has no open WhatsApp window and no ack template; no auto-reply sent", lead_id)


# --- follow-up sequence ---------------------------------------------------------------------

def send_followup(lead):
    stage = next((s for s in STAGES if s > lead.followup_stage), None)
    if stage is None or not lead.phone:
        lead.next_followup_at = None
        db.session.commit()
        return
    history = list(lead.messages)
    previous = [m.body for m in history if m.is_followup and m.status == "sent"]
    line = ai.draft_followup_line(lead, history, stage, previous)
    name = lead.name or "there"

    if window_open(lead):
        msg = send_whatsapp(lead, text=f"Hi {name}. {line}", is_followup=True, stage=stage)
    else:
        msg = send_whatsapp(lead, template=f"day{stage}", is_followup=True, stage=stage,
                            params=[name, ai.PRODUCT_LABELS[lead.product_interest], line])

    now = utcnow()
    if msg.status == "failed":
        lead.followup_failures += 1
        if lead.followup_failures >= MAX_SEND_FAILURES:
            lead.followup_paused = True
            lead.next_followup_at = None
            log.error("Lead %s: %d follow-up sends failed; sequence paused", lead.id, lead.followup_failures)
        else:
            lead.next_followup_at = now + timedelta(hours=1)
    else:
        lead.followup_failures = 0
        lead.followup_stage = stage
        if stage == STAGES[-1]:
            lead.status = "cold"
        due = next_followup_time(lead)
        # If the app was down and stages piled up, still leave a gap between messages.
        lead.next_followup_at = max(due, now + timedelta(hours=12)) if due else None
    db.session.commit()


def run_due_followups():
    """Scheduler tick. One bad lead must never stop the others."""
    due = (Lead.query.filter(Lead.status.in_(ACTIVE), Lead.followup_paused.is_(False),
                             Lead.next_followup_at.isnot(None), Lead.next_followup_at <= utcnow()).all())
    for lead_id in [l.id for l in due]:
        try:
            lead = db.session.get(Lead, lead_id)
            if lead and lead.status in ACTIVE and not lead.followup_paused:
                send_followup(lead)
        except Exception:
            db.session.rollback()
            log.exception("Follow-up failed for lead %s", lead_id)
