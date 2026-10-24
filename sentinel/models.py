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

    lead = db.relationship("Lead", back_populates="messages")
