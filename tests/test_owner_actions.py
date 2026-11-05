"""Dashboard reply box and WhatsApp owner commands."""
import logging
from datetime import timedelta

from sentinel import service, whatsapp
from sentinel.models import Lead, Message, db, utcnow
from test_webhook_inbound import LEAD, OWNER, post, text_value

OTHER = "2348099999999"


def make_lead(app, sent, claude, mid="wamid.first", body="Hi EHR"):
    claude["intent"] = "exploring"
    post(app, text_value(LEAD, mid=mid, body=body))
    db.session.expire_all()
    sent.clear()
    return Lead.query.one()


def client(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["auth"], sess["csrf"] = True, "t"
    return c


def age_inbound(hours):
    for m in Message.query.filter_by(direction="inbound"):
        m.sent_at = utcnow() - timedelta(hours=hours)
    db.session.commit()


def owner_says(app, body, mid="wamid.own"):
    post(app, text_value(OWNER, mid=mid, body=body))
    db.session.expire_all()


def to_owner(sent):
    return [s[2] for s in sent if s[1] == OWNER]


# --- dashboard box ------------------------------------------------------------------------------

def test_dashboard_reply_inside_window_sends_marks_contacted_and_clears_flag(app, sent, claude, caplog):
    caplog.set_level(logging.INFO)
    lead = make_lead(app, sent, claude)
    lead.reminder_count = 2
    db.session.commit()
    c = client(app)
    page = c.get(f"/leads/{lead.id}").data.decode()
    assert "Reply window open until" in page and "disabled" not in page.split('aria-label="Send reply"')[0].split("<textarea")[1]
    age_inbound(0)  # keep the window open
    r = c.post(f"/leads/{lead.id}/reply", data={"csrf": "t", "text": "Hello, Fredrick here SECRETREPLY"}, follow_redirects=True)
    assert b"Reply sent" in r.data
    assert sent == [("text", LEAD, "Hello, Fredrick here SECRETREPLY")]
    db.session.expire_all()
    lead = Lead.query.one()
    out = Message.query.filter_by(direction="outbound", sender="owner").one()
    assert out.body.startswith("Hello, Fredrick") and out.status == "sent"
    assert lead.followup_paused and lead.reminder_count == 0
    assert service.reply_wait_since(lead) is None
    assert b"<b>You</b>" in c.get(f"/leads/{lead.id}").data
    assert "SECRETREPLY" not in caplog.text and "tok" not in caplog.text.replace("token", "")


def test_dashboard_reply_outside_window_refused_and_form_disabled(app, sent, claude):
    lead = make_lead(app, sent, claude)
    age_inbound(30)
    c = client(app)
    page = c.get(f"/leads/{lead.id}").data.decode()
    assert "Window closed. Message the lead from your own number." in page
    assert page.count("disabled") >= 2
    r = c.post(f"/leads/{lead.id}/reply", data={"csrf": "t", "text": "hi"}, follow_redirects=True)
    assert b"Window closed" in r.data and sent == []
    assert not Lead.query.one().followup_paused


def test_dashboard_reply_failure_shows_meta_error_and_changes_nothing(app, sent, claude, monkeypatch):
    lead = make_lead(app, sent, claude)
    lead.status = "ready"  # a waiting prospect: only Fredrick can answer
    db.session.commit()

    def boom(to, body):
        raise whatsapp.WhatsAppError("HTTP 400: Re-engagement message (code 131047)")

    monkeypatch.setattr(whatsapp, "send_text", boom)
    r = client(app).post(f"/leads/{lead.id}/reply", data={"csrf": "t", "text": "hi"}, follow_redirects=True)
    assert b"code 131047" in r.data and b"Re-engagement message" in r.data
    db.session.expire_all()
    lead = Lead.query.one()
    assert not lead.followup_paused
    assert Message.query.filter_by(sender="owner", status="failed").count() == 1  # stored, shown as not delivered
    assert service.reply_wait_since(lead) is not None  # a failed send does not clear the flag


def test_dashboard_reply_requires_csrf_and_limits_length(app, sent, claude):
    lead = make_lead(app, sent, claude)
    c = client(app)
    assert c.post(f"/leads/{lead.id}/reply", data={"text": "hi"}).status_code == 400
    assert c.post(f"/leads/{lead.id}/reply", data={"csrf": "bad", "text": "hi"}).status_code == 400
    r = c.post(f"/leads/{lead.id}/reply", data={"csrf": "t", "text": "x" * 1001}, follow_redirects=True)
    assert b"too long" in r.data and sent == []
    assert app.test_client().post(f"/leads/{lead.id}/reply", data={}).status_code in (302, 400)


# --- owner commands -----------------------------------------------------------------------------

def test_done_marks_contacted_and_confirms(app, sent, claude):
    lead = make_lead(app, sent, claude)
    lead.followup_paused = True
    lead.reminder_count = 1
    db.session.commit()
    age_inbound(0.01)
    assert service.reply_wait_since(Lead.query.one()) is not None
    owner_says(app, f"done {lead.id}")
    assert service.reply_wait_since(Lead.query.one()) is None
    assert Lead.query.one().followup_paused and Lead.query.one().reminder_count == 0
    assert to_owner(sent) == [f"Done: Amaka (id {lead.id}) marked contacted. Bot paused, reminders stopped."]
    assert Lead.query.count() == 1  # the command did not create a lead for the owner


def test_reply_command_sends_to_lead_and_confirms(app, sent, claude):
    lead = make_lead(app, sent, claude)
    owner_says(app, f"Reply {lead.id} Hello Amaka, I will call you at 3pm")
    assert ("text", LEAD, "Hello Amaka, I will call you at 3pm") in sent
    assert to_owner(sent) == [f"Sent to Amaka (id {lead.id}). Marked contacted."]
    assert Message.query.filter_by(sender="owner", direction="outbound").count() == 1
    assert Lead.query.one().followup_paused


def test_reply_command_with_closed_window_tells_owner_to_use_own_number(app, sent, claude):
    lead = make_lead(app, sent, claude)
    age_inbound(30)
    owner_says(app, f"reply {lead.id} hello")
    assert [s for s in sent if s[1] == LEAD] == []
    assert to_owner(sent) == [f"Window closed for Amaka (id {lead.id}). Message them from your own number, "
                              f'then send "done {lead.id}".']


def test_list_shows_up_to_five_waiting_leads_with_minutes_and_product(app, sent, claude):
    for i in range(7):
        l = service.find_or_create_lead(phone=f"23480000000{i}", name=f"Lead{i}")
        l.product_interest = "ehr_cloud"
        service.record_inbound(l, "hi", "whatsapp", external_id=f"w{i}", at=utcnow() - timedelta(minutes=10 + i))
    owner_says(app, "list")
    msg = to_owner(sent)[0]
    lines = msg.splitlines()
    assert lines[0] == "Needs a reply:" and len([l for l in lines if l[0].isdigit()]) == 5
    assert "EHR Cloud" in lines[1] and "min" in lines[1] and "(+2 more)" in msg
    assert "Lead6" in lines[1]  # longest waiting first


def test_list_when_nothing_waits(app, sent, claude):
    owner_says(app, "list")
    assert to_owner(sent) == ["No leads need a reply."]


def test_help_lists_commands(app, sent, claude):
    owner_says(app, "HELP")
    msg = to_owner(sent)[0]
    for word in ("list", "done <id>", "reply <id> <text>", "help"):
        assert word in msg


def test_bad_ids_and_usage_are_rejected_politely(app, sent, claude):
    make_lead(app, sent, claude)
    for i, body in enumerate(["done 999", "done abc", "reply 999 hi", "done", "reply 1"]):
        sent.clear()
        owner_says(app, body, mid=f"wamid.o{i}")
        answer = to_owner(sent)[0]
        assert answer.startswith(("No lead with id", "Usage:")), (body, answer)
    assert not [s for s in sent if s[1] == LEAD]
    assert not Lead.query.one().followup_paused


def test_command_from_other_number_is_treated_as_normal_lead_message(app, sent, claude):
    claude["intent"] = "exploring"
    post(app, text_value(OTHER, mid="wamid.x", body="done 1"))
    db.session.expire_all()
    assert not to_owner(sent)  # no confirmation, no command executed
    lead = Lead.query.filter_by(phone=OTHER).one()
    assert lead.id and not lead.followup_paused
    post(app, text_value(OTHER, mid="wamid.y", body="reply 1 pwned"))
    assert ("text", LEAD, "pwned") not in sent and all(s[1] != LEAD for s in sent)


def test_non_command_owner_message_ignored_as_before(app, sent, claude, caplog):
    caplog.set_level(logging.INFO)
    owner_says(app, "thanks, noted")
    assert sent == [] and Lead.query.count() == 0
    assert "ignored (sender is OWNER_WHATSAPP_NUMBER" in caplog.text


def test_owner_as_lead_still_works_for_non_commands(app, sent, claude):
    app.config["OWNER_AS_LEAD"] = True
    owner_says(app, "hello I want EHR")
    assert Lead.query.one().phone == OWNER


# --- alerts carry the id and the done line ------------------------------------------------------

def test_alerts_and_reminders_include_id_and_done_instruction(app, sent, claude):
    lead = make_lead(app, sent, claude)
    lead.followup_paused = True
    db.session.commit()
    post(app, text_value(LEAD, mid="wamid.w", body="Hello you there?"))
    db.session.expire_all()
    alert = to_owner(sent)[0]
    assert f'(lead {lead.id}) Reply "done {lead.id}" after you have contacted them.' in alert
    sent.clear()
    since = service.reply_wait_since(Lead.query.one())
    service.run_owner_reminders(now=since + timedelta(minutes=61))
    assert f'Reply "done {lead.id}" after you have contacted them.' in to_owner(sent)[0]
    assert "Reminder 1/3" in to_owner(sent)[0]
