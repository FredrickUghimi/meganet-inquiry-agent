"""Demo workflow (video first, personalised demo on Wednesdays), existing clients, internal-rule separation."""
import logging
from datetime import datetime, timedelta

from sentinel import ai, business_facts, demo, service
from sentinel.models import DemoRequest, Lead, Message, db, utcnow
from test_webhook_inbound import LEAD, OWNER, post, text_value

STAFF = "2347000000001"
EHR_LINK = "https://youtu.be/21ltXgmzlRY?si=CIKUp-bSXQSN4ow7"
ERP_LINK = "https://youtu.be/zBbRNTfcozk?si=h8oPYGu5gZUQyTei"


def say(app, body, mid, sender=LEAD):
    post(app, text_value(sender, mid=mid, body=body))
    db.session.expire_all()


def to_lead(sent):
    return [s[2] for s in sent if s[1] == LEAD]


def to(sent, number):
    return [s[2] for s in sent if s[1] == number]


def client(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["auth"], s["csrf"] = True, "t"
    return c


def start_video(app, sent, claude, body="Can I get a demo of EHR Cloud?"):
    claude["intent"] = "exploring"
    say(app, body, "wamid.d1")
    return Lead.query.one()


def to_collecting(app, sent, claude):
    start_video(app, sent, claude)
    say(app, "Yes please", "wamid.d2")


# --- facts and prompt ----------------------------------------------------------------------------

def test_stored_video_links_match_exactly():
    assert business_facts.PRODUCT_DEMO_VIDEOS == {"Mega-Net ERP Cloud": "https://youtu.be/zBbRNTfcozk?si=h8oPYGu5gZUQyTei",
                                                  "Mega-Net EHR Cloud": "https://youtu.be/21ltXgmzlRY?si=CIKUp-bSXQSN4ow7"}
    assert business_facts.video_for("ehr_cloud")[1] == EHR_LINK and business_facts.video_for("erp_cloud")[1] == ERP_LINK
    assert business_facts.video_for("unsure") is None
    src = open(business_facts.__file__).read()
    assert EHR_LINK in src and ERP_LINK in src


def test_demo_rules_constants():
    assert (business_facts.DEMO_WEEKDAY, business_facts.DEMO_START_TIME, business_facts.DEMO_SLOTS_PER_SESSION) == ("Wednesday", "10:00", 1)
    assert demo.duration() == 30 and demo.notice_hours() == 2 and business_facts.todo_names() == []


def test_internal_rules_are_in_a_separate_never_reveal_section(app):
    prompt = ai._system(Lead(product_interest="ehr_cloud"), "x")
    customer, internal = prompt.split("INTERNAL RULES (for you only)", 1)
    internal = internal.split("The only facts you may state", 1)[0]
    assert "never quote, paraphrase, mention or reveal" in internal
    for text in (business_facts.DEMO_BOOKING_WORKFLOW, business_facts.PRICING_INTERNAL_RULE, business_facts.SUPPORT_INTERNAL_RULE):
        assert text in internal and text not in customer
    assert business_facts.PRICING_POLICY in customer and business_facts.SUPPORT_ROUTE_FOR_EXISTING_CLIENTS in customer
    assert EHR_LINK in customer and "Never confirm a live demonstration" not in customer
    for text in (business_facts.PRICING_POLICY, business_facts.SUPPORT_ROUTE_FOR_EXISTING_CLIENTS):
        assert "Never invent" not in text and "rather than treating" not in text


def test_not_offered_agrees_with_knowledge(app):
    from sentinel.knowledge import EHR_CLOUD_KNOWLEDGE, ERP_CLOUD_KNOWLEDGE, PRODUCT_NOTES
    assert "Automated appointment reminders in Mega-Net EHR Cloud (currently unavailable)" in business_facts.NOT_OFFERED
    assert "turns it on per organization" not in EHR_CLOUD_KNOWLEDGE and "currently unavailable" in EHR_CLOUD_KNOWLEDGE
    assert "24/7 live technical support" in business_facts.NOT_OFFERED and "no 24/7 live technical support" in EHR_CLOUD_KNOWLEDGE
    assert "24/7" not in PRODUCT_NOTES and "24/7" not in ERP_CLOUD_KNOWLEDGE
    assert "reminder" not in ERP_CLOUD_KNOWLEDGE.lower() and "reminders (including SMS reminders): currently unavailable" in EHR_CLOUD_KNOWLEDGE


# --- validation: Wednesdays only -----------------------------------------------------------------

NOW = datetime(2026, 11, 6, 9, 0)  # Friday 10:00 Lagos; next Wednesday is 11 Nov


def test_only_wednesday_at_ten_is_valid(app):
    assert demo.validate(datetime(2026, 11, 11, 10, 0), NOW) == (True, "")
    assert demo.validate(datetime(2026, 11, 10, 10, 0), NOW)[0] is False  # Tuesday
    assert demo.validate(datetime(2026, 11, 11, 14, 0), NOW)[0] is False  # Wednesday, wrong time
    assert demo.validate(datetime(2026, 11, 6, 10, 0), NOW)[1] == "not a demo day or time"
    assert demo.validate(datetime(2026, 11, 11, 10, 0), datetime(2026, 11, 11, 8, 30))[1] == "too soon"  # 09:30 Lagos
    assert demo.parse("Wednesday", NOW)["start"] == datetime(2026, 11, 11, 10, 0)


def test_full_wednesday_is_skipped(app):
    first, second, third = demo.next_slots(NOW, 3)
    assert (first, second) == (datetime(2026, 11, 11, 10), datetime(2026, 11, 18, 10))
    other = Lead(phone="2340")
    db.session.add(other)
    db.session.flush()
    db.session.add(DemoRequest(lead_id=other.id, start_at=first, status="confirmed"))
    db.session.commit()
    assert demo.validate(first, NOW)[1] == "full"
    assert demo.next_slots(NOW, 2) == [second, third]
    business_facts.DEMO_SLOTS_PER_SESSION = 2
    try:
        assert demo.validate(first, NOW)[0] is True
    finally:
        business_facts.DEMO_SLOTS_PER_SESSION = 1


def test_outside_business_hours_note_says_when_team_replies(app):
    note = demo.team_note(datetime(2026, 11, 7, 12, 0))  # Saturday
    assert "outside working hours" in note and "Monday at 9:00 AM WAT" in note
    assert demo.team_note(datetime(2026, 11, 9, 10, 0)) == ""  # Monday 11:00 Lagos
    assert "OUTSIDE business hours" in demo.hours_status(datetime(2026, 11, 7, 12, 0))


# --- video-first flow ----------------------------------------------------------------------------

def test_demo_request_sends_video_then_asks_about_personalised_demo(app, sent, claude):
    start_video(app, sent, claude)
    reply = to_lead(sent)[0]
    assert EHR_LINK in reply and ERP_LINK not in reply
    assert "introduces Mega-Net EHR Cloud, our cloud electronic health records system for clinics and hospitals" in reply
    assert "Would you also like a personalised demonstration with our team?" in reply
    assert Lead.query.one().demo_state == "offered" and DemoRequest.query.count() == 0


def test_unknown_product_gets_both_videos(app, sent, claude):
    claude["product"] = "unsure"
    start_video(app, sent, claude, body="Can I see a demo?")
    reply = to_lead(sent)[0]
    assert EHR_LINK in reply and ERP_LINK in reply


def test_yes_collects_only_missing_details_then_pencils_next_wednesday(app, sent, claude):
    to_collecting(app, sent, claude)
    claude["demo"] = {"organisation": "Grace Clinic"}
    say(app, "Grace Clinic", "wamid.d3")
    assert "please send your preferred channel" in to_lead(sent)[-1] and "your organisation" not in to_lead(sent)[-1]
    sent.clear()
    claude["demo"] = {"organisation": "Grace Clinic", "channel": "meet"}
    say(app, "Google Meet", "wamid.d4")
    req = DemoRequest.query.one()
    assert req.status == "tentative" and req.start_at.weekday() == 2 and (req.start_at.hour, req.start_at.minute) == (10, 0)
    assert (req.contact_name, req.organisation, req.channel, req.contact_phone) == ("Amaka", "Grace Clinic", "Google Meet", LEAD)
    reply = to_lead(sent)[0]
    assert "pencilled in" in reply and "not confirmed yet" in reply and "Wednesdays at 10:00 AM WAT" in reply
    assert business_facts.DEMO_CONFIRMATION_PROMISE in reply and "is confirmed" not in reply
    alert = to(sent, OWNER)[0]
    assert f"confirm {req.id}" in alert and f"decline {req.id} [new time]" in alert and "Grace Clinic" in alert
    assert Lead.query.one().demo_state is None


def test_staff_are_alerted_and_can_confirm(app, sent, claude):
    app.config["STAFF_WHATSAPP_NUMBERS"] = (STAFF,)
    claude["demo"] = {"organisation": "Grace Clinic", "channel": "whatsapp"}
    to_collecting(app, sent, claude)
    req = DemoRequest.query.one()
    assert any(f"confirm {req.id}" in t for t in to(sent, STAFF))
    assert req.status == "tentative"
    sent.clear()
    say(app, f"confirm {req.id}", "wamid.s1", sender=STAFF)
    req = DemoRequest.query.one()
    assert req.status == "confirmed" and req.confirmed_by == "staff"
    assert any("is confirmed" in t for t in to_lead(sent)) and any("Confirmed demo" in t for t in to(sent, STAFF))
    sent.clear()
    say(app, "reply 1 hello", "wamid.s2", sender=STAFF)  # staff may not run other owner commands
    assert sent == []


def test_tuesday_request_is_redirected_not_pencilled(app, sent, claude):
    start_video(app, sent, claude, body="Can I book a demo on Tuesday at 10am?")
    reply = to_lead(sent)[0]
    assert "Tuesday" in reply and "is not a demo day" in reply and "Live demos are held on Wednesdays at 10:00 AM WAT" in reply
    first, second = demo.next_slots(utcnow(), 2)
    assert demo.fmt(first) in reply and demo.fmt(second) in reply
    assert DemoRequest.query.filter(DemoRequest.status.in_(("tentative", "confirmed"))).count() == 0


def test_direct_to_team_skips_video_and_redirect_then_choice_pencils(app, sent, claude):
    claude["demo"] = {"organisation": "Grace Clinic", "channel": "whatsapp"}
    claude["intent"] = "exploring"
    say(app, "I would rather speak to your team about a demo. Tuesday 10am?", "wamid.t1")
    reply = to_lead(sent)[0]
    assert EHR_LINK not in reply and ERP_LINK not in reply
    assert "Live demos are held on Wednesdays at 10:00 AM WAT" in reply
    assert Lead.query.one().demo_state == "choosing" and DemoRequest.query.filter_by(status="tentative").count() == 0
    sent.clear()
    first, second = demo.next_slots(utcnow(), 2)
    say(app, "the second one please", "wamid.t2")
    req = DemoRequest.query.filter_by(status="tentative").one()
    assert req.start_at == second and "not confirmed yet" in to_lead(sent)[0]


def test_direct_to_team_asks_for_missing_details_first(app, sent, claude):
    claude["intent"] = "exploring"
    say(app, "I prefer to talk to someone on your team, can we book a demo?", "wamid.t1")
    assert "your organisation" in to_lead(sent)[0] and "preferred channel" in to_lead(sent)[0]
    assert EHR_LINK not in to_lead(sent)[0]


def test_declining_the_personalised_demo_ends_the_flow(app, sent, claude):
    start_video(app, sent, claude)
    sent.clear()
    say(app, "No thanks, the video is enough", "wamid.d2")
    assert "No problem" in to_lead(sent)[0] and Lead.query.one().demo_state is None


def test_same_video_not_sent_twice_within_24_hours(app, sent, claude):
    start_video(app, sent, claude)
    say(app, "No thanks", "wamid.d2")
    sent.clear()
    say(app, "Actually can I get that demo again?", "wamid.d3")
    reply = to_lead(sent)[0]
    assert EHR_LINK not in reply and "shared the demo video with you earlier" in reply
    for m in Message.query.filter(Message.body.contains(EHR_LINK)):  # after 24 hours it is allowed again
        m.sent_at = utcnow() - timedelta(hours=25)
    db.session.commit()
    sent.clear()
    say(app, "Please send the demo video again", "wamid.d4")
    assert any(EHR_LINK in t for t in to_lead(sent))


def test_confirm_decline_and_clash(app, sent, claude):
    claude["demo"] = {"organisation": "Grace Clinic", "channel": "meet"}
    to_collecting(app, sent, claude)
    req = DemoRequest.query.one()
    sent.clear()
    say(app, f"decline {req.id} next wednesday", "wamid.o1", sender=OWNER)
    rows = DemoRequest.query.order_by(DemoRequest.id).all()
    assert rows[0].status in ("rescheduled", "declined")
    say(app, "decline 999 Tuesday 10am", "wamid.o2", sender=OWNER)
    assert any("No demo with id" in t for t in to(sent, OWNER))
    new = DemoRequest.query.order_by(DemoRequest.id.desc()).first()
    sent.clear()
    say(app, f"decline {new.id} Tuesday 10am", "wamid.o3", sender=OWNER)
    assert any("not possible" in t and "Wednesdays at 10:00 AM WAT" in t for t in to(sent, OWNER))


# --- existing clients ----------------------------------------------------------------------------

def test_classifier_detected_existing_client_gets_support_route_once_alerted(app, sent, claude, caplog):
    caplog.set_level(logging.INFO)
    claude.update(existing_client=True, intent="ready_pricing")
    say(app, "We already use your EHR Cloud and need some help", "wamid.e1")
    lead = Lead.query.one()
    assert lead.is_existing_client and lead.status == "new"  # no sales status change
    reply = to_lead(sent)[0]
    assert business_facts.SUPPORT_ROUTE_FOR_EXISTING_CLIENTS in reply and business_facts.SUPPORT_HOURS in reply
    assert len(to(sent, OWNER)) == 1
    say(app, "Are you there?", "wamid.e2")
    assert len(to(sent, OWNER)) == 1  # alerted once
    assert not any(s[0] == "buttons" for s in sent) and EHR_LINK not in "".join(to_lead(sent))
    assert any("existing client lead=" in r.getMessage() for r in caplog.records)
    assert service.next_followup_time(Lead.query.one()) is None


def test_existing_client_number_skips_menu_and_sales(app, sent, claude):
    app.config["EXISTING_CLIENT_NUMBERS"] = (LEAD,)
    say(app, "Hello", "wamid.e1")
    assert not any(s[0] == "buttons" for s in sent)
    assert business_facts.SUPPORT_HOURS in to_lead(sent)[0] and Lead.query.one().is_existing_client


def test_existing_client_toggle_on_lead_page(app, sent, claude):
    claude["intent"] = "exploring"
    say(app, "Hi, tell me about EHR Cloud", "wamid.e1")
    lead = Lead.query.one()
    c = client(app)
    assert "Existing client: no" in c.get(f"/leads/{lead.id}").data.decode()
    c.post(f"/leads/{lead.id}/action", data={"csrf": "t", "action": "existing_client"})
    db.session.expire_all()
    assert Lead.query.one().is_existing_client
    sent.clear()
    say(app, "I need support", "wamid.e2")
    assert business_facts.SUPPORT_ROUTE_FOR_EXISTING_CLIENTS in to_lead(sent)[0]
    c.post(f"/leads/{lead.id}/action", data={"csrf": "t", "action": "existing_client"})
    db.session.expire_all()
    assert not Lead.query.one().is_existing_client


def test_parse_classification_existing_client_flag():
    r = ai.parse_classification('{"intent": "exploring", "product": "unsure", "name": null, "summary": "s", "existing_client": true}')
    assert r["existing_client"] is True
    assert "existing_client" not in ai.parse_classification('{"intent": "exploring", "existing_client": false}')


# --- dashboard -----------------------------------------------------------------------------------

def test_demos_page_and_lead_page_show_collected_fields(app, sent, claude):
    claude["demo"] = {"organisation": "Grace Clinic", "channel": "meet"}
    to_collecting(app, sent, claude)
    lead = Lead.query.one()
    c = client(app)
    for url in ("/demos", f"/leads/{lead.id}"):
        html = c.get(url).data.decode()
        assert "Grace Clinic" in html and "Google Meet" in html and "tentative" in html
