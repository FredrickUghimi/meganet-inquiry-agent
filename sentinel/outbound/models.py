"""Outbound prospecting tables (Phase 1: structure only).

Additive: every table is new and created by db.create_all(); nothing existing is altered except two nullable/defaulted
columns on `leads` (see sentinel/__init__.py NEW_COLUMNS). Phase 1 never writes a Prospect, Draft or ConsentRecord by
itself: the tables exist so later phases (and the gate and settings code) have somewhere to read from.

There is deliberately NO suppression table. The single do-not-contact list is `sentinel.models.OptOut` (`opt_outs`)."""
import json

from sqlalchemy import event

from ..models import db, utcnow


class Campaign(db.Model):
    """One configuration per product: 'erp' (SMEs, retailers, pharmacies, hotels...) and 'ehr' (clinics, hospitals...)."""
    __tablename__ = "outbound_campaigns"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(10), nullable=False, unique=True)  # erp / ehr
    name = db.Column(db.String(80), nullable=False)
    product = db.Column(db.String(10), nullable=False)  # erp_cloud / ehr_cloud (same values as Lead.product_interest)
    active = db.Column(db.Boolean, nullable=False, default=False)
    segments = db.Column(db.Text, nullable=False, default="[]")  # JSON list
    scoring = db.Column(db.Text, nullable=False, default="{}")  # JSON weights and thresholds
    opener_rules = db.Column(db.Text, nullable=False, default="{}")  # JSON: pain themes, tone, banned terms
    allowed_channels = db.Column(db.Text, nullable=False, default="[]")  # JSON list
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class ProspectSource(db.Model):
    """Where prospects may come from, with a recorded terms-of-service review. Empty in Phase 1."""
    __tablename__ = "outbound_sources"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(40), nullable=False, unique=True)
    name = db.Column(db.String(120), nullable=False)
    kind = db.Column(db.String(20), nullable=False)  # registry/directory/web/referral/manual_list/inbound
    tos_note = db.Column(db.String(500))
    tos_reviewed_on = db.Column(db.Date)
    allowed_use = db.Column(db.String(300))
    enabled = db.Column(db.Boolean, nullable=False, default=False)


class Prospect(db.Model):
    """A business we might contact. EMPTY in Phase 1: nothing creates these rows."""
    __tablename__ = "outbound_prospects"

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("outbound_campaigns.id"), nullable=False, index=True)
    source_id = db.Column(db.Integer, db.ForeignKey("outbound_sources.id"), index=True)
    source_url = db.Column(db.String(500))
    found_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    org_name = db.Column(db.String(200), nullable=False)
    segment = db.Column(db.String(60))
    city = db.Column(db.String(80))
    state = db.Column(db.String(60))
    website = db.Column(db.String(300))
    contact_role = db.Column(db.String(80))
    contact_name = db.Column(db.String(120))
    business_phone = db.Column(db.String(40))
    business_email = db.Column(db.String(255))
    basis = db.Column(db.String(30))  # b2b_legitimate_interest / consent / inbound / referral / existing_relationship
    score = db.Column(db.Integer)
    score_reasons = db.Column(db.Text)  # JSON list
    pain_hypothesis = db.Column(db.String(300))
    research_notes = db.Column(db.String(600))
    status = db.Column(db.String(16), nullable=False, default="discovered", index=True)
    review_expires_at = db.Column(db.DateTime)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"))
    retention_until = db.Column(db.DateTime, index=True)
    last_action_at = db.Column(db.DateTime)


class Draft(db.Model):
    """A drafted message and, once sent, the record that it was sent. EMPTY in Phase 1."""
    __tablename__ = "outbound_drafts"

    id = db.Column(db.Integer, primary_key=True)
    prospect_id = db.Column(db.Integer, db.ForeignKey("outbound_prospects.id"), nullable=False, index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("outbound_campaigns.id"), index=True)
    channel = db.Column(db.String(20), nullable=False)  # whatsapp_cloud/email/wa_manual/email_manual/call
    kind = db.Column(db.String(12), nullable=False, default="first_touch")  # first_touch / followup
    subject = db.Column(db.String(200))
    body = db.Column(db.Text)
    template_params = db.Column(db.Text)  # JSON
    version = db.Column(db.Integer, nullable=False, default=1)
    status = db.Column(db.String(12), nullable=False, default="draft", index=True)
    # draft / approved / rejected / expired / sending / sent / failed / blocked
    approved_at = db.Column(db.DateTime)
    approved_by = db.Column(db.String(20))  # only "owner" is ever accepted (see approval.py)
    edited_by_human = db.Column(db.Boolean, nullable=False, default=False)
    sent_at = db.Column(db.DateTime, index=True)
    sent_mode = db.Column(db.String(8))  # auto / manual
    external_id = db.Column(db.String(128))
    delivery_status = db.Column(db.String(10))
    error = db.Column(db.String(500))
    block_reason = db.Column(db.String(200))
    expires_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class ConsentRecord(db.Model):
    """A recorded MARKETING opt-in (or its withdrawal) for a contact. Written only by outbound/consent.py, which refuses
    to treat an inbound message as an opt-in. EMPTY in Phase 1."""
    __tablename__ = "outbound_consents"

    id = db.Column(db.Integer, primary_key=True)
    contact_key = db.Column(db.String(34), nullable=False, index=True)  # E.164 key from service.phone_key
    channel = db.Column(db.String(12), nullable=False)  # whatsapp / email
    kind = db.Column(db.String(12), nullable=False)  # opt_in / withdrawal
    evidence_kind = db.Column(db.String(30), nullable=False)
    evidence_note = db.Column(db.String(300))  # where/how it was given; never a message body
    recorded_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    recorded_by = db.Column(db.String(20), nullable=False)
    prospect_id = db.Column(db.Integer, db.ForeignKey("outbound_prospects.id"))
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"))


class OutboundSetting(db.Model):
    """Editable settings. `value` is JSON. Defaults live in code (outbound/settings.py SPEC)."""
    __tablename__ = "outbound_settings"

    key = db.Column(db.String(40), primary_key=True)
    value = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_by = db.Column(db.String(20), nullable=False, default="owner")


class AuditEvent(db.Model):
    """Append-only record of every outbound action. Holds ids and reasons, never message text or personal data.
    Updating or deleting a row through the ORM raises."""
    __tablename__ = "outbound_audit"

    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    actor = db.Column(db.String(20), nullable=False)  # owner / system
    action = db.Column(db.String(60), nullable=False, index=True)
    entity = db.Column(db.String(30))
    entity_id = db.Column(db.String(40))
    detail = db.Column(db.Text, nullable=False, default="{}")  # JSON, validated by audit.record

    @property
    def detail_dict(self):
        return json.loads(self.detail or "{}")


@event.listens_for(AuditEvent, "before_update")
def _audit_no_update(mapper, connection, target):
    raise RuntimeError("outbound audit events cannot be changed")


@event.listens_for(AuditEvent, "before_delete")
def _audit_no_delete(mapper, connection, target):
    raise RuntimeError("outbound audit events cannot be deleted")


class ReviewReport(db.Model):
    """The report produced when the gate count is reached (generated in a later phase). EMPTY in Phase 1."""
    __tablename__ = "outbound_review_reports"

    id = db.Column(db.Integer, primary_key=True)
    generated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    sent_count = db.Column(db.Integer, nullable=False, default=0)
    metrics = db.Column(db.Text, nullable=False, default="{}")  # JSON
    acknowledged_at = db.Column(db.DateTime)


class OutboundSpend(db.Model):
    """Estimated Claude and WhatsApp spend for outbound work, in US dollars, for the monthly hard stop."""
    __tablename__ = "outbound_spend"

    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    kind = db.Column(db.String(10), nullable=False)  # claude / whatsapp
    usd = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(120))
