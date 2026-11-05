"""Core lead logic: intake, auto-reply, classification, escalation, follow-up sequence."""
import logging
import re
from datetime import timedelta

from flask import current_app
from sqlalchemy import func

from . import ai, emailer, menu, templates_meta, whatsapp
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


def record_inbound(lead, body, channel, external_id=None, at=None, choice_id=None):
    """Store a message from the lead and restart the follow-up clock. Returns None for duplicates."""
    if external_id and Message.query.filter_by(external_id=external_id).first():
        return None
    at = at or utcnow()
    msg = Message(lead=lead, direction="inbound", channel=channel, body=body, sent_at=at,
                  external_id=external_id, status="received", handled=False, choice_id=choice_id)
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


def window_closes_at(lead):
    """When free-form WhatsApp text to this lead stops being allowed, or None if the window is closed."""
    last = (Message.query.filter_by(lead_id=lead.id, direction="inbound", channel="whatsapp")
            .order_by(Message.sent_at.desc()).first())
    if not last or utcnow() - last.sent_at >= WINDOW:
        return None
    return last.sent_at + WINDOW


def send_whatsapp(lead, *, text=None, template=None, params=(), is_followup=False, stage=None, sender=None, reply_to=None):
    """Send and log. Never raises: a failed send is stored as a failed Message. Returns the Message."""
    cfg = current_app.config
    external_id = error = None
    quote_dropped = False
    try:
        if template:
            params = [templates_meta.clean_param(p) for p in params]
            name = cfg[templates_meta.TEMPLATES[template]["env"]]
            external_id = whatsapp.send_template(lead.phone, name, cfg["WHATSAPP_TEMPLATE_LANG"], params)
            text = templates_meta.render(template, params)
        elif reply_to:
            try:
                external_id = whatsapp.send_text(lead.phone, text, reply_to=reply_to)
            except Exception as exc:  # Meta may refuse the quote (deleted/expired message): send it plain
                log.info("quoted send to lead %s rejected (%s); sending without the quote", lead.id, exc)
                quote_dropped = True
                external_id = whatsapp.send_text(lead.phone, text)
        else:
            external_id = whatsapp.send_text(lead.phone, text)
    except Exception as exc:
        error = str(exc)[:500]
        log.error("WhatsApp send to lead %s failed: %s", lead.id, error)
        if template:
            text = templates_meta.render(template, params)
    msg = Message(lead_id=lead.id, direction="outbound", channel="whatsapp", body=text, sent_at=utcnow(),
                  is_followup=is_followup, followup_stage=stage, external_id=external_id,
                  status="failed" if error else "sent", error=error, handled=True, sender=sender)
    db.session.add(msg)
    if not error:
        lead.last_contact_at = msg.sent_at
    db.session.commit()
    msg.quote_dropped = quote_dropped  # transient, for the caller's flash note
    return msg


def _alert_whatsapp(lead, line, url):
    """WhatsApp alert to Fredrick: plain text if his window is open, else the escalation template."""
    cfg = current_app.config
    owner = cfg["OWNER_WHATSAPP_NUMBER"]
    if not whatsapp.is_configured():
        log.warning("WhatsApp not connected; owner alert for lead %s skipped", lead.id)
        return False
    if not owner:
        log.warning("OWNER_WHATSAPP_NUMBER not set; cannot alert about lead %s", lead.id)
        return False
    try:
        whatsapp.send_text(owner, f"{line}\n{url}")
        return True
    except Exception as exc:
        log.warning("Free-form owner alert failed (%s); trying template", exc)
    tmpl = cfg["WHATSAPP_TEMPLATE_ESCALATION"]
    if tmpl:
        try:
            whatsapp.send_template(owner, tmpl, cfg["WHATSAPP_TEMPLATE_LANG"], [line, url])
            return True
        except Exception:
            log.exception("Owner alert template failed for lead %s", lead.id)
    else:
        log.error("Owner alert for lead %s not delivered. Message the business number from your phone to "
                  "reopen the 24h window, or set WHATSAPP_TEMPLATE_ESCALATION.", lead.id)
    return False


def send_owner_alert(lead, line):
    """Alert Fredrick by WhatsApp and, if configured, email. Failures are logged and never propagate.
    Returns True if any channel delivered; stamps lead.last_owner_alert_at (the throttle) on success."""
    url = f"{current_app.config['PUBLIC_BASE_URL']}/leads/{lead.id}"
    suffix = f' (lead {lead.id}) Reply "done {lead.id}" after you have contacted them.'
    line = templates_meta.clean_param(line, 400 - len(suffix)) + suffix
    delivered = _alert_whatsapp(lead, line, url)
    mailed = emailer.send_alert(f"Mega-Net lead: {lead.display_name}", f"{line}\n{url}")
    delivered = bool(delivered or mailed)
    if delivered:
        lead.last_owner_alert_at = utcnow()
        db.session.commit()
    return delivered


def notify_owner(lead, summary, kind):
    """'Lead is ready' alert."""
    return send_owner_alert(
        lead, f"{lead.display_name} ({ai.PRODUCT_LABELS[lead.product_interest]}) ready for {kind}. {summary}")


# --- waiting prospects (ready / paused / contacted) -----------------------------------------

OWNER_ALERT_THROTTLE = timedelta(minutes=10)
REMINDER_REPEAT = timedelta(hours=3)
MAX_REMINDERS = 3
CLOSED = ("closed_won", "closed_lost")


def is_waiting_state(lead):
    """Fredrick owns this conversation: ready, paused, or marked contacted (which pauses the bot)."""
    return lead.status not in CLOSED and (lead.status == "ready" or lead.followup_paused or lead.needs_owner)


def _waiting_inbound(lead):
    """Inbound messages still waiting for an answer, oldest first.
    Ready/paused/contacted leads are answered only by Fredrick (a stored owner message or Mark contacted);
    for other leads any successful outbound message, including the bot's, counts as the answer."""
    if lead.status in CLOSED:
        return []
    out = Message.query.filter_by(lead_id=lead.id, direction="outbound")
    if is_waiting_state(lead):
        out = out.filter(Message.status != "failed", db.or_(Message.channel == "manual", Message.sender == "owner"))
    else:
        out = out.filter(Message.status != "failed")
    marker = out.order_by(Message.sent_at.desc()).first()
    q = Message.query.filter_by(lead_id=lead.id, direction="inbound")
    if marker:
        q = q.filter(Message.sent_at > marker.sent_at)
    return q.order_by(Message.sent_at.asc(), Message.id.asc()).all()


def waiting_message_ids(lead):
    return {m.id for m in _waiting_inbound(lead)}


def reply_wait_since(lead):
    """When the oldest inbound message still waiting for an answer arrived, or None."""
    waiting = _waiting_inbound(lead)
    return waiting[0].sent_at if waiting else None


def wait_label(lead, now=None):
    """'2h 5m' style waiting time, or None when nothing is waiting."""
    since = reply_wait_since(lead)
    if since is None:
        return None
    minutes = max(0, int(((now or utcnow()) - since).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 24 * 60:
        return f"{minutes // 60}h {minutes % 60}m"
    return f"{minutes // 1440}d {(minutes % 1440) // 60}h"


def _quote(lead, limit=80):
    last = (Message.query.filter_by(lead_id=lead.id, direction="inbound")
            .order_by(Message.sent_at.desc(), Message.id.desc()).first())
    text = templates_meta.clean_param(last.body if last else "", limit)
    return text + ("..." if last and len(templates_meta.clean_param(last.body, 10000)) > limit else "")


def holding_text(lead):
    first = (lead.name or "").strip().split(" ")[0]
    greeting = f"Thank you, {first}." if first else "Thank you for your message."
    return f"{greeting} Fredrick has your message and will contact you personally soon."


def _waiting_reply(lead, newly_ready, dry_run, say):
    """A waiting prospect wrote again: holding reply (cooldown) + owner alert (throttle). Returns (outcome, reason)."""
    cfg, now, notes = current_app.config, utcnow(), []
    if newly_ready:
        notes.append("owner already alerted that the lead is ready")
    elif lead.last_owner_alert_at and now - lead.last_owner_alert_at < OWNER_ALERT_THROTTLE:
        notes.append("owner alert throttled (alerted <10 min ago)")
    elif dry_run:
        notes.append("dry run: would alert the owner")
    else:
        sent_alert = send_owner_alert(
            lead, f'{lead.display_name} wrote while waiting for you: "{_quote(lead)}"')
        notes.append("owner alerted" if sent_alert else "owner alert FAILED (see logs)")
    say(f"waiting lead (status={lead.status}, paused={lead.followup_paused}): " + "; ".join(notes))

    cooldown = timedelta(hours=cfg["HOLD_REPLY_COOLDOWN_HOURS"])
    why = f"lead is {'ready' if lead.status == 'ready' else 'paused/contacted'}; " + "; ".join(notes)
    if not lead.phone:
        return "skipped", why + "; no phone for a holding reply"
    if not whatsapp.is_configured():
        return "skipped", why + "; WhatsApp not configured"
    if lead.last_hold_reply_at and now - lead.last_hold_reply_at < cooldown:
        return "skipped", why + f"; holding reply already sent at {lead.last_hold_reply_at:%H:%M} UTC (cooldown {cooldown})"
    if not window_open(lead):
        return "skipped", why + "; 24h window closed, cannot send free-form holding reply"
    if dry_run:
        return "skipped", why + "; dry run: would send the holding reply"
    msg = send_whatsapp(lead, text=holding_text(lead))
    if msg.status == "failed":
        return "failed", f"holding reply send failed: {msg.error}"
    lead.last_hold_reply_at = now
    db.session.commit()
    return "holding", why


def run_owner_reminders(now=None):
    """Tick: remind Fredrick about waiting prospects he has not answered (first after OWNER_REPLY_REMINDER_MINUTES,
    then every 3 hours, at most 3 per unanswered message)."""
    now = now or utcnow()
    first_after = timedelta(minutes=current_app.config["OWNER_REPLY_REMINDER_MINUTES"])
    leads = Lead.query.filter(Lead.status.notin_(CLOSED), db.or_(Lead.status == "ready", Lead.followup_paused.is_(True))).all()
    for lead_id in [l.id for l in leads]:
        try:
            lead = db.session.get(Lead, lead_id)
            since = reply_wait_since(lead)
            if since is None:
                continue
            if lead.reminder_episode_at != since:
                lead.reminder_episode_at, lead.reminder_count, lead.last_reminder_at = since, 0, None
                db.session.commit()
            if lead.reminder_count >= MAX_REMINDERS:
                continue
            due = since + first_after + REMINDER_REPEAT * lead.reminder_count
            if lead.last_reminder_at:
                due = max(due, lead.last_reminder_at + REMINDER_REPEAT)
            if now < due:
                continue
            n = lead.reminder_count + 1
            line = (f'Reminder {n}/{MAX_REMINDERS}: {lead.display_name} has waited {wait_label(lead, now)} for your '
                    f'reply. Last message: "{_quote(lead)}"')
            if send_owner_alert(lead, line):
                lead.reminder_count, lead.last_reminder_at = n, now
                db.session.commit()
                log.info("reminder lead=%s outcome=sent n=%s/%s waited=%s", lead.id, n, MAX_REMINDERS, wait_label(lead, now))
            else:
                log.warning("reminder lead=%s outcome=failed (owner alert not delivered)", lead.id)
        except Exception:
            db.session.rollback()
            log.exception("Reminder failed for lead %s", lead_id)


# --- Fredrick acting on a waiting prospect (dashboard box and WhatsApp commands) -------------

def mark_contacted(lead, note=True):
    """Fredrick is handling this lead: pause the bot, clear the needs-reply flag, stop the reminders."""
    now = utcnow()
    if note:
        db.session.add(Message(lead_id=lead.id, direction="outbound", channel="manual", sent_at=now,
                               body="Marked as contacted by Fredrick.", handled=True, sender="owner"))
    lead.last_contact_at = now
    lead.followup_paused = True
    lead.needs_owner = lead.awaiting_choice = False
    if lead.status == "new":
        lead.status = "engaged"
    lead.next_followup_at = None
    lead.reminder_count, lead.last_reminder_at, lead.reminder_episode_at = 0, None, None
    db.session.commit()


MAX_OWNER_REPLY = 1000


def owner_reply(lead, text, quote=None):
    """Send Fredrick's free-form text to the lead. Returns (ok, detail). On success the lead is marked contacted.
    Never logs the text."""
    text = (text or "").strip()
    if not text:
        return False, "Write a message first."
    if len(text) > MAX_OWNER_REPLY:
        return False, f"Message is too long ({len(text)} of {MAX_OWNER_REPLY} characters)."
    if not lead.phone:
        return False, "This lead has no phone number."
    if not whatsapp.is_configured():
        return False, "WhatsApp is not connected."
    if not window_closes_at(lead):
        log.info("owner_reply lead=%s outcome=refused (24h window closed)", lead.id)
        return False, "Window closed. Message the lead from your own number."
    quote_id, note = None, ""
    if quote is not None:
        if quote.lead_id != lead.id or quote.direction != "inbound":
            return False, "That quoted message does not belong to this lead."
        if quote.external_id and quote.channel == "whatsapp":
            quote_id = quote.external_id
        else:
            note = " Note: that message has no WhatsApp id, so it was sent without the quote."
    msg = send_whatsapp(lead, text=text, sender="owner", reply_to=quote_id)
    if msg.status == "failed":
        log.info("owner_reply lead=%s outcome=failed (%s)", lead.id, msg.error)
        return False, f"Send failed: {msg.error}"
    if msg.quote_dropped:
        note = " Note: WhatsApp rejected the quote, so it was sent without it."
    mark_contacted(lead, note=False)
    log.info("owner_reply lead=%s outcome=sent chars=%d quoted=%s", lead.id, len(text), bool(quote_id and not msg.quote_dropped))
    return True, "Reply sent. Lead marked contacted; the bot is paused for them." + note


COMMANDS = ("done", "reply", "list", "help")
HELP_TEXT = ('Commands:\n"list": leads waiting for you\n"done <id>": mark contacted\n'
             '"reply <id> <text>": send text to the lead\n"help": this list')


def is_owner_command(text):
    words = (text or "").strip().split(None, 1)
    return bool(words) and words[0].lower().rstrip(":") in COMMANDS


def _lead_from_arg(arg):
    try:
        return db.session.get(Lead, int((arg or "").lstrip("#")))
    except ValueError:
        return None


def owner_command(text):
    """Run one owner command. Returns (command_name, confirmation_text_for_Fredrick)."""
    parts = text.strip().split(None, 2)
    cmd = parts[0].lower().rstrip(":")
    arg = parts[1] if len(parts) > 1 else ""
    if cmd == "help":
        return cmd, HELP_TEXT
    if cmd == "list":
        rows = sorted(((reply_wait_since(l), l) for l in Lead.query.all()), key=lambda r: (r[0] is None, r[0]))
        rows = [(w, l) for w, l in rows if w is not None]
        if not rows:
            return cmd, "No leads need a reply."
        now = utcnow()
        lines = [f"{l.id} {l.display_name}, {int((now - w).total_seconds() // 60)} min, "
                 f"{ai.PRODUCT_LABELS[l.product_interest]}" for w, l in rows[:5]]
        more = f"\n(+{len(rows) - 5} more)" if len(rows) > 5 else ""
        return cmd, "Needs a reply:\n" + "\n".join(lines) + more
    lead = _lead_from_arg(arg)
    if not arg:
        return cmd, f"Usage: {cmd} <id>" + (" <text>" if cmd == "reply" else "")
    if lead is None:
        return cmd, f'No lead with id "{arg[:20]}". Send "list" to see who needs a reply.'
    if cmd == "done":
        mark_contacted(lead)
        return cmd, f"Done: {lead.display_name} (id {lead.id}) marked contacted. Bot paused, reminders stopped."
    body = parts[2] if len(parts) > 2 else ""
    if not body.strip():
        return cmd, "Usage: reply <id> <text>"
    ok, detail = owner_reply(lead, body)
    if ok:
        return cmd, f"Sent to {lead.display_name} (id {lead.id}). Marked contacted."
    if detail.startswith("Window closed"):
        return cmd, (f"Window closed for {lead.display_name} (id {lead.id}). Message them from your own number, "
                     f'then send "done {lead.id}".')
    return cmd, detail


def run_owner_command(text):
    """Execute an owner command and text the result back to Fredrick."""
    owner = current_app.config["OWNER_WHATSAPP_NUMBER"]
    try:
        cmd, answer = owner_command(text)
    except Exception:
        db.session.rollback()
        log.exception("Owner command failed")
        cmd, answer = "error", "Sorry, that command failed. Check the logs."
    try:
        whatsapp.send_text(owner, answer)
        log.info("owner command=%s outcome=confirmed", cmd)
    except Exception as exc:
        log.warning("owner command=%s confirmation not delivered: %s", cmd, exc)


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

    if not dry_run and not retry:
        history = list(lead.messages)
        choice = _choice_for(lead, pending)
        if choice:
            return _handle_choice(lead, choice, history, dry_run, say)
        if lead.awaiting_choice:  # typed something else instead of choosing: ignore the menu, process as usual
            lead.awaiting_choice = False
            db.session.commit()

    first_contact = not Message.query.filter(
        Message.lead_id == lead_id, Message.direction == "outbound", Message.status != "failed",
        Message.is_followup.is_(False)).count()
    inbound_count = Message.query.filter_by(lead_id=lead_id, direction="inbound").count()
    history = list(lead.messages)
    say(f"first_contact={first_contact} inbound_count={inbound_count} history={len(history)}")

    if (first_contact and inbound_count == len(pending) and lead.phone and menu_allowed(lead)
            and whatsapp.is_configured() and window_open(lead)
            and all(m.choice_id is None and _is_greeting(m.body) for m in pending)):
        say("first message is a bare greeting: sending the tap menu instead of a draft")
        if dry_run:
            return "skipped", "dry run: would send the tap menu"
        msg = send_menu(lead)
        if msg.status == "failed":
            return "failed", f"menu send failed: {msg.error}"
        return "replied", "menu"

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
    if was_ready and new_status in ("new", "engaged"):
        new_status = "ready"  # a waiting prospect saying "hello?" is still waiting for Fredrick
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

    if not lead.phone:
        return "skipped", "lead has no phone"
    if lead.followup_paused or (was_ready and new_status == "ready"):
        return _waiting_reply(lead, newly_ready, dry_run, say)
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


# --- first-contact tap menu -----------------------------------------------------------------

def _is_greeting(text):
    verdict = menu.classify_opener(text)
    if verdict == "unsure":
        return ai.is_generic_opener(text)
    return verdict == "yes"


def menu_allowed(lead):
    """Never to ready, paused, contacted or closed leads, and not twice within the cooldown."""
    if lead.status not in ACTIVE or lead.followup_paused or lead.needs_owner:
        return False
    return not (lead.menu_sent_at and utcnow() - lead.menu_sent_at < menu.MENU_COOLDOWN)


def send_menu(lead):
    """Send the reply-button menu; if Meta rejects it, send the numbered text instead. Stores each outbound
    message with its WhatsApp id. Returns the stored Message (status 'failed' if both sends failed)."""
    try:
        external_id, body, via = whatsapp.send_buttons(lead.phone, menu.MENU_BODY, menu.MENU_BUTTONS), menu.MENU_BODY, "buttons"
        msg = Message(lead_id=lead.id, direction="outbound", channel="whatsapp", body=body, sent_at=utcnow(),
                      external_id=external_id, status="sent", handled=True)
        db.session.add(msg)
        lead.last_contact_at = msg.sent_at
        db.session.commit()
    except Exception as exc:
        log.info("menu lead=%s fallback used: interactive message rejected (%s); sending plain text",
                 lead.id, type(exc).__name__)
        msg, via = send_whatsapp(lead, text=menu.MENU_FALLBACK_TEXT), "text"
    if msg.status != "failed":
        lead.menu_sent_at, lead.awaiting_choice = msg.sent_at, True
        db.session.commit()
        log.info("menu lead=%s sent via=%s", lead.id, via)
    return msg


def _choice_for(lead, pending):
    """The menu id the lead's latest unanswered message stands for, or None. A tap counts any time (unless
    Fredrick already owns the lead); typed 1/2/3/EHR/ERP only while the lead is awaiting a choice."""
    if len(pending) != 1 or is_waiting_state(lead):
        return None
    m = pending[0]
    if m.choice_id in menu.BUTTON_IDS:
        log.info("menu lead=%s tap received choice=%s", lead.id, m.choice_id)
        return m.choice_id
    if lead.awaiting_choice:
        typed = menu.typed_choice(m.body)
        if typed:
            log.info("menu lead=%s typed reply received choice=%s", lead.id, typed)
            return typed
    return None


def _handle_choice(lead, choice, history, dry_run, say):
    """Act on a tap (or typed 1/2/3). Returns (outcome, reason)."""
    if dry_run:
        return "skipped", f"dry run: would handle menu choice {choice}"
    lead.awaiting_choice = False
    if lead.status in ("new", "cold"):
        lead.status = "engaged"
    lead.intent = "exploring"
    if choice == "other":
        lead.needs_owner = True
        lead.summary = "Chose Something else on the welcome menu."
        text = menu.OTHER_REPLY
    else:
        lead.product_interest = choice
        text = None
    lead.next_followup_at = next_followup_time(lead)
    db.session.commit()
    say(f"menu choice={choice} status={lead.status} product={lead.product_interest}")
    if not whatsapp.is_configured():
        return "skipped", "WhatsApp not configured (WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID)"
    if text is None:
        ai.reset_draft_info()
        text = ai.draft_reply(lead, history, "qualify")
    msg = send_whatsapp(lead, text=text)
    if choice == "other":
        send_owner_alert(lead, menu.OTHER_ALERT.format(name=lead.display_name))
    if msg.status == "failed":
        return "failed", f"WhatsApp send failed: {msg.error}"
    return "replied", f"menu choice {choice}"


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
