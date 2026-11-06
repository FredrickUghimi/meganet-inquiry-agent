"""Core lead logic: intake, auto-reply, classification, escalation, follow-up sequence."""
import logging
import re
from datetime import timedelta

from flask import current_app
from sqlalchemy import func

from . import ai, emailer, knowledge, menu, templates_meta, whatsapp
from . import business_facts, demo
from .models import DemoRequest, Lead, Message, OpenQuestion, db, utcnow

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


def mask_phone_detail(raw, head=6, tail=2):
    """For lists and confirmation pages: keep the first 6 digits and the last 2 (234801*****78)."""
    digits = normalize_phone(raw)
    if not digits:
        return "-"
    if len(digits) <= head + tail:
        return "*" * len(digits)
    return digits[:head] + "*" * (len(digits) - head - tail) + digits[-tail:]


# --- deleting leads ---------------------------------------------------------------------------

def lead_counts(lead_ids):
    """{lead_id: {"messages": n, "demos": n, "questions": n}} for the given ids (ids with no lead are left out)."""
    ids = [i for i in lead_ids]
    out = {i: {"messages": 0, "demos": 0, "questions": 0} for (i,) in db.session.query(Lead.id).filter(Lead.id.in_(ids))}
    for key, model in (("messages", Message), ("demos", DemoRequest), ("questions", OpenQuestion)):
        for lead_id, n in (db.session.query(model.lead_id, func.count(model.id))
                           .filter(model.lead_id.in_(ids)).group_by(model.lead_id)):
            if lead_id in out:
                out[lead_id][key] = n
    return out


def delete_leads(lead_ids):
    """Delete leads together with everything that belongs to them (messages, demo requests, open questions), in ONE
    transaction. Reminder state, menu state and alert throttles live on the lead row, so they go with it. Touches no other
    table. Returns {"leads", "messages", "demos", "questions"} counts. Logs one INFO line per lead, never message text."""
    ids = sorted(set(int(i) for i in lead_ids))
    counts = lead_counts(ids)
    ids = [i for i in ids if i in counts]
    totals = {"leads": len(ids), "messages": 0, "demos": 0, "questions": 0}
    if not ids:
        return totals
    try:
        for model in (OpenQuestion, DemoRequest, Message):  # children first (Postgres enforces the foreign keys)
            model.query.filter(model.lead_id.in_(ids)).delete(synchronize_session=False)
        Lead.query.filter(Lead.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    db.session.expire_all()
    for i in ids:
        c = counts[i]
        for k in ("messages", "demos", "questions"):
            totals[k] += c[k]
        log.info("lead deleted id=%s messages=%d demo_requests=%d open_questions=%d", i, c["messages"], c["demos"],
                 c["questions"])
    return totals


def describe_deleted(t):
    return (f"Deleted {t['leads']} lead(s): {t['messages']} message(s), {t['demos']} demo request(s), "
            f"{t['questions']} open question(s).")


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
    if lead.is_existing_client and not lead.upgrade_enquiry:
        return None  # existing clients get no automatic follow-ups unless they are a sales lead
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


def _ack_text(lead, history):
    """Last resort when Claude cannot produce a non-repeating reply: a short, different acknowledgement that mentions
    the customer's latest message."""
    latest = next((m.body for m in reversed(history) if m.direction == "inbound"), "")
    snippet = " ".join(latest.split()[:8]) or "your message"
    options = (f"Thanks for your message ({snippet}). I have passed it to the team and they will get back to you shortly.",
               f"Got it, thank you. The team has your note about \"{snippet}\" and will follow up with you.",
               f"Thank you, I have noted \"{snippet}\" and asked the team to look at it personally.")
    return next((o for o in options if not ai.repeats(o, history)), options[-1] + " Thank you for your patience.")


def _repeat_guard(lead, text):
    """Send-time guard on EVERY text path: a reply identical or near-identical (similarity above 0.8) to any bot message
    in the last 24 hours is regenerated by Claude with the customer's latest message in focus. If that fails, a short
    different acknowledgement goes out, the lead is flagged for the team, and a WARNING is logged."""
    history = list(lead.messages)
    if not ai.repeats(text, history) and not ai.is_retired(text):
        return text
    log.info("lead=%s reply is too similar to a recent bot message; asking Claude to regenerate", lead.id)
    try:
        new = ai.regenerate_message(lead, history, text)
        if new and not ai.repeats(new, history):
            return new
    except Exception as exc:
        log.warning("lead=%s regeneration failed: %s", lead.id, ai.describe_error(exc))
    log.warning("lead=%s repeat guard: no fresh reply from Claude; sending a short acknowledgement and flagging the lead",
                lead.id)
    lead.needs_owner = True
    lead.ai_note = ("Reply was a short acknowledgement: Claude could not produce a reply that differs from earlier "
                    "messages. Needs a person.")[:400]
    db.session.commit()
    send_owner_alert(lead, f"{lead.display_name}: the bot could not find a fresh reply to their latest message.")
    return _ack_text(lead, history)


def send_whatsapp(lead, *, text=None, template=None, params=(), is_followup=False, stage=None, sender=None, reply_to=None,
                  guard=True):
    """Send and log. Never raises: a failed send is stored as a failed Message. Returns the Message.
    Free-form bot text goes through the repeat guard (not Fredrick's own messages, not templates)."""
    cfg = current_app.config
    if text and not template and guard and sender != "owner":
        text = _repeat_guard(lead, text)
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


def human_handled(lead):
    """A person owns this conversation: paused / marked contacted, or an owner or staff message was stored within
    OWNER_TAKEOVER_HOURS (and not handed back). 'Ready' alone does not count, unless BOT_ANSWERS_AFTER_READY=0."""
    cfg = current_app.config
    if lead.followup_paused:
        return True
    if lead.status == "ready" and not cfg["BOT_ANSWERS_AFTER_READY"]:
        return True
    since = utcnow() - timedelta(hours=cfg["OWNER_TAKEOVER_HOURS"])
    if lead.handback_at:
        since = max(since, lead.handback_at)
    return Message.query.filter(
        Message.lead_id == lead.id, Message.direction == "outbound", Message.status != "failed",
        db.or_(Message.sender.in_(("owner", "staff")), Message.channel == "manual"), Message.sent_at >= since).count() > 0


def is_waiting_state(lead):
    """Only a person answers this lead: human-handled, or flagged as needing Fredrick (chose "Something else")."""
    return lead.status not in CLOSED and (human_handled(lead) or bool(lead.needs_owner))


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


def _claude_text(lead, situation, fixed_text, context=""):
    """A fixed-purpose reply (holding, support route, 'something else') written by Claude with the latest message in
    focus. The fixed text is only the fallback: when it is used, a WARNING is logged and the lead page shows a note."""
    turn = ai.draft_turn(lead, list(lead.messages), situation, context, fallback_text=fixed_text, structured=False)
    if turn.get("fallback"):
        log.warning("lead=%s %s reply used the fixed fallback text: %s", lead.id, situation, turn.get("error"))
        lead.ai_note = f"Fixed fallback text used for a {situation.replace('_', ' ')} reply (Claude: {turn.get('error')})."[:400]
        db.session.commit()
    return turn["reply"], turn


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
    text, _ = _claude_text(lead, "holding", holding_text(lead) + demo.team_note(now))
    msg = send_whatsapp(lead, text=text)
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
    leads = Lead.query.filter(Lead.status.notin_(CLOSED)).all()
    for lead_id in [l.id for l in leads]:
        try:
            lead = db.session.get(Lead, lead_id)
            if lead is None or not is_waiting_state(lead):  # deleted while this tick was running
                continue
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


# --- open questions and demo scheduling ------------------------------------------------------

FLAG_LABEL = {"complaint": "a complaint", "custom_request": "a custom requirement", "unanswered": "a question the bot could not answer"}


def _record_open_question(lead, flag, question, already_alerted, say):
    """Remember what the bot could not handle and tell Fredrick once (throttled), so a human is alerted only when needed."""
    db.session.add(OpenQuestion(lead_id=lead.id, kind=flag, question=" ".join(str(question).split())[:500]))
    db.session.commit()
    now = utcnow()
    if already_alerted or (lead.last_owner_alert_at and now - lead.last_owner_alert_at < OWNER_ALERT_THROTTLE):
        say(f"open question recorded ({flag}); owner alert not sent (already alerted recently)")
        return
    sent_alert = send_owner_alert(lead, f'{lead.display_name} raised {FLAG_LABEL[flag]}: "'
                                  f'{templates_meta.clean_param(question, 160)}"')
    say(f"open question recorded ({flag}); owner {'alerted' if sent_alert else 'alert FAILED'}")


# One-line descriptions use only what PRODUCT_NOTES says about each product; never what a video contains.
VIDEO_BLURB = {"ehr_cloud": "our cloud electronic health records system for clinics and hospitals",
               "erp_cloud": "our cloud ERP for businesses (operations, finance and inventory)"}
PERSONALISED_RE = re.compile(r"personali[sz]ed\s+(live\s+)?(demo|demonstration)", re.I)
OFFER_LINE_RE = re.compile(knowledge.OFFER_LINE_RE, re.I)
_URL_RE = re.compile(r"(?:https?://|www\.)\S+|\byoutu\.be/\S+", re.I)
OFFER_COOLDOWN = timedelta(hours=24)
MAX_IGNORED_OFFERS = 2
SILENCE_GAP = timedelta(days=3)
_HUMAN_RE = re.compile(r"\b(talk|speak|chat)\s+(to|with)\s+(a\s+|an\s+|the\s+|one\s+)?(real\s+)?(person|human|someone|somebody|"
                       r"agent|fredrick|representative|manager|owner)\b|\breal person\b|\bcustomer (care|service)\b|"
                       r"\bcall me\b", re.I)
_QUESTIONISH = re.compile(r"\?|\b(how|what|when|why|which|where|who|do you|can you|is it|are you|does|will)\b", re.I)


def _ago(delta):
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)} minutes ago"
    if minutes < 24 * 60:
        return f"{minutes // 60} hours ago"
    return f"{minutes // 1440} days ago"


def _video_sent_at(lead, url):
    last = (Message.query.filter(Message.lead_id == lead.id, Message.direction == "outbound", Message.status != "failed",
                                 Message.body.contains(url)).order_by(Message.sent_at.desc()).first())
    return last.sent_at if last else None


def _video_sent_recently(lead, url):
    at = _video_sent_at(lead, url)
    return bool(at and utcnow() - at < timedelta(hours=24))


def personalised_state(lead):
    """asked / accepted / declined / None. Values from the old scripted flow are mapped, never reset."""
    return {"offered": "asked", "collecting": "accepted", "choosing": "accepted"}.get(lead.demo_state, lead.demo_state)


def offer_allowed(lead, now=None, wants_demo=False):
    """May this reply ask 'would you like a personalised demo?'. At most once per 24 hours, never after it was accepted
    or declined, and never again once two asks were ignored (until the customer asks for a demo again)."""
    now = now or utcnow()
    if personalised_state(lead) in ("accepted", "declined"):
        return False, f"already {personalised_state(lead)}"
    if lead.demo_offer_asked_at and now - lead.demo_offer_asked_at < OFFER_COOLDOWN:
        return False, "asked in the last 24 hours"
    if lead.demo_offer_ignored >= MAX_IGNORED_OFFERS and not wants_demo:
        return False, "asked twice and ignored: wait for the customer to ask"
    return True, ""


def _draft_demo(lead):
    d = DemoRequest.query.filter_by(lead_id=lead.id, status="draft").order_by(DemoRequest.id.desc()).first()
    if d is None:
        d = DemoRequest(lead_id=lead.id, requested_text="", status="draft", duration=demo.duration())
        prev = (DemoRequest.query.filter(DemoRequest.lead_id == lead.id, DemoRequest.status != "draft")
                .order_by(DemoRequest.id.desc()).first())
        if prev:  # a new request (for example a reschedule) starts from what we already know
            d.contact_name, d.organisation, d.contact_phone, d.channel = (prev.contact_name, prev.organisation,
                                                                          prev.contact_phone, prev.channel)
        db.session.add(d)
        db.session.flush()
    return d


def _missing_details(lead, d):
    have = {"your name": d.contact_name or lead.name, "your organisation": d.organisation,
            "your phone number": d.contact_phone or lead.phone, "your preferred channel (WhatsApp video call or Google Meet)": d.channel}
    return [label for label, v in have.items() if not v]


def _is_repeat_question(lead, pending):
    """True if the customer is asking the same question again (a loop): the latest message is near-identical to an
    earlier message from them that was a question."""
    ids = {m.id for m in pending}
    latest = " ".join(m.body for m in pending)
    if len(_norm_text(latest)) < 8 or not _QUESTIONISH.search(latest):
        return False
    for m in Message.query.filter_by(lead_id=lead.id, direction="inbound").order_by(Message.sent_at.desc()).limit(10):
        if m.id not in ids and ai.similarity(latest, m.body) > 0.85:
            return True
    return False


def _norm_text(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def demo_context(lead, history, pending, now, intent=None):
    """Everything the draft needs to know about this lead's demo journey, so Claude (not a script) decides what to
    say. Facts only: the code never turns this into a fixed reply."""
    latest = " ".join(m.body for m in pending)
    lines = ["Demo and conversation context (use it, do not recite it):"]
    for p in VIDEO_BLURB:
        label, url = business_facts.video_for(p)
        at = _video_sent_at(lead, url)
        lines.append(f"- {label} demo video: " + (f"sent {_ago(now - at)}" + (" (do NOT send it again today)" if now - at < timedelta(hours=24) else "")
                                                  if at else "not sent yet"))
    lines.append(f"- Video descriptions you may use: EHR Cloud is {VIDEO_BLURB['ehr_cloud']}; ERP Cloud is {VIDEO_BLURB['erp_cloud']}. "
                 "Never describe what the video contains.")
    state = personalised_state(lead)
    allowed, why = offer_allowed(lead, now, wants_demo=DEMO_RE_HINT(latest, intent))
    if state == "accepted":
        lines.append("- The customer ACCEPTED a personalised demo with the team.")
    elif state == "declined":
        lines.append("- The customer DECLINED a personalised demo. Do not push it; accept their choice.")
    elif lead.demo_offer_asked_at:
        lines.append(f"- You offered a personalised live demo with the team ({demo.rule_text()}) {_ago(now - lead.demo_offer_asked_at)}"
                     f"; ignored {lead.demo_offer_ignored} time(s). A reply like 'yes', 'Wednesday works' or 'I'd like a live "
                     "demo' ACCEPTS it: set demo_signal accepted_personalised and ask only for the details still missing "
                     "(name, organisation, phone, WhatsApp video call or Google Meet). Do not send the video again and do not "
                     "make the offer again. A thank-you or another question is NOT an answer: just answer it.")
    else:
        lines.append("- You have not yet asked about a personalised demo.")
    lines.append("- You MAY ask the personalised-demo question in this reply." if allowed else
                 f"- You must NOT ask the personalised-demo question in this reply ({why}).")
    d = DemoRequest.query.filter_by(lead_id=lead.id, status="draft").order_by(DemoRequest.id.desc()).first()
    missing = _missing_details(lead, d) if d else ["your name", "your organisation", "your preferred channel (WhatsApp video call or Google Meet)"]
    if state == "accepted":
        got = {k: v for k, v in (("name", (d.contact_name if d else None) or lead.name),
                                 ("organisation", d.organisation if d else None),
                                 ("phone", (d.contact_phone if d else None) or lead.phone),
                                 ("channel", d.channel if d else None)) if v}
        lines.append(f"- Details collected: {got or 'none'}. Still missing: {', '.join(missing) or 'nothing'}. Ask only for what is missing.")
    for dm in DemoRequest.query.filter(DemoRequest.lead_id == lead.id, DemoRequest.status.in_(("tentative", "confirmed"))).all():
        if dm.start_at:
            lines.append(f"- Demo #{dm.id}: {demo.fmt(dm.start_at)}, status {dm.status}"
                         + (" (pencilled in, NOT confirmed: say the team will confirm)" if dm.status == "tentative" else ""))
    slots = demo.next_slots(now, 2, lead.id)
    lines.append(f"- Live demos run on {demo.rule_text()}. Next free: " + (demo.join_slots(slots) if slots else "none, say the team will suggest a date") + ".")
    parsed = demo.parse(latest, now)
    if parsed["kind"] == "slot":
        ok, reason = demo.validate(parsed["start"], now, lead_id=lead.id)
        lines.append(f"- The customer's message names {demo.fmt(parsed['start'])}: " + ("this is a VALID demo slot." if ok else
                     f"this is NOT possible ({reason}); explain the demo day and offer {demo.join_slots(slots) or 'a date the team will confirm'}."))
    elif parsed["kind"] == "vague":
        lines.append("- The customer's message hints at a time but not a clear one; suggest the next free demo day instead of asking an open question.")
    if state == "declined" or (d is None and state is None and not DEMO_RE_HINT(latest, intent)):
        nxt = "no demo step is needed; just answer."
    elif state == "accepted" and missing:
        nxt = "ask for the missing details (only those) in one short question."
    elif state == "accepted" and slots:
        nxt = f"propose {demo.fmt(slots[0])}; the demo is only pencilled in after the customer agrees to a time."
    else:
        nxt = "if they want a demo: share the product video (if not sent today), then at most one question."
    lines.append(f"- Suggested next step: {nxt}")
    inbound = [m for m in history if m.direction == "inbound" and m.id not in {p.id for p in pending}]
    if inbound and pending and pending[0].sent_at - inbound[-1].sent_at > SILENCE_GAP:
        lines.append(f"- The customer was silent for {_ago(pending[0].sent_at - inbound[-1].sent_at).replace(' ago', '')} "
                     "before this message: welcome them back warmly and continue; do not restart.")
    if _is_repeat_question(lead, pending):
        lines.append("- The customer is repeating a question they already asked. Acknowledge it, answer differently or "
                     "more directly, and set needs_human with the reason 'loop: customer repeated the same question'.")
    return "\n".join(lines)


def DEMO_RE_HINT(latest, intent):
    return intent == "ready_demo" or bool(ai.DEMO_WORDS.search(latest or ""))


def _staff_alert(line):
    """Free-form WhatsApp to each configured staff member (best effort; never raises)."""
    if not whatsapp.is_configured():
        return
    for number in current_app.config["STAFF_WHATSAPP_NUMBERS"]:
        try:
            whatsapp.send_text(number, line)
        except Exception as exc:
            log.warning("staff alert to %s failed: %s", mask_phone(number), exc)


def _pencil(lead, d, start):
    """Turn the draft into a tentative demo and alert Fredrick and staff. Never confirms."""
    for old in DemoRequest.query.filter(DemoRequest.lead_id == lead.id, DemoRequest.status == "tentative",
                                        DemoRequest.id != d.id).all():
        old.status = "rescheduled"
    d.status, d.start_at, d.duration = "tentative", start, demo.duration()
    d.requested_text = (d.requested_text or f"Personalised demo, {d.channel}")[:300]
    db.session.commit()
    line = (f'{d.contact_name or lead.display_name} ({d.organisation or "no organisation given"}) wants a personalised '
            f'demo: {demo.fmt(start)} by {d.channel}, phone {d.contact_phone}. Demo {d.id}. Reply "confirm {d.id}" or '
            f'"decline {d.id} [new time]".')
    send_owner_alert(lead, line)
    _staff_alert(line)
    log.info("demo lead=%s request=%s pencilled (tentative)", lead.id, d.id)


def sanitize_links(text):
    """Only the stored demo video links may appear in a reply, character for character. Any other link Claude wrote (or a
    rewritten or shortened copy of one) is removed."""
    approved = set(business_facts.PRODUCT_DEMO_VIDEOS.values())

    def keep(m):
        url = m.group(0)
        stripped = url.rstrip(".,;:!?)")
        return url if stripped in approved or url in approved else ""
    return re.sub(r"[ \t]+(\n)", r"\1", _URL_RE.sub(keep, text)).strip()


def fill_video_placeholder(lead, reply):
    """Replace the {video_url} placeholder of the approved first reply with the exact stored link for the lead's product.
    If that video was already sent in the last 24 hours, the 'Watch our product demonstration' paragraph is dropped.
    Returns (reply, video_inserted)."""
    if knowledge.VIDEO_PLACEHOLDER not in reply:
        return reply, False
    vid = business_facts.video_for(lead.product_interest)
    if vid and not _video_sent_recently(lead, vid[1]):
        return reply.replace(knowledge.VIDEO_PLACEHOLDER, vid[1]), True
    paragraphs = [p for p in reply.split("\n\n") if knowledge.VIDEO_PLACEHOLDER not in p]
    return "\n\n".join(paragraphs).strip(), False


def record_offer(lead, now):
    """The personalised-demo offer was made now: from here the 24-hour and two-ignored-asks rules apply."""
    lead.demo_offer_asked_at, lead.demo_offer_open = now, True
    if personalised_state(lead) is None:
        lead.demo_state = "asked"


def finish_first_reply(lead, reply, now, dry_run=False):
    """Code side of the approved first reply (also used after a menu tap): insert the exact video link, drop stray links,
    and record at send time that the video was sent (it is then in the message itself, with the product and time) and that
    the live-demo offer was made. Returns (reply, actions)."""
    reply, inserted = fill_video_placeholder(lead, reply)
    reply = sanitize_links(reply)
    actions = ["video"] if inserted else []
    if OFFER_LINE_RE.search(reply) and offer_allowed(lead, now)[0]:
        actions.append("offer")
        if not dry_run:
            record_offer(lead, now)
            db.session.commit()
    return reply, actions


def _insert_after_first_paragraph(text, extra):
    if "\n\n" in text:
        head, rest = text.split("\n\n", 1)
        return f"{head}\n\n{extra}\n\n{rest}"
    return f"{text}\n\n{extra}"


def _strip_offer(text):
    """Remove the personalised-demo question when asking is not allowed. Returns None if nothing else was left, so the
    caller gets a real reply from Claude instead of a canned filler."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [p for p in parts if not PERSONALISED_RE.search(p)]
    return " ".join(kept).strip() or None


def _set_state(lead, new):
    """The personalised-demo state only moves forward: None -> asked -> accepted/declined. An explicit change of mind
    (accepted <-> declined) is allowed; going back to asked/None never is."""
    cur = personalised_state(lead)
    if new == cur or new is None:
        return
    if new == "asked" and cur in ("accepted", "declined"):
        return
    lead.demo_state = new


def apply_turn(lead, turn, pending, history, now, dry_run, say, situation=None):
    """Apply the structured draft: validate its demo_signal and update state. Returns the final reply text (Claude's
    reply plus any exact video links) and a list of actions taken. Guardrails only: nothing here writes the customer's
    reply, and a demo is never confirmed (only `confirm <id>` does that)."""
    reply, sig, actions = turn["reply"], turn["demo_signal"], []
    latest = " ".join(m.body for m in pending)
    first_reply_offer = False
    placed = set()  # products whose video link is already in this reply
    if situation == "first_product_reply":
        # the approved first reply: the code inserts the exact video link; the live-demo line is the personalised-demo offer
        reply, inserted = fill_video_placeholder(lead, reply)
        first_reply_offer = bool(OFFER_LINE_RE.search(reply))
        if inserted:
            actions.append("video")
            placed.add(lead.product_interest)
    allowed, _ = offer_allowed(lead, now, wants_demo=(sig == "wants_demo"))
    before = personalised_state(lead)

    # 1. was the standing offer answered?
    answered = sig in ("accepted_personalised", "declined_personalised", "gave_details", "proposed_time")
    ignored_inc = bool(lead.demo_offer_open and not answered)
    # 2. state transitions (monotonic)
    new_state = None
    if sig in ("accepted_personalised", "gave_details", "proposed_time"):
        new_state = "accepted"
    elif sig == "declined_personalised":
        new_state = "declined"
    # 3. video (exact links added by code; never twice in 24 hours)
    video = turn.get("share_video")
    prefers_team = demo.prefers_team(latest)
    if sig == "wants_demo" and not video and not prefers_team and before != "declined":
        video = lead.product_interest if lead.product_interest in VIDEO_BLURB else "both"
    links = []
    if video and not prefers_team:
        for p in (list(VIDEO_BLURB) if video == "both" else [video]):
            label, url = business_facts.video_for(p)
            if p not in placed and url not in reply and not _video_sent_recently(lead, url):
                links.append(f"{label} demo video: {url}")
    if links:
        reply = _insert_after_first_paragraph(reply, "\n".join(links))
        actions.append("video")
    # 4. the personalised-demo question: at most once per 24 hours
    asked = (turn["asked_personalised_offer"] or first_reply_offer
             or bool(PERSONALISED_RE.search(reply) and "?" in reply))
    if asked and not allowed:
        log.info("lead=%s reply asked the personalised-demo question while not allowed; removed", lead.id)
        stripped = _strip_offer(reply)
        if stripped is None:  # the whole reply was the offer: ask Claude for a real answer to the latest message
            try:
                stripped = _strip_offer(ai.regenerate_message(lead, history, reply)) or _ack_text(lead, history)
            except Exception as exc:
                log.warning("lead=%s regeneration after removing the offer failed: %s", lead.id, ai.describe_error(exc))
                stripped = _ack_text(lead, history)
        reply = stripped
        asked = False
        actions.append("offer-removed")
    # 5. details and pencilling
    pencilled = None
    d = None
    f = turn["fields"]
    gave = any(f.values())
    if new_state == "accepted" or (before == "accepted" and gave):
        d = _draft_demo(lead) if not dry_run else (DemoRequest.query.filter_by(lead_id=lead.id, status="draft")
                                                    .order_by(DemoRequest.id.desc()).first() or DemoRequest(lead_id=lead.id))
        if not dry_run:
            # what the customer just told us wins over what we had (they may be correcting themselves)
            d.contact_name = f["name"] or d.contact_name or lead.name
            d.organisation = f["organisation"] or d.organisation
            d.contact_phone = (re.sub(r"[\s\-]", "", f["phone"]) if f["phone"] else None) or d.contact_phone or lead.phone
            d.channel = demo.CHANNEL_NAMES.get(f["channel"]) or d.channel
            if f["name"] and not lead.name:
                lead.name = f["name"][:120]
        if sig == "proposed_time" and turn["proposed_time"]:
            parsed = demo.parse(turn["proposed_time"], now)
            if parsed["kind"] != "slot":
                parsed = demo.parse(latest, now)
            if parsed["kind"] == "slot":
                ok, reason = demo.validate(parsed["start"], now, lead_id=lead.id)
                if ok and not _missing_details(lead, d):
                    pencilled = parsed["start"]
                elif not ok:
                    if not re.search(r"wednesday", reply, re.I):  # Claude did not already explain the demo day
                        reply += "\n\n" + demo.redirect_text(now, lead.id, parsed["start"])
                        actions.append("redirect-appended")
                    actions.append(f"slot-invalid:{reason}")
            if not dry_run and not d.requested_text:
                d.requested_text = turn["proposed_time"][:300]
    if pencilled:
        if not dry_run:
            _pencil(lead, d, pencilled)
        actions.append(f"pencilled:{pencilled:%Y-%m-%d}")
        if not re.search(r"pencil|not confirmed|tentative", reply, re.I):
            reply += f"\n\nI have pencilled in {demo.fmt(pencilled)}. It is not confirmed yet: the team will confirm it."
        if demo.team_note(now) and not re.search(r"working hours|business hours", reply, re.I):
            reply += demo.team_note(now)
    if sig == "asked_to_reschedule":
        actions.append("reschedule-requested")
    reply = sanitize_links(reply)
    # 6. persist
    if not dry_run:
        if ignored_inc:
            lead.demo_offer_ignored += 1
        if lead.demo_offer_open and (answered or ignored_inc):
            lead.demo_offer_open = False
        if sig == "wants_demo":
            lead.demo_offer_ignored = 0  # genuine interest: asking again is welcome
        if new_state:
            _set_state(lead, new_state)
        if asked:
            record_offer(lead, now)
        db.session.commit()
    say(f"demo signal={sig} state={lead.demo_state} actions={actions or '-'}")
    return reply, actions


# --- existing clients -------------------------------------------------------------------------

def support_reply_text(now=None):
    return (f"Thank you for being a Mega-Net client. {business_facts.SUPPORT_ROUTE_FOR_EXISTING_CLIENTS} "
            f"Support hours: {business_facts.SUPPORT_HOURS}" + demo.team_note(now or utcnow()))


def _existing_client_message(lead, result, latest, dry_run, say):
    """Each message from an existing client is support or sales. Support gets the support route and no sales flow
    (returns the outcome). Sales is treated as a normal sales lead: the flag stays, the lead page shows 'Existing
    client, upgrade enquiry', and Fredrick is alerted (returns None so the normal pipeline continues)."""
    topic = result.get("client_topic")
    if topic not in ("support", "sales"):
        topic = ai.client_topic_hint(latest)
    elif topic == "support" and ai.client_topic_hint(latest) == "sales":
        topic = "sales"  # an upgrade / migration / pricing / demo word is never support
    say(f"existing client: topic={topic}")
    if topic == "sales":
        log.info("existing client lead=%s sales enquiry (upgrade), normal sales flow", lead.id)
        if not dry_run and not lead.upgrade_enquiry:
            lead.upgrade_enquiry = True
            lead.next_followup_at = next_followup_time(lead)
            db.session.commit()
            send_owner_alert(lead, f"{lead.display_name} is an existing client with an upgrade enquiry (sales).")
        return None
    if not dry_run and lead.upgrade_enquiry:
        lead.upgrade_enquiry = False
        lead.next_followup_at = None
        db.session.commit()
    if human_handled(lead):
        return None  # a person is on this conversation: the waiting-prospect path applies
    return _existing_client_reply(lead, dry_run, "support")


def _existing_client_reply(lead, dry_run, why):
    """Existing clients get the support route, not sales flows or menus. Alerts Fredrick once; logs it."""
    log.info("existing client lead=%s (%s): support reply, no sales flow", lead.id, why)
    if dry_run:
        return "skipped", "dry run: existing client, would send the support route"
    if not lead.existing_alerted_at:
        send_owner_alert(lead, f"{lead.display_name} is an existing client and wrote in. I gave them the support route.")
        lead.existing_alerted_at = utcnow()
        db.session.commit()
    if not (lead.phone and whatsapp.is_configured() and window_open(lead)):
        return "skipped", "existing client; no open WhatsApp window for the support reply"
    text, _ = _claude_text(lead, "support_route", support_reply_text())
    msg = send_whatsapp(lead, text=text)
    return ("failed", f"WhatsApp send failed: {msg.error}") if msg.status == "failed" else ("replied", "existing client support")


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


COMMANDS = ("done", "reply", "list", "help", "confirm", "decline")
STAFF_COMMANDS = ("confirm", "decline", "help")  # what staff numbers may send
HELP_TEXT = ('Commands:\n"list": leads waiting for you\n"done <id>": mark contacted\n'
             '"reply <id> <text>": send text to the lead\n"confirm <demo id>": confirm a demo\n'
             '"decline <demo id> [new time]": decline or reschedule a demo\n"help": this list')


def _tell_lead(lead, text):
    """Free-form message from the bot to the lead inside the 24h window. Returns True if it was sent."""
    if not lead.phone or not whatsapp.is_configured() or not window_closes_at(lead):
        return False
    return send_whatsapp(lead, text=text, guard=False).status != "failed"


def _demo_command(cmd, arg, rest, by="owner"):
    try:
        req = db.session.get(DemoRequest, int(arg.lstrip("#")))
    except ValueError:
        req = None
    if not arg:
        return f"Usage: {cmd} <demo id>" + (" [new time]" if cmd == "decline" else "")
    if req is None:
        return f'No demo with id "{arg[:20]}".'
    lead, now = req.lead, utcnow()
    if req.status in ("declined", "rescheduled"):
        return f"Demo {req.id} is already {req.status}."
    if cmd == "confirm":
        if req.status == "confirmed":
            return f"Demo {req.id} is already confirmed."
        if req.status == "draft" or not req.start_at:
            return f"Demo {req.id} has no time yet, so it cannot be confirmed."
        local_now = demo.now_local(now)
        if req.start_at <= local_now:
            return (f"Cannot confirm demo {req.id}: its start time ({demo.fmt(req.start_at)}) has already passed. "
                    f"Send: decline {req.id} [new time]")
        taken = demo.used(req.start_at.date(), exclude_id=req.id)
        if taken >= demo.slots_per_session():
            return (f"Cannot confirm demo {req.id}: {req.start_at:%A} {req.start_at.day} {req.start_at:%B} is already "
                    f"full of confirmed demos ({taken} of {demo.slots_per_session()}). Nothing changed. "
                    f"Send: decline {req.id} [new time]")
        warn = ""
        if req.start_at < local_now + timedelta(hours=demo.notice_hours()):
            warn = (f" Warning: it starts in under {demo.notice_hours()} hours, so the prospect has had little notice.")
        req.status, req.confirmed_by = "confirmed", by
        db.session.commit()
        told = _tell_lead(lead, f"Good news: your demo is confirmed for {demo.fmt(req.start_at)}. We look forward to it.")
        return (f"Confirmed demo {req.id}: {lead.display_name}, {demo.fmt(req.start_at)}. "
                + ("Lead told." if told else "Window closed: message them yourself.") + warn)
    # decline [new time]
    parsed = demo.parse(rest, now) if rest.strip() else {"kind": "none"}
    if rest.strip() and parsed["kind"] != "slot":
        return "Could not read the new time. Nothing changed. Example: decline 3 Tuesday 2pm"
    if parsed["kind"] == "slot":
        ok, why = demo.validate(parsed["start"], now, exclude_id=req.id, lead_id=lead.id)
        if not ok:
            return f"That new time is not possible ({why}); demos run on {demo.rule_text()}. Nothing changed."
        req.status = "rescheduled"
        new = DemoRequest(lead_id=lead.id, requested_text="Proposed by Fredrick", start_at=parsed["start"],
                          duration=demo.duration(), channel=req.channel)
        db.session.add(new)
        db.session.commit()
        told = _tell_lead(lead, f"Sorry, {demo.fmt(req.start_at)} does not work for us. Would {demo.fmt(new.start_at)} "
                                "suit you instead?")
        return (f"Demo {req.id} rescheduled. Proposed demo {new.id}: {demo.fmt(new.start_at)}. "
                f'Send "confirm {new.id}" once {lead.display_name} agrees. ' + ("Lead asked." if told else "Window closed: message them yourself."))
    req.status = "declined"
    db.session.commit()
    slots = demo.next_slots(now, 2, lead.id)
    told = _tell_lead(lead, f"Sorry, {demo.fmt(req.start_at)} is not available. "
                            + (f"The next available are {demo.join_slots(slots)}. Which would you prefer?" if slots
                               else "The team will suggest another date."))
    return f"Declined demo {req.id}. " + ("Lead offered other times." if told else "Window closed: message them yourself.")


def is_owner_command(text):
    words = (text or "").strip().split(None, 1)
    return bool(words) and words[0].lower().rstrip(":") in COMMANDS


def _lead_from_arg(arg):
    try:
        return db.session.get(Lead, int((arg or "").lstrip("#")))
    except ValueError:
        return None


def owner_command(text, by="owner"):
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
    if cmd in ("confirm", "decline"):
        return cmd, _demo_command(cmd, arg, parts[2] if len(parts) > 2 else "", by)
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


def run_owner_command(text, sender=None, by="owner"):
    """Execute an owner (or staff) command and text the result back to whoever sent it."""
    owner = sender or current_app.config["OWNER_WHATSAPP_NUMBER"]
    try:
        cmd, answer = owner_command(text, by)
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

    if lead.phone in current_app.config["EXISTING_CLIENT_NUMBERS"] and not lead.is_existing_client and not dry_run:
        lead.is_existing_client = True
        lead.next_followup_at = None
        db.session.commit()
        log.info("existing client lead=%s flagged by EXISTING_CLIENT_NUMBERS", lead_id)

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
            and whatsapp.is_configured() and window_open(lead) and not ai.repeats(menu.MENU_BODY, history)
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
    if result.get("existing_client") and not lead.is_existing_client and not dry_run:
        lead.is_existing_client = True
        db.session.commit()
        log.info("existing client lead=%s detected by the classifier", lead_id)
    if lead.is_existing_client:
        outcome = _existing_client_message(lead, result, latest, dry_run, say)
        if outcome:
            return outcome
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
    if human_handled(lead):
        return _waiting_reply(lead, newly_ready, dry_run, say)
    flag = result.get("flag")
    if flag and not dry_run:
        _record_open_question(lead, flag, result.get("question") or latest, newly_ready, say)
    if flag:
        situation = flag
    elif first_contact and lead.product_interest in knowledge.FIRST_REPLIES:
        situation = "first_product_reply"  # the product is known: the approved first reply
    elif first_contact:
        situation = "first_contact"
    elif newly_ready or (new_status == "ready" and intent in ("ready_demo", "ready_pricing")):
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
        now = utcnow()
        context = demo_context(lead, history, pending, now, intent)
        fixed = None
        if situation == "first_product_reply":
            context += "\n" + knowledge.first_reply_brief(lead.product_interest, lead.name)
            fixed = knowledge.first_reply(lead.product_interest, lead.name)  # the approved text is the fallback too
        ai.reset_draft_info()
        turn = ai.draft_turn(lead, history, situation, context, fallback_text=fixed)
        info = ai.last_draft_info()
        if info:
            say(f"draft stop_reason={info['stop_reason']} usage={info['usage']} retried={info['retried']} "
                f"fallback={info['fallback']}" + (f" ({info['error']})" if info["fallback"] else ""))
        if turn.get("fallback"):
            log.warning("Lead %s: reply used fixed fallback text: %s", lead_id, turn.get("error"))
            if not dry_run:
                draft_note = f"Reply used fixed fallback text (Claude: {turn.get('error')})."
                lead.ai_note = " | ".join(n for n in (ai_note, draft_note) if n)[:400] or None
                db.session.commit()
        # deterministic backstops for the hand-over and loop rules
        if not turn.get("needs_human") and _HUMAN_RE.search(latest) and not ai.DEMO_WORDS.search(latest):
            turn = dict(turn, needs_human="customer asked to talk to a person", handover=True)
        if not turn.get("needs_human") and _is_repeat_question(lead, pending):
            turn = dict(turn, needs_human="loop: customer repeated the same question")
        text, actions = apply_turn(lead, turn, pending, history, now, dry_run, say, situation)
        say(f"draft ready ({len(text)} chars)")
        say(f"drafted reply text: {text!r}", detail=True)
        if dry_run:
            return "skipped", "dry run: would send free-form text (pass --send to really send)"
        msg = send_whatsapp(lead, text=text)
        if msg.status != "failed" and turn.get("needs_human"):
            _escalate(lead, turn["needs_human"], turn.get("handover", False))
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


def _escalate(lead, reason, handover):
    """A person must step in: flag the lead (Needs your reply) and alert Fredrick. A request to talk to a person also
    hands the lead over (the bot then sends only holding replies). The reason is never logged."""
    lead.needs_owner = True
    if handover:
        lead.followup_paused = True
        lead.next_followup_at = None
    db.session.commit()
    kind = "handover" if handover else "needs a person"
    log.info("escalated lead=%s (%s)", lead.id, kind)
    send_owner_alert(lead, f"{lead.display_name} {kind}: {templates_meta.clean_param(reason, 140)}")


# --- first-contact tap menu -----------------------------------------------------------------

def _is_greeting(text):
    verdict = menu.classify_opener(text)
    if verdict == "unsure":
        return ai.is_generic_opener(text)
    return verdict == "yes"


def menu_allowed(lead):
    """Never to ready, paused, contacted or closed leads, and not twice within the cooldown."""
    if lead.status not in ACTIVE or lead.followup_paused or lead.needs_owner or lead.is_existing_client:
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
        text = None
    else:
        lead.product_interest = choice
        text = None
    lead.next_followup_at = next_followup_time(lead)
    db.session.commit()
    say(f"menu choice={choice} status={lead.status} product={lead.product_interest}")
    if not whatsapp.is_configured():
        return "skipped", "WhatsApp not configured (WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID)"
    if choice == "other":
        text, _ = _claude_text(lead, "other_choice", menu.OTHER_REPLY)
    else:  # they chose a product: the approved first product reply, with the exact video link inserted by code
        text = ai.draft_turn(lead, history, "first_product_reply", knowledge.first_reply_brief(choice, lead.name),
                             fallback_text=knowledge.first_reply(choice, lead.name), structured=False)["reply"]
        text, _ = finish_first_reply(lead, text, utcnow())
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
