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


def mask_phone(raw):
    """For logs: keep the country prefix and last 2 digits, mask the middle (234*******78)."""
    digits = normalize_phone(raw)
    if not digits:
        return "-"
    if len(digits) <= 5:
        return "*" * len(digits)
    return digits[:3] + "*" * (len(digits) - 5) + digits[-2:]


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
    if not whatsapp.is_configured():
        log.warning("WhatsApp not connected; owner alert for lead %s skipped", lead.id)
        return False
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


def handle_lead_reply(lead_id, *, retry=False, dry_run=False, trace=None):
    """Run the reply pipeline for a lead and return (outcome, reason).

    Always logs one INFO line when it starts and one when it ends ("reply lead=N finished outcome=... reason=...").
    outcome is one of: replied, skipped, failed.
    retry=True re-processes the lead's latest inbound message even if it was already handled.
    dry_run=True makes the real Claude calls but writes nothing and sends nothing.
    trace, if given, is called with each decision line (used by scripts/diagnose.py).
    """
    def say(msg, detail=False):
        """detail=True lines (lead text, drafted reply) go to trace only, never to the logs."""
        if not detail:
            log.info("reply lead=%s %s", lead_id, msg)
        if trace:
            trace(msg)

    say(f"started (retry={retry} dry_run={dry_run})")
    try:
        outcome, reason = _reply_pipeline(lead_id, retry, dry_run, say)
    except Exception as exc:
        db.session.rollback()
        log.exception("reply lead=%s crashed", lead_id)
        outcome, reason = "failed", f"{type(exc).__name__}: {exc}"[:300]
    say(f"finished outcome={outcome} reason={reason}")
    return outcome, reason


def _reply_pipeline(lead_id, retry, dry_run, say):
    model = current_app.config["ANTHROPIC_MODEL"]
    lead = db.session.get(Lead, lead_id)
    if lead is None:
        return "skipped", "lead not found"
    say(f"lead status={lead.status} paused={lead.followup_paused} stage={lead.followup_stage} "
        f"sender={mask_phone(lead.phone)} whatsapp_configured={whatsapp.is_configured()}")

    if retry:
        latest = (Message.query.filter_by(lead_id=lead_id, direction="inbound")
                  .order_by(Message.sent_at.desc(), Message.id.desc()).first())
        pending = [latest] if latest else []
    else:
        pending = Message.query.filter_by(lead_id=lead_id, direction="inbound", handled=False).all()
    if not pending:
        return "skipped", "no inbound message to answer" if retry else "no unhandled inbound messages (already answered or claimed)"
    say(f"{len(pending)} inbound message(s) to answer")

    if not dry_run and not retry:
        claimed = (Message.query.filter(Message.id.in_([m.id for m in pending]), Message.handled.is_(False))
                   .update({"handled": True}, synchronize_session=False))
        db.session.commit()
        if not claimed:
            return "skipped", "another worker claimed the messages"
    elif not dry_run:
        Message.query.filter(Message.id.in_([m.id for m in pending])).update({"handled": True},
                                                                              synchronize_session=False)
        db.session.commit()

    if lead.status in ("closed_won", "closed_lost"):
        return "skipped", f"lead is {lead.status}; no auto-reply (reopen it on the dashboard)"

    first_contact = not Message.query.filter(
        Message.lead_id == lead_id, Message.direction == "outbound", Message.status != "failed",
        Message.is_followup.is_(False)).count()
    inbound_count = Message.query.filter_by(lead_id=lead_id, direction="inbound").count()
    history = list(lead.messages)
    say(f"first_contact={first_contact} inbound_count={inbound_count} history={len(history)}")

    ai_note = None
    ai._local.classify_info = None
    try:
        result = ai.classify(lead, history)
        say(f"claude classify ok (model {model}): intent={result['intent']} product={result['product']}")
        say(f"classification detail: name={result['name']!r} summary={result['summary']!r}", detail=True)
    except Exception as exc:
        err = ai.describe_error(exc)
        log.error("Classification failed for lead %s (model %s): %s; treating as exploring", lead_id, model, err)
        say(f"claude classify FAILED (model {model}): {err}; treating as exploring")
        ai_note = f"Claude classification failed (model {model}): {err}"[:400]
        result = {"intent": "exploring", "product": "unsure", "name": None, "summary": ""}
    cinfo = ai.last_classify_info()
    if cinfo:  # trace only (diagnose script); raw model text never goes to the production logs
        say(f"classify stop_reason={cinfo['stop_reason']} input_tokens={cinfo['input_tokens']} "
            f"output_tokens={cinfo['output_tokens']} non_text_blocks={cinfo['non_text_blocks']} "
            f"retried={cinfo['retried']}", detail=True)
        say(f"classify raw text: {cinfo['raw_text']!r}", detail=True)

    latest = " ".join(m.body for m in pending)
    hint = ai.heuristic_intent(latest)
    if hint and result["intent"] == "exploring":
        say(f"keyword safety net: latest message asks about {'pricing' if hint == 'ready_pricing' else 'a demo'}; "
            f"upgrading intent exploring -> {hint}")
        reason = "Asked about pricing" if hint == "ready_pricing" else "Asked for a demo"
        if hint == "ready_pricing" and ai.DEMO_WORDS.search(latest):
            reason += " and a demo"
        result = dict(result, intent=hint, summary=f"{reason}. {result['summary']}".strip()[:300])
    intent = result["intent"]
    old_status = lead.status
    was_ready = old_status == "ready"
    if intent == "exploring":
        new_status = "engaged" if (inbound_count > 1 or lead.status != "new") else "new"
    else:
        new_status = STATUS_FOR_INTENT[intent]
    if not dry_run:
        if result["product"] != "unsure":
            lead.product_interest = result["product"]
        if result.get("name") and not lead.name:
            lead.name = result["name"][:120]
        lead.intent, lead.summary = intent, (result["summary"] or lead.summary)
        lead.ai_note = ai_note
        lead.status = new_status
        lead.next_followup_at = next_followup_time(lead)
        db.session.commit()
    newly_ready = new_status == "ready" and not was_ready
    say(f"decision: status {old_status} -> {new_status}, "
        f"newly_ready={newly_ready}")
    if newly_ready and not dry_run:
        notify_owner(lead, result["summary"], "pricing" if intent == "ready_pricing" else "a demo")

    if lead.followup_paused or not lead.phone or (was_ready and new_status == "ready"):
        reason = ("follow-ups paused for this lead (bot stays quiet)" if lead.followup_paused else
                  "lead has no phone" if not lead.phone else "lead already ready; Fredrick is handling it")
        return "skipped", reason
    if first_contact:
        situation = "first_contact"
    elif newly_ready:
        situation = "ready"
    elif new_status == "closed_lost":
        situation = "not_fit"
    else:
        situation = "continue"
    say(f"situation={situation}")

    if not whatsapp.is_configured():
        return "skipped", "WhatsApp not configured (WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID)"
    cfg_ack = current_app.config["WHATSAPP_TEMPLATE_ACK"]
    # The window is open when the lead's latest WhatsApp message is recent. In a dry run it is read-only anyway.
    is_open = window_open(lead)
    say(f"24h window open={is_open}, ack template={'set' if cfg_ack else 'not set'}")
    if is_open:
        ai.reset_draft_info()
        text = ai.draft_reply(lead, history, situation)
        info = ai.last_draft_info()
        if info:
            say(f"draft stop_reason={info['stop_reason']} usage={info['usage']} retried={info['retried']} "
                f"fallback={info['fallback']}" + (f" ({info['error']})" if info["fallback"] else ""))
            if info["fallback"]:
                log.warning("Lead %s: reply used canned fallback text because Claude failed: %s", lead_id, info["error"])
            if not dry_run:
                draft_note = f"Reply used canned fallback text (Claude failed: {info['error']})." if info["fallback"] else None
                lead.ai_note = " | ".join(n for n in (ai_note, draft_note) if n)[:400] or None
                db.session.commit()
        say(f"draft ready ({len(text)} chars)")
        say(f"drafted reply text: {text!r}", detail=True)
        if dry_run:
            return "skipped", "dry run: would send free-form text (pass --send to really send)"
        msg = send_whatsapp(lead, text=text)
    elif first_contact and cfg_ack:
        say("would send the ack template (fixed text, no Claude draft)", detail=True)
        if dry_run:
            return "skipped", "dry run: would send the ack template (pass --send to really send)"
        msg = send_whatsapp(lead, template="ack", params=[lead.name or "there", ai.PRODUCT_LABELS[lead.product_interest]])
    else:
        return "skipped", "no open WhatsApp window and not a first contact with an ack template"
    if msg.status == "failed":
        return "failed", f"WhatsApp send failed: {msg.error}"
    return "replied", situation


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


def run_unanswered(min_age=timedelta(minutes=2)):
    """Safety net: inbound messages still unhandled after min_age (a reply thread that never ran or was killed
    before claiming them) get the reply pipeline now."""
    cutoff = utcnow() - min_age
    ids = [r[0] for r in db.session.query(Message.lead_id).filter(
        Message.direction == "inbound", Message.handled.is_(False), Message.sent_at <= cutoff).distinct()]
    for lead_id in ids:
        log.warning("Sweeper: lead %s has unanswered inbound messages; running the reply pipeline", lead_id)
        handle_lead_reply(lead_id)


def run_due_followups():
    """Scheduler tick. One bad lead must never stop the others."""
    if not whatsapp.is_configured():
        log.warning("WhatsApp not connected; follow-up tick skipped (leads stay queued)")
        return
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
