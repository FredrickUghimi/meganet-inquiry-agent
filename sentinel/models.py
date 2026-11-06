from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def utcnow():
    """Naive UTC. All timestamps are stored as UTC and converted only for display."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Lead(db.Model):
    __tablename__ = "leads"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120))
    phone = db.Column(db.String(32), unique=True, index=True)  # digits only, country code included
    email = db.Column(db.String(255), index=True)
    product_interest = db.Column(db.String(10), nullable=False, default="unsure")  # ehr_cloud/erp_cloud/unsure
    status = db.Column(db.String(12), nullable=False, default="new", index=True)
    # new / engaged / ready / cold / closed_won / closed_lost
    source = db.Column(db.String(10), nullable=False, default="whatsapp")  # whatsapp/form/email
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_contact_at = db.Column(db.DateTime)  # last message in either direction
    last_inbound_at = db.Column(db.DateTime)  # last message from the lead; follow-up timing hangs off this
    next_followup_at = db.Column(db.DateTime, index=True)
    followup_stage = db.Column(db.Integer, nullable=False, default=0)  # 0/1/3/7: last stage sent
    followup_paused = db.Column(db.Boolean, nullable=False, default=False)
    followup_failures = db.Column(db.Integer, nullable=False, default=0)
    intent = db.Column(db.String(20))  # latest Claude classification
    summary = db.Column(db.String(300))  # latest one-line Claude summary
    last_hold_reply_at = db.Column(db.DateTime)  # last automatic "Fredrick has your message" reply
    last_owner_alert_at = db.Column(db.DateTime)  # last alert to Fredrick about this lead (throttle)
    reminder_count = db.Column(db.Integer, nullable=False, default=0)  # reminders sent this waiting episode
    last_reminder_at = db.Column(db.DateTime)
    reminder_episode_at = db.Column(db.DateTime)  # which unanswered message the reminder_count belongs to
    menu_sent_at = db.Column(db.DateTime)  # last first-contact tap menu (6h no-repeat rule)
    awaiting_choice = db.Column(db.Boolean, nullable=False, default=False)  # menu sent, no choice yet
    needs_owner = db.Column(db.Boolean, nullable=False, default=False)  # chose "Something else": Fredrick must answer
    demo_state = db.Column(db.String(12))  # demo workflow: offered / collecting / choosing; null = not in the workflow
    is_existing_client = db.Column(db.Boolean, nullable=False, default=False)  # already uses Mega-Net: support, not sales
    existing_alerted_at = db.Column(db.DateTime)  # owner told once that this lead is an existing client
    handback_at = db.Column(db.DateTime)  # Fredrick handed the lead back to the bot (resume/reopen)
    ai_note = db.Column(db.String(400))  # visible warning when Claude classification last failed; null when fine

    messages = db.relationship("Message", back_populates="lead", order_by="Message.sent_at, Message.id",
                               cascade="all, delete-orphan")

    @property
    def display_name(self):
        return self.name or self.phone or self.email or f"Lead #{self.id}"


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=False, index=True)
    direction = db.Column(db.String(8), nullable=False)  # inbound/outbound
    channel = db.Column(db.String(10), nullable=False)  # whatsapp/form/email/manual
    body = db.Column(db.Text, nullable=False)
    sent_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    is_followup = db.Column(db.Boolean, nullable=False, default=False)
    followup_stage = db.Column(db.Integer)  # nullable
    external_id = db.Column(db.String(128), unique=True)  # WhatsApp message id (dedupes webhook retries)
    status = db.Column(db.String(10), nullable=False, default="sent")  # received/sent/failed
    error = db.Column(db.String(500))
    handled = db.Column(db.Boolean, nullable=False, default=True)  # inbound only: auto-reply pass done
    choice_id = db.Column(db.String(32))  # inbound: id of the button/list row the lead tapped
    sender = db.Column(db.String(8))  # outbound only: "owner" (Fredrick, shown as "You"); null = the bot

    lead = db.relationship("Lead", back_populates="messages")


class DemoRequest(db.Model):
    """A demo the lead asked for. 'tentative' until Fredrick or the team confirms it; never call it confirmed before."""
    __tablename__ = "demo_requests"

    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=False, index=True)
    requested_text = db.Column(db.String(300), nullable=False, default="")  # what the lead wrote (shown on the lead page)
    start_at = db.Column(db.DateTime)  # naive local time in Africa/Lagos (business_facts.BUSINESS_HOURS tz), not UTC
    duration = db.Column(db.Integer, nullable=False, default=30)  # minutes
    channel = db.Column(db.String(60))
    status = db.Column(db.String(12), nullable=False, default="tentative")  # draft (collecting details)/tentative/confirmed/declined/rescheduled
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    confirmed_by = db.Column(db.String(20))  # "owner" or "staff"
    contact_name = db.Column(db.String(120))  # collected for a personalised demo
    organisation = db.Column(db.String(160))
    contact_phone = db.Column(db.String(40))

    lead = db.relationship("Lead")


class OpenQuestion(db.Model):
    """Something the bot could not answer from its notes, recorded for Fredrick or the team to answer."""
    __tablename__ = "open_questions"

    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False, default="unanswered")  # unanswered/complaint/custom_request
    question = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    lead = db.relationship("Lead")
