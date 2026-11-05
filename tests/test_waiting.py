"""Waiting prospects: holding reply, owner alerts, reminders, dashboard flag, email."""
import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sentinel import emailer, service
from sentinel.models import Lead, Message, db, utcnow
from test_webhook_inbound import LEAD, OWNER, post, text_value

HOLD = "Fredrick has your message and will contact you personally soon."


def say(app, mid, body="Hello you there?"):
    post(app, text_value(LEAD, mid=mid, body=body))
    db.session.expire_all()


def make_waiting(app, sent, claude, how="paused"):
    """Create the lead via a normal first message, then put it in a waiting state."""
    say(app, "wamid.first", "Hi, EHR Cloud please")
    lead = Lead.query.one()
    if how == "paused":
        lead.followup_paused = True
    elif how == "ready":
        lead.status = "ready"
    lead.last_owner_alert_at = lead.last_hold_reply_at = None
    db.session.commit()
    sent.clear()
    return lead


def holding(sent):
    return [s for s in sent if s[1] == LEAD and HOLD in s[2]]


def alerts(sent):
    return [s for s in sent if s[1] == OWNER]


@pytest.mark.parametrize("how", ["paused", "ready"])
def test_holding_reply_sent_once_then_throttled(app, sent, claude, caplog, how):
    claude["intent"] = "exploring"
    make_waiting(app, sent, claude, how)
    caplog.set_level(logging.INFO)
    say(app, "wamid.a")
    assert len(holding(sent)) == 1
    assert holding(sent)[0][2].startswith("Thank you, Amaka.")
    assert any("finished outcome=holding" in r.getMessage() for r in caplog.records)
    say(app, "wamid.b", "Anybody?")
    assert len(holding(sent)) == 1  # cooldown
    lead = Lead.query.one()
    lead.last_hold_reply_at = utcnow() - timedelta(hours=5)
    db.session.commit()
    say(app, "wamid.c", "Hello???")
    assert len(holding(sent)) == 2


def test_holding_reply_has_no_price_or_questions(app, sent, claude):
    claude["intent"] = "exploring"
    lead = make_waiting(app, sent, claude)
    text = service.holding_text(lead)
    assert "?" not in text and "$" not in text and "₦" not in text
    assert service.holding_text(Lead(name=None)).startswith("Thank you for your message.")


def test_owner_alert_for_each_waiting_message_with_throttle(app, sent, claude):
    claude["intent"] = "exploring"
    make_waiting(app, sent, claude)
    say(app, "wamid.a", "Hello you there? " + "x" * 200)
    first = alerts(sent)
    assert len(first) == 1
    assert "Amaka" in first[0][2] and "https://sentinel.test/leads/" in first[0][2]
    quote = first[0][2].split('"')[1]
    assert quote.startswith("Hello you there?") and len(quote) <= 83
    say(app, "wamid.b")
    assert len(alerts(sent)) == 1  # inside 10 minutes
    lead = Lead.query.one()
    lead.last_owner_alert_at = utcnow() - timedelta(minutes=11)
    db.session.commit()
    say(app, "wamid.c")
    assert len(alerts(sent)) == 2


def test_alert_falls_back_to_template_when_window_closed(app, sent, claude, monkeypatch):
    from sentinel import whatsapp
    claude["intent"] = "exploring"
    make_waiting(app, sent, claude)

    def fail_text(to, body):
        if to == OWNER:
            raise whatsapp.WhatsAppError("outside window")
        sent.append(("text", to, body))
        return "wamid.x"

    monkeypatch.setattr(whatsapp, "send_text", fail_text)
    say(app, "wamid.a")
    assert any(s[0] == "template" and s[1] == OWNER and s[2] == "meganet_lead_alert" for s in sent)


def test_engaged_lead_behaviour_unchanged(app, sent, claude):
    claude["intent"] = "exploring"
    say(app, "wamid.a", "Hi, we run a clinic")
    say(app, "wamid.b", "We have 15 staff")
    assert [s[2] for s in sent if s[1] == LEAD] == ["reply:first_contact", "reply:continue"]
    assert alerts(sent) == [] and not holding(sent)
    lead = Lead.query.one()
    assert lead.status == "engaged" and lead.last_hold_reply_at is None


def test_closed_lead_stays_silent(app, sent, claude):
    claude["intent"] = "exploring"
    lead = make_waiting(app, sent, claude)
    lead.status = "closed_lost"
    db.session.commit()
    say(app, "wamid.a")
    assert sent == []


def test_newly_ready_does_not_double_alert(app, sent, claude):
    claude["intent"] = "ready_pricing"
    say(app, "wamid.a", "How much does it cost?")
    assert len(alerts(sent)) == 1


# --- reminders -----------------------------------------------------------------------------------

def test_reminders_at_right_times_and_stop_after_three(app, sent, claude):
    claude["intent"] = "exploring"
    make_waiting(app, sent, claude)
    say(app, "wamid.a", "Please reply")
    since = service.reply_wait_since(Lead.query.one())
    sent.clear()

    service.run_owner_reminders(now=since + timedelta(minutes=59))
    assert alerts(sent) == []
    service.run_owner_reminders(now=since + timedelta(minutes=61))
    assert len(alerts(sent)) == 1 and "Reminder 1/3" in alerts(sent)[0][2]
    service.run_owner_reminders(now=since + timedelta(hours=3))  # <3h after the 1st reminder
    assert len(alerts(sent)) == 1
    service.run_owner_reminders(now=since + timedelta(hours=4, minutes=2))
    assert len(alerts(sent)) == 2 and "Reminder 2/3" in alerts(sent)[1][2]
    service.run_owner_reminders(now=since + timedelta(hours=7, minutes=5))
    assert len(alerts(sent)) == 3 and "Reminder 3/3" in alerts(sent)[2][2]
    service.run_owner_reminders(now=since + timedelta(hours=40))
    assert len(alerts(sent)) == 3  # never more than 3


def test_mark_contacted_stops_reminders_and_clears_flag(app, sent, claude):
    claude["intent"] = "exploring"
    make_waiting(app, sent, claude)
    say(app, "wamid.a")
    lead = Lead.query.one()
    assert service.reply_wait_since(lead) is not None
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["auth"], sess["csrf"] = True, "t"
    assert b"Needs your reply" in c.get(f"/leads/{lead.id}").data
    for m in Message.query.filter_by(direction="inbound"):  # messages that arrived earlier than the tap
        m.sent_at -= timedelta(seconds=5)
    db.session.commit()
    c.post(f"/leads/{lead.id}/action", data={"action": "contacted", "csrf": "t"})
    db.session.expire_all()
    assert service.reply_wait_since(Lead.query.one()) is None
    assert b"Needs your reply" not in c.get(f"/leads/{lead.id}").data
    sent.clear()
    service.run_owner_reminders(now=utcnow() + timedelta(hours=10))
    assert alerts(sent) == []
    # a new message starts a new waiting episode with a fresh reminder count
    for m in Message.query.filter(Message.channel == "manual"):
        m.sent_at -= timedelta(seconds=5)
    db.session.commit()
    say(app, "wamid.b", "Hello again")
    assert service.reply_wait_since(Lead.query.one()) is not None
    assert Lead.query.one().reminder_count == 0


# --- dashboard -----------------------------------------------------------------------------------

def test_dashboard_badge_and_needs_reply_filter(app, sent, claude):
    claude["intent"] = "exploring"
    make_waiting(app, sent, claude)
    say(app, "wamid.a")
    other = service.find_or_create_lead(phone="2348022222222", name="Calm Person")
    service.record_inbound(other, "hi", "whatsapp", external_id="w9")
    db.session.add(Message(lead_id=other.id, direction="outbound", channel="whatsapp", body="hello",
                           sent_at=utcnow() + timedelta(seconds=5), status="sent"))
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["auth"], sess["csrf"] = True, "t"
    page = c.get("/leads").data.decode()
    assert "Needs your reply" in page and ">Needs reply<" in page.replace("<b>", "").replace("</b>", "")
    filtered = c.get("/leads?status=needs_reply").data.decode()
    assert "Amaka" in filtered and "Calm Person" not in filtered
    assert "Calm Person" in c.get("/leads").data.decode()


def test_wait_label_formats(app):
    lead = service.find_or_create_lead(phone=LEAD)
    service.record_inbound(lead, "x", "whatsapp", external_id="w1", at=utcnow() - timedelta(hours=2, minutes=5))
    assert service.wait_label(lead) == "2h 5m"


# --- optional email ------------------------------------------------------------------------------

EMAIL = {"SMTP_HOST": "smtp.test", "SMTP_PORT": 587, "SMTP_USER": "u", "SMTP_PASSWORD": "pa55-secret",
         "ALERT_EMAIL_TO": "fred@test", "ALERT_EMAIL_FROM": "bot@test"}


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.sent, self.login_args = [], None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, u, p):
        self.login_args = (u, p)

    def send_message(self, m):
        self.sent.append(m)


def test_email_sent_only_when_fully_configured(app, sent, claude, monkeypatch, caplog):
    monkeypatch.setattr(emailer.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.instances.clear()
    claude["intent"] = "exploring"
    lead = make_waiting(app, sent, claude)
    caplog.set_level(logging.DEBUG)
    say(app, "wamid.a")
    assert FakeSMTP.instances == []  # blank settings: nothing sent
    assert "SMTP" not in caplog.text and "smtp" not in caplog.text.lower().replace("smtplib", "")

    app.config.update(EMAIL)
    lead = Lead.query.one()
    lead.last_owner_alert_at = None
    db.session.commit()
    say(app, "wamid.b", "Anyone there?")
    mail = FakeSMTP.instances[0]
    assert mail.login_args == ("u", "pa55-secret") and len(mail.sent) == 1
    assert mail.sent[0]["To"] == "fred@test" and "Anyone there?" in mail.sent[0].get_content()
    assert "pa55-secret" not in caplog.text

    app.config["SMTP_PASSWORD"] = None  # one missing value switches email off again
    assert emailer.is_configured() is False
    assert emailer.send_alert("s", "b") is None


def test_email_failure_never_logs_password(app, sent, claude, monkeypatch, caplog):
    class Boom(FakeSMTP):
        def login(self, u, p):
            raise RuntimeError(f"auth failed for {p}")

    monkeypatch.setattr(emailer.smtplib, "SMTP", Boom)
    app.config.update(EMAIL)
    assert emailer.send_alert("s", "b") is False
    assert "pa55-secret" not in caplog.text and "RuntimeError" in caplog.text
