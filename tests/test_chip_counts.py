"""Filter-chip counts from grouped queries must equal the old load-every-lead computation."""
from datetime import timedelta

from sentinel import dashboard, service
from sentinel.models import Lead, Message, db, utcnow


def old_counts():
    everyone = Lead.query.all()
    counts = {"all": len(everyone), "partner": sum(1 for l in everyone if l.partner_enquiry),
              "needs_reply": sum(1 for l in everyone if service.reply_wait_since(l) is not None)}
    for s in dashboard.STATUS_ORDER:
        counts[s] = sum(1 for l in everyone if l.status == s)
    return counts


def seed(app):
    statuses = ["new", "new", "engaged", "ready", "ready", "cold", "closed_won", "closed_lost", "closed_lost"]
    for i, st in enumerate(statuses):
        lead = service.find_or_create_lead(phone=f"23480000000{i:02d}", name=f"Lead {i}")
        lead.status = st
        lead.partner_enquiry = i % 3 == 0
        lead.followup_paused = i == 3  # ready + paused: only a person answers
        service.record_inbound(lead, "hello", "whatsapp", external_id=f"w{i}", at=utcnow() - timedelta(hours=2))
        if i in (1, 5):  # answered by the bot / by the owner
            db.session.add(Message(lead_id=lead.id, direction="outbound", channel="manual", sender="owner", body="hi",
                                   sent_at=utcnow() - timedelta(hours=1), status="sent"))
    db.session.commit()


def test_grouped_counts_match_the_old_per_lead_counts(app):
    seed(app)
    new = dashboard.chip_counts()
    assert new == old_counts()
    assert new["all"] == 9 and new["closed_lost"] == 2 and new["partner"] == 3
    assert new["needs_reply"] > 0  # the seed really exercises the needs-reply rule


def test_counts_with_no_leads(app):
    assert dashboard.chip_counts() == old_counts() == {"all": 0, "partner": 0, "needs_reply": 0,
                                                       **{s: 0 for s in dashboard.STATUS_ORDER}}
